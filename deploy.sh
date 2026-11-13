#!/usr/bin/env bash
# ======================================================================
#   PRISM — Zero-Friction One-Command Deployer (Linux / macOS)
# ======================================================================
#   Penggunaan:
#     ./deploy.sh                # Deploy instan (universal simulator / host fallback)
#     ./deploy.sh --hardware     # Aktifkan passthrough USB SA124B fisik
#     ./deploy.sh --proxy        # Aktifkan reverse proxy HTTPS Caddy (port 80/443)
#     ./deploy.sh --tunnel       # Aktifkan Cloudflare Tunnel (CGNAT/4G/Starlink)
#     ./deploy.sh --build        # Rebuild container image dari Dockerfile
#     ./deploy.sh --down         # Hentikan semua container
#     ./deploy.sh --logs         # Pantau log realtime
#     ./deploy.sh --status       # Cek status container
# ======================================================================

set -euo pipefail

cd "$(dirname "$0")"

echo "======================================================================"
echo "  PRISM — Signal Hound SA124B Zero-Friction Deployer"
echo "======================================================================"

# 1. Pastikan Docker dan Docker Compose terpasang
if ! command -v docker &> /dev/null; then
    echo "[ERROR] Docker belum terpasang di sistem ini."
    echo "Silakan pasang Docker Engine: https://docs.docker.com/engine/install/"
    exit 1
fi

# Tentukan perintah docker compose (plugin v2 atau standalone)
DOCKER_COMPOSE="docker compose"
if ! docker compose version &> /dev/null; then
    if command -v docker-compose &> /dev/null; then
        DOCKER_COMPOSE="docker-compose"
    else
        echo "[ERROR] Docker Compose belum terpasang."
        exit 1
    fi
fi

# 2. Inisialisasi file .env otomatis jika belum ada
if [ ! -f .env ]; then
    echo "[*] File .env belum ditemukan. Membuat otomatis dari .env.example..."
    cp .env.example .env
fi

# 3. Otomatis generate SECRET_KEY jika masih kosong di .env
if grep -q '^SECRET_KEY=$' .env || grep -q '^SECRET_KEY=""$' .env || ! grep -q '^SECRET_KEY=' .env; then
    echo "[*] Menghasilkan SECRET_KEY acak untuk keamanan sesi..."
    NEW_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))" 2>/dev/null || \
                openssl rand -base64 36 2>/dev/null || \
                LC_ALL=C tr -dc 'A-Za-z0-9_=-' < /dev/urandom | head -c 48 || true)
    if [ -n "$NEW_SECRET" ]; then
        if grep -q '^SECRET_KEY=' .env; then
            sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${NEW_SECRET}|" .env
        else
            echo "SECRET_KEY=${NEW_SECRET}" >> .env
        fi
    fi
fi

# 4. Otomatis siapkan ADMIN_PASSWORD jika masih kosong di .env
if grep -q '^ADMIN_PASSWORD=$' .env || grep -q '^ADMIN_PASSWORD=""$' .env || ! grep -q '^ADMIN_PASSWORD=' .env; then
    DEFAULT_PASS="Admin123456!"
    echo "[*] Menetapkan kata sandi admin awal di .env: ${DEFAULT_PASS}"
    if grep -q '^ADMIN_PASSWORD=' .env; then
        sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${DEFAULT_PASS}|" .env
    else
        echo "ADMIN_PASSWORD=${DEFAULT_PASS}" >> .env
    fi
fi

# 5. Pastikan direktori volume lokal tersedia
mkdir -p data recordings reports logs

