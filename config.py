"""Konfigurasi aplikasi, path data, dan pemuatan/penyimpanan config.json.

Semua path dapat dioverride lewat environment variable supaya build Docker
bisa memetakan volume tanpa mengubah kode.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Optional[Path] = None) -> None:
    """Muat `.env` ke `os.environ` tanpa menghapus karakter khusus.

    Secara historis `run.bat` memuat `.env` lewat batch `for /f` dengan
    `setlocal enabledelayedexpansion`. CMD memperlakukan `!` sebagai penanda
    ekspansi tertunda dan memotongnya, sehingga `ADMIN_PASSWORD=Admin123456!`
    tiba di server sebagai `Admin123456` dan login admin gagal dengan
    "kredensial tidak sah". Karena itu `run.bat` tidak lagi mem-parse `.env`;
    pembacaan dilakukan sepenuhnya di sini, di mana tidak ada perlakuan khusus
    tersebut dan nilai dibaca apa adanya.

    Variabel yang sudah ada di environment tetap dibiarkan, supaya pengaturan
    dari docker-compose, CI, skrip pengujian keamanan, atau shell operator yang
    sah tetap menang.
    """
    env_file = (path if path is not None else BASE_DIR / ".env").resolve()
    if not env_file.is_file():
        return
    try:
        text = env_file.read_text(encoding="utf-8-sig")
    except OSError:
        return

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # Buang quoting pasangan bila pengguna memakainya; jangan sentuh isinya.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        # Jangan timpa variabel yang sudah diset di luar berkas ini.
        if os.environ.get(key) in (None, ""):
            os.environ[key] = value


_load_dotenv()



def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default


DATA_DIR = _env_path("SA_DATA_DIR", BASE_DIR / "data")
RECORDINGS_DIR = _env_path("SA_RECORDINGS_DIR", BASE_DIR / "recordings")
REPORTS_DIR = _env_path("SA_REPORTS_DIR", BASE_DIR / "reports")
LOGS_DIR = _env_path("SA_LOGS_DIR", BASE_DIR / "logs")
STATIC_DIR = BASE_DIR / "static"

DB_PATH = DATA_DIR / "monitoring.db"
USERS_DB_PATH = DATA_DIR / "users.db"
CONFIG_PATH = DATA_DIR / "config.json"

for _d in (DATA_DIR, RECORDINGS_DIR, REPORTS_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


DEFAULT_SWEEP: Dict[str, Any] = {
    "preset": "vhf_airband",
    "center_hz": 127_500_000.0,
    "span_hz": 19_000_000.0,
    "rbw_hz": 30_000.0,
    "vbw_hz": 30_000.0,
    "ref_level_dbm": -20.0,
    "atten_db": 10,
    "gain_db": 0,
    "preamp": False,
    "detector": "average",
    "scale": "log",
    "image_reject": True,
}

DEFAULT_AUDIO: Dict[str, Any] = {
    "demod": "FM",
    "if_bw_hz": 15_000.0,
    "lpf_hz": 8_000.0,
    "hpf_hz": 300.0,
    "deemphasis_us": 75.0,
    "dwell_sec": 12.0,
    "hang_time_sec": 2.0,
    "max_recording_sec": 30.0,
}

DEFAULT_OCCUPANCY: Dict[str, Any] = {
    "threshold_mode": "adaptive",  # "adaptive" | "fixed"
    "fixed_threshold_dbm": -65.0,
    "adaptive_offset_db": 20.0,    # noise floor + 20 dB -> tidak trigger oleh riak noise
    "channel_bw_hz": 12_500.0,
    "trigger_cycles": 3,           # butuh 3 sapuan berturut-turut sebelum dianggap terisi
    "auto_record": False,          # rekam HANYA saat operator menyalakannya
}

DEFAULT_SECURITY: Dict[str, Any] = {
    "require_auth": True,
    "token_ttl_minutes": 720,
    "login_rate_limit": 5,
    "login_rate_window_sec": 60,
    "public_read": False,
}

DEFAULT_DEVICE: Dict[str, Any] = {
    "serial": None,      # None = auto-detect perangkat pertama
    "simulate": False,   # True = paksa mode simulator
}

DEFAULTS: Dict[str, Any] = {
    "device": DEFAULT_DEVICE,
    "sweep": DEFAULT_SWEEP,
    "audio": DEFAULT_AUDIO,
    "occupancy": DEFAULT_OCCUPANCY,
    "security": DEFAULT_SECURITY,
}


# --------------------------------------------------------------------------- #
# Override dari environment variable
#
# Variabel lingkungan adalah setelan tingkat-deployment (dari .env /
# docker-compose) sehingga diterapkan DI ATAS config.json yang tersimpan:
# mengubah SA_SIMULATE di .env harus langsung berlaku, bukan diam-diam
# dikalahkan oleh file config lama. Hanya variabel yang benar-benar diisi
# yang menimpa, jadi config.json tetap otoritatif untuk nilai lain.
# --------------------------------------------------------------------------- #
def _env_str(name: str) -> Optional[str]:
    raw = os.environ.get(name)
    return raw.strip() if raw and raw.strip() else None


def _env_bool(name: str) -> Optional[bool]:
    raw = _env_str(name)
    if raw is None:
        return None
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_float(name: str) -> Optional[float]:
    raw = _env_str(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _env_int(name: str) -> Optional[int]:
    value = _env_float(name)
    return int(value) if value is not None else None


def env_overrides() -> Dict[str, Dict[str, Any]]:
    """Kumpulkan override dari variabel lingkungan SA_* / SECRET_KEY terkait."""
    out: Dict[str, Dict[str, Any]] = {"device": {}, "security": {},
                                      "occupancy": {}, "audio": {}}

    simulate = _env_bool("SA_SIMULATE")
    if simulate is not None:
        out["device"]["simulate"] = simulate

    serial = _env_str("SA_DEVICE_SERIAL")
    if serial is not None:
        try:
            out["device"]["serial"] = int(serial)
        except ValueError:
            out["device"]["serial"] = serial

    ttl = _env_int("SA_TOKEN_TTL_MINUTES")
    if ttl is not None:
        out["security"]["token_ttl_minutes"] = ttl

    rate_limit = _env_int("SA_LOGIN_RATE_LIMIT")
    if rate_limit is not None:
        out["security"]["login_rate_limit"] = rate_limit

    rate_window = _env_float("SA_LOGIN_RATE_WINDOW_SEC")
    if rate_window is not None:
        out["security"]["login_rate_window_sec"] = rate_window

    threshold_mode = _env_str("SA_THRESHOLD_MODE")
    if threshold_mode in ("adaptive", "fixed"):
        out["occupancy"]["threshold_mode"] = threshold_mode

    for env_name, section, key in (
        ("SA_FIXED_THRESHOLD_DBM", "occupancy", "fixed_threshold_dbm"),
        ("SA_ADAPTIVE_OFFSET_DB", "occupancy", "adaptive_offset_db"),
        ("SA_DWELL_SEC", "audio", "dwell_sec"),
        ("SA_MAX_RECORDING_SEC", "audio", "max_recording_sec"),
        ("SA_HANG_TIME_SEC", "audio", "hang_time_sec"),
    ):
        value = _env_float(env_name)
        if value is not None:
            out[section][key] = value

    return out


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class ConfigStore:
    """Config JSON yang aman diakses dari thread HTTP maupun thread capture."""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {}
        self.load()

    def load(self) -> Dict[str, Any]:
        with self._lock:
            disk: Dict[str, Any] = {}
            if self._path.exists():
                try:
                    disk = json.loads(self._path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    disk = {}
            merged = _deep_merge(DEFAULTS, disk)
            # Setelan tingkat-deployment menang atas file yang tersimpan.
            for section, values in env_overrides().items():
                if values:
                    merged[section] = {**merged.get(section, {}), **values}
            self._data = merged
            self.save()
            return dict(self._data)

    def save(self) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self._path)

    def get(self, section: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data.get(section, {}))

    def all(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def update(self, section: str, values: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            current = self._data.setdefault(section, {})
            current.update(values or {})
            self.save()
            return dict(current)


CONFIG = ConfigStore()
