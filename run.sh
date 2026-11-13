#!/usr/bin/env bash
# ======================================================================
#   PRISM — Signal Hound SA124B Web Dashboard & Automation (Linux)
# ======================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "======================================================================"
echo "  PRISM — Signal Hound SA124B Web Dashboard & Automation"
echo "======================================================================"

# Deteksi Python 3 sistem
SYSTEM_PYTHON=""
if command -v python3 &>/dev/null; then
    SYSTEM_PYTHON="python3"
elif command -v python &>/dev/null; then
    SYSTEM_PYTHON="python"
else
    echo "[ERROR] Python 3 tidak ditemukan. Pasang Python 3.10+ terlebih dahulu."
    exit 1
fi

# Siapkan virtual environment otomatis (.venv) untuk isolasi bersih
if [ ! -d ".venv" ]; then
    echo "[*] Membuat virtual environment (.venv)..."
    "$SYSTEM_PYTHON" -m venv .venv || {
        echo "[WARN] Gagal membuat .venv via venv module, menggunakan Python sistem."
    }
fi

if [ -f ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
    PIP_BIN=".venv/bin/pip"
else
    PYTHON_BIN="$SYSTEM_PYTHON"
    PIP_BIN="pip"
fi

echo "[*] Menggunakan Python: $($PYTHON_BIN --version 2>&1) ($PYTHON_BIN)"

# Periksa dependensi utama (fastapi, uvicorn, numpy)
if ! "$PYTHON_BIN" -c "import fastapi, uvicorn, numpy" &>/dev/null; then
    echo "[*] Memasang dependensi dari requirements.txt..."
    "$PYTHON_BIN" -m pip install --upgrade pip >/dev/null 2>&1 || true
    "$PYTHON_BIN" -m pip install -r requirements.txt
fi

# Buat direktori runtime jika belum ada
mkdir -p data recordings reports logs

# Pastikan file .env ada
if [ ! -f ".env" ]; then
    echo "[*] Membuat .env awal dari .env.example..."
    cp .env.example .env
fi

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
export SA_PORT="${SA_PORT:-3487}"

echo ""
echo "[*] Memulai server Prism..."
echo "[*] Akses dashboard melalui browser:"
echo "      Lokal:       http://localhost:${SA_PORT}"
echo "      Jaringan:    http://${SA_HOST}:${SA_PORT}"
echo ""
echo "Tekan Ctrl+C untuk menghentikan server."
echo "======================================================================"
echo ""

exec "$PYTHON_BIN" server.py
