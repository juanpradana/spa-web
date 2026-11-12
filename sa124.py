"""Binding ctypes untuk API Signal Hound SA44/SA124 (sa_api.dll / libsa_api.so).

Lapisan ini adalah satu-satunya tempat yang menyentuh pustaka native. Semua
pemanggilan dibungkus `_lock` (RLock) karena USB SA124B tidak boleh diakses
dua thread sekaligus: mengganti mode saat sweep berjalan akan menyebabkan
saUSBCommErr.

Jika pustaka atau perangkat tidak tersedia, modul otomatis jatuh ke
`SimulatedSA124` supaya seluruh aplikasi (UI, okupansi, rekam WAV, laporan)
tetap dapat dijalankan dan diuji tanpa perangkat keras.
"""
from __future__ import annotations

import ctypes
import math
import os
import platform
import random
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Konstanta API
# --------------------------------------------------------------------------- #
SA_SWEEPING = 0x0
SA_REAL_TIME = 0x1
SA_IQ = 0x2
SA_AUDIO = 0x3
SA_TG_SWEEP = 0x4

SA_MIN_MAX = 0
SA_AVERAGE = 1

SA_LOG_SCALE = 0
SA_LIN_SCALE = 1
SA_LOG_FULL_SCALE = 2
SA_LIN_FULL_SCALE = 3

SA_AUTO_ATTEN = -1
SA_AUTO_GAIN = -1

SA_AUDIO_AM = 0
SA_AUDIO_FM = 1
SA_AUDIO_USB = 2
SA_AUDIO_LSB = 3
SA_AUDIO_CW = 4

AUDIO_MODES = {
    "AM": SA_AUDIO_AM,
    "FM": SA_AUDIO_FM,
    "USB": SA_AUDIO_USB,
    "LSB": SA_AUDIO_LSB,
    "CW": SA_AUDIO_CW,
}

DEVICE_TYPES = {
    0: "None",
    1: "SA44",
    2: "SA44B",
    3: "SA124A",
    4: "SA124B",
}

SA_MAX_DEVICES = 8

# SA124B: rentang 100 kHz .. 12.4 GHz (marketing) / API menerima sampai 13 GHz.
MIN_FREQ_HZ = 100_000.0
MAX_FREQ_HZ = 12_400_000_000.0
MIN_SPAN_HZ = 10.0
MAX_IF_BANDWIDTH_HZ = 250_000.0
MAX_IQ_SAMPLE_RATE = 486_111.111

# Blok audio yang dikembalikan saGetAudio selalu 4096 sampel.
AUDIO_BLOCK_SAMPLES = 4096
AUDIO_SAMPLES_PER_SEC = 32_000.0  # nilai nominal; diukur ulang saat runtime


# --------------------------------------------------------------------------- #
# Batas RBW hardware SA124B & helper — definisi tunggalnya di channels.py
#
# Sebelumnya blok ini diduplikasi persis di sini dan di channels.py, sehingga
# dua salinan bisa menyimpang tanpa ketahuan. channels.py tidak mengimpor
# modul ini, jadi impor searah ini tidak membentuk siklus.
# --------------------------------------------------------------------------- #
from channels import (  # noqa: E402  (impor setelah konstanta agar urutan baca jelas)
    LOW_BAND_EDGE_HZ,
    NARROW_SPAN_MAX_HZ,
    NARROW_SPAN_MIN_HZ,
    RBW_ABS_MIN_HZ,
    RBW_MAX_SWEPT_HZ,
    RBW_MIN_NARROW_HZ,
    RBW_MIN_WIDE_HZ,
    clamp_rbw,
    clamp_vbw,
)


def atten_index(atten_db: int) -> int:
    """Map attenuasi dB ke indeks hardware SA124B: -1=auto, 0=0dB, 1=10dB, 2=20dB, 3=30dB."""
    if atten_db <= 0:
        return SA_AUTO_ATTEN
    if atten_db <= 10:
        return 1
    if atten_db <= 20:
        return 2
    return 3


def gain_index(gain_db: int, preamp: bool) -> int:
    """Map IF gain dB ke indeks hardware: -1=auto, 0=-16dB, 1=0dB, 2=+12dB."""
    if preamp:
        # Dengan preamp aktif, gunakan auto gain
        return SA_AUTO_GAIN
    if gain_db <= -10:
        return 0  # -16 dB
    if gain_db <= 0:
        return 1  # 0 dB
    return 2  # +12 dB


