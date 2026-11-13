# PRISM — Pemantau Okupansi Spektrum & Perekam Audio SA124B

Web dashboard untuk **Signal Hound SA124B** (100 kHz – 12.4 GHz): pengukuran
okupansi kanal berstandar ITU-R, perekaman audio otomatis saat sinyal terdeteksi,
pemantauan real-time, dan penjadwalan survei.

## Fitur Utama

- **Monitoring spektrum real-time**: grafik spektrum 60 FPS + waterfall spektrogram
  bergulir (palet Turbo/Viridis/Jet/Monokrom)
- **Rekaman audio otomatis**: saat kanal terdeteksi melampaui ambang batas,
  SA124B melakukan dwell dan mendemodulasi audio (AM/FM/USB/LSB/CW), disimpan
  sebagai WAV: `{frekuensi}MHz_{timestamp}.wav`
- **Okupansi kanal (ITU-R SM.1880 / SM.1537)**: persentase keterisian, daya puncak,
  daya rata-rata, dan jumlah transmisi per kanal; ekspor Excel/CSV
- **Penjadwalan survei**: buat kampanye sweep terjadwal (waktu, durasi, pengulangan,
  threshold adaptif/tetap), mulai manual, pantau riwayat
- **Live audio streaming**: dengarkan langsung dari browser dengan Web Audio API
  dan VU meter
- **Keamanan**: autentikasi JWT HS256, RBAC (`admin`/`operator`/`viewer`), rate
  limiting login (5×/menit/IP), proteksi path traversal, security headers lengkap
- **Cross-platform**: jalankan native di Windows/Linux, atau paket penuh via
  Docker Compose (termasuk reverse proxy Caddy HTTPS otomatis)

---

## Persyaratan Sistem

- Python 3.12+
- Perangkat keras **Signal Hound SA124B** terhubung via USB (opsional — mode
  simulator tersedia untuk uji coba)
- Browser modern: Chrome 120+, Edge 120+, Firefox 125+, Safari 17+ (Web Audio &
  WebSocket API didukung penuh)

## Instalasi Native (Windows)

