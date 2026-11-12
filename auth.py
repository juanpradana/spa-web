"""Autentikasi (JWT HS256 + PBKDF2) dan otorisasi berbasis peran (RBAC).

JWT diimplementasikan langsung dengan hmac/hashlib sehingga tidak butuh
dependensi pihak ketiga tambahan. Hanya HS256 yang diterima saat verifikasi
(algorithm confusion dicegah secara struktural).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

from config import CONFIG, USERS_DB_PATH, DATA_DIR

PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
KEY_BYTES = 32
JWT_ALG = "HS256"

ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER)

# Hierarki izin: peran yang lebih tinggi mewarisi izin peran di bawahnya.
ROLE_RANK = {ROLE_VIEWER: 1, ROLE_OPERATOR: 2, ROLE_ADMIN: 3}


# --------------------------------------------------------------------------- #
# Secret key
# --------------------------------------------------------------------------- #
def _load_or_create_secret() -> bytes:
    env = os.environ.get("SECRET_KEY")
    if env:
        return env.encode("utf-8")
    path = DATA_DIR / "secret.key"
    if path.exists():
        data = path.read_bytes().strip()
        if data:
            return data
    secret = base64.urlsafe_b64encode(secrets.token_bytes(48))
    path.write_bytes(secret)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret


SECRET_KEY = _load_or_create_secret()


# --------------------------------------------------------------------------- #
# Password hashing (PBKDF2-HMAC-SHA256)
# --------------------------------------------------------------------------- #
def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    if not password or len(password) < 8:
        raise ValueError("password minimal 8 karakter")
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, KEY_BYTES)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iter_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, len(expected))
    return hmac.compare_digest(dk, expected)


# --------------------------------------------------------------------------- #
# JWT HS256
# --------------------------------------------------------------------------- #
def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def create_token(payload: Dict[str, Any], ttl_minutes: Optional[int] = None) -> str:
    if ttl_minutes is None:
        ttl_minutes = int(CONFIG.get("security").get("token_ttl_minutes", 720))
    now = int(time.time())
    body = dict(payload)
    body.update({"iat": now, "nbf": now, "exp": now + ttl_minutes * 60, "jti": secrets.token_hex(8)})
    header = {"alg": JWT_ALG, "typ": "JWT"}
    signing_input = ".".join(
        _b64u_encode(json.dumps(part, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        for part in (header, body)
    )
    sig = hmac.new(SECRET_KEY, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64u_encode(sig)}"


class TokenError(Exception):
    pass


def decode_token(token: str) -> Dict[str, Any]:
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError as exc:
        raise TokenError("format token tidak valid") from exc

    signing_input = f"{header_b64}.{payload_b64}"
    expected = hmac.new(SECRET_KEY, signing_input.encode("ascii"), hashlib.sha256).digest()

    try:
        provided = _b64u_decode(sig_b64)
    except (ValueError, base64.binascii.Error) as exc:
        raise TokenError("signature tidak dapat didekode") from exc

    if not hmac.compare_digest(expected, provided):
        raise TokenError("signature tidak cocok")

    try:
        header = json.loads(_b64u_decode(header_b64))
        payload = json.loads(_b64u_decode(payload_b64))
    except (ValueError, base64.binascii.Error) as exc:
        raise TokenError("payload tidak dapat didekode") from exc

    # Tolak alg apapun selain HS256 ("none" / alg confusion).
    if header.get("alg") != JWT_ALG:
        raise TokenError("algoritma token tidak didukung")

    now = int(time.time())
    if int(payload.get("exp", 0)) < now:
        raise TokenError("token sudah kedaluwarsa")
    if int(payload.get("nbf", 0)) > now + 5:
        raise TokenError("token belum berlaku")
    return payload


# --------------------------------------------------------------------------- #
# Penyimpanan pengguna (SQLite)
# --------------------------------------------------------------------------- #
@dataclass
class User:
    id: int
    username: str
    role: str
    created_at: float
    last_login: Optional[float]
    must_change_password: bool
    disabled: bool


class UserStore:
    def __init__(self, path: Path = USERS_DB_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._session() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_login REAL,
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    disabled INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)"
            )

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            username=row["username"],
            role=row["role"],
            created_at=row["created_at"],
            last_login=row["last_login"],
            must_change_password=bool(row["must_change_password"]),
            disabled=bool(row["disabled"]),
        )

    def count(self) -> int:
        with self._lock, self._session() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def get(self, username: str) -> Optional[User]:
        with self._lock, self._session() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        return self._row_to_user(row) if row else None

    def get_password_hash(self, username: str) -> Optional[str]:
        with self._lock, self._session() as conn:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
        return row["password_hash"] if row else None

    def list(self) -> List[User]:
        with self._lock, self._session() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [self._row_to_user(r) for r in rows]

    def create(
        self,
        username: str,
        password: str,
        role: str,
        must_change_password: bool = False,
    ) -> User:
        username = (username or "").strip()
        if not username or len(username) > 64 or not all(c.isalnum() or c in "-_." for c in username):
            raise ValueError("username hanya boleh huruf, angka, titik, dash, underscore (maks 64)")
        if role not in ROLES:
            raise ValueError(f"role harus salah satu dari {ROLES}")
        pw_hash = hash_password(password)
        with self._lock, self._session() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO users (username, password_hash, role, created_at, must_change_password)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (username, pw_hash, role, time.time(), int(must_change_password)),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("username sudah dipakai") from exc
            uid = int(cur.lastrowid)
        return self.get_by_id(uid)  # type: ignore[return-value]

    def get_by_id(self, user_id: int) -> Optional[User]:
        with self._lock, self._session() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def set_password(self, username: str, new_password: str, must_change: bool = False) -> None:
        pw_hash = hash_password(new_password)
        with self._lock, self._session() as conn:
            cur = conn.execute(
                "UPDATE users SET password_hash = ?, must_change_password = ? WHERE username = ?",
                (pw_hash, int(must_change), username),
            )
            if cur.rowcount == 0:
                raise ValueError("pengguna tidak ditemukan")

    def set_role(self, username: str, role: str) -> None:
        if role not in ROLES:
            raise ValueError(f"role harus salah satu dari {ROLES}")
        with self._lock, self._session() as conn:
            cur = conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
            if cur.rowcount == 0:
                raise ValueError("pengguna tidak ditemukan")

    def set_disabled(self, username: str, disabled: bool) -> None:
        with self._lock, self._session() as conn:
            cur = conn.execute(
                "UPDATE users SET disabled = ? WHERE username = ?", (int(disabled), username)
            )
            if cur.rowcount == 0:
                raise ValueError("pengguna tidak ditemukan")

    def delete(self, username: str) -> None:
        with self._lock, self._session() as conn:
            cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
            if cur.rowcount == 0:
                raise ValueError("pengguna tidak ditemukan")

    def touch_login(self, username: str) -> None:
        with self._lock, self._session() as conn:
            conn.execute("UPDATE users SET last_login = ? WHERE username = ?", (time.time(), username))

    def admins(self) -> List[User]:
        return [u for u in self.list() if u.role == ROLE_ADMIN and not u.disabled]


USERS = UserStore()


def bootstrap_admin() -> Optional[str]:
    """Buat admin awal jika belum ada pengguna. Return password awal (sekali saja)."""
    if USERS.count() > 0:
        return None
    env_pw = os.environ.get("ADMIN_PASSWORD")
    password = env_pw or secrets.token_urlsafe(12)
    USERS.create("admin", password, ROLE_ADMIN, must_change_password=not env_pw)
    return password


def _password_matches(username: str, password: str) -> bool:
    """True bila `password` sudah sama dengan hash yang tersimpan (aman untuk PBKDF2)."""
    stored = USERS.get_password_hash(username)
    if not stored:
        return False
    return verify_password(password, stored)


def sync_admin_password() -> Optional[str]:
    """Samakan password admin di DB dengan `ADMIN_PASSWORD` dari environment.

    Dipanggil setiap startup. Tanpa ini, mengubah `ADMIN_PASSWORD` di `.env`
    setelah database pengguna pernah dibuat tidak berpengaruh apa-apa — hash
    lama tetap dipakai, sehingga login gagal meski `.env` sudah diperbarui.

    Sengaja idempoten dan tanpa efek samping lain: hash hanya ditulis ulang
    ketika nilai di environment benar-benar berbeda dari yang tersimpan, jadi
    tidak ada penulisan DB berulang dan password dinamis oleh admin tetap utuh
    selama variabel environment tidak disentuh.
    """
    env_pw = os.environ.get("ADMIN_PASSWORD")
    if not env_pw:
        return None
    user = USERS.get("admin")
    if user is None:
        # Tidak ada admin sama sekali: bootstrap biasa yang menanganinya.
        return None
    if _password_matches("admin", env_pw):
        return None
    try:
        USERS.set_password("admin", env_pw, must_change=False)
    except ValueError:
        return None
    return env_pw


# --------------------------------------------------------------------------- #
# Dependensi FastAPI
# --------------------------------------------------------------------------- #
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login", auto_error=False)


def _extract_token(request: Request, bearer: Optional[str]) -> Optional[str]:
    if bearer:
        return bearer
    # WebSocket & EventSource tidak selalu bisa mengirim header.
    qp = request.query_params.get("token")
    if qp:
        return qp
    return request.headers.get("x-auth-token")


async def get_current_user(
    request: Request,
    bearer: Optional[str] = Depends(oauth2_scheme),
) -> User:
    token = _extract_token(request, bearer)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token autentikasi tidak ditemukan",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(token)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = USERS.get(payload.get("sub", ""))
    if user is None or user.disabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Pengguna tidak valid")
    # Token lama harus dibatalkan setelah ganti password.
    pwd_stamp = payload.get("pwd")
    if pwd_stamp is not None and pwd_stamp != _password_stamp(user.username):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token sudah tidak berlaku"
        )
    return user


def _password_stamp(username: str) -> str:
    stored = USERS.get_password_hash(username) or ""
    return hashlib.sha256(stored.encode("utf-8")).hexdigest()[:16]


def password_stamp(username: str) -> str:
    return _password_stamp(username)


def require_role(minimum: str):
    """Dependency factory: butuh peran minimal tertentu."""
    min_rank = ROLE_RANK[minimum]

    async def _guard(user: User = Depends(get_current_user)) -> User:
        if ROLE_RANK.get(user.role, 0) < min_rank:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Butuh hak akses minimal '{minimum}'",
            )
        return user

    return _guard


require_viewer = require_role(ROLE_VIEWER)
require_operator = require_role(ROLE_OPERATOR)
require_admin = require_role(ROLE_ADMIN)


def authenticate_ws_token(token: Optional[str]) -> Optional[User]:
    """Validasi token untuk handshake WebSocket (tanpa HTTPException)."""
    if not token:
        return None
    try:
        payload = decode_token(token)
    except TokenError:
        return None
    user = USERS.get(payload.get("sub", ""))
    if user is None or user.disabled:
        return None
    # Sama seperti `get_current_user`: token lama harus gugur setelah ganti
    # password. Tanpa cek ini, koneksi WebSocket tetap hidup sampai `exp`,
    # sehingga reset kata sandi oleh admin tidak benar-benar memutus akses.
    pwd_stamp = payload.get("pwd")
    if pwd_stamp is not None and pwd_stamp != _password_stamp(user.username):
        return None
    return user