class SAError(RuntimeError):
    """Error dari pustaka Signal Hound (atau dari lapisan simulasi)."""


# --------------------------------------------------------------------------- #
# Pemuatan pustaka native
# --------------------------------------------------------------------------- #
def _library_candidates() -> List[Path]:
    env = os.environ.get("SA_API_PATH")
    cands: List[Path] = []
    if env:
        p = Path(env)
        cands.append(p if p.is_file() else p / _lib_name())
    if sys.platform == "win32":
        spike = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Signal Hound" / "Spike"
        cands.append(spike / "sa_api.dll")
    else:
        cands += [
            Path("/usr/local/lib/libsa_api.so"),
            Path("/usr/lib/libsa_api.so"),
            Path("/opt/signalhound/libsa_api.so"),
            Path(__file__).resolve().parent / "libsa_api.so",
        ]
    cands.append(Path(__file__).resolve().parent / _lib_name())
    return cands


def _lib_name() -> str:
    return "sa_api.dll" if sys.platform == "win32" else "libsa_api.so"


def load_library() -> Tuple[Optional[ctypes.CDLL], Optional[str]]:
    """Kembalikan (library, error_message)."""
    for path in _library_candidates():
        try:
            if not path.is_file():
                continue
            if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
                os.add_dll_directory(str(path.parent))
            lib = ctypes.WinDLL(str(path)) if sys.platform == "win32" else ctypes.CDLL(str(path))
            return lib, None
        except OSError as exc:
            last = f"{path}: {exc}"
            continue
    return None, "pustaka sa_api tidak ditemukan"


def _bind(lib: ctypes.CDLL) -> None:
    """Tetapkan argtypes/restype secara eksplisit (mencegah crash 64-bit)."""
    c_int_p = ctypes.POINTER(ctypes.c_int)
    c_double_p = ctypes.POINTER(ctypes.c_double)
    c_float_p = ctypes.POINTER(ctypes.c_float)

    lib.saGetAPIVersion.restype = ctypes.c_char_p
    lib.saGetAPIVersion.argtypes = []

    lib.saGetSerialNumberList.restype = ctypes.c_int
    lib.saGetSerialNumberList.argtypes = [c_int_p, c_int_p]

    lib.saOpenDevice.restype = ctypes.c_int
    lib.saOpenDevice.argtypes = [c_int_p]

    lib.saOpenDeviceBySerialNumber.restype = ctypes.c_int
    lib.saOpenDeviceBySerialNumber.argtypes = [c_int_p, ctypes.c_int]

    lib.saCloseDevice.restype = ctypes.c_int
    lib.saCloseDevice.argtypes = [ctypes.c_int]

    lib.saAbort.restype = ctypes.c_int
    lib.saAbort.argtypes = [ctypes.c_int]

    lib.saPreset.restype = ctypes.c_int
    lib.saPreset.argtypes = [ctypes.c_int]

    lib.saGetSerialNumber.restype = ctypes.c_int
    lib.saGetSerialNumber.argtypes = [ctypes.c_int, c_int_p]

    lib.saGetDeviceType.restype = ctypes.c_int
    lib.saGetDeviceType.argtypes = [ctypes.c_int, c_int_p]

    lib.saGetFirmwareString.restype = ctypes.c_int
    lib.saGetFirmwareString.argtypes = [ctypes.c_int, ctypes.c_char_p]

    lib.saGetErrorString.restype = ctypes.c_int
    lib.saGetErrorString.argtypes = [ctypes.c_int, ctypes.c_char_p]

    lib.saQueryTemperature.restype = ctypes.c_int
    lib.saQueryTemperature.argtypes = [ctypes.c_int, c_float_p]

    lib.saConfigCenterSpan.restype = ctypes.c_int
    lib.saConfigCenterSpan.argtypes = [ctypes.c_int, ctypes.c_double, ctypes.c_double]

    lib.saConfigSweepCoupling.restype = ctypes.c_int
    lib.saConfigSweepCoupling.argtypes = [
        ctypes.c_int, ctypes.c_double, ctypes.c_double, ctypes.c_bool,
    ]

    lib.saConfigLevel.restype = ctypes.c_int
    lib.saConfigLevel.argtypes = [ctypes.c_int, ctypes.c_double]

    lib.saConfigGainAtten.restype = ctypes.c_int
    lib.saConfigGainAtten.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_bool]

    lib.saConfigAcquisition.restype = ctypes.c_int
    lib.saConfigAcquisition.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]

    lib.saConfigProcUnits.restype = ctypes.c_int
    lib.saConfigProcUnits.argtypes = [ctypes.c_int, ctypes.c_int]

    lib.saConfigAudio.restype = ctypes.c_int
    lib.saConfigAudio.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_double, ctypes.c_double,
        ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ]

    lib.saConfigIQ.restype = ctypes.c_int
    lib.saConfigIQ.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_double]

    lib.saInitiate.restype = ctypes.c_int
    lib.saInitiate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]

    lib.saQuerySweepInfo.restype = ctypes.c_int
    lib.saQuerySweepInfo.argtypes = [ctypes.c_int, c_int_p, c_double_p, c_double_p]

    lib.saGetSweep_32f.restype = ctypes.c_int
    lib.saGetSweep_32f.argtypes = [ctypes.c_int, c_float_p, c_float_p]

    lib.saGetAudio.restype = ctypes.c_int
    lib.saGetAudio.argtypes = [ctypes.c_int, c_float_p]

    lib.saGetIQ_32f.restype = ctypes.c_int
    lib.saGetIQ_32f.argtypes = [ctypes.c_int, c_float_p]

    if hasattr(lib, "saQueryStreamInfo"):
        lib.saQueryStreamInfo.restype = ctypes.c_int
        lib.saQueryStreamInfo.argtypes = [ctypes.c_int, c_int_p, c_double_p]


