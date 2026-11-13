"""Lapisan keamanan: rate limiting, sanitasi path, security headers, audit log."""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Deque, Dict, Iterable, Optional, Tuple

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from config import LOGS_DIR


# --------------------------------------------------------------------------- #
# Rate limiting (sliding window in-memory, per IP + bucket)
# --------------------------------------------------------------------------- #
class RateLimiter:
    def __init__(self) -> None:
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_sec: float) -> Tuple[bool, float]:
        """Kembalikan (allowed, retry_after_sec)."""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            while bucket and now - bucket[0] > window_sec:
                bucket.popleft()
            if len(bucket) >= limit:
                retry = window_sec - (now - bucket[0])
                return False, max(retry, 0.1)
            bucket.append(now)
            return True, 0.0

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def sweep(self, max_age: float = 3600.0) -> None:
        now = time.monotonic()
        with self._lock:
            for key in list(self._hits):
                bucket = self._hits[key]
                while bucket and now - bucket[0] > max_age:
                    bucket.popleft()
                if not bucket:
                    self._hits.pop(key, None)


RATE_LIMITER = RateLimiter()


def client_ip(request: Request) -> str:
    """Ambil IP klien. Header X-Forwarded-For hanya dipercaya bila di belakang proxy.

    Bila `security.trusted_proxy` true (set `SA_TRUSTED_PROXY=true` di lingkungan
    yang ber-Caddy/nginx di depan), ambil entri pertama dari header
    `x-forwarded-for` — yang biasanya adalah IP klien asli. Tanpa flag itu,
    header dari klien sembarang akan dipalsukan, jadi tetap kembalikan IP socket.
    """
    from config import CONFIG

    cfg = CONFIG.get("security")
    if cfg.get("trusted_proxy"):
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------- #
# Sanitasi path (anti path-traversal)
# --------------------------------------------------------------------------- #
class UnsafePathError(ValueError):
    pass


def safe_file_path(base_dir: Path, user_name: str, allowed_suffixes: Iterable[str] = ()) -> Path:
    """Resolve `user_name` di dalam `base_dir`, tolak traversal dan nama aneh."""
    if not user_name or user_name in {".", ".."}:
        raise UnsafePathError("nama file tidak valid")
    if "\x00" in user_name:
        raise UnsafePathError("nama file mengandung byte NUL")
    if "/" in user_name or "\\" in user_name:
        raise UnsafePathError("nama file tidak boleh memuat separator direktori")

    candidate = (base_dir / user_name).resolve()
    root = base_dir.resolve()
    if candidate != root and root not in candidate.parents:
        raise UnsafePathError("path keluar dari direktori yang diizinkan")
    if not candidate.is_file():
        raise UnsafePathError("file tidak ditemukan")

    if allowed_suffixes:
        suffixes = tuple(s.lower() for s in allowed_suffixes)
        if candidate.suffix.lower() not in suffixes:
            raise UnsafePathError(f"tipe file tidak diizinkan (harus {', '.join(suffixes)})")
    return candidate


def safe_slug(value: str, max_len: int = 64) -> str:
    """Bersihkan string yang akan dipakai sebagai bagian nama file."""
    keep = []
    for ch in value or "":
        if ch.isalnum() or ch in "-_. ":
            keep.append(ch)
        else:
            keep.append("_")
    slug = "".join(keep).strip().replace(" ", "_")
    return slug[:max_len] or "unnamed"


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
class AuditLog:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or (LOGS_DIR / "audit.jsonl")
        self._lock = threading.Lock()

    def write(self, actor: str, action: str, detail: Optional[dict] = None, ip: str = "-") -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "actor": actor,
            "action": action,
            "detail": detail or {},
            "ip": ip,
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            try:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass


AUDIT = AuditLog()


# --------------------------------------------------------------------------- #
# Middleware: security headers + rate limit global
# --------------------------------------------------------------------------- #
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    CSP = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self' ws: wss:; "
        "font-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Content-Security-Policy", self.CSP)
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        headers["Cache-Control"] = headers.get("Cache-Control", "no-store")
        return response


def too_many_requests(retry_after: float) -> JSONResponse:
    return JSONResponse(
        {"detail": "Terlalu banyak permintaan. Coba lagi nanti."},
        status_code=429,
        headers={"Retry-After": str(int(retry_after) + 1)},
    )
