# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

# Label metadata
LABEL maintainer="Prism Project"
LABEL description="Signal Hound SA124B Spectrum Occupancy & Audio Automation Dashboard"

# Hindari buffering output Python dan penulisan file .pyc
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SA_DATA_DIR=/app/data \
    SA_RECORDINGS_DIR=/app/recordings \
    SA_REPORTS_DIR=/app/reports \
    SA_LOGS_DIR=/app/logs \
    SA_HOST=0.0.0.0 \
    SA_PORT=3487
# Catatan: SA_PORT di atas adalah default port INTERNAL container. Nilainya
# dapat dioverride lewat environment compose (docker-compose.yml) atau .env.
# Port host dipetakan di compose, tidak di sini.

# Pasang pustaka sistem: libusb untuk akses FTDI / SA124, udev, curl untuk healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    libusb-1.0-0 \
    udev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Salin daftar dependensi terlebih dahulu agar layer caching optimal
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Buat direktori kerja & runtime
RUN mkdir -p /app/data /app/recordings /app/reports /app/logs /app/static

# Salin kode aplikasi
COPY . .

# Buat pengguna non-root demi prinsip least-privilege (keamanan)
# Tambahkan ke grup dialout dan plugdev agar berhak mengakses node USB /dev/bus/usb
RUN groupadd -r appgroup && \
    useradd -r -g appgroup -G dialout,plugdev -s /sbin/nologin appuser && \
    chown -R appuser:appgroup /app

USER appuser

EXPOSE 3487

# Healthcheck berbasis HTTP REST API endpoint root /
# Port dinamis membaca SA_PORT (default 3487).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://127.0.0.1:${SA_PORT:-3487}/ || exit 1

ENTRYPOINT ["python", "server.py"]
