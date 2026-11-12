#!/usr/bin/env bash
# ======================================================================
#   HANDFISH — Signal Hound SA124B Web Dashboard & Automation (Linux)
# ======================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "======================================================================"
echo "  HANDFISH — Signal Hound SA124B Web Dashboard & Automation"
echo "======================================================================"

# Deteksi Python 3
PYTHON_BIN=""
if command -v python3 &>/dev/null; then
    PYTHON_BIN="python3"
elif command -v python &>/dev/null; then
    PYTHON_BIN="python"
else
    echo "[ERROR] Python 3 tidak ditemukan. Pasang Python 3.10+ terlebih dahulu."
    exit 1
fi

echo "[*] Menggunakan Python: $($PYTHON_BIN --version)"

# Buat direktori runtime jika belum ada
mkdir -p data recordings reports logs

# Muat file konfigurasi .env jika tersedia
if [ -f ".env" ]; then
    echo "[*] Memuat variabel dari .env"
    set -o allexport
    # shellcheck disable=SC1091
    source .env
    set +o allexport
fi

# Pengaturan default
export SA_HOST="${SA_HOST:-0.0.0.0}"
export SA_PORT="${SA_PORT:-8000}"

echo ""
echo "[*] Memulai server Handfish..."
echo "[*] Akses dashboard melalui browser:"
echo "      Lokal:       http://localhost:${SA_PORT}"
echo "      Jaringan:    http://${SA_HOST}:${SA_PORT}"
echo ""
echo "Tekan Ctrl+C untuk menghentikan server."
echo "======================================================================"
echo ""

exec "$PYTHON_BIN" server.py
