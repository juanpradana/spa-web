@echo off

title Handfish — SA124B Spectrum Occupancy ^& Audio Recorder

echo ======================================================================
echo   HANDFISH — Signal Hound SA124B Web Dashboard ^& Automation
echo ======================================================================
echo.

:: Pindah ke direktori skrip ini berada
cd /d "%~dp0"

:: Cari executable Python
set "PYTHON_EXE="
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
) else (
    for /f "delims=" %%I in ('where python 2^>nul') do (
        if not defined PYTHON_EXE set "PYTHON_EXE=%%I"
    )
)

if not defined PYTHON_EXE (
    echo [ERROR] Python tidak ditemukan di sistem.
    echo Pasang Python 3.10+ dari https://www.python.org/
    pause
    exit /b 1
)

echo [*] Menggunakan Python: "%PYTHON_EXE%"

:: Buat direktori runtime jika belum ada
if not exist "data" mkdir data
if not exist "recordings" mkdir recordings
if not exist "reports" mkdir reports
if not exist "logs" mkdir logs

:: BEKAS: pembacaan .env lewat batch "for /f" dihapus di sini.
:: Dengan setlocal enabledelayedexpansion aktif, CMD memperlakukan "!" sebagai
:: penanda ekspansi tertunda dan memotongnya dari nilai variabel — sehingga
:: ADMIN_PASSWORD=Admin123456! tiba di server sebagai Admin123456 dan login
:: admin gagal. Pembacaan .env dilakukan sepenuhnya oleh Python
:: (config.py:_load_dotenv), yang membaca berkas UTF-8 utuh apa adanya.

:: Pengaturan host dan port default (hanya untuk banner konsol)
if "%SA_HOST%"=="" set SA_HOST=0.0.0.0
if "%SA_PORT%"=="" set SA_PORT=8000

echo.
echo [*] Memulai server Handfish...
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
