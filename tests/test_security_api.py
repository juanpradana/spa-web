"""Uji keamanan, autentikasi RBAC, rate limiting, proteksi path traversal, dan
autentikasi WebSocket.

Menggunakan invokasi langsung ASGI (tanpa httpx / socket jaringan) dan
menjalankan lifespan aplikasi sungguhan sehingga bootstrap admin benar-benar
terjadi — sama seperti saat server dijalankan.

Dijalankan: `python tests/test_security_api.py`
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Isolasi penuh: direktori sementara + tanpa perangkat keras
tmpdir = tempfile.TemporaryDirectory()
os.environ["SA_DATA_DIR"] = str(Path(tmpdir.name) / "data")
os.environ["SA_RECORDINGS_DIR"] = str(Path(tmpdir.name) / "recordings")
os.environ["SA_REPORTS_DIR"] = str(Path(tmpdir.name) / "reports")
os.environ["SA_LOGS_DIR"] = str(Path(tmpdir.name) / "logs")
os.environ["SA_SIMULATE"] = "true"
os.environ["ADMIN_PASSWORD"] = "AdminRahasia123!"
os.environ["SECRET_KEY"] = "kunci-rahasia-pengujian-keamanan-32b"
os.environ["SA_LOGIN_RATE_LIMIT"] = "5"
os.environ["SA_LOGIN_RATE_WINDOW_SEC"] = "60"

import server as server_mod                                     # noqa: E402
from auth import authenticate_ws_token, ROLE_OPERATOR, ROLE_VIEWER  # noqa: E402
from security import RATE_LIMITER                                # noqa: E402

app = server_mod.app
lifespan = server_mod.lifespan

FAILURES: List[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "LULUS" if condition else "GAGAL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


# --------------------------------------------------------------------------- #
# Klien ASGI mandiri (HTTP + WebSocket)
# --------------------------------------------------------------------------- #
class Response:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        self.status_code = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class AsgiClient:
    def __init__(self, asgi_app: Any, client_ip: str = "127.0.0.1") -> None:
        self.app = asgi_app
        self.ip = client_ip

    def _scope(self, method: str, path: str,
               headers: Optional[Dict[str, str]],
               body: bytes, query: Optional[Dict[str, str]]) -> dict:
        raw_headers: List[tuple] = []
        for k, v in (headers or {}).items():
            raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
        if body:
            raw_headers.append((b"content-type", b"application/json"))
            raw_headers.append((b"content-length", str(len(body)).encode("ascii")))
        return {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": path,
            # raw_path membiarkan escape %2F tetap ter-encode sehingga cocok
            # dengan pola rute; Starlette men-dekode nilainya untuk parameter.
            "raw_path": path.encode("ascii"),
            "query_string": urlencode(query or {}).encode("ascii"),
            "headers": raw_headers,
            "client": (self.ip, 51234),
            "server": ("testserver", 80),
        }

    async def request(self, method: str, path: str,
                      headers: Optional[Dict[str, str]] = None,
                      json_data: Optional[Any] = None,
                      query: Optional[Dict[str, str]] = None) -> Response:
        body = json.dumps(json_data).encode("utf-8") if json_data is not None else b""
        scope = self._scope(method, path, headers, body, query)

        status = 500
        resp_headers: Dict[str, str] = {}
        chunks: List[bytes] = []

        async def receive() -> dict:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(msg: dict) -> None:
            nonlocal status
            if msg["type"] == "http.response.start":
                status = msg["status"]
                for k, v in msg.get("headers", []):
                    resp_headers[k.decode("latin-1").lower()] = v.decode("latin-1")
            elif msg["type"] == "http.response.body":
                chunks.append(msg.get("body", b""))

        await self.app(scope, receive, send)
        return Response(status, resp_headers, b"".join(chunks))

    async def get(self, path: str, **kw) -> Response:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, json: Optional[Any] = None, **kw) -> Response:
        return await self.request("POST", path, json_data=json, **kw)

    async def websocket(self, full_path: str, send_text: Optional[str] = None) -> List[dict]:
        """Handshake WebSocket ASGI; kembalikan daftar pesan dari server.

        `scope["path"]` harus TANPA query string — Starlette mencocokkan rute
        berdasarkan path, sedangkan token dikirim lewat `query_string`.
        """
        path, _, query = full_path.partition("?")
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query.encode("ascii"),
            "headers": [],
            "client": (self.ip, 51235),
            "server": ("testserver", 80),
            "subprotocols": [],
        }

        sent: List[dict] = [{"type": "websocket.connect"}]
        received: List[dict] = []

        async def receive() -> dict:
            if sent:
                return sent.pop(0)
            # Setelah tersambung, tunggu disconnect dari sisi klien.
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(msg: dict) -> None:
            received.append(msg)
            # Begitu server menerima koneksi, kirim pesan uji (jika ada).
            if msg["type"] == "websocket.accept" and send_text is not None:
                sent.append({"type": "websocket.receive", "text": send_text})

        await self.app(scope, receive, send)
        return received


def _accept_code(msgs: List[dict]) -> Optional[int]:
    for m in msgs:
        if m["type"] == "websocket.close":
            return m.get("code")
    return None


def _texts(msgs: List[dict]) -> List[str]:
    return [m["text"] for m in msgs if m["type"] == "websocket.send" and "text" in m]


# --------------------------------------------------------------------------- #
# Alur pengujian
# --------------------------------------------------------------------------- #
async def run() -> None:
    client = AsgiClient(app, client_ip="192.168.1.100")

    print("== 1. Security Headers Middleware ==")
    r = await client.get("/")
    check("status 200", r.status_code == 200)
    check("X-Content-Type-Options: nosniff",
          r.headers.get("x-content-type-options") == "nosniff")
    check("X-Frame-Options: DENY", r.headers.get("x-frame-options") == "DENY")
    check("Referrer-Policy strict-origin-when-cross-origin",
          "strict-origin" in r.headers.get("referrer-policy", ""))
    check("Content-Security-Policy hadir",
          "default-src" in r.headers.get("content-security-policy", ""))
    check("Permissions-Policy hadir", "microphone=()" in r.headers.get("permissions-policy", ""))
    check("Cache-Control: no-store", r.headers.get("cache-control") == "no-store")

    print("\n== 2. Autentikasi Admin Bootstrap ==")
    r = await client.post("/api/auth/login",
                          json={"username": "admin", "password": "AdminRahasia123!"})
    check("login admin berhasil (HTTP 200)", r.status_code == 200, f"code={r.status_code}")
    admin_token = r.json().get("access_token")
    check("token JWT diterbitkan", bool(admin_token))
    ah = {"Authorization": f"Bearer {admin_token}"}

    r = await client.get("/api/auth/me", headers=ah)
    check("get me mengenali peran admin", r.json().get("role") == "admin")

    print("\n== 2b. Login dengan Kata Sandi Salah Ditolak ==")
    r = await client.post("/api/auth/login",
                          json={"username": "admin", "password": "kata-sandi-salah"})
    check("kata sandi salah ditolak (HTTP 401)", r.status_code == 401, f"code={r.status_code}")

    print("\n== 3. Penolakan Akses Tanpa Token (HTTP 401) ==")
    check("RF status tanpa token ditolak",
          (await client.get("/api/rf/status")).status_code == 401)
    check("daftar pengguna tanpa token ditolak",
          (await client.get("/api/auth/users")).status_code == 401)
    check("statistik okupansi tanpa token ditolak",
          (await client.get("/api/occupancy/stats")).status_code == 401)
    check("daftar rekaman tanpa token ditolak",
          (await client.get("/api/recordings")).status_code == 401)
    check("token palsu ditolak",
          (await client.get("/api/rf/status",
                            headers={"Authorization": "Bearer palsu.palsu.palsu"})).status_code == 401)

    print("\n== 4. Pembuatan Pengguna & Penegakan RBAC ==")
    r = await client.post("/api/auth/users", headers=ah,
                          json={"username": "operator1",
                                "password": "OperatorPassword123!",
                                "role": ROLE_OPERATOR})
    check("admin dapat membuat operator", r.status_code == 200, f"code={r.status_code}")

    r = await client.post("/api/auth/users", headers=ah,
                          json={"username": "viewer1",
                                "password": "ViewerPassword123!",
                                "role": ROLE_VIEWER})
    check("admin dapat membuat viewer", r.status_code == 200, f"code={r.status_code}")

    r = await client.post("/api/auth/users", headers=ah,
                          json={"username": "operator1",
                                "password": "OperatorPassword123!",
                                "role": ROLE_OPERATOR})
    check("username duplikat ditolak (HTTP 409)", r.status_code == 409, f"code={r.status_code}")

    r = await client.post("/api/auth/users", headers=ah,
                          json={"username": "../jahat", "password": "Password12345!", "role": ROLE_VIEWER})
    check("username dengan karakter tidak sah ditolak (HTTP 422)",
          r.status_code == 422, f"code={r.status_code}")

    r = await client.post("/api/auth/users", headers=ah,
                          json={"username": "roleaneh", "password": "Password12345!", "role": "superadmin"})
    check("peran tidak dikenal ditolak (HTTP 400)", r.status_code == 400, f"code={r.status_code}")

    op_token = (await client.post("/api/auth/login",
                                  json={"username": "operator1",
                                        "password": "OperatorPassword123!"})).json()["access_token"]
    vw_token = (await client.post("/api/auth/login",
                                  json={"username": "viewer1",
                                        "password": "ViewerPassword123!"})).json()["access_token"]
    oh = {"Authorization": f"Bearer {op_token}"}
    vh = {"Authorization": f"Bearer {vw_token}"}

    # --- Viewer: hanya baca ---
    check("viewer dapat membaca status RF (200)",
          (await client.get("/api/rf/status", headers=vh)).status_code == 200)
    check("viewer dapat membaca preset (200)",
          (await client.get("/api/rf/presets", headers=vh)).status_code == 200)
    check("viewer dapat membaca okupansi (200)",
          (await client.get("/api/occupancy/stats", headers=vh)).status_code == 200)
    check("viewer DITOLAK mengganti preset (403)",
          (await client.post("/api/rf/preset", headers=vh,
                             json={"preset_key": "vhf_marine"})).status_code == 403)
    check("viewer DITOLAK menyetel sweep (403)",
          (await client.post("/api/rf/sweep", headers=vh,
                             json={"center_hz": 145e6})).status_code == 403)
    check("viewer DITOLAK mendengar audio (403)",
          (await client.post("/api/rf/listen", headers=vh,
                             json={"freq_hz": 145e6, "demod": "FM"})).status_code == 403)
    check("viewer DITOLAK menyalakan engine (403)",
          (await client.post("/api/rf/start", headers=vh)).status_code == 403)
    check("viewer DITOLAK menghapus rekaman (403)",
          (await client.request("DELETE", "/api/recordings/x.wav", headers=vh)).status_code == 403)
    check("viewer DITOLAK mengelola pengguna (403)",
          (await client.get("/api/auth/users", headers=vh)).status_code == 403)
    check("viewer DITOLAK mereset okupansi (403)",
          (await client.post("/api/occupancy/reset", headers=vh)).status_code == 403)

    # --- Operator: kontrol RF, bukan administrasi ---
    check("operator DAPAT mengganti preset (200)",
          (await client.post("/api/rf/preset", headers=oh,
                             json={"preset_key": "vhf_marine"})).status_code == 200)
    check("operator DAPAT menyetel sweep (200)",
          (await client.post("/api/rf/sweep", headers=oh,
                             json={"center_hz": 145e6, "span_hz": 2e6})).status_code == 200)
    check("operator DAPAT mengaktifkan autorecord (200)",
          (await client.post("/api/rf/autorecord", headers=oh,
                             json={"enabled": True})).status_code == 200)
    check("operator DAPAT menyetel ambang okupansi (200)",
          (await client.post("/api/rf/occupancy-config", headers=oh,
                             json={"threshold_mode": "fixed",
                                   "fixed_threshold_dbm": -90.0})).status_code == 200)
    check("operator DITOLAK mengelola pengguna (403)",
          (await client.get("/api/auth/users", headers=oh)).status_code == 403)
    check("operator DITOLAK mereset okupansi (403)",
          (await client.post("/api/occupancy/reset", headers=oh)).status_code == 403)
    check("operator DITOLAK menghapus jadwal (403)",
          (await client.request("DELETE", "/api/schedules/apa-saja", headers=oh)).status_code == 403)
    # Peralihan mode perangkat mengubah sumber sinyal seluruh sistem: hanya admin.
    check("operator DITOLAK mengubah mode perangkat (403)",
          (await client.post("/api/rf/device", headers=oh,
                             json={"simulate": True})).status_code == 403)
    check("viewer DITOLAK mengubah mode perangkat (403)",
          (await client.post("/api/rf/device", headers=vh,
                             json={"simulate": True})).status_code == 403)

    # --- Admin: penuh ---
    check("admin DAPAT mereset okupansi (200)",
          (await client.post("/api/occupancy/reset", headers=ah)).status_code == 200)
    check("admin DAPAT mengubah mode perangkat (200)",
          (await client.post("/api/rf/device", headers=ah,
                             json={"simulate": True})).status_code == 200)

    # Validasi rentang nomor seri pada endpoint perangkat
    check("nomor seri 0 ditolak (422)",
          (await client.post("/api/rf/device", headers=ah,
                             json={"simulate": False, "serial": 0})).status_code == 422)
    check("nomor seri negatif ditolak (422)",
          (await client.post("/api/rf/device", headers=ah,
                             json={"simulate": False, "serial": -5})).status_code == 422)
    check("nomor seri di luar int32 ditolak (422)",
          (await client.post("/api/rf/device", headers=ah,
                             json={"simulate": False, "serial": 99_999_999_999})).status_code == 422)

    # `rf/config` mengekspos pilihan perangkat agar panel Admin bisa disinkronkan
    cfg = (await client.get("/api/rf/config", headers=ah)).json()
    check("rf/config memuat bagian device", "device" in cfg,
          f"keys={sorted(cfg.keys())}")
    check("rf/config device memuat simulate",
          isinstance(cfg.get("device", {}).get("simulate"), bool),
          f"simulate={cfg.get('device', {}).get('simulate')!r}")

    # Kembalikan ke simulator supaya sisa pengujian tetap tanpa perangkat keras
    await client.post("/api/rf/device", headers=ah, json={"simulate": True})

    # --- Lindungan anti-lockout ---
    r = await client.request("DELETE", "/api/auth/users/admin", headers=ah)
    check("admin tidak dapat menghapus akunnya sendiri (400)", r.status_code == 400, f"code={r.status_code}")

    print("\n== 5. Proteksi Path Traversal ==")
    # Slug tanpa slash: harus ditolak oleh safe_file_path (HTTP 400)
    slug_payloads = ["..", ".", "secret.key", "users.db", "invalid_ext.txt", "nul\x00.wav"]
    for p in slug_payloads:
        code = (await client.get(f"/api/recordings/{p}/download", headers=vh)).status_code
        check(f"slug '{p}' ditolak aman (400)", code == 400, f"code={code}")

    # Payload traversal ter-encode (%2F / %5C) mencapai penangan rute
    encoded_payloads = [
        "..%2F..%2Fetc%2Fpasswd",
        "..%5C..%5Cwindows%5Cwin.ini",
        "subdir%2F..%2F..%2F..%2Fdata%2Fusers.db",
        "..%2Fsecret.key",
    ]
    for p in encoded_payloads:
        code = (await client.get(f"/api/recordings/{p}/download", headers=vh)).status_code
        check(f"traversal ter-encode '{p}' ditolak aman (400)", code == 400, f"code={code}")

    # Direktori data tidak boleh dijangkau
    code = (await client.get("/api/recordings/users.db/download", headers=vh)).status_code
    check("file di luar direktori rekaman ditolak (400)", code == 400, f"code={code}")

    print("\n== 6. Rate Limiting Percobaan Login ==")
    RATE_LIMITER.reset("login:203.0.113.42")
    attacker = AsgiClient(app, client_ip="203.0.113.42")

    codes = []
    for _ in range(5):
        r = await attacker.post("/api/auth/login",
                                json={"username": "admin", "password": "Salah"})
        codes.append(r.status_code)
    check("5 percobaan gagal pertama semuanya 401",
          all(c == 401 for c in codes), f"codes={codes}")

    r6 = await attacker.post("/api/auth/login",
                             json={"username": "admin", "password": "Salah"})
    check("percobaan ke-6 diblokir (HTTP 429)", r6.status_code == 429, f"code={r6.status_code}")
    check("header Retry-After hadir pada 429", "retry-after" in r6.headers)

    innocent = AsgiClient(app, client_ip="198.51.100.99")
    r = await innocent.post("/api/auth/login",
                            json={"username": "admin", "password": "Salah"})
    check("klien dari IP lain tidak terdampak (HTTP 401)", r.status_code == 401, f"code={r.status_code}")

    print("\n== 7. Autentikasi WebSocket ==")
    msgs = await client.websocket("/ws/spectrum")
    check("ws tanpa token ditolak dengan kode 1008 (policy violation)",
          _accept_code(msgs) == 1008, f"close={_accept_code(msgs)}")

    msgs = await client.websocket("/ws/spectrum?token=palsu.tidak.valid")
    check("ws dengan token palsu ditolak dengan kode 1008",
          _accept_code(msgs) == 1008, f"close={_accept_code(msgs)}")

    msgs = await client.websocket(f"/ws/spectrum?token={admin_token}", send_text="ping")
    check("ws dengan token sah diterima", any(m["type"] == "websocket.accept" for m in msgs))
    check("ws membalas ping dengan pong", "pong" in _texts(msgs), f"texts={_texts(msgs)}")

    msgs = await client.websocket(f"/ws/audio?token={vw_token}")
    check("viewer (peran terendah) boleh berlangganan ws spektrum/audio",
          any(m["type"] == "websocket.accept" for m in msgs))

    print("\n== 8. Helper Autentikasi Token ==")
    check("token kosong ditolak", authenticate_ws_token(None) is None)
    check("token acak ditolak", authenticate_ws_token("a.b.c") is None)
    u = authenticate_ws_token(admin_token)
    check("token sah memetakan ke pengguna", u is not None and u.username == "admin")

    print("\n== 8b. Token WebSocket Gugur Setelah Ganti Kata Sandi ==")
    # Token dibuat dengan `pwd` stamp; setelah password berubah stamp ikut
    # berubah, sehingga baik HTTP maupun WebSocket harus menolak token lama.
    check("token viewer sah sebelum ganti sandi",
          authenticate_ws_token(vw_token) is not None)
    r = await client.post("/api/auth/change-password", headers=vh,
                          json={"old_password": "ViewerPassword123!",
                                "new_password": "ViewerPassword456!"})
    check("viewer dapat mengganti kata sandinya (200)", r.status_code == 200,
          f"code={r.status_code}")
    check("token WS viewer lama ditolak setelah ganti sandi",
          authenticate_ws_token(vw_token) is None)
    check("token HTTP viewer lama ditolak setelah ganti sandi (401)",
          (await client.get("/api/rf/status", headers=vh)).status_code == 401)
    check("WS viewer dengan token lama ditutup 1008",
          _accept_code(await client.websocket(f"/ws/audio?token={vw_token}")) == 1008)

    print("\n== 9. Validasi Rentang Parameter RF ==")
    check("frekuensi di bawah 100 kHz ditolak (422)",
          (await client.post("/api/rf/sweep", headers=oh,
                             json={"center_hz": 50.0})).status_code == 422)
    check("frekuensi di atas 12.4 GHz ditolak (422)",
          (await client.post("/api/rf/sweep", headers=oh,
                             json={"center_hz": 20e9})).status_code == 422)
    check("atenuasi di luar 0-60 dB ditolak (422)",
          (await client.post("/api/rf/sweep", headers=oh,
                             json={"atten_db": 99})).status_code == 422)
    check("preset tidak dikenal ditolak (404)",
          (await client.post("/api/rf/preset", headers=oh,
                             json={"preset_key": "preset_karangan"})).status_code == 404)

    print("\n== 9b. Token Viewer Disegarkan Setelah Ganti Kata Sandi ==")
    # Bagian 8b mengganti kata sandi viewer1 sehingga token lama otomatis gugur
    # (klaim `pwd` berubah). Segarkan token sebelum pengujian RBAC 10-14 yang
    # masih memakai header viewer.
    r = await client.post("/api/auth/login",
                          json={"username": "viewer1", "password": "ViewerPassword456!"})
    check("viewer dapat login kembali dengan sandi baru (200)", r.status_code == 200,
          f"code={r.status_code}")
    vw_token = r.json().get("access_token")
    vh = {"Authorization": f"Bearer {vw_token}"}
    check("token viewer baru sah untuk HTTP (200)",
          (await client.get("/api/rf/status", headers=vh)).status_code == 200)
    check("token viewer baru sah untuk WebSocket",
          authenticate_ws_token(vw_token) is not None)

    print("\n== 10. Endpoint Baru: GET /api/rf/devices (RBAC & Info Driver) ==")
    r = await client.get("/api/rf/devices", headers=vh)
    check("viewer boleh akses /api/rf/devices (200)", r.status_code == 200)
    data = json.loads(r.body)
    check("respon punya library_loaded boolean", isinstance(data.get("library_loaded"), bool))
    check("respon punya serials array", isinstance(data.get("serials"), list))
    check("respon punya current object", isinstance(data.get("current"), dict))

    r = await client.get("/api/rf/devices")
    check("tanpa token /api/rf/devices 401", r.status_code == 401)

    print("\n== 11. POST /api/occupancy/delete (Bulk Delete Kanal) ==")
    # Admin/Operator boleh akses
    r = await client.post("/api/occupancy/delete", headers=oh, json={"keys": ["100000000"]})
    check("operator boleh hapus okupansi (200/404)", r.status_code in (200, 404))
    # Viewer tidak boleh
    r = await client.post("/api/occupancy/delete", headers=vh, json={"keys": ["100000000"]})
    check("viewer dilarang hapus okupansi (403)", r.status_code == 403)
    # Validasi min_length=1
    r = await client.post("/api/occupancy/delete", headers=oh, json={"keys": []})
    check("keys kosong ditolak (422)", r.status_code == 422)
    # Keys terlalu banyak >5000
    r = await client.post("/api/occupancy/delete", headers=oh, json={"keys": [f"{i}" for i in range(5001)]})
    check("keys >5000 ditolak (422)", r.status_code == 422)

    print("\n== 12. POST /api/recordings/bulk-delete (Bulk Delete Rekaman) ==")
    # Buat file WAV uji di direktori rekaman sementara
    rec_dir = Path(tmpdir.name) / "recordings"
    rec_dir.mkdir(exist_ok=True)
    import wave
    import struct
    for name in ["test1.wav", "test2.wav", "test3.wav"]:
        wf = wave.open(str(rec_dir / name), "wb")
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(struct.pack("<" + "h" * 8000, *[0]*8000))
        wf.close()

    r = await client.post("/api/recordings/bulk-delete", headers=oh, json={"filenames": ["test1.wav", "test2.wav"]})
    check("operator boleh hapus bulk (200)", r.status_code == 200)
    d = json.loads(r.body)
    check("deleted=2", d.get("deleted") == 2)
    # Verifikasi file benar-benar terhapus
    check("test1.wav & test2.wav benar-benar terhapus", not (rec_dir/"test1.wav").exists() and not (rec_dir/"test2.wav").exists())
    check("test3.wav masih ada", (rec_dir/"test3.wav").exists())

    # Path traversal attempt
    r = await client.post("/api/recordings/bulk-delete", headers=oh, json={"filenames": ["../test3.wav"]})
    check("path traversal ../ ditolak/tidak menghapus", r.status_code == 200)  # 200 tapi deleted=0, failed=["../test3.wav"]
    d = json.loads(r.body)
    check("path traversal masuk failed list", "../test3.wav" in d.get("failed", []))

    # Viewer tidak boleh
    r = await client.post("/api/recordings/bulk-delete", headers=vh, json={"filenames": ["test3.wav"]})
    check("viewer dilarang hapus bulk (403)", r.status_code == 403)

    print("\n== 13. POST /api/recordings/delete-all ==")
    r = await client.post("/api/recordings/delete-all", headers=oh)
    check("operator boleh delete-all (200)", r.status_code == 200)
    d = json.loads(r.body)
    check("semua file terhapus", d.get("deleted") == 1)  # hanya test3.wav tersisa
    check("direktori rekaman bersih", len(list(rec_dir.glob("*.wav"))) == 0)

    r = await client.post("/api/recordings/delete-all", headers=vh)
    check("viewer dilarang delete-all (403)", r.status_code == 403)

    # Uji guard require_admin pada POST /api/rf/device & /api/occupancy/reset:
    # keduanya mengubah sumber/riwayat data sistem sehingga hanya admin.
    print("\n== 14. POST /api/rf/device & /api/occupancy/reset (Guard Admin) ==")
    r = await client.post("/api/rf/device", headers=oh, json={"simulate": True})
    check("operator dilarang set device (403)", r.status_code == 403)
    r = await client.post("/api/rf/device", headers=vh, json={"simulate": True})
    check("viewer dilarang set device (403)", r.status_code == 403)
    r = await client.post("/api/occupancy/reset", headers=oh)
    check("operator dilarang mereset okupansi (403)", r.status_code == 403)
    r = await client.post("/api/rf/device", headers=ah, json={"simulate": True})
    check("admin boleh set device (200)", r.status_code == 200)
    r = await client.post("/api/occupancy/reset", headers=ah)
    check("admin boleh mereset okupansi (200)", r.status_code == 200)


async def main_async() -> int:
    ctx = lifespan(app)
    await ctx.__aenter__()
    try:
        await run()
    finally:
        await ctx.__aexit__(None, None, None)

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"HASIL: {len(FAILURES)} pemeriksaan GAGAL")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("HASIL: semua pemeriksaan keamanan LULUS")
    return 0


def _cleanup_tmp() -> None:
    """Hapus direktori sementara; abaikan bila handle WAV masih terbuka di Windows."""
    import time

    for _ in range(10):
        try:
            tmpdir.cleanup()
            return
        except PermissionError:
            time.sleep(0.3)


if __name__ == "__main__":
    try:
        code = asyncio.run(main_async())
    finally:
        _cleanup_tmp()
    sys.exit(code)
