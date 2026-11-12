"""Mesin akuisisi background (Scan-and-Dwell) untuk SA124B.

Satu thread dedicated memegang kepemilikan perangkat. State machine:
  IDLE -> SWEEPING -> (jika kanal aktif & auto-record) -> DWELL_RECORD -> SWEEPING
                   -> (jika operator klik 'Dengar')   -> MANUAL_AUDIO -> SWEEPING

Semua transisi dilindungi RLock di dalam `sa124.py` sehingga tidak ada balapan
akses USB. Konsumen WebSocket (spektrum, waterfall, audio) hanya menerima
salinan buffer melalui antrean in-memory non-blocking.
"""
from __future__ import annotations

import asyncio
import math
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from channels import PRESETS, Channel, Preset, clamp_rbw, clamp_vbw, get_preset
from config import CONFIG, RECORDINGS_DIR
from occupancy import OCC
from recorder import RecordingMeta, WavRecorder, unique_path
from sa124 import (
    AUDIO_SAMPLES_PER_SEC,
    SA_SWEEPING,
    DeviceInfo,
    SADevice,
    SAError,
    SimulatedSA124,
    load_library,
)

STATE_IDLE = "IDLE"
STATE_SWEEPING = "SWEEPING"
STATE_DWELL_RECORD = "DWELL_RECORD"
STATE_MANUAL_AUDIO = "MANUAL_AUDIO"


@dataclass
class SweepFrame:
    timestamp: float
    center_hz: float
    span_hz: float
    start_hz: float
    bin_size_hz: float
    length: int
    levels: List[float]
    noise_floor_dbm: float
    threshold_dbm: float
    occupied_channels: List[dict]


class CaptureEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = STATE_IDLE
        self._device: Optional[SADevice] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._state_event = threading.Event()
        self._config_dirty: bool = False  # preset diterapkan tapi belum dikonfigurasi ke perangkat
        self._sweep_configured: bool = False  # perangkat sudah pernah diberi geometri sapuan

        # Konfigurasi aktif
        cfg_preset = CONFIG.get("sweep").get("preset", "vhf_airband")
        self._preset: Preset = get_preset(cfg_preset) or PRESETS[next(iter(PRESETS))]
        self._channels: List[Channel] = self._preset.channels()
        self._active_channel_indices: List[Tuple[Channel, int]] = []

        # Kontrol audio
        self._manual_audio_freq: Optional[float] = None
        self._manual_audio_demod: str = "FM"
        self._current_recorder: Optional[WavRecorder] = None
        self._manual_recorder: Optional[WavRecorder] = None
        self._manual_record_started: float = 0.0
        self._manual_record_freq: Optional[float] = None
        self._manual_record_demod: str = "FM"
        self._dwell_freq: Optional[float] = None
        self._dwell_started: float = 0.0
        self._dwell_last_above: float = 0.0
        self._auto_record_enabled: bool = bool(
            CONFIG.get("occupancy").get("auto_record", False)
        )
        # Akumulasi okupansi dapat dijeda operator tanpa menghentikan sapuan.
        self._occupancy_enabled: bool = True
        self._hit_counters: Dict[float, int] = {}

        # Subscriber WebSocket
        self._spectrum_subscribers: Set[asyncio.Queue] = set()
        self._audio_subscribers: Set[asyncio.Queue] = set()
        self._event_subscribers: Set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Info runtime
        self._fps: float = 0.0
        self._sweep_count: int = 0
        self._last_frame: Optional[SweepFrame] = None
        self._device_info: DeviceInfo = DeviceInfo(connected=False, simulated=False)
        self._last_error: Optional[str] = None

    # -- siklus hidup engine ----------------------------------------------- #
    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def initialize_device(self, force_simulate: bool = False, serial: Optional[int] = None) -> DeviceInfo:
        with self._lock:
            if self._device is not None and self._device.is_open:
                return self._device.info

            cfg_dev = CONFIG.get("device")
            simulate = force_simulate or bool(cfg_dev.get("simulate", False))
            target_serial = serial or cfg_dev.get("serial")

            if not simulate:
                lib, err = load_library()
                if lib is not None:
                    try:
                        dev = SADevice(serial=target_serial)
                        info = dev.open()
                        self._device = dev
                        self._device_info = info
                        return info
                    except SAError as exc:
                        self._last_error = f"Perangkat gagal dibuka ({exc}), beralih ke simulasi"
                else:
                    self._last_error = f"DLL tidak ditemukan ({err}), beralih ke simulasi"

            sim = SimulatedSA124(serial=target_serial)
            info = sim.open()
            self._device = sim
            self._device_info = info
            return info

    def reconnect_device(self, simulate: Optional[bool] = None,
                         serial: Optional[int] = None) -> DeviceInfo:
        """Tutup perangkat berjalan lalu buka ulang dengan mode/serial baru.

        Dipakai panel Admin untuk beralih antara instrumen fisik dan simulator
        tanpa me-restart server. Serial `None` berarti auto-detect; `simulate`
        `None` berarti pakai nilai yang tersimpan di config.
        """
        self.stop()

        with self._lock:
            values: Dict[str, Any] = {}
            if simulate is not None:
                values["simulate"] = bool(simulate)
            if serial is not None:
                values["serial"] = int(serial)
            if values:
                CONFIG.update("device", values)

            old = self._device
            self._device = None
            self._last_error = None

        if old is not None:
            try:
                old.close()
            except Exception:
                pass

        info = self.initialize_device()
        # Perangkat baru belum tahu geometri sapuan: tandai kotor supaya
        # `_step_sweep` mengonfigurasinya pada siklus pertama.
        with self._lock:
            self._config_dirty = True

        self.start()
        return info

    def start(self, preset_key: Optional[str] = None) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._device is None or not self._device.is_open:
                self.initialize_device()

            if preset_key:
                p = get_preset(preset_key)
                if p:
                    self._preset = p
                    self._channels = p.channels()

            self._stop_event.clear()
            self._state = STATE_SWEEPING
            self._thread = threading.Thread(target=self._run_loop, name="SA124-Capture", daemon=True)
            self._thread.start()

    def _drain_audio_subscribers(self) -> None:
        """Kuras semua antrean subscriber audio agar tidak ada sisa suara setelah stop."""
        with self._lock:
            for q in list(self._audio_subscribers):
                while not q.empty():
                    try:
                        q.get_nowait()
                    except Exception:
                        break

    def stop(self) -> None:
        # Kuras audio subscribers SEBELUM menghentikan thread agar tidak ada
        # paket audio sisa yang dikirim ke klien.
        self._drain_audio_subscribers()
        with self._lock:
            self._stop_event.set()
            thr = self._thread
        if thr is not None and thr.is_alive() and thr != threading.current_thread():
            thr.join(timeout=3.0)
        with self._lock:
            self._state = STATE_IDLE
            self._thread = None
            if self._device is not None:
                try:
                    self._device.stop_stream()
                except Exception:
                    pass

    # -- konfigurasi runtime ----------------------------------------------- #
    def apply_preset(self, preset_key: str) -> bool:
        p = get_preset(preset_key)
        if not p:
            return False
        with self._lock:
            self._preset = p
            self._channels = p.channels()
            self._hit_counters.clear()
            # Preset menentukan geometri sapuan: sinkronkan ke CONFIG supaya
            # `_step_sweep` (yang membaca CONFIG) tidak memakai nilai lama.
            CONFIG.update("sweep", {
                "preset": p.key,
                "center_hz": p.center_hz,
                "span_hz": p.span_hz,
                "rbw_hz": p.rbw_hz,
                "vbw_hz": p.vbw_hz,
                "ref_level_dbm": p.ref_level_dbm,
                "atten_db": p.atten_db,
            })
            CONFIG.update("audio", {
                "demod": p.demod,
                "if_bw_hz": p.if_bw_hz,
            })
            self._config_dirty = True
            # Bila sedang sweeping, minta rekonfigurasi di siklus berikutnya.
            if self._state == STATE_SWEEPING and self._device is not None:
                try:
                    self._device.stop_stream()
                except Exception:
                    pass
        return True

    def set_sweep_params(
        self,
        center_hz: Optional[float] = None,
        span_hz: Optional[float] = None,
        rbw_hz: Optional[float] = None,
        ref_level_dbm: Optional[float] = None,
        atten_db: Optional[int] = None,
    ) -> None:
        with self._lock:
            sw = CONFIG.get("sweep")
            if center_hz is not None:
                sw["center_hz"] = float(center_hz)
            if span_hz is not None:
                sw["span_hz"] = float(span_hz)
            if rbw_hz is not None:
                sw["rbw_hz"] = float(rbw_hz)
            if ref_level_dbm is not None:
                sw["ref_level_dbm"] = float(ref_level_dbm)
            if atten_db is not None:
                sw["atten_db"] = int(atten_db)
            CONFIG.update("sweep", sw)
            # Buat preset custom on-the-fly
            self._preset = Preset(
                key="custom",
                name="Kustom",
                category="Kustom",
                description="Pengaturan manual operator",
                center_hz=sw["center_hz"],
                span_hz=sw["span_hz"],
                rbw_hz=sw["rbw_hz"],
                vbw_hz=sw["rbw_hz"],
                ref_level_dbm=sw["ref_level_dbm"],
                atten_db=sw["atten_db"],
            )
            self._channels = self._preset.channels()
            self._config_dirty = True
            if self._state == STATE_SWEEPING and self._device is not None:
                try:
                    self._device.stop_stream()
                except Exception:
                    pass

    def set_auto_record(self, enabled: bool) -> None:
        with self._lock:
            self._auto_record_enabled = bool(enabled)
            CONFIG.update("occupancy", {"auto_record": self._auto_record_enabled})

    def set_occupancy_tracking(self, enabled: bool) -> None:
        """Aktifkan / jeda akumulasi statistik kanal tanpa menghentikan sapuan."""
        with self._lock:
            self._occupancy_enabled = bool(enabled)

    def start_manual_audio(self, freq_hz: float, demod: str = "FM", record: bool = False) -> dict:
        """Mulai demodulasi manual; bila `record` juga ingat ke file WAV.

        Perangkat tidak bisa menyapu sekaligus mendemodulasi audio, jadi mesin
        beralih ke STATE_MANUAL_AUDIO dan sapuan dijeda sampai diteghentikan.
        """
        freq = float(freq_hz)
        demod_u = str(demod).upper()
        with self._lock:
            self._manual_audio_freq = freq
            self._manual_audio_demod = demod_u
            self._state = STATE_MANUAL_AUDIO
            self._config_dirty = True
            if self._device is not None:
                try:
                    self._device.stop_stream()
                except Exception:
                    pass

        info: Dict[str, Any] = {"status": "ok", "freq_hz": freq, "demod": demod_u}
        if record:
            info["record"] = self.start_manual_record(freq_hz=freq, demod=demod_u)
        return info

    def stop_manual_audio(self) -> None:
        self._drain_audio_subscribers()
        with self._lock:
            self._manual_audio_freq = None
            if self._manual_recorder is not None:
                self.stop_manual_record()
            if self._state == STATE_MANUAL_AUDIO:
                self._state = STATE_SWEEPING
                self._config_dirty = True
                if self._device is not None:
                    try:
                        self._device.stop_stream()
                    except Exception:
                        pass

    # -- rekam audio manual ------------------------------------------------ #
    def start_manual_record(self, freq_hz: Optional[float] = None,
                            demod: Optional[str] = None) -> dict:
        """Rekam audio (dari demodulator manual) ke file WAV.

        Bila belum ada sesi dengar aktif, sesi dibuat otomatis pada frekuensi
        yang diminta supaya operator bisa merekam tanpa dua langkah terpisah.
        """
        freq = float(freq_hz if freq_hz is not None else (self._manual_audio_freq or 0.0))
        if freq <= 0:
            return {"status": "error", "message": "Frekuensi rekam belum ditentukan."}

        demod_u = str(demod or self._manual_audio_demod or "FM").upper()

        with self._lock:
            if self._manual_recorder is not None:
                return {
                    "status": "already_recording",
                    "freq_hz": self._manual_record_freq,
                    "filename": self._manual_recorder.path.name,
                }
            if self._stage_active():
                return {
                    "status": "error",
                    "message": "Mesin sedang merekam dwell otomatis — tunggu sampai selesai.",
                }
            if self._manual_audio_freq is None:
                self._manual_audio_freq = freq
                self._manual_audio_demod = demod_u
                self._state = STATE_MANUAL_AUDIO
                self._config_dirty = True
            dev = self._device
            if dev is not None:
                try:
                    dev.stop_stream()
                except Exception:
                    pass

        rate = AUDIO_SAMPLES_PER_SEC
        try:
            rate = dev.audio_rate or rate
        except Exception:
            pass
        if not rate or rate <= 0:
            try:
                rate = dev.measure_audio_rate(blocks=4)
            except Exception:
                rate = AUDIO_SAMPLES_PER_SEC

        path = unique_path(RECORDINGS_DIR, freq)
        rec = WavRecorder(path, sample_rate=rate, freq_hz=freq)
        try:
            rec.open()
        except Exception as exc:
            return {"status": "error", "message": f"Gagal membuka berkas WAV: {exc}"}

        with self._lock:
            self._manual_recorder = rec
            self._manual_record_freq = freq
            self._manual_record_started = time.time()
            self._manual_record_demod = demod_u
            meta = {
                "type": "recording_started",
                "freq_hz": freq,
                "freq_mhz": round(freq / 1e6, 4),
                "filename": path.name,
                "manual": True,
                "demod": demod_u,
                "started_at": datetime.now().isoformat(timespec="seconds"),
            }
        self._broadcast(self._event_subscribers, meta)
        return {"status": "ok", "freq_hz": freq, "filename": path.name,
                "manual": True, "demod": demod_u}

    def stop_manual_record(self) -> dict:
        """Tutup rekaman manual dan tulis event `recording_finished`."""
        with self._lock:
            rec = self._manual_recorder
            freq = self._manual_record_freq
            demod = self._manual_record_demod
            self._manual_recorder = None
            self._manual_record_freq = None
            self._manual_record_started = 0.0

        if rec is None:
            return {"status": "not_recording"}

        meta = rec.close(reason="manual_stop")
        if meta is None:
            return {"status": "discarded", "message": "Tidak ada audio terekam."}

        self._broadcast(self._event_subscribers, {
            "type": "recording_finished",
            "manual": True,
            "meta": meta.as_dict(),
        })
        return {"status": "ok", "freq_hz": freq, "demod": demod,
                "filename": meta.filename, "duration_sec": meta.duration_sec}

    def _stage_active(self) -> bool:
        """True bila rekaman dwell otomatis sedang memakai perangkat."""
        return (self._state == STATE_DWELL_RECORD
                and self._current_recorder is not None
                and self._dwell_freq is not None)

    # -- subscriber WebSocket ---------------------------------------------- #
    def subscribe_spectrum(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._spectrum_subscribers.add(q)

    def unsubscribe_spectrum(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._spectrum_subscribers.discard(q)

    def subscribe_audio(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._audio_subscribers.add(q)

    def unsubscribe_audio(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._audio_subscribers.discard(q)

    def subscribe_events(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._event_subscribers.add(q)

    def unsubscribe_events(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._event_subscribers.discard(q)

    def _broadcast(self, target: Set[asyncio.Queue], item: Any) -> None:
        if not target or self._loop is None:
            return
        for q in list(target):
            try:
                self._loop.call_soon_threadsafe(self._put_nowait_drop_old, q, item)
            except RuntimeError:
                pass

    @staticmethod
    def _put_nowait_drop_old(q: asyncio.Queue, item: Any) -> None:
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            pass

    # -- loop utama -------------------------------------------------------- #
    def _run_loop(self) -> None:
        fps_t0 = time.perf_counter()
        fps_frames = 0
        while not self._stop_event.is_set():
            try:
                state = self._state
                if state == STATE_SWEEPING:
                    self._step_sweep()
                elif state == STATE_DWELL_RECORD:
                    self._step_dwell_audio()
                elif state == STATE_MANUAL_AUDIO:
                    self._step_manual_audio()
                else:
                    time.sleep(0.05)

                fps_frames += 1
                now = time.perf_counter()
                if now - fps_t0 >= 1.0:
                    self._fps = fps_frames / (now - fps_t0)
                    fps_frames = 0
                    fps_t0 = now

            except SAError as exc:
                self._last_error = f"Hardware error: {exc}"
                self._broadcast(self._event_subscribers, {"type": "error", "message": str(exc)})
                time.sleep(0.5)
            except Exception as exc:  # pragma: no cover
                self._last_error = f"Engine error: {exc}"
                time.sleep(0.5)

    def _step_sweep(self) -> None:
        dev = self._device
        if dev is None:
            time.sleep(0.05)
            return

        p = self._preset
        sw_cfg = CONFIG.get("sweep")
        center = sw_cfg.get("center_hz", p.center_hz)
        span = sw_cfg.get("span_hz", p.span_hz)
        rbw = sw_cfg.get("rbw_hz", p.rbw_hz)
        ref = sw_cfg.get("ref_level_dbm", p.ref_level_dbm)
        atten = sw_cfg.get("atten_db", p.atten_db)

        # Inisialisasi sweep jika belum berjalan atau konfigurasi berubah.
        # `self._sweep_configured` (bukan `_active_channel_indices`) menentukan
        # "belum pernah dikonfigurasi": preset/span manual yang tak memetakan
        # satu kanal pun tidak boleh memicu configure+initiate setiap siklus.
        needs_init = (
            not self._sweep_configured
            or not hasattr(dev, "_mode")
            or getattr(dev, "_mode") != SA_SWEEPING  # SA_SWEEPING = 0
            or self._config_dirty
        )
        if needs_init:
            dev.configure_sweep(
                center_hz=center,
                span_hz=span,
                rbw_hz=rbw,
                ref_level_dbm=ref,
                atten_db=atten,
            )
            dev.initiate_sweep()
            self._config_dirty = False
            self._sweep_configured = True

        freqs, levels, start_freq, bin_size = dev.get_sweep()
        if not levels:
            return

        # Pemetaan kanal memakai geometri yang dilaporkan perangkat
        # (saQuerySweepInfo), bukan (center - span/2) sintetis: pustaka Signal
        # Hound membulatkan start/bin dan selisihnya menggeser indeks kanal.
        if needs_init:
            self._map_channel_bins(len(levels), start_freq, bin_size)

        self._sweep_count += 1

        # Estimasi noise floor (median 20% terbawah agar sinyal kuat tidak bias)
        sorted_lev = sorted(levels)
        k = max(1, int(len(sorted_lev) * 0.2))
        noise_floor = sum(sorted_lev[:k]) / k

        # Hitung threshold aktif
        occ_cfg = CONFIG.get("occupancy")
        mode = occ_cfg.get("threshold_mode", "adaptive")
        if mode == "fixed":
            threshold = float(occ_cfg.get("fixed_threshold_dbm", -85.0))
        else:
            offset = float(occ_cfg.get("adaptive_offset_db", 10.0))
            threshold = noise_floor + offset

        # Evaluasi per kanal
        occupied: List[dict] = []
        highest_freq: Optional[float] = None
        highest_dbm: float = -200.0
        tracking = self._occupancy_enabled

        for ch, idx in self._active_channel_indices:
            if idx < 0 or idx >= len(levels):
                continue
            # Ambil level maksimum di sekitar kanal (+- 1 bin)
            lo = max(0, idx - 1)
            hi = min(len(levels), idx + 2)
            peak = max(levels[lo:hi])
            is_above = peak >= threshold

            if tracking:
                # Akumulasi statistik kanal (ITU-R SM.1880) hanya saat pengukuran
                # aktif; saat dijeda, tampilan langsung tetap jalan tetapi
                # hitungan okupansi tidak bertambah.
                OCC.record_channel_sample(ch.freq_hz, peak, is_above, label=ch.label or f"{ch.mhz:.4f} MHz")

            cnt = self._hit_counters.get(ch.freq_hz, 0)
            if is_above:
                if tracking:
                    cnt += 1
                    self._hit_counters[ch.freq_hz] = cnt
                occupied.append({
                    "freq_hz": ch.freq_hz,
                    "freq_mhz": ch.mhz,
                    "level_dbm": round(peak, 1),
                    "above_db": round(peak - threshold, 1),
                    "hits": cnt,
                })
                if peak > highest_dbm:
                    highest_dbm = peak
                    highest_freq = ch.freq_hz
            elif tracking:
                self._hit_counters[ch.freq_hz] = max(0, cnt - 1)

        # Buat frame untuk client
        frame = SweepFrame(
            timestamp=time.time(),
            center_hz=center,
            span_hz=span,
            start_hz=start_freq,
            bin_size_hz=bin_size,
            length=len(levels),
            levels=levels,
            noise_floor_dbm=round(noise_floor, 1),
            threshold_dbm=round(threshold, 1),
            occupied_channels=occupied,
        )
        self._last_frame = frame
        self._broadcast_spectrum(frame)

        # Auto-record trigger: jika auto-record aktif dan ada kanal lolos filter siklus
        trigger_cycles = int(occ_cfg.get("trigger_cycles", 2))
        auto_rec = bool(occ_cfg.get("auto_record", self._auto_record_enabled))

        if auto_rec and highest_freq is not None:
            if self._hit_counters.get(highest_freq, 0) >= trigger_cycles:
                self._enter_dwell_record(highest_freq)

    def _map_channel_bins(self, sweep_len: int, start_freq: float, bin_size: float) -> None:
        """Peta frekuensi kanal ke indeks bin terdekat."""
        indices: List[Tuple[Channel, int]] = []
        if bin_size <= 0:
            self._active_channel_indices = []
            return
        stop_freq = start_freq + sweep_len * bin_size
        for ch in self._channels:
            if start_freq <= ch.freq_hz <= stop_freq:
                idx = int(round((ch.freq_hz - start_freq) / bin_size))
                idx = max(0, min(sweep_len - 1, idx))
                indices.append((ch, idx))
        self._active_channel_indices = indices

    # -- dwell audio recording --------------------------------------------- #
    def _enter_dwell_record(self, freq_hz: float) -> None:
        dev = self._device
        if dev is None:
            return
        audio_cfg = CONFIG.get("audio")
        demod = audio_cfg.get("demod", self._preset.demod or "FM")
        if_bw = float(audio_cfg.get("if_bw_hz", self._preset.if_bw_hz))

        try:
            dev.stop_stream()
            dev.configure_audio(
                center_hz=freq_hz,
                demod=demod,
                if_bw_hz=if_bw,
                lpf_hz=float(audio_cfg.get("lpf_hz", 8_000.0)),
                hpf_hz=float(audio_cfg.get("hpf_hz", 300.0)),
                deemphasis_us=float(audio_cfg.get("deemphasis_us", 75.0)),
            )
            dev.initiate_audio()
        except SAError as exc:
            self._last_error = f"Gagal masuk mode audio: {exc}"
            return

        rate = dev.audio_rate
        # Rate nominal hanya dipakai sebagai fallback; nilai sebenarnya diukur
        # empiris dari perangkat supaya durasi file WAV akurat.
        if not rate or rate <= 0:
            try:
                rate = dev.measure_audio_rate(blocks=4)
            except Exception:
                rate = AUDIO_SAMPLES_PER_SEC
        path = unique_path(RECORDINGS_DIR, freq_hz)
        rec = WavRecorder(path, sample_rate=rate, freq_hz=freq_hz)
        rec.open()

        self._current_recorder = rec
        self._dwell_freq = freq_hz
        self._dwell_started = time.time()
        self._dwell_last_above = time.time()
        self._state = STATE_DWELL_RECORD

        self._broadcast(self._event_subscribers, {
            "type": "recording_started",
            "freq_hz": freq_hz,
            "freq_mhz": round(freq_hz / 1e6, 4),
            "filename": path.name,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        })

    def _step_dwell_audio(self) -> None:
        dev = self._device
        rec = self._current_recorder
        if dev is None or rec is None or self._dwell_freq is None:
            self._state = STATE_SWEEPING
            return

        audio_cfg = CONFIG.get("audio")
        dwell_limit = float(audio_cfg.get("dwell_sec", 12.0))
        hang_time = float(audio_cfg.get("hang_time_sec", 2.0))
        max_rec = float(audio_cfg.get("max_recording_sec", 30.0))

        block = dev.get_audio_block()
        rec.write(block)
        self._broadcast_audio_chunk(block)

        # Hitung daya blok audio untuk evaluasi squelch hang-time
        peak = max(abs(min(block)), abs(max(block)))
        now = time.time()
        # Jika ada sinyal (peak > 0.05), perbarui waktu kedudukan
        if peak > 0.05:
            self._dwell_last_above = now

        # Periksa kondisi selesai
        dur = now - self._dwell_started
        silence_dur = now - self._dwell_last_above

        should_stop = False
        reason = "completed"
        if dur >= max_rec:
            should_stop = True
            reason = "max_duration"
        elif dur >= dwell_limit and silence_dur >= hang_time:
            should_stop = True
            reason = "squelch_hang"

        if should_stop:
            meta = rec.close(reason=reason)
            self._current_recorder = None
            self._dwell_freq = None
            self._state = STATE_SWEEPING
            try:
                dev.stop_stream()
            except Exception:
                pass
            if meta:
                self._broadcast(self._event_subscribers, {
                    "type": "recording_finished",
                    "meta": meta.as_dict(),
                })

    # -- manual audio listening -------------------------------------------- #
    def _step_manual_audio(self) -> None:
        dev = self._device
        freq = self._manual_audio_freq
        if dev is None or freq is None:
            self._state = STATE_SWEEPING
            return

        audio_cfg = CONFIG.get("audio")
        if not hasattr(dev, "_mode") or getattr(dev, "_mode") != 3:  # SA_AUDIO = 3
            try:
                dev.configure_audio(
                    center_hz=freq,
                    demod=self._manual_audio_demod,
                    if_bw_hz=float(audio_cfg.get("if_bw_hz", 15_000.0)),
                    lpf_hz=float(audio_cfg.get("lpf_hz", 8_000.0)),
                    hpf_hz=float(audio_cfg.get("hpf_hz", 300.0)),
                    deemphasis_us=float(audio_cfg.get("deemphasis_us", 75.0)),
                )
                dev.initiate_audio()
            except SAError as exc:
                self._last_error = f"Gagal masuk mode audio manual: {exc}"
                self._state = STATE_SWEEPING
                return

        block = dev.get_audio_block()

        with self._lock:
            rec = self._manual_recorder

        if rec is not None:
            try:
                rec.write(block)
            except Exception as exc:  # pragma: no cover - I/O disk
                self._last_error = f"Gagal menulis WAV: {exc}"
                self.stop_manual_record()

        self._broadcast_audio_chunk(block)

    # -- binary streaming format ------------------------------------------- #
    def _broadcast_spectrum(self, frame: SweepFrame) -> None:
        """Kirim trace sebagai payload biner cepat:
        Header:
          uint32 magic       = 0x53504543 ("SPEC")
          uint32 length      = N
          float64 timestamp
          float64 start_hz
          float64 bin_size_hz
          float32 noise_floor_dbm
          float32 threshold_dbm
        Body:
          N * float32 levels
        JSON event trailing (opsional) via event channel.
        """
        if not self._spectrum_subscribers:
            return
        n = len(frame.levels)
        hdr = struct.pack(
            "<4sIdddff",
            b"SPEC",
            n,
            frame.timestamp,
            frame.start_hz,
            frame.bin_size_hz,
            frame.noise_floor_dbm,
            frame.threshold_dbm,
        )
        body = struct.pack(f"<{n}f", *frame.levels)
        msg = hdr + body
        self._broadcast(self._spectrum_subscribers, msg)

    def _broadcast_audio_chunk(self, samples: List[float]) -> None:
        """Kirim blok audio sebagai biner PCM float32 ke /ws/audio."""
        if not self._audio_subscribers:
            return
        n = len(samples)
        payload = struct.pack(f"<{n}f", *samples)
        self._broadcast(self._audio_subscribers, payload)

    # -- status ------------------------------------------------------------ #
    @property
    def device_info(self) -> DeviceInfo:
        dev = self._device
        return dev.info if dev else self._device_info

    def status(self) -> dict:
        dev = self._device
        info = dev.info if dev else self._device_info
        temp = dev.temperature() if dev and dev.is_open else None
        with self._lock:
            rec = self._manual_recorder
            manual_freq = self._manual_audio_freq
            manual_demod = self._manual_audio_demod
            manual_started = self._manual_record_started
        return {
            "state": self._state,
            "running": self._thread is not None and self._thread.is_alive(),
            "device": info.as_dict(),
            "temperature_c": temp,
            "preset": self._preset.key,
            "preset_name": self._preset.name,
            "fps": round(self._fps, 1),
            "sweeps": self._sweep_count,
            "channel_count": len(self._channels),
            "auto_record": self._auto_record_enabled,
            "occupancy_tracking": self._occupancy_enabled,
            "manual_recording": rec is not None,
            "manual_record_filename": rec.path.name if rec is not None else None,
            "manual_record_elapsed_sec": (
                round(time.time() - manual_started, 1)
                if rec is not None and manual_started > 0 else 0.0
            ),
            "manual_audio": self._state == STATE_MANUAL_AUDIO,
            "manual_freq_hz": manual_freq,
            "manual_demod": manual_demod,
            "last_error": self._last_error,
            "recording_active": (self._current_recorder is not None) or (rec is not None),
            "dwell_freq_hz": self._dwell_freq,
        }


ENGINE = CaptureEngine()
