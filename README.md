# HANDFISH — Pemantau Okupansi Spektrum & Perekam Audio SA124B

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
   cd handfish
   .\run.bat
   ```

3. Buka browser: `http://localhost:8000`

Server membuat akun `admin` secara otomatis. Kata sandi awal hanya dicetak ke
konsol **satu kali**, saat basis data pengguna pertama kali dibuat. Akun itu
ditandai *harus ganti kata sandi*, sehingga jendela **Ganti Kata Sandi** menutupi
seluruh aplikasi setiap kali login sampai kata sandi baru ditetapkan; sesudah itu
sesi ditutup dan Anda login ulang dengan kata sandi baru. Kata sandi juga dapat
diganti kapan saja lewat **Admin → Ganti Kata Sandi**.

## Instalasi Native (Linux / Raspberry Pi)

```
cd handfish
chmod +x run.sh
./run.sh
```

Akses via browser pada host: `http://<ip-host>:8000`.

## Instalasi Docker Compose

```
# Mode dasar (HTTP, port 8000)
docker compose up -d

# Mode production + HTTPS otomatis (Let's Encrypt)
docker compose --profile proxy up -d
```

variabel konfigurasi (opsional): `SA_HOST`, `SA_PORT`, `SECRET_KEY`,
`ADMIN_PASSWORD`, `SA_SIMULATE`, `SA_DEVICE_SERIAL`, `DOMAIN`.

Untuk panduan lengkap, lihat bagian *Deployment* di bawah.

## Struktur Proyek

```
handfish/
├── auth.py          # Autentikasi JWT HS256, hashing PBKDF2-SHA256, RBAC
├── security.py      # Rate limiter, sanitasi path, security headers, audit log
├── config.py        # Konfigurasi & override environment
├── sa124.py         # Binding C API SA124B atau hardware simulator
├── engine.py        # CaptureEngine: scan-and-dwell, WebSocket broadcast
├── channels.py      # 13 preset pita + generator kanal per preset
├── recorder.py      # Pembuat, pengolah, & penghapus rekaman WAV
├── occupancy.py     # Akumulator & statistik okupansi kanal (SQLite WAL)
├── scheduler.py     # Penjadwal kampanye survei okupansi
├── reports.py       # Ekspor laporan Excel/CSV
├── server.py        # FastAPI REST API + WebSocket streaming
├── static/          # Single-page app (HTML/CSS/JS)
├── tests/           # Uji engine end-to-end & uji keamanan API
├── data/            # Database SQLite & kunci rahasia (diabaikan git)
├── recordings/      # Berkas WAV hasil perekaman   (diabaikan git)
├── reports/         # Berkas ekspor Excel/CSV     (diabaikan git)
├── logs/            # Audit log & log aplikasi    (diabaikan git)
├── Dockerfile
├── docker-compose.yml
├── Caddyfile
├── requirements.txt
├── .gitignore
├── .env.example
├── run.bat
├── run.sh
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

### Mode Dasar

```
docker compose up -d
```

- Container `handfish-app` terbuka di port 8000.
- Volume persisten: `./data` (database & kredensial), `./recordings`, `./reports`,
  `./logs` — datanya bertahan meskipun container dihapus/dibuat ulang.
- Passthrough USB (`/dev/bus/usb`) aktif bila host adalah Linux.

### Mode Production dengan Caddy Reverse Proxy

```
docker compose --profile proxy up -d
```

1. Atur `DOMAIN=your.domain.com` di `.env` atau lewat environment.
2. Jalankan dengan profil `proxy` untuk memulai Caddy.
3. Caddy menerbitkan sertifikat Let's Encrypt otomatis untuk domain yang Anda
   setel. Dashboard dapat diakses via `https://your.domain.com`.

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

```
# Engine end-to-end (dengan simulator)
python tests/smoke_engine.py

# Keamanan API (RBAC, rate limiting, path traversal, WebSocket auth)
python tests/test_security_api.py
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
