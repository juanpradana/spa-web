"""Perekam audio WAV dengan penamaan berbasis frekuensi.

Nama file: ``{MHz:.4f}MHz_{YYYYMMDD_HHMMSS}.wav`` — mis. ``156.8000MHz_20261111_143022.wav``.
Ditulis sebagai PCM 16-bit mono memakai modul standar ``wave`` (tanpa dependensi
eksternal). Sample rate berasal dari pengukuran empiris perangkat, bukan asumsi.
"""
from __future__ import annotations

import math
import re
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

try:
    import numpy as _np
except ImportError:  # pragma: no cover - numpy sudah tersedia di target
    _np = None

FILENAME_RE = re.compile(r"^(?P<freq>\d+(?:\.\d+)?)MHz_(?P<ts>\d{8}_\d{6})(?:_\d+)?\.wav$")


def wav_filename(freq_hz: float, when: Optional[datetime] = None, suffix: int = 0) -> str:
    ts = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")
    base = f"{freq_hz / 1e6:.4f}MHz_{ts}"
    if suffix:
        base += f"_{suffix}"
    return base + ".wav"


def parse_wav_filename(name: str) -> Optional[dict]:
    m = FILENAME_RE.match(name)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), "%Y%m%d_%H%M%S")
    except ValueError:
        return None
    return {
        "freq_hz": float(m.group("freq")) * 1e6,
        "freq_mhz": float(m.group("freq")),
        "timestamp": ts.isoformat(),
        "timestamp_epoch": ts.timestamp(),
    }


def _to_pcm16(samples: List[float]) -> bytes:
    """Konversi float [-1, 1] -> PCM16 dengan clipping aman."""
    if _np is not None:
        arr = _np.asarray(samples, dtype=_np.float32)
        arr = _np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0)
        _np.clip(arr, -1.0, 1.0, out=arr)
        return (_np.rint(arr * 32767.0).astype("<i2")).tobytes()
    out = bytearray()
    for value in samples:
        if math.isnan(value) or math.isinf(value):
            value = 0.0
        value = max(-1.0, min(1.0, value))
        out += int(round(value * 32767.0)).to_bytes(2, "little", signed=True)
    return bytes(out)


@dataclass
class RecordingMeta:
    filename: str
    path: str
    freq_hz: float
    freq_mhz: float
    started_at: str
    duration_sec: float
    sample_rate: int
    frames: int
    size_bytes: int
    demod: str
    peak_dbfs: float = -120.0
    reason: str = "auto"
    preset: str = ""
    channels_seen: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "freq_hz": self.freq_hz,
            "freq_mhz": round(self.freq_mhz, 4),
            "started_at": self.started_at,
            "duration_sec": round(self.duration_sec, 2),
            "sample_rate": self.sample_rate,
            "size_bytes": self.size_bytes,
            "demod": self.demod,
            "peak_dbfs": round(self.peak_dbfs, 1),
            "reason": self.reason,
            "preset": self.preset,
        }


class WavRecorder:
    """Menulis satu file WAV secara streaming (buka -> append -> tutup)."""

    def __init__(self, path: Path, sample_rate: float, freq_hz: float) -> None:
        self.path = path
        self.freq_hz = freq_hz
        self._rate = max(8_000, min(192_000, int(round(sample_rate))))
        self._wf: Optional[wave.Wave_write] = None
        self._lock = threading.Lock()
        self._frames = 0
        self._peak = 0.0
        self._started = datetime.now()
        self._closed = False

    def open(self) -> None:
        with self._lock:
            if self._wf is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._wf = wave.open(str(self.path), "wb")
            self._wf.setnchannels(1)
            self._wf.setsampwidth(2)
            self._wf.setframerate(self._rate)

    def write(self, samples: List[float]) -> None:
        if not samples:
            return
        with self._lock:
            if self._wf is None or self._closed:
                return
            peak = max(abs(min(samples)), abs(max(samples)))
            if peak > self._peak:
                self._peak = peak
            self._wf.writeframes(_to_pcm16(samples))
            self._frames += len(samples)

    @property
    def duration(self) -> float:
        return self._frames / self._rate if self._rate else 0.0

    @property
    def frames(self) -> int:
        return self._frames

    @property
    def sample_rate(self) -> int:
        return self._rate

    @property
    def peak_dbfs(self) -> float:
        if self._peak <= 0:
            return -120.0
        return 20.0 * math.log10(self._peak)

    def close(self, reason: str = "auto") -> Optional[RecordingMeta]:
        with self._lock:
            if self._closed:
                return None
            self._closed = True
            if self._wf is not None:
                try:
                    self._wf.close()
                finally:
                    self._wf = None
            if self._frames == 0:
                # Rekaman kosong tidak ada gunanya; hapus supaya tidak menyesatkan.
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    pass
                return None
            size = self.path.stat().st_size if self.path.exists() else 0
            return RecordingMeta(
                filename=self.path.name,
                path=str(self.path),
                freq_hz=self.freq_hz,
                freq_mhz=self.freq_hz / 1e6,
                started_at=self._started.isoformat(timespec="seconds"),
                duration_sec=self.duration,
                sample_rate=self._rate,
                frames=self._frames,
                size_bytes=size,
                demod="",
                peak_dbfs=self.peak_dbfs,
                reason=reason,
            )


