@echo off

title Prism - SA124B Spectrum Occupancy ^& Audio Recorder

echo ======================================================================
echo   PRISM - Signal Hound SA124B Web Dashboard ^& Automation
echo ======================================================================
echo.

:: Pindah ke direktori skrip ini berada
cd /d "%~dp0"

:: Cari executable Python sistem
set "SYS_PYTHON="
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "SYS_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
) else (
    for /f "delims=" %%I in ('where python 2^>nul') do (
        if not defined SYS_PYTHON set "SYS_PYTHON=%%I"
    )
)

if not defined SYS_PYTHON (
    echo [ERROR] Python tidak ditemukan di sistem.
    echo Pasang Python 3.10+ dari https://www.python.org/
    pause
    exit /b 1
)

:: Siapkan virtual environment otomatis (.venv) jika belum ada
if not exist ".venv\Scripts\python.exe" (
    echo [*] Menyiapkan virtual environment Python di folder .venv ...
    "%SYS_PYTHON%" -m venv .venv
)

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=%SYS_PYTHON%"
)

echo [*] Menggunakan Python: "%PYTHON_EXE%"

:: Periksa dependensi utama (fastapi, uvicorn, numpy)
"%PYTHON_EXE%" -c "import fastapi, uvicorn, numpy" >nul 2>nul
if errorlevel 1 (
    echo [*] Memasang dependensi dari requirements.txt...
    "%PYTHON_EXE%" -m pip install -r requirements.txt
)

:: Buat file .env awal jika belum ada
if not exist ".env" (
    echo [*] Membuat file .env awal dari .env.example...
    copy /y ".env.example" ".env" >nul
)

:: Buat direktori runtime jika belum ada
if not exist "data" mkdir data
if not exist "recordings" mkdir recordings
if not exist "reports" mkdir reports
if not exist "logs" mkdir logs

:: BEKAS: pembacaan .env lewat batch "for /f" dihapus di sini.
:: Dengan setlocal enabledelayedexpansion aktif, CMD memperlakukan "!" sebagai
:: penanda ekspansi tertunda dan memotongnya dari nilai variabel -- sehingga
:: ADMIN_PASSWORD=Admin123456! tiba di server sebagai Admin123456 dan login
:: admin gagal. Pembacaan .env dilakukan sepenuhnya oleh Python
:: (config.py:_load_dotenv), yang membaca berkas UTF-8 utuh apa adanya.

:: Pengaturan host dan port default (hanya untuk banner konsol)
if "%SA_HOST%"=="" set SA_HOST=0.0.0.0
if "%SA_PORT%"=="" set SA_PORT=3487

echo.
echo [*] Memulai server Prism...
echo [*] Akses dashboard melalui browser:
echo       Lokal:       http://localhost:%SA_PORT%
echo       Jaringan:    http://0.0.0.0:%SA_PORT%
echo.
echo Tekan Ctrl+C untuk menghentikan server.
echo ======================================================================
echo.

"%PYTHON_EXE%" server.py

if errorlevel 1 (
    echo.
    echo [ERROR] Server berhenti dengan kode kesalahan %errorlevel%.
    pause
)
