"""Server FastAPI: REST API + WebSocket streaming untuk Handfish.

Tanggung jawab:
  - Autentikasi JWT HS256 + otorisasi RBAC (admin / operator / viewer).
  - Rate limiting pada login, security headers, audit log.
  - Kontrol perangkat RF (preset, span/center, demod audio, auto-record).
  - Statistik okupansi kanal (ITU-R SM.1880) dan ekspor Excel/CSV.
  - Manajemen rekaman WAV dengan proteksi path traversal.
  - Penjadwalan kampanye survei okupansi.
  - Stream biner berlatensi rendah: spektrum float32 (/ws/spectrum),
    audio PCM float32 (/ws/audio), dan event JSON (/ws/events).

Jalankan: `python server.py` atau `uvicorn server:app --host 0.0.0.0 --port 8000`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from auth import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    USERS,
    User,
    authenticate_ws_token,
    bootstrap_admin,
    get_current_user,
    require_admin,
    require_operator,
    require_viewer,
    sync_admin_password,
    verify_password,
)
from channels import PRESETS, categories, preset_list
from config import CONFIG, RECORDINGS_DIR, STATIC_DIR
from engine import ENGINE
from occupancy import OCC
from recorder import (
    delete_all_recordings,
    delete_recording,
    delete_recordings,
    scan_recordings,
)
from reports import export_csv, export_excel
from sa124 import list_serial_numbers, load_library
from scheduler import SCHEDULES, SCHEDULER, Schedule, parse_local
from security import (
    AUDIT,
    RATE_LIMITER,
    SecurityHeadersMiddleware,
    UnsafePathError,
    client_ip,
    safe_file_path,
    too_many_requests,
)

logging.basicConfig(
    level=os.environ.get("SA_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("handfish")

ENABLE_DOCS = os.environ.get("SA_ENABLE_DOCS", "").lower() in {"1", "true", "yes"}


def _user_dict(user: User) -> Dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "disabled": user.disabled,
        "must_change_password": user.must_change_password,
        "last_login": user.last_login,
    }


# --------------------------------------------------------------------------- #
# Hub WebSocket
# --------------------------------------------------------------------------- #
class WebSocketHub:
    """Himpunan koneksi aktif per jenis stream.

    Pengiriman dilakukan dari task `pump_*` di event loop yang sama, jadi
    himpunan ini tidak perlu lock async; lock hanya melindungi mutasi dari
    handler koneksi.
    """

    def __init__(self) -> None:
        self.spectrum: Set[WebSocket] = set()
        self.audio: Set[WebSocket] = set()
        self.events: Set[WebSocket] = set()
        self._lock = threading.Lock()

    def _set(self, kind: str) -> Set[WebSocket]:
        return {"spectrum": self.spectrum, "audio": self.audio, "events": self.events}[kind]

    def add(self, kind: str, ws: WebSocket) -> None:
        with self._lock:
            self._set(kind).add(ws)

    def remove(self, kind: str, ws: WebSocket) -> None:
        with self._lock:
            self._set(kind).discard(ws)

    async def send_bytes(self, kind: str, data: bytes) -> None:
        with self._lock:
            targets = list(self._set(kind))
        if not targets:
            return
        dead: List[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.append(ws)
        if dead:
            with self._lock:
                for ws in dead:
                    self._set(kind).discard(ws)

    async def send_json(self, kind: str, obj: Any) -> None:
        with self._lock:
            targets = list(self._set(kind))
        if not targets:
            return
        payload = json.dumps(obj, ensure_ascii=False)
        dead: List[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        if dead:
            with self._lock:
                for ws in dead:
                    self._set(kind).discard(ws)


HUB = WebSocketHub()


# --------------------------------------------------------------------------- #
# Model request
# --------------------------------------------------------------------------- #
class LoginReq(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class ChangePasswordReq(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=8, max_length=256)


class DeviceConfigReq(BaseModel):
    """Pilihan sumber sinyal: instrumen fisik atau simulator.

    Keduanya opsional — field yang tidak dikirim berarti "pertahankan nilai
    yang sekarang tersimpan".
    """
    simulate: Optional[bool] = None
    serial: Optional[int] = Field(default=None, ge=1, le=2_147_483_647)


class CreateUserReq(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(..., min_length=8, max_length=256)
    role: str = Field(ROLE_VIEWER)


class UpdateUserReq(BaseModel):
    password: Optional[str] = Field(None, min_length=8, max_length=256)
    role: Optional[str] = None
    disabled: Optional[bool] = None


class PresetReq(BaseModel):
    preset_key: str = Field(..., max_length=64)


class SweepReq(BaseModel):
    center_hz: Optional[float] = Field(None, ge=100_000.0, le=12_400_000_000.0)
    span_hz: Optional[float] = Field(None, gt=0.0, le=12_400_000_000.0)
    rbw_hz: Optional[float] = Field(None, gt=0.0, le=10_000_000.0)
    ref_level_dbm: Optional[float] = Field(None, ge=-150.0, le=20.0)
    atten_db: Optional[int] = Field(None, ge=0, le=60)


class ListenReq(BaseModel):
    freq_hz: float = Field(..., ge=100_000.0, le=12_400_000_000.0)
    demod: str = Field("FM", max_length=8)


class AutoRecordReq(BaseModel):
    enabled: bool


class ManualRecordReq(BaseModel):
    """Mulai rekam audio manual. Frekuensi opsional: bila kosong, memakai
    frekuensi sesi dengar yang sedang aktif."""
    freq_hz: Optional[float] = Field(None, ge=100_000.0, le=12_400_000_000.0)
    demod: Optional[str] = Field(None, max_length=8)


class OccupancyTrackingReq(BaseModel):
    enabled: bool


class OccupancyConfigReq(BaseModel):
    threshold_mode: Optional[str] = None          # "adaptive" | "fixed"
    fixed_threshold_dbm: Optional[float] = Field(None, ge=-150.0, le=20.0)
    adaptive_offset_db: Optional[float] = Field(None, ge=0.0, le=60.0)
    dwell_sec: Optional[float] = Field(None, gt=0.0, le=600.0)
    max_recording_sec: Optional[float] = Field(None, gt=0.0, le=600.0)
    hang_time_sec: Optional[float] = Field(None, ge=0.0, le=60.0)


class ScheduleReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    preset: str = Field(..., max_length=64)
    start_at: str = Field(..., max_length=32)
    duration_min: float = Field(60.0, gt=0.0, le=10_080.0)
    repeat: str = Field("none")
    enabled: bool = True
    auto_record: bool = True
    threshold_mode: str = Field("adaptive")
    fixed_threshold_dbm: float = Field(-85.0, ge=-150.0, le=20.0)
    adaptive_offset_db: float = Field(10.0, ge=0.0, le=60.0)
    notes: str = Field("", max_length=500)


class OccupancyDeleteReq(BaseModel):
    keys: List[str] = Field(..., min_length=1, max_length=5000)


class BulkDeleteRecordingsReq(BaseModel):
    filenames: List[str] = Field(..., min_length=1, max_length=5000)


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()

    initial_password = bootstrap_admin()
    if initial_password:
        banner = (
            "\n" + "=" * 68 + "\n"
            "  AKUN ADMIN AWAL DIBUAT — ganti kata sandi setelah login pertama\n"
            f"  username: admin\n  password: {initial_password}\n" + "=" * 68 + "\n"
        )
        print(banner, flush=True)
        logger.warning("Akun admin awal dibuat; kata sandi dicetak di atas.")
    else:
        # .env berubah setelah DB pengguna pernah dibuat: samakan hash-nya
        # supaya login tidak gagal dengan "kredensial tidak sah".
        synced = sync_admin_password()
        if synced:
            print(
                "\n" + "=" * 68 + "\n"
                "  Kata sandi admin disamakan dengan ADMIN_PASSWORD dari .env\n"
                + "=" * 68 + "\n",
                flush=True,
            )
            logger.warning("Password admin disinkronkan dari environment.")

    ENGINE.set_event_loop(loop)
    info = ENGINE.initialize_device()
    logger.info(
        "Perangkat: %s | serial=%s | simulasi=%s",
        info.model, info.serial, info.simulated,
    )

    # Antrean jembatan dari thread capture (hardware) ke event loop asyncio.
    spectrum_q: asyncio.Queue = asyncio.Queue(maxsize=120)
    audio_q: asyncio.Queue = asyncio.Queue(maxsize=120)
    events_q: asyncio.Queue = asyncio.Queue(maxsize=120)

    ENGINE.subscribe_spectrum(spectrum_q)
    ENGINE.subscribe_audio(audio_q)
    ENGINE.subscribe_events(events_q)

    async def pump(queue: asyncio.Queue, kind: str, as_json: bool) -> None:
        while True:
            item = await queue.get()
            try:
                if as_json:
                    await HUB.send_json(kind, item)
                else:
                    await HUB.send_bytes(kind, item)
            except Exception as exc:  # jangan biarkan satu kegagalan mematikan pump
                logger.debug("pump %s: %s", kind, exc)

    tasks = [
        loop.create_task(pump(spectrum_q, "spectrum", False)),
        loop.create_task(pump(audio_q, "audio", False)),
        loop.create_task(pump(events_q, "events", True)),
    ]

    SCHEDULER.start(loop)

    preset_key = CONFIG.get("sweep").get("preset", "vhf_airband")
    if preset_key not in PRESETS:
        preset_key = next(iter(PRESETS))
    ENGINE.start(preset_key)
    logger.info("CaptureEngine berjalan (preset awal: %s)", preset_key)

    try:
        yield
    finally:
        logger.info("Menghentikan scheduler & capture engine...")
        await SCHEDULER.stop()
        ENGINE.stop()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        try:
            OCC.flush()
        except Exception:
            pass


app = FastAPI(
    title="Handfish — SA124B Spectrum Occupancy & Audio Automation",
    description="Pemantauan okupansi spektrum 100 kHz–12.4 GHz dan perekaman audio otomatis.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs" if ENABLE_DOCS else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if ENABLE_DOCS else None,
)

app.add_middleware(SecurityHeadersMiddleware)


# --------------------------------------------------------------------------- #
# Autentikasi & manajemen pengguna
# --------------------------------------------------------------------------- #
@app.post("/api/auth/login")
async def login(req: LoginReq, request: Request):
    ip = client_ip(request)
    cfg_sec = CONFIG.get("security")
    limit = int(cfg_sec.get("login_rate_limit", 5))
    window = float(cfg_sec.get("login_rate_window_sec", 60))

    # `check()` mencatat satu percobaan; percobaan gagal ikut terhitung.
    allowed, retry_after = RATE_LIMITER.check(f"login:{ip}", limit, window)
    if not allowed:
        AUDIT.write(req.username, "login_rate_limited", {"ip": ip}, ip=ip)
        return too_many_requests(retry_after)

    user = USERS.get(req.username)
    stored = USERS.get_password_hash(req.username) if user else None
    if user is None or stored is None or not verify_password(req.password, stored):
        AUDIT.write(req.username, "login_failed", {"ip": ip}, ip=ip)
        # Pesan seragam: jangan bocorkan apakah username ada.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nama pengguna atau kata sandi tidak valid.",
        )
    if user.disabled:
        AUDIT.write(user.username, "login_disabled", {"ip": ip}, ip=ip)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akun ini dinonaktifkan. Hubungi administrator.",
        )

    from auth import create_token, password_stamp

    ttl = int(cfg_sec.get("token_ttl_minutes", 720))
    token = create_token(
        {"sub": user.username, "role": user.role, "pwd": password_stamp(user.username)},
        ttl_minutes=ttl,
    )
    USERS.touch_login(user.username)
    AUDIT.write(user.username, "login_success", {"role": user.role}, ip=ip)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in_minutes": ttl,
        "user": _user_dict(user),
    }


@app.get("/api/auth/me")
async def get_me(user: User = Depends(require_viewer)):
    return _user_dict(user)


@app.post("/api/auth/change-password")
async def change_password(req: ChangePasswordReq, user: User = Depends(require_viewer)):
    stored = USERS.get_password_hash(user.username)
    if not stored or not verify_password(req.old_password, stored):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Kata sandi saat ini salah.",
        )
    USERS.set_password(user.username, req.new_password, must_change=False)
    AUDIT.write(user.username, "change_password")
    # Token lama otomatis gugur karena `pwd` stamp ikut berubah.
    return {"status": "ok", "message": "Kata sandi diperbarui. Silakan login ulang."}


@app.get("/api/auth/users")
async def list_users(_: User = Depends(require_admin)):
    return [_user_dict(u) for u in USERS.list()]


@app.post("/api/auth/users")
async def create_user(req: CreateUserReq, admin: User = Depends(require_admin)):
    if req.role not in (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER):
        raise HTTPException(status_code=400, detail=f"Role tidak sah: {req.role}")
    if USERS.get(req.username):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Pengguna sudah ada.")
    try:
        created = USERS.create(req.username, req.password, req.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AUDIT.write(admin.username, "create_user", {"created": req.username, "role": req.role})
    return _user_dict(created)


@app.put("/api/auth/users/{username}")
async def update_user(username: str, req: UpdateUserReq, admin: User = Depends(require_admin)):
    target = USERS.get(username)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pengguna tidak ditemukan.")
    if req.role is not None:
        if req.role not in (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER):
            raise HTTPException(status_code=400, detail=f"Role tidak sah: {req.role}")
        # Cegah lockout: jangan turunkan admin aktif terakhir.
        if target.role == ROLE_ADMIN and req.role != ROLE_ADMIN and len(USERS.admins()) <= 1:
            raise HTTPException(status_code=400, detail="Tidak dapat menurunkan admin terakhir.")
        USERS.set_role(username, req.role)
    if req.password is not None:
        USERS.set_password(username, req.password, must_change=False)
    if req.disabled is not None:
        if req.disabled and target.role == ROLE_ADMIN and len(USERS.admins()) <= 1:
            raise HTTPException(status_code=400, detail="Tidak dapat menonaktifkan admin terakhir.")
        if req.disabled and username == admin.username:
            raise HTTPException(status_code=400, detail="Tidak dapat menonaktifkan akun Anda sendiri.")
        USERS.set_disabled(username, req.disabled)
    AUDIT.write(admin.username, "update_user", {"target": username,
                                                "role": req.role,
                                                "password_changed": req.password is not None,
                                                "disabled": req.disabled})
    return _user_dict(USERS.get(username))  # type: ignore[arg-type]


@app.delete("/api/auth/users/{username}")
async def delete_user(username: str, admin: User = Depends(require_admin)):
    target = USERS.get(username)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pengguna tidak ditemukan.")
    if username == admin.username:
        raise HTTPException(status_code=400, detail="Tidak dapat menghapus akun Anda sendiri.")
    if target.role == ROLE_ADMIN and len(USERS.admins()) <= 1:
        raise HTTPException(status_code=400, detail="Tidak dapat menghapus admin terakhir.")
    USERS.delete(username)
    AUDIT.write(admin.username, "delete_user", {"deleted": username})
    return {"status": "ok", "deleted": username}


# --------------------------------------------------------------------------- #
# Kontrol RF
# --------------------------------------------------------------------------- #
@app.get("/api/rf/status")
async def rf_status(_: User = Depends(require_viewer)):
    st = ENGINE.status()
    st["scheduler"] = SCHEDULER.status()
    st["ws_clients"] = {
        "spectrum": len(HUB.spectrum),
        "audio": len(HUB.audio),
        "events": len(HUB.events),
    }
    return st


@app.get("/api/rf/presets")
async def rf_presets(_: User = Depends(require_viewer)):
    return {"presets": preset_list(), "categories": categories()}


@app.post("/api/rf/preset")
async def rf_set_preset(req: PresetReq, user: User = Depends(require_operator)):
    if req.preset_key not in PRESETS:
        raise HTTPException(status_code=404, detail=f"Preset tidak dikenal: {req.preset_key}")
    ENGINE.apply_preset(req.preset_key)
    CONFIG.update("sweep", {"preset": req.preset_key})
    AUDIT.write(user.username, "set_preset", {"preset": req.preset_key})
    return {"status": "ok", "preset": req.preset_key}


@app.post("/api/rf/sweep")
async def rf_set_sweep(req: SweepReq, user: User = Depends(require_operator)):
    ENGINE.set_sweep_params(
        center_hz=req.center_hz,
        span_hz=req.span_hz,
        rbw_hz=req.rbw_hz,
        ref_level_dbm=req.ref_level_dbm,
        atten_db=req.atten_db,
    )
    AUDIT.write(user.username, "set_sweep_params", req.model_dump(exclude_none=True))
    cfg = CONFIG.get("sweep")
    return {"status": "ok", "sweep": cfg}


@app.post("/api/rf/listen")
async def rf_listen(req: ListenReq, user: User = Depends(require_operator)):
    ENGINE.start_manual_audio(freq_hz=req.freq_hz, demod=req.demod.upper())
    AUDIT.write(user.username, "listen", {"freq_hz": req.freq_hz, "demod": req.demod})
    return {"status": "ok", "freq_hz": req.freq_hz, "demod": req.demod.upper()}


@app.post("/api/rf/listen/stop")
async def rf_listen_stop(user: User = Depends(require_operator)):
    ENGINE.stop_manual_audio()
    AUDIT.write(user.username, "listen_stop")
    return {"status": "ok"}


@app.post("/api/rf/autorecord")
async def rf_autorecord(req: AutoRecordReq, user: User = Depends(require_operator)):
    ENGINE.set_auto_record(req.enabled)
    AUDIT.write(user.username, "set_auto_record", {"enabled": req.enabled})
    return {"status": "ok", "auto_record": req.enabled}


@app.post("/api/rf/record/start")
async def rf_record_start(req: ManualRecordReq, user: User = Depends(require_operator)):
    """Mulai rekam audio manual ke WAV.

    Tidak ada auto-record di sini: berkas hanya ditulis setelah operator
    menekan tombol rekam.
    """
    res = ENGINE.start_manual_record(
        freq_hz=req.freq_hz,
        demod=req.demod.upper() if req.demod else None,
    )
    AUDIT.write(user.username, "manual_record_start", req.model_dump(exclude_none=True))
    return res


@app.post("/api/rf/record/stop")
async def rf_record_stop(user: User = Depends(require_operator)):
    res = ENGINE.stop_manual_record()
    AUDIT.write(user.username, "manual_record_stop", {"status": res.get("status")})
    return res


@app.post("/api/rf/occupancy/tracking")
async def rf_occupancy_tracking(req: OccupancyTrackingReq, user: User = Depends(require_operator)):
    """Jeda / lanjutkan akumulasi statistik okupansi (sapuan tetap jalan)."""
    ENGINE.set_occupancy_tracking(req.enabled)
    AUDIT.write(user.username, "set_occupancy_tracking", {"enabled": req.enabled})
    return {"status": "ok", "occupancy_tracking": req.enabled}


@app.get("/api/rf/devices")
async def rf_devices(_: User = Depends(require_viewer)):
    """Enumerasi perangkat SA124B yang benar-benar tersambung.

    Dipakai panel Pemilihan Perangkat untuk membedakan tiga keadaan: driver
    tidak terpasang, driver terpasang tapi tidak ada alat di USB, dan driver +
    alat siap. Tanpa ini operator hanya melihat simulator tanpa tahu sebabnya.
    """
    lib_loaded = False
    lib_error: Optional[str] = None
    try:
        lib = load_library()
        lib_loaded = lib is not None
    except Exception as exc:  # pragma: no cover - tergantung instalasi driver
        lib_error = str(exc)

    serials: List[int] = []
    serial_error: Optional[str] = None
    if lib_loaded:
        try:
            serials = [int(s) for s in list_serial_numbers()]
        except Exception as exc:  # pragma: no cover - tergantung USB
            serial_error = str(exc)

    dev = CONFIG.get("device")
    cfg_serial = dev.get("serial")
    current_serial = cfg_serial if isinstance(cfg_serial, int) else None
    info = ENGINE.device_info
    if current_serial is None and info.simulated is False and info.serial:
        try:
            current_serial = int(info.serial)
        except (ValueError, TypeError):
            pass

    return {
        "library_loaded": lib_loaded,
        "library_error": lib_error,
        "serials": serials,
        "serial_error": serial_error,
        "current": {
            "configured_simulate": bool(dev.get("simulate", False)),
            "configured_serial": current_serial,
            "connected": bool(info.connected),
            "simulated": bool(info.simulated),
            "model": info.model,
            "active_serial": info.serial,
            "error": info.error,
        },
    }


@app.post("/api/rf/device")
async def rf_set_device(req: DeviceConfigReq, user: User = Depends(require_admin)):
    """Beralih antara instrumen fisik dan simulator, atau pilih nomor seri.

    Perangkat dibuka ulang dan engine dimulai kembali; riwayat okupansi yang
    sudah tersimpan tidak terpengaruh.
    """
    cfg_dev = CONFIG.get("device")
    simulate = req.simulate if req.simulate is not None else bool(cfg_dev.get("simulate", False))
    serial = req.serial if req.serial is not None else cfg_dev.get("serial")

    if serial is not None and (serial <= 0 or serial > 2_147_483_647):
        raise HTTPException(status_code=400, detail="Nomor seri tidak sah.")

    info = ENGINE.reconnect_device(simulate=bool(simulate), serial=serial)
    AUDIT.write(user.username, "set_device",
                {"simulate": bool(simulate), "serial": serial,
                 "connected": info.connected, "simulated": info.simulated})

    # Pengguna meminta mode fisik tetapi engine jatuh ke simulator: laporkan
    # sebabnya supaya panel Admin bisa menampilkannya.
    warning = None
    if req.simulate is False and info.simulated:
        warning = info.error or "Perangkat fisik tidak terjangkau — beralih ke simulator."

    return {"status": "ok", "device": info.as_dict(), "warning": warning}


@app.get("/api/rf/config")
async def rf_config(_: User = Depends(require_viewer)):
    """Konfigurasi aktif untuk menyelaraskan panel kontrol UI."""
    dev = CONFIG.get("device")
    return {"sweep": CONFIG.get("sweep"),
            "audio": CONFIG.get("audio"),
            "occupancy": CONFIG.get("occupancy"),
            "device": {"simulate": bool(dev.get("simulate", False)),
                       "serial": dev.get("serial")}}


@app.post("/api/rf/occupancy-config")
async def rf_occupancy_config(req: OccupancyConfigReq, user: User = Depends(require_operator)):
    occ_values: Dict[str, Any] = {}
    audio_values: Dict[str, Any] = {}

    if req.threshold_mode is not None:
        if req.threshold_mode not in ("adaptive", "fixed"):
            raise HTTPException(status_code=400, detail="threshold_mode harus adaptive/fixed")
        occ_values["threshold_mode"] = req.threshold_mode
    if req.fixed_threshold_dbm is not None:
        occ_values["fixed_threshold_dbm"] = req.fixed_threshold_dbm
    if req.adaptive_offset_db is not None:
        occ_values["adaptive_offset_db"] = req.adaptive_offset_db
    if req.dwell_sec is not None:
        audio_values["dwell_sec"] = req.dwell_sec
    if req.max_recording_sec is not None:
        audio_values["max_recording_sec"] = req.max_recording_sec
    if req.hang_time_sec is not None:
        audio_values["hang_time_sec"] = req.hang_time_sec

    if occ_values:
        CONFIG.update("occupancy", occ_values)
    if audio_values:
        CONFIG.update("audio", audio_values)

    AUDIT.write(user.username, "set_occupancy_config",
                {**occ_values, **audio_values})
    return {"status": "ok",
            "occupancy": CONFIG.get("occupancy"),
            "audio": CONFIG.get("audio")}


@app.post("/api/rf/start")
async def rf_start(user: User = Depends(require_operator)):
    ENGINE.start()
    AUDIT.write(user.username, "capture_start")
    return {"status": "ok", "state": "running"}


@app.post("/api/rf/stop")
async def rf_stop(user: User = Depends(require_operator)):
    ENGINE.stop()
    AUDIT.write(user.username, "capture_stop")
    return {"status": "ok", "state": "stopped"}


# --------------------------------------------------------------------------- #
# Okupansi
# --------------------------------------------------------------------------- #
@app.get("/api/occupancy/stats")
async def occupancy_stats(_: User = Depends(require_viewer)):
    stats = OCC.live_stats()
    channels = [s.as_dict() for s in stats.values()]
    return {
        "channels": channels,
        "total_channels": len(channels),
        "occupied_channels": sum(1 for c in channels if c["occupancy_pct"] > 0),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


@app.post("/api/occupancy/flush")
async def occupancy_flush(user: User = Depends(require_operator)):
    written = OCC.flush()
    AUDIT.write(user.username, "occupancy_flush", {"channels": written})
    return {"status": "ok", "channels_written": written}


@app.post("/api/occupancy/reset")
async def occupancy_reset(user: User = Depends(require_admin)):
    OCC.reset_all()
    AUDIT.write(user.username, "occupancy_reset")
    return {"status": "ok", "message": "Statistik okupansi direset."}


@app.post("/api/occupancy/delete")
async def occupancy_delete(req: OccupancyDeleteReq, user: User = Depends(require_operator)):
    """Hapus kanal terpilih (multi-select) dari SQLite dan akumulator live."""
    deleted = OCC.delete_channels(req.keys)
    AUDIT.write(user.username, "occupancy_delete", {"count": deleted, "keys": len(req.keys)})
    return {"status": "ok", "deleted": deleted}


# --------------------------------------------------------------------------- #
# Rekaman
# --------------------------------------------------------------------------- #
@app.get("/api/recordings")
async def list_recordings(
    limit: int = Query(500, ge=1, le=5000),
    _: User = Depends(require_viewer),
):
    items = scan_recordings(RECORDINGS_DIR, limit=limit)
    return {"recordings": items, "count": len(items)}


def _resolve_recording(filename: str):
    try:
        return safe_file_path(RECORDINGS_DIR, filename, allowed_suffixes=[".wav"])
    except UnsafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/recordings/{filename}/download")
async def download_recording(filename: str, _: User = Depends(require_viewer)):
    path = _resolve_recording(filename)
    return FileResponse(path, media_type="audio/wav", filename=path.name)


@app.get("/api/recordings/{filename}/stream")
async def stream_recording(filename: str, _: User = Depends(require_viewer)):
    path = _resolve_recording(filename)

    def iterfile():
        with path.open("rb") as fh:
            while chunk := fh.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        iterfile(),
        media_type="audio/wav",
        headers={"Content-Disposition": f'inline; filename="{path.name}"'},
    )


@app.delete("/api/recordings/{filename}")
async def remove_recording(filename: str, user: User = Depends(require_operator)):
    try:
        ok = delete_recording(RECORDINGS_DIR, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="File rekaman tidak ditemukan.")
    AUDIT.write(user.username, "delete_recording", {"filename": filename})
    return {"status": "ok", "deleted": filename}


@app.post("/api/recordings/bulk-delete")
async def remove_recordings(
    req: BulkDeleteRecordingsReq, user: User = Depends(require_operator),
):
    """Hapus beberapa rekaman sekaligus. Nama tidak sah hanya dilewat (tidak 500)."""
    deleted, failed = delete_recordings(RECORDINGS_DIR, req.filenames)
    AUDIT.write(user.username, "delete_recordings_bulk",
                {"deleted": deleted, "failed": len(failed), "requested": len(req.filenames)})
    return {"status": "ok", "deleted": deleted, "failed": failed}


@app.post("/api/recordings/delete-all")
async def remove_all_recordings(user: User = Depends(require_operator)):
    deleted = delete_all_recordings(RECORDINGS_DIR)
    AUDIT.write(user.username, "delete_recordings_all", {"deleted": deleted})
    return {"status": "ok", "deleted": deleted}


# --------------------------------------------------------------------------- #
# Penjadwalan
# --------------------------------------------------------------------------- #
@app.get("/api/schedules")
async def get_schedules(_: User = Depends(require_viewer)):
    return SCHEDULER.status()


@app.post("/api/schedules")
async def create_schedule(req: ScheduleReq, user: User = Depends(require_operator)):
    if req.preset not in PRESETS:
        raise HTTPException(status_code=404, detail=f"Preset tidak dikenal: {req.preset}")
    if req.repeat not in ("none", "daily", "weekly"):
        raise HTTPException(status_code=400, detail="repeat harus none/daily/weekly")
    if req.threshold_mode not in ("adaptive", "fixed"):
        raise HTTPException(status_code=400, detail="threshold_mode harus adaptive/fixed")
    try:
        parse_local(req.start_at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    created = SCHEDULES.create(Schedule(
        id="",
        name=req.name,
        preset=req.preset,
        start_at=req.start_at,
        duration_min=req.duration_min,
        repeat=req.repeat,
        enabled=req.enabled,
        auto_record=req.auto_record,
        threshold_mode=req.threshold_mode,
        fixed_threshold_dbm=req.fixed_threshold_dbm,
        adaptive_offset_db=req.adaptive_offset_db,
        notes=req.notes,
        owner=user.username,
    ))
    AUDIT.write(user.username, "create_schedule", {"schedule_id": created.id, "preset": req.preset})
    return created.as_dict()


@app.post("/api/schedules/{schedule_id}/start-now")
async def start_schedule_now(schedule_id: str, user: User = Depends(require_operator)):
    sched = SCHEDULES.get(schedule_id)
    if sched is None:
        raise HTTPException(status_code=404, detail="Jadwal tidak ditemukan.")
    try:
        run = SCHEDULER.start_now(sched, owner=user.username)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return run.as_dict()


@app.post("/api/schedules/cancel-active")
async def cancel_active(user: User = Depends(require_operator)):
    run = SCHEDULER.cancel(reason=f"Dibatalkan oleh {user.username}")
    if run is None:
        raise HTTPException(status_code=400, detail="Tidak ada survei yang sedang berjalan.")
    return run.as_dict()


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str, user: User = Depends(require_admin)):
    if not SCHEDULES.delete(schedule_id):
        raise HTTPException(status_code=404, detail="Jadwal tidak ditemukan.")
    AUDIT.write(user.username, "delete_schedule", {"schedule_id": schedule_id})
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Ekspor laporan
# --------------------------------------------------------------------------- #
@app.get("/api/reports/excel")
async def report_excel(user: User = Depends(require_viewer)):
    stats = OCC.all_stats() or OCC.live_stats()
    if not stats:
        raise HTTPException(status_code=400, detail="Belum ada data okupansi untuk diekspor.")
    try:
        path = export_excel(stats)
    except RuntimeError as exc:  # openpyxl tidak terpasang
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    AUDIT.write(user.username, "export_excel", {"file": path.name})
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@app.get("/api/reports/csv")
async def report_csv(user: User = Depends(require_viewer)):
    stats = OCC.all_stats() or OCC.live_stats()
    if not stats:
        raise HTTPException(status_code=400, detail="Belum ada data okupansi untuk diekspor.")
    path = export_csv(stats)
    AUDIT.write(user.username, "export_csv", {"file": path.name})
    return FileResponse(path, media_type="text/csv", filename=path.name)


# --------------------------------------------------------------------------- #
# WebSocket (autentikasi via ?token= — browser tidak bisa set header)
# --------------------------------------------------------------------------- #
async def _ws_authenticate(websocket: WebSocket) -> Optional[User]:
    token = websocket.query_params.get("token") or websocket.headers.get("x-auth-token")
    user = authenticate_ws_token(token)
    if user is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="token tidak valid")
        return None
    return user


async def _ws_serve(websocket: WebSocket, kind: str) -> None:
    await websocket.accept()
    user = await _ws_authenticate(websocket)
    if user is None:
        return
    HUB.add(kind, websocket)
    try:
        while True:
            # Klien hanya mengirim ping; data mengalir satu arah dari server.
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        HUB.remove(kind, websocket)


@app.websocket("/ws/spectrum")
async def ws_spectrum(websocket: WebSocket):
    await _ws_serve(websocket, "spectrum")


@app.websocket("/ws/audio")
async def ws_audio(websocket: WebSocket):
    await _ws_serve(websocket, "audio")


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    await _ws_serve(websocket, "events")


# --------------------------------------------------------------------------- #
# Statis
# --------------------------------------------------------------------------- #
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    page = STATIC_DIR / "index.html"
    if page.exists():
        return FileResponse(page)
    return {"message": "Handfish API aktif. Antarmuka statis belum tersedia."}


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("SA_HOST", "127.0.0.1")
    port = int(os.environ.get("SA_PORT", "8000"))
    print(f"[*] Handfish berjalan di http://{host}:{port}")
    uvicorn.run("server:app", host=host, port=port, log_level="info", reload=False)