# --------------------------------------------------------------------------- #
# Info perangkat
# --------------------------------------------------------------------------- #
@dataclass
class DeviceInfo:
    connected: bool
    simulated: bool
    serial: Optional[int] = None
    model: str = "tidak diketahui"
    firmware: str = "?"
    api_version: str = "?"
    temperature_c: Optional[float] = None
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "connected": self.connected,
            "simulated": self.simulated,
            "serial": self.serial,
            "model": self.model,
            "firmware": self.firmware,
            "api_version": self.api_version,
            "temperature_c": self.temperature_c,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# Implementasi nyata
# --------------------------------------------------------------------------- #
class SA124:
    """Pembungkus perangkat SA124B (thread-safe)."""

    def __init__(self, serial: Optional[int] = None) -> None:
        self._lock = threading.RLock()
        self._lib: Optional[ctypes.CDLL] = None
        self._device = ctypes.c_int(-1)
        self._open = False
        self._sweep_len = 0
        self._start_freq = 0.0
        self._bin_size = 0.0
        self._mode: Optional[int] = None
        self._serial = serial
        self._info = DeviceInfo(connected=False, simulated=False)
        self._sweep_buf = None
        self._audio_buf = None
        self._audio_center_hz: Optional[float] = None
        self._audio_rate = AUDIO_SAMPLES_PER_SEC

    # -- siklus hidup ------------------------------------------------------ #
    def open(self) -> DeviceInfo:
        with self._lock:
            if self._open:
                return self._info
            lib, err = load_library()
            if lib is None:
                raise SAError(err or "pustaka sa_api tidak dapat dimuat")
            _bind(lib)
            self._lib = lib

            api_version = b"?"
            try:
                v = lib.saGetAPIVersion()
                api_version = v if isinstance(v, bytes) else b"?"
            except Exception:  # pragma: no cover - bergantung DLL
                pass

            dev = ctypes.c_int(-1)
            if self._serial:
                status = lib.saOpenDeviceBySerialNumber(ctypes.byref(dev), ctypes.c_int(int(self._serial)))
            else:
                status = lib.saOpenDevice(ctypes.byref(dev))
            if status != 0:
                raise SAError(self._err(status) or f"saOpenDevice gagal (status {status})")

            self._device = dev
            self._open = True

            serial_val = ctypes.c_int(0)
            if lib.saGetSerialNumber(dev, ctypes.byref(serial_val)) == 0:
                self._serial = serial_val.value

            dtype = ctypes.c_int(0)
            model = "tidak diketahui"
            if lib.saGetDeviceType(dev, ctypes.byref(dtype)) == 0:
                model = DEVICE_TYPES.get(dtype.value, f"tipe {dtype.value}")

            fw = ctypes.create_string_buffer(64)
            firmware = "?"
            if lib.saGetFirmwareString(dev, fw) == 0:
                firmware = fw.value.decode("ascii", "replace")

            temp = ctypes.c_float(0.0)
            temperature = None
            if lib.saQueryTemperature(dev, ctypes.byref(temp)) == 0:
                temperature = round(float(temp.value), 1)

            self._info = DeviceInfo(
                connected=True,
                simulated=False,
                serial=self._serial,
                model=model,
                firmware=firmware,
                api_version=api_version.decode("ascii", "replace") if isinstance(api_version, bytes) else str(api_version),
                temperature_c=temperature,
            )
            return self._info

    def close(self) -> None:
        with self._lock:
            if self._open and self._lib is not None:
                try:
                    self._lib.saCloseDevice(self._device)
                except Exception:  # pragma: no cover
                    pass
            self._open = False
            self._mode = None
            self._info = DeviceInfo(connected=False, simulated=False, error=self._info.error)

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def info(self) -> DeviceInfo:
        return self._info

    def temperature(self) -> Optional[float]:
        with self._lock:
            if not self._open or self._lib is None:
                return None
            temp = ctypes.c_float(0.0)
            if self._lib.saQueryTemperature(self._device, ctypes.byref(temp)) == 0:
                value = round(float(temp.value), 1)
                self._info.temperature_c = value
                return value
            return None

    # -- mode -------------------------------------------------------------- #
    def _abort_if_running(self) -> None:
        if self._mode is not None and self._lib is not None and self._open:
            try:
                self._lib.saAbort(self._device)
            except Exception:  # pragma: no cover
                pass
            self._mode = None

    def configure_sweep(
        self,
        center_hz: float,
        span_hz: float,
        rbw_hz: float = 30_000.0,
        vbw_hz: Optional[float] = None,
        ref_level_dbm: float = -20.0,
        atten_db: int = 10,
        gain_db: int = 0,
        preamp: bool = False,
        detector: str = "average",
        scale: str = "log",
        image_reject: bool = True,
    ) -> None:
        with self._lock:
            self._require_open()
            self._abort_if_running()

            center_hz = _clamp(center_hz, MIN_FREQ_HZ, MAX_FREQ_HZ)
            span_hz = _clamp(span_hz, MIN_SPAN_HZ, MAX_FREQ_HZ)
            # Jaga seluruh rentang tetap di dalam kemampuan perangkat.
            half = span_hz / 2.0
            if center_hz - half < MIN_FREQ_HZ:
                center_hz = MIN_FREQ_HZ + half
            if center_hz + half > MAX_FREQ_HZ:
                center_hz = MAX_FREQ_HZ - half

            # RBW/VBW dibatasi ke kemampuan demodulator I/Q internal SA124B.
            # Melewatkan nilai di luar batas membuat pustaka Signal Hound
            # meng-clamp diam-diam sehingga geometri sapuan ≠ permintaan UI.
            rbw_hz = clamp_rbw(center_hz, span_hz, rbw_hz)
            vbw = clamp_vbw(rbw_hz, vbw_hz if vbw_hz else rbw_hz)

            self._check(self._lib.saConfigCenterSpan(self._device, ctypes.c_double(center_hz), ctypes.c_double(span_hz)), "saConfigCenterSpan")
            self._check(self._lib.saConfigSweepCoupling(
                self._device, ctypes.c_double(rbw_hz), ctypes.c_double(vbw), ctypes.c_bool(image_reject)
            ), "saConfigSweepCoupling")
            self._check(self._lib.saConfigLevel(self._device, ctypes.c_double(ref_level_dbm)), "saConfigLevel")
            self._check(self._lib.saConfigGainAtten(
                self._device,
                ctypes.c_int(atten_index(atten_db)),
                ctypes.c_int(gain_index(gain_db, preamp)),
                ctypes.c_bool(preamp),
            ), "saConfigGainAtten")

            det = SA_AVERAGE if str(detector).lower().startswith("avg") else SA_MIN_MAX
            sc = {"log": SA_LOG_SCALE, "lin": SA_LIN_SCALE, "logfull": SA_LOG_FULL_SCALE}.get(
                str(scale).lower(), SA_LOG_SCALE
            )
            self._check(self._lib.saConfigAcquisition(self._device, ctypes.c_int(det), ctypes.c_int(sc)), "saConfigAcquisition")

    def initiate_sweep(self) -> None:
        with self._lock:
            self._require_open()
            self._check(self._lib.saInitiate(self._device, ctypes.c_int(SA_SWEEPING), ctypes.c_int(0)), "saInitiate(SWEEPING)")
            self._mode = SA_SWEEPING
            self._read_sweep_info()

    def _read_sweep_info(self) -> None:
        length = ctypes.c_int(0)
        start = ctypes.c_double(0.0)
        bin_size = ctypes.c_double(0.0)
        self._check(
            self._lib.saQuerySweepInfo(self._device, ctypes.byref(length), ctypes.byref(start), ctypes.byref(bin_size)),
            "saQuerySweepInfo",
        )
        self._sweep_len = int(length.value)
        self._start_freq = float(start.value)
        self._bin_size = float(bin_size.value)
        self._sweep_buf = None

    @property
    def sweep_length(self) -> int:
        return self._sweep_len

    def get_sweep(self) -> Tuple[List[float], List[float], float, float]:
        """Kembalikan (freqs_hz, levels_dbm, start_freq, bin_size)."""
        with self._lock:
            self._require_open()
            if self._mode != SA_SWEEPING:
                raise SAError("perangkat tidak dalam mode sweeping")
            if self._sweep_len <= 0:
                self._read_sweep_info()
            buf_min = (ctypes.c_float * self._sweep_len)()
            buf_max = (ctypes.c_float * self._sweep_len)()
            self._check(self._lib.saGetSweep_32f(self._device, buf_min, buf_max), "saGetSweep_32f")
            levels = list(buf_max)
            freqs = [self._start_freq + i * self._bin_size for i in range(self._sweep_len)]
            return freqs, levels, self._start_freq, self._bin_size

    # -- audio ------------------------------------------------------------- #
    def configure_audio(
        self,
        center_hz: float,
        demod: str = "FM",
        if_bw_hz: float = 15_000.0,
        lpf_hz: float = 8_000.0,
        hpf_hz: float = 300.0,
        deemphasis_us: float = 75.0,
    ) -> None:
        with self._lock:
            self._require_open()
            self._abort_if_running()
            mode = AUDIO_MODES.get(str(demod).upper())
            if mode is None:
                raise SAError(f"mode demodulasi tidak dikenal: {demod}")
            self._audio_center_hz = float(center_hz)
            if_bw = _clamp(if_bw_hz, 500.0, 500_000.0)
            lpf = _clamp(lpf_hz, 1_000.0, 12_000.0)
            hpf = _clamp(hpf_hz, 20.0, 1_000.0)
            deemph = _clamp(deemphasis_us, 1.0, 100.0) if mode == SA_AUDIO_FM else 0.0

            self._check(self._lib.saConfigCenterSpan(
                self._device,
                ctypes.c_double(_clamp(center_hz, MIN_FREQ_HZ, MAX_FREQ_HZ)),
                ctypes.c_double(min(if_bw, MAX_IF_BANDWIDTH_HZ)),
            ), "saConfigCenterSpan(audio)")
            self._check(self._lib.saConfigAudio(
                self._device, ctypes.c_int(mode), ctypes.c_double(center_hz),
                ctypes.c_double(if_bw), ctypes.c_double(lpf), ctypes.c_double(hpf), ctypes.c_double(deemph),
            ), "saConfigAudio")

    def initiate_audio(self) -> None:
        with self._lock:
            self._require_open()
            self._check(self._lib.saInitiate(self._device, ctypes.c_int(SA_AUDIO), ctypes.c_int(0)), "saInitiate(AUDIO)")
            self._mode = SA_AUDIO
            self._audio_buf = (ctypes.c_float * AUDIO_BLOCK_SAMPLES)()

    def stop_stream(self) -> None:
        with self._lock:
            self._abort_if_running()

    def get_audio_block(self) -> List[float]:
        """Blok 4096 sampel float. Blocking ~128 ms."""
        with self._lock:
            self._require_open()
            if self._mode != SA_AUDIO:
                raise SAError("perangkat tidak dalam mode audio")
            if self._audio_buf is None:
                self._audio_buf = (ctypes.c_float * AUDIO_BLOCK_SAMPLES)()
            self._check(self._lib.saGetAudio(self._device, self._audio_buf), "saGetAudio")
            return list(self._audio_buf)

    def measure_audio_rate(self, blocks: int = 8) -> float:
        """Ukur sample rate audio secara empiris (bukan asumsi)."""
        with self._lock:
            if self._mode != SA_AUDIO:
                raise SAError("perangkat tidak dalam mode audio")
            start = time.perf_counter()
            for _ in range(blocks):
                self.get_audio_block()
            elapsed = time.perf_counter() - start
            if elapsed <= 0:
                return AUDIO_SAMPLES_PER_SEC
            rate = (AUDIO_BLOCK_SAMPLES * blocks) / elapsed
            self._audio_rate = float(rate)
            return self._audio_rate

    @property
    def audio_rate(self) -> float:
        return self._audio_rate

    # -- IQ ---------------------------------------------------------------- #
    def initiate_iq(self, decimation: int = 1, bandwidth: float = MAX_IF_BANDWIDTH_HZ) -> None:
        with self._lock:
            self._require_open()
            self._abort_if_running()
            self._check(self._lib.saConfigCenterSpan(
                self._device, ctypes.c_double(MIN_FREQ_HZ), ctypes.c_double(bandwidth)
            ), "saConfigCenterSpan(iq)")
            self._check(self._lib.saConfigIQ(self._device, ctypes.c_int(int(decimation)), ctypes.c_double(bandwidth)), "saConfigIQ")
            self._check(self._lib.saInitiate(self._device, ctypes.c_int(SA_IQ), ctypes.c_int(0)), "saInitiate(IQ)")
            self._mode = SA_IQ

    # -- internal ---------------------------------------------------------- #
    def _require_open(self) -> None:
        if not self._open or self._lib is None:
            raise SAError("perangkat belum dibuka")

    def _err(self, status: int) -> str:
        if not self._lib:
            return f"status {status}"
        try:
            buf = ctypes.create_string_buffer(128)
            if self._lib.saGetErrorString(ctypes.c_int(status), buf) == 0:
                return buf.value.decode("ascii", "replace")
        except Exception:  # pragma: no cover
            pass
        return f"status {status}"

    def _check(self, status: int, where: str) -> None:
        # status > 0 adalah peringatan (mis. RBW di-clamp), bukan kegagalan.
        if status < 0:
            raise SAError(f"{where}: {self._err(status)}")