# 6. Parse Argumen Baris Perintah
COMPOSE_FILES=("-f" "docker-compose.yml")
PROFILES=()
BUILD_FLAG=""
ACTION="up"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --hardware|-hw)
            if [ -f "docker-compose.hardware.yml" ]; then
                COMPOSE_FILES+=("-f" "docker-compose.hardware.yml")
                echo "[*] Mode Perangkat Keras aktif (USB passthrough /dev/bus/usb diaktifkan)"
            else
                echo "[WARN] docker-compose.hardware.yml tidak ditemukan, melanjutkan tanpa passthrough."
            fi
            shift
            ;;
        --proxy)
            PROFILES+=("--profile" "proxy")
            echo "[*] Reverse Proxy Caddy aktif (HTTPS otomatis di port 80/443)"
            shift
            ;;
        --tunnel)
            PROFILES+=("--profile" "tunnel")
            echo "[*] Cloudflare Tunnel aktif (akses remote tanpa IP publik)"
            shift
            ;;
        --build)
            BUILD_FLAG="--build"
            shift
            ;;
        --down)
            ACTION="down"
            shift
            ;;
        --logs)
            ACTION="logs"
            shift
            ;;
        --status|--ps)
            ACTION="ps"
            shift
            ;;
        --restart)
            ACTION="restart"
            shift
            ;;
        --help|-h)
            echo "Penggunaan: ./deploy.sh [OPSI]"
            echo "  --hardware, -hw   Aktifkan passthrough USB SA124B fisik ke container"
            echo "  --proxy           Aktifkan Caddy reverse proxy HTTPS (port 80/443)"
            echo "  --tunnel          Aktifkan Cloudflare Tunnel untuk akses remote CGNAT"
            echo "  --build           Paksa rebuild container image dari Dockerfile"
            echo "  --down            Hentikan dan bersihkan container Prism"
            echo "  --logs            Pantau keluaran log container secara langsung"
            echo "  --status          Lihat status container aktif"
            echo "  --restart         Restart container Prism"
            exit 0
            ;;
        *)
            echo "[WARN] Argumen tidak dikenal: $1 (diabaikan)"
            shift
            ;;
    esac
done

# 7. Eksekusi Perintah Sesuai Aksi
if [ "$ACTION" = "down" ]; then
    echo "[*] Menghentikan container Prism..."
    $DOCKER_COMPOSE "${COMPOSE_FILES[@]}" "${PROFILES[@]}" down
    echo "[✓] Container berhasil dihentikan."
    exit 0
elif [ "$ACTION" = "logs" ]; then
    $DOCKER_COMPOSE "${COMPOSE_FILES[@]}" "${PROFILES[@]}" logs -f
    exit 0
elif [ "$ACTION" = "ps" ]; then
    $DOCKER_COMPOSE "${COMPOSE_FILES[@]}" "${PROFILES[@]}" ps
    exit 0
elif [ "$ACTION" = "restart" ]; then
    echo "[*] Merestart container Prism..."
    $DOCKER_COMPOSE "${COMPOSE_FILES[@]}" "${PROFILES[@]}" restart
    echo "[✓] Container berhasil direstart."
    exit 0
fi

# Jalankan docker compose up
echo "[*] Menjalankan Prism via Docker Compose..."
$DOCKER_COMPOSE "${COMPOSE_FILES[@]}" "${PROFILES[@]}" up -d $BUILD_FLAG

# 8. Ekstraksi Informasi Port & Kredensial untuk Tampilan Pengguna
PORT=$(grep -E '^SA_PORT=' .env 2>/dev/null | cut -d '=' -f2 | tr -d '\r"' || echo "3487")
PORT=${PORT:-3487}

PASSWORD=$(grep -E '^ADMIN_PASSWORD=' .env 2>/dev/null | cut -d '=' -f2- | tr -d '\r"' || echo "Admin123456!")
PASSWORD=${PASSWORD:-Admin123456!}

HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "IP_SERVER")

echo ""
echo "======================================================================"
echo "  [✓] DEPLOYMENT BERHASIL & PRISM AKTIF!"
echo "======================================================================"
echo "  Dashboard Web:     http://localhost:${PORT}"
if [ -n "$HOST_IP" ] && [ "$HOST_IP" != "IP_SERVER" ]; then
    echo "  Akses Jaringan:    http://${HOST_IP}:${PORT}"
fi
echo ""
echo "  Kredensial Login Administrator:"
echo "    Username:        admin"
echo "    Password:        ${PASSWORD}"
echo ""
echo "  Perintah Bantuan Singkat:"
echo "    Pantau log:      ./deploy.sh --logs"
echo "    Cek status:      ./deploy.sh --status"
echo "    Hentikan:        ./deploy.sh --down"
echo "======================================================================"
echo ""