1. Pasang [Python 3.12+](https://www.python.org/downloads/).
2. Salin satu folder proyek dan jalankan di PowerShell:

   ```
   cd prism
   .\run.bat
   ```

3. Buka browser: `http://localhost:3487`

Server membuat akun `admin` secara otomatis. Kata sandi awal hanya dicetak ke
konsol **satu kali**, saat basis data pengguna pertama kali dibuat. Akun itu
ditandai *harus ganti kata sandi*, sehingga jendela **Ganti Kata Sandi** menutupi
seluruh aplikasi setiap kali login sampai kata sandi baru ditetapkan; sesudah itu
sesi ditutup dan Anda login ulang dengan kata sandi baru. Kata sandi juga dapat
diganti kapan saja lewat **Admin → Ganti Kata Sandi**.

## Instalasi Native (Linux / Raspberry Pi)

```
cd prism
chmod +x run.sh
./run.sh
```

Akses via browser pada host: `http://<ip-host>:3487`.

## Instalasi Cepat — One-Command Deploy (Direkomendasikan)

**Windows (PowerShell / CMD):**
```
deploy.bat
```

**Linux / macOS / WSL:**
```
./deploy.sh
```

Skrip deployer otomatis:
- Membuat `.env` dari template jika belum ada
- Menghasilkan `SECRET_KEY` aman secara otomatis
- Menetapkan kata sandi admin (`Admin123456!`) dan menampilkannya langsung di konsol
- Memulai container Prism di port **3487**
- Mencetak URL akses dan kredensial secara rapi

Opsi tambahan:
| Flag | Deskripsi |
|------|-----------|
| `--hardware` | Aktifkan passthrough USB fisik SA124B (hanya Linux host) |
| `--proxy` | Aktifkan Caddy reverse proxy HTTPS otomatis (port 80/443) |
| `--tunnel` | Aktifkan Cloudflare Tunnel (akses remote CGNAT/4G/Starlink) |
| `--build` | Rebuild image container dari Dockerfile |
| `--down` | Hentikan semua container |
| `--logs` | Pantau log realtime |
| `--status` | Lihat status container |

---

## Instalasi Native (Zero-Friction)

**Windows (PowerShell / CMD):**
```
.\run.bat
```

**Linux / macOS / Raspberry Pi / WSL:**
```
./run.sh
```

Skrip runner native otomatis:
- Membuat virtual environment `.venv` jika belum ada
- Memasang `requirements.txt` jika modul belum terpasang
- Membuat `.env` awal jika belum ada
- Menjalankan server di port **3487**

---

## Instalasi Docker Compose (Manual)

```
# Mode dasar (HTTP, port 3487 — siap reverse proxy)
docker compose up -d

# Mode dengan reverse proxy Caddy bawaan (HTTPS otomatis)
docker compose --profile proxy up -d

# Mode dengan Cloudflare Tunnel (akses remote tanpa IP publik)
docker compose --profile tunnel up -d
```

Variabel konfigurasi (opsional): `SA_HOST`, `SA_PORT`, `SECRET_KEY`,
`ADMIN_PASSWORD`, `SA_SIMULATE`, `SA_DEVICE_SERIAL`, `DOMAIN`, `TUNNEL_TOKEN`.

Catatan: **File `docker-compose.yml` dasar TIDAK memuat passthrough USB** — berjalan 100% universal di Windows, macOS, Linux, dan Cloud VPS. Untuk hardware fisik SA124B pada host Linux, gunakan:
```
docker compose -f docker-compose.yml -f docker-compose.hardware.yml up -d
```
Atau lebih mudah: `./deploy.sh --hardware`.

## Struktur Proyek

```
prism/
├── auth.py                  # Autentikasi JWT HS256, hashing PBKDF2-SHA256, RBAC
├── security.py              # Rate limiter, sanitasi path, security headers, audit log
├── config.py                # Konfigurasi & override environment
├── sa124.py                 # Binding C API SA124B atau hardware simulator
├── engine.py                # CaptureEngine: scan-and-dwell, WebSocket broadcast
├── channels.py              # 13 preset pita + generator kanal per preset
├── recorder.py              # Pembuat, pengolah, & penghapus rekaman WAV
├── occupancy.py             # Akumulator & statistik okupansi kanal (SQLite WAL)
├── scheduler.py             # Penjadwal kampanye survei okupansi
├── reports.py               # Ekspor laporan Excel/CSV
├── server.py                # FastAPI REST API + WebSocket streaming
├── static/                  # Single-page app (HTML/CSS/JS)
├── tests/                   # Uji engine end-to-end & uji keamanan API
├── data/                    # Database SQLite & kunci rahasia (diabaikan git)
├── recordings/              # Berkas WAV hasil perekaman   (diabaikan git)
├── reports/                 # Berkas ekspor Excel/CSV     (diabaikan git)
├── logs/                    # Audit log & log aplikasi    (diabaikan git)
├── Dockerfile
├── docker-compose.yml       # Compose universal (crash-proof di Windows/macOS/Linux/VPS)
├── docker-compose.hardware.yml # Ekstensi USB passthrough SA124B untuk host Linux
├── Caddyfile                # Konfigurasi reverse proxy Caddy HTTPS otomatis
├── nginx.conf.example       # Contoh konfigurasi reverse proxy Nginx eksternal
├── deploy.bat               # Skrip deployer 1-klik untuk Windows
├── deploy.sh                # Skrip deployer 1-klik untuk Linux / macOS
├── requirements.txt
├── .gitignore
├── .dockerignore
├── .env.example
├── run.bat                  # Runner native Windows (auto-venv & auto-requirements)
├── run.sh                   # Runner native Linux (auto-venv & auto-requirements)
├── LICENSE
└── README.md
```

---

## Arsitektur Keamanan

| Lapisan | Mekanisme |
|---|---|
| **Autentikasi** | JWT HS256, TTL default 12 jam, password stamp pembatalan otomatis setelah ganti kata sandi |
| **Otorisasi** | RBAC peran: `admin` (lengkap), `operator` (kontrol RF), `viewer` (hanya baca) |
| **Rate limiting** | Sliding window per IP pada endpoint login (5×/menit), HTTP 429 + header `Retry-After` |
| **WebSockets** | Autentikasi token lewat `?token=` sebelum koneksi diterima, ditutup (code 1008) bila tidak sah |
| **Path traversal** | Semua nama file dipaksa slug alfanumerik; path traversal `%2F`/`%5C` ditolak (HTTP 400) |
| **Headers** | CSP, X-Frame-Options: DENY, X-Content-Type-Options: nosniff, Referrer-Policy, Permissions-Policy, HSTS via proxy |
| **Audit** | Log JSONL tiap aksi kritis (login, hapus pengguna, reset okupansi, hapus rekaman) |

---

## Cara Kerja: Scan-and-Dwell (ITU-R SM.1537 / SM.1880)

1. **Sweeping (mendekati 100% duty cycle)**: perangkat menyapu seluruh kanal; setiap
   kerangka spektrum dievaluasi per kanal terhadap ambang batas.
2. **Thresholding adaptif**: ambang = noise floor + offset yang dapat dikonfigurasi.
3. **Dwell & demodulasi**: bila kanal terdeteksi melampaui ambang, perangkat
   dikonfigurasi ke frekuensi kanal, demodulasi audio dilakukan, dan audio dikirim
   secara live ke browser sekaligus ditulis ke file WAV.
4. **Pengembalian ke sweeping**: setelah dwell selesai, perangkat kembali menyapu.

Karena SA124B hanya memiliki satu tuner, sweep dan demodulasi tidak terjadi secara
bersamaan — pola scan-and-dwell inilah solusi yang dipilih untuk tetap mendekati
sweep berkelanjutan sambil merekam setiap kedudukan.

---

## Deployment Lengkap (Docker Compose)

### Mode Dasar (Universal & Crash-Proof)

```
docker compose up -d
```
Atau cukup jalankan `deploy.bat` (Windows) / `./deploy.sh` (Linux).

- Container `prism-app` terbuka di port **3487** (`http://localhost:3487`),
  siap diakses langsung atau ditaruh di belakang reverse proxy seperti Nginx, Caddy, Traefik, atau Cloudflare Tunnel.
- Konfigurasi dasar **tidak menyertakan node host `/dev/bus/usb`**, sehingga dijamin
  dapat berjalan langsung di Windows Docker Desktop (WSL2), macOS, maupun Cloud VPS (AWS/Hetzner/DigitalOcean)
  tanpa error startup.
- Volume persisten: `./data` (database & kredensial), `./recordings`, `./reports`,
  `./logs` — datanya bertahan meskipun container di-recreate/diupdate.
- Healthcheck otomatis menyesuaikan port yang dikonfigurasi pada `SA_PORT`.

### Passthrough Perangkat Keras SA124B (Host Linux)

Bila Anda menjalankan container di host Linux fisik (Ubuntu/Debian/Raspberry Pi)
dengan kabel USB SA124B tercolok:

```bash
# Menggunakan skrip otomasi:
./deploy.sh --hardware

# Atau perintah compose manual:
docker compose -f docker-compose.yml -f docker-compose.hardware.yml up -d
```

### Akses Remote & Reverse Proxy

#### Pilihan A: Cloudflare Tunnel (Paling Mudah — Tanpa IP Publik / Port Forwarding)

Sangat cocok untuk stasiun pemantau radio di lapangan yang memakai modem seluler
4G/LTE atau Starlink di balik CGNAT.

1. Buka [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/) → **Networks** → **Tunnels**.
2. Buat Tunnel baru, tambahkan Public Hostname yang mengarah ke `http://prism:3487`.
3. Salin token tunnel dan masukkan ke `.env`:
   ```env
   TUNNEL_TOKEN=eyJh...
   ```
4. Jalankan:
   ```bash
   # Windows:
   deploy.bat --tunnel

   # Linux:
   ./deploy.sh --tunnel
   # Atau: docker compose --profile tunnel up -d
   ```
Dashboard Anda langsung memiliki HTTPS publik global tanpa perlu membuka port router!

#### Pilihan B: Caddy Bawaan (HTTPS Otomatis Let's Encrypt)

```bash
# Windows:
deploy.bat --proxy

# Linux:
./deploy.sh --proxy
# Atau: docker compose --profile proxy up -d
```

1. Atur `DOMAIN=your.domain.com` di `.env`.
2. Buka port 80 dan 443 di firewall/router Anda.
3. Caddy menerbitkan sertifikat Let's Encrypt otomatis. Akses: `https://your.domain.com`.

#### Pilihan C: Nginx Sendiri

Bila Anda sudah memiliki Nginx di server, cukup gunakan file contoh:
```bash
sudo cp nginx.conf.example /etc/nginx/conf.d/prism.conf
# Sesuaikan domain & upstream ke 127.0.0.1:3487
sudo nginx -t && sudo systemctl reload nginx
```
Pastikan `SA_TRUSTED_PROXY=true` di `.env` agar pembatasan laju login dan
catatan audit mencatat IP klien asli (membaca header `X-Forwarded-For`).

Untuk ekspos ke internet, pastikan Anda sudah:

- Membuka port 80/443 pada firewall/ISP.
- Membuat record A/AAAA DNS yang mengarah ke IP publik host.
- **Mengganti `SECRET_KEY` dan `ADMIN_PASSWORD`** — biarkan keduanya kosong berarti
  kata sandi dan kunci diregenerasi secara acak setiap kali container dimulai ulang,
  sehingga semua sesi menjadi tidak sah.

### Mode Simulator (Tanpa Perangkat Keras)

Untuk demo atau uji coba tanpa SA124B:

```
# Native: SA_SIMULATE=true di .env, atau:
export SA_SIMULATE=true   # Linux
$env:SA_SIMULATE='true'   # PowerShell
./run.sh                  # atau run.bat
```

```
# Docker:
SA_SIMULATE=true docker compose up -d
```

Atau lewat GUI **Admin → Mode Perangkat**. Server juga otomatis jatuh ke simulator
jika perangkat fisik tidak terdeteksi.

---

## Antarmuka Pengguna

### Monitor

- Grafik spektrum real-time (log/linear), marker sentuh/kursor untuk membaca
  frekuensi & daya tepat, peak hold.
- **Interaksi Spektrum Cerdas**:
  - **Klik-untuk-Dengar**: saat mendengarkan audio, klik pada canvas spektrum
    langsung memindahkan frekuensi demodulasi ke titik yang diklik (Alt+Klik
    atau Double-Click untuk mulai dengar).
  - **Crop Rentang (Box-Zoom)**: klik-kiri & drag horizontal pada spektrum untuk
    menyeleksi rentang frekuensi tertentu secara interaktif.
  - **Geser Span (Pan)**: klik-kanan/tengah drag untuk menggeser frekuensi tengah
    (*center frequency*) secara dinamis.
  - **Scroll-Wheel Zoom**: perbesar/perkecil span secara halus langsung dari roda mouse.
  - **Toolbar Span**: tombol pintas ◀ Geser, Geser ▶, Zoom In, Zoom Out, dan Reset Span.
- Waterfall bergulir dengan palet warna dapat diganti dan gain kontras disesuaikan.
- Chip status: perangkat (terhubung/di-simulasikan), mode mesin (IDLE/SWEEPING/
  DWELL_RECORD/MANUAL_AUDIO), frame rate & suhu. Klik chip perangkat untuk
  membuka **Modal Pemilihan & Diagnostik Perangkat**.

### Okupansi

- Tabel per kanal: okupansi (%), jumlah sampel, daya puncak & rata-rata, jumlah
  transmisi, bar warna; diurutkan/diklik header kolom.
- **Manajemen Selektif**: checkbox multi-select per kanal, tombol *Hapus Terpilih*,
  dan tombol *Hapus Semua* (membersihkan database SQLite dan memori akumulator live).
- Ekspor Excel/CSV, reset data statistik (admin).

### Rekaman

- Daftar rekaman audio hasil deteksi, putar langsung di browser, unduh.
- **Manajemen Selektif**: checkbox multi-select rekaman WAV, tombol *Hapus Terpilih*,
  dan tombol *Hapus Semua* dengan proteksi anti-path traversal.
- Filter berdasarkan frekuensi/nama file.

### Jadwal

- Kampanye survei terjadwal (waktu mulai, durasi, pengulangan daily/weekly),
  mulai manual, status aktif & sisa waktu.
- Riwayat eksekusi dengan ringkasan okupansi & jumlah rekaman.

### Admin & Perangkat

- Manajemen pengguna (buat/ubah/hapus, nonaktifkan), ganti kata sandi sendiri,
  sinkronisasi konfigurasi RF dari server.
- **Modal Pemilihan Perangkat**: diagnostik driver DLL `sa_api.dll`, status USB,
  pilihan instrumen fisik auto-detect / serial tertentu / simulator, serta tombol
  *Pindai Ulang USB*. Jika kabel USB belum tersambung, modal memberikan panduan
  diagnostik yang jelas.
- Sinkronisasi kata sandi otomatis dari `.env` saat aplikasi dijalankan.

### Responsivitas

- Desktop: panel spektrum lebar, waterfall samping, panel kontrol di kanan.
- Tablet: 2 kolom, panel kontrol digeser dari samping.
- Ponsel: 1 kolom, kontrol sebagai laci bawah, navigasi bottom bar.

---

## API Endpoints Ringkas

| Endpoint | Akses minimal | Deskripsi |
|---|---|---|
| `POST /api/auth/login` | — | Login → JWT access token |
| `GET /api/auth/me` | viewer | Profil pengguna saat ini |
| `GET/PUT/DELETE /api/auth/users` | admin | Manajemen pengguna |
| `GET /api/rf/status` | viewer | Status perangkat, mesin, suhu |
| `GET /api/rf/presets` | viewer | Daftar preset pita |
| `POST /api/rf/preset` | operator | Ubah preset aktif |
| `POST /api/rf/sweep` | operator | Setel center/span/RBW/ref/atten |
| `POST /api/rf/listen` | operator | Demodulasi audio langsung |
| `POST /api/rf/listen/stop` | operator | Hentikan dengar audio seketika |
| `POST /api/rf/autorecord` | operator | Aktifkan/nonaktifkan rekam otomatis |
| `GET /api/rf/devices` | viewer | Diagnostik library driver & enumerasi USB |
| `POST /api/rf/device` | admin | Beralih instrumen fisik ↔ simulator, setel nomor seri |
| `GET /api/rf/config` | viewer | Konfigurasi sweep/audio/okupansi/perangkat aktif |
| `POST /api/rf/start` | operator | Mulai capture engine |
| `POST /api/rf/stop` | operator | Hentikan capture engine |
| `GET /api/occupancy/stats` | viewer | Statistik okupansi live |
| `POST /api/occupancy/delete` | operator | Hapus kanal okupansi terpilih (bulk) |
| `POST /api/occupancy/reset` | admin | Reset seluruh statistik okupansi |
| `GET /api/recordings` | viewer | Daftar rekaman |
| `GET /api/recordings/{fn}/stream|download` | viewer | Putar/unduh WAV |
| `DELETE /api/recordings/{fn}` | operator | Hapus rekaman tunggal |
| `POST /api/recordings/bulk-delete` | operator | Hapus beberapa rekaman sekaligus |
| `POST /api/recordings/delete-all` | operator | Hapus seluruh rekaman WAV |
| `GET/POST /api/schedules` | viewer | Daftar & buat jadwal |
| `POST /api/schedules/{id}/start-now` | operator | Mulai survei manual |
| `POST /api/schedules/cancel-active` | operator | Hentikan survei aktif |
| `DELETE /api/schedules/{id}` | admin | Hapus jadwal |
| `GET /api/reports/excel|csv` | viewer | Ekspor laporan |
| `WS /ws/spectrum` | viewer | Frame spektrum float32 biner |
| `WS /ws/audio` | viewer | PCM float32 live audio |
| `WS /ws/events` | viewer | Event JSON (rekam mulai/selesai) |

---

## Uji Coba

```bash
# Engine end-to-end (dengan simulator)
python tests/smoke_engine.py

# Keamanan API (RBAC, rate limiting, path traversal, WebSocket auth)
python tests/test_security_api.py

# Retune audio tanpa interupsi stream (drag-to-tune)
python tests/test_retune.py

# Integritas scope & fungsi frontend JavaScript (mencegah ReferenceError)
python tests/test_js_scope.py
node tests/test_js_dom.js
```

Uji keamanan mencakup lebih dari 100 pemeriksaan: header keamanan, autentikasi,
RBAC per peran, penolakan akses tanpa token, penolakan kata sandi salah, proteksi
path traversal (termasuk payload ter-encode `%2F`/`%5C`), pembatasan laju login
(5×/menit → HTTP 429), pembatalan token JWT setelah ganti kata sandi, dan
penolakan koneksi WebSocket tanpa token.

---

## Lisensi

Dirilis di bawah [MIT License](LICENSE). Anda bebas menggunakan, memodifikasi,
dan mendistribusikan ulang perangkat lunak ini, dengan atau tanpa perubahan,
asalkan notice hak cipta tetap disertakan.

Penggunaan untuk pemantauan spektrum pribadi/profesional harus mematuhi regulasi
frekuensi radio yang berlaku di yurisdiksi Anda.