# --------------------------------------------------------------------------- #
# Simulator (dipakai bila perangkat/pustaka tidak ada)
# --------------------------------------------------------------------------- #
class SimulatedSA124:
    """Menghasilkan spektrum & audio sintetis yang menyerupai perangkat nyata."""

    def __init__(self, serial: Optional[int] = None) -> None:
        self._lock = threading.RLock()
        self._serial = serial or 23276018
        self._sweep_len = 0
        self._start_freq = 0.0
        self._bin_size = 0.0
        self._mode: Optional[int] = None
        self._center = 100e6
        self._span = 20e6
        self._audio_rate = AUDIO_SAMPLES_PER_SEC
        self._t0 = time.time()
        self._audio_phase = 0.0
        self._audio_center_hz: Optional[float] = None
        self._info = DeviceInfo(
            connected=True,
            simulated=True,
            serial=self._serial,
            model="SA124B (simulasi)",
            firmware="sim-1.0",
            api_version="simulated",
            temperature_c=34.5,
            error=None,
        )
        # Carrier sintetis: (freq_hz, level_dbm, lebar_hz, modulasi)
        self._carriers: List[Tuple[float, float, float, str]] = [
            (127_125_000.0, -52.0, 8_000.0, "AM"),
            (127_750_000.0, -68.0, 6_000.0, "AM"),
            (156_800_000.0, -46.0, 12_000.0, "FM"),
            (433_920_000.0, -61.0, 12_500.0, "FM"),
            (2_437_000_000.0, -58.0, 20_000_000.0, "FM"),
            (5_785_000_000.0, -66.0, 10_000_000.0, "FM"),
        ]
        self._rng = random.Random(20261111)

    def open(self) -> DeviceInfo:
        return self._info

    def close(self) -> None:
        self._mode = None
        self._info = DeviceInfo(connected=False, simulated=True)

    @property
    def is_open(self) -> bool:
        return True

    @property
    def info(self) -> DeviceInfo:
        return self._info

    def temperature(self) -> Optional[float]:
        return self._info.temperature_c

    def configure_sweep(self, center_hz: float, span_hz: float, **_: object) -> None:
        with self._lock:
            self._center = _clamp(center_hz, MIN_FREQ_HZ, MAX_FREQ_HZ)
            self._span = _clamp(span_hz, MIN_SPAN_HZ, MAX_FREQ_HZ)
            self._mode = None

    def initiate_sweep(self) -> None:
        with self._lock:
            self._sweep_len = max(200, min(2000, int(self._span / 10_000.0)))
            self._start_freq = self._center - self._span / 2.0
            self._bin_size = self._span / self._sweep_len
            self._mode = SA_SWEEPING

    @property
    def sweep_length(self) -> int:
        return self._sweep_len

    def get_sweep(self) -> Tuple[List[float], List[float], float, float]:
        with self._lock:
            time.sleep(0.03)  # meniru waktu sweep nyata
            t = time.time() - self._t0
            levels: List[float] = []
            for i in range(self._sweep_len):
                f = self._start_freq + i * self._bin_size
                noise = -110.0 + self._rng.gauss(0.0, 1.6)
                level = noise
                for cf, amp, width, mod in self._carriers:
                    if abs(f - cf) < max(width, self._bin_size):
                        # Modulasi lambat supaya okupansi bervariasi terhadap waktu.
                        duty = 0.5 + 0.5 * math.sin(t * 0.35 + cf / 1e6)
                        burst = 1.0 if duty > 0.35 else 0.0
                        if burst:
                            level = max(level, amp + self._rng.gauss(0.0, 1.0))
                levels.append(round(level, 2))
            freqs = [self._start_freq + i * self._bin_size for i in range(self._sweep_len)]
            return freqs, levels, self._start_freq, self._bin_size

    def configure_audio(self, center_hz: float, demod: str = "FM", **_: object) -> None:
        with self._lock:
            self._center = center_hz
            self._demod = str(demod).upper()
            self._mode = None
            self._audio_center_hz = float(center_hz)

    def initiate_audio(self) -> None:
        self._mode = SA_AUDIO

    def stop_stream(self) -> None:
        self._mode = None

    def get_audio_block(self) -> List[float]:
        with self._lock:
            time.sleep(0.02)
            n = AUDIO_BLOCK_SAMPLES
            out: List[float] = []
            rate = AUDIO_SAMPLES_PER_SEC
            # Cari carrier terdekat untuk menentukan ada/tidaknya sinyal.
            active = any(abs(self._center - cf) < 15_000.0 for cf, *_ in self._carriers)
            tone = 900.0 if self._center > 100e6 else 440.0
            for k in range(n):
                self._audio_phase += 2 * math.pi * tone / rate
                if active:
                    sample = 0.35 * math.sin(self._audio_phase) + self._rng.gauss(0.0, 0.02)
                else:
                    sample = self._rng.gauss(0.0, 0.01)
                out.append(sample)
            return out

    def measure_audio_rate(self, blocks: int = 8) -> float:
        start = time.perf_counter()
        for _ in range(blocks):
            self.get_audio_block()
        elapsed = max(time.perf_counter() - start, 1e-6)
        self._audio_rate = (AUDIO_BLOCK_SAMPLES * blocks) / elapsed
        return self._audio_rate

    @property
    def audio_rate(self) -> float:
        return self._audio_rate

    def initiate_iq(self, **_: object) -> None:
        self._mode = SA_IQ


# --------------------------------------------------------------------------- #
# Helper
# --------------------------------------------------------------------------- #
def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def list_serial_numbers() -> Tuple[List[int], Optional[str]]:
    lib, err = load_library()
    if lib is None:
        return [], err
    try:
        _bind(lib)
        buf = (ctypes.c_int * SA_MAX_DEVICES)()
        count = ctypes.c_int(0)
        status = lib.saGetSerialNumberList(buf, ctypes.byref(count))
        if status < 0:
            return [], f"saGetSerialNumberList gagal (status {status})"
        return [int(buf[i]) for i in range(count.value)], None
    except Exception as exc:  # pragma: no cover
        return [], str(exc)


def platform_tag() -> str:
    return f"{platform.system()} {platform.release()} ({platform.machine()})"


# Alias supaya konsumen tidak perlu tahu implementasi mana yang dipakai.
SADevice = SA124