def unique_path(directory: Path, freq_hz: float, when: Optional[datetime] = None) -> Path:
    """Cari nama file yang belum dipakai (hindari menimpa rekaman lain)."""
    when = when or datetime.now()
    candidate = directory / wav_filename(freq_hz, when)
    counter = 1
    while candidate.exists():
        candidate = directory / wav_filename(freq_hz, when, counter)
        counter += 1
    return candidate


def scan_recordings(directory: Path, limit: int = 500) -> List[dict]:
    """Daftar rekaman di direktori, terbaru lebih dulu."""
    if not directory.exists():
        return []
    items: List[dict] = []
    for path in directory.glob("*.wav"):
        try:
            stat = path.stat()
        except OSError:
            continue
        meta = parse_wav_filename(path.name) or {"freq_hz": 0.0, "freq_mhz": 0.0}
        duration = 0.0
        rate = 0
        try:
            with wave.open(str(path), "rb") as wf:
                rate = wf.getframerate()
                frames = wf.getnframes()
                duration = frames / rate if rate else 0.0
        except (wave.Error, OSError):
            pass
        items.append({
            "filename": path.name,
            "freq_hz": meta.get("freq_hz", 0.0),
            "freq_mhz": round(meta.get("freq_mhz", 0.0), 4),
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "modified_epoch": stat.st_mtime,
            "duration_sec": round(duration, 2),
            "sample_rate": rate,
        })
    items.sort(key=lambda x: x["modified_epoch"], reverse=True)
    return items[:limit]


def delete_recording(directory: Path, filename: str) -> bool:
    """Hapus satu rekaman. Nama harus file langsung di dalam `directory`."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError("nama file tidak valid")
    target = (directory / filename).resolve()
    root = directory.resolve()
    if root not in target.parents:
        raise ValueError("path keluar dari direktori rekaman")
    if not target.is_file():
        return False
    target.unlink()
    return True


def delete_recordings(directory: Path, filenames: List[str]) -> Tuple[int, List[str]]:
    """Hapus sejumlah rekaman sekaligus (multi-select).

    Kembalikan (jumlah_terhapus, daftar_gagal). Validasi path dilakukan untuk
    setiap file agar satu nama file yang tidak valid tidak membiarkan file lain
    terlewat begitu saja.
    """
    deleted = 0
    failed: List[str] = []
    for fn in filenames:
        if not fn or not isinstance(fn, str):
            continue
        try:
            if delete_recording(directory, fn):
                deleted += 1
            else:
                failed.append(fn)
        except (ValueError, OSError):
            failed.append(fn)
    return deleted, failed


def delete_all_recordings(directory: Path) -> int:
    """Hapus seluruh file .wav di dalam `directory`. Kembalikan jumlah yang dihapus."""
    if not directory.exists():
        return 0
    deleted = 0
    for path in directory.glob("*.wav"):
        try:
            if path.is_file():
                path.unlink()
                deleted += 1
        except OSError:
            pass
    return deleted

