@echo off
setlocal DisableDelayedExpansion

title Prism - Zero-Friction One-Command Deployer (Windows)

echo ======================================================================
echo   PRISM - Signal Hound SA124B Zero-Friction Deployer (Windows)
echo ======================================================================
echo.

cd /d "%~dp0"

:: 1. Periksa ketersediaan Docker
where docker >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Docker tidak ditemukan di PATH sistem Anda.
    echo Pastikan Docker Desktop for Windows sudah terpasang dan berjalan:
    echo https://docs.docker.com/desktop/install/windows-install/
    pause
    exit /b 1
)

:: 2. Inisialisasi berkas .env jika belum ada
if not exist ".env" (
    echo [*] File .env belum ditemukan. Membuat otomatis dari .env.example...
    copy /y ".env.example" ".env" >nul
)

:: 3. Jalankan helper PowerShell untuk memastikan SECRET_KEY dan ADMIN_PASSWORD aman
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$envFile = '.env';" ^
    "if (Test-Path $envFile) {" ^
    "    $lines = Get-Content $envFile -Encoding UTF8;" ^
    "    $hasSecret = $false; $hasPass = $false; $updated = $false;" ^
    "    $newLines = @();" ^
    "    foreach ($line in $lines) {" ^
    "        if ($line -match '^SECRET_KEY=(.*)$') {" ^
    "            $hasSecret = $true;" ^
    "            if ([string]::IsNullOrWhiteSpace($matches[1])) {" ^
    "                $token = [System.Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(36)).Replace('+','-').Replace('/','_').TrimEnd('=');" ^
    "                $newLines += 'SECRET_KEY=' + $token;" ^
    "                $updated = $true;" ^
    "            } else { $newLines += $line; }" ^
    "        } elseif ($line -match '^ADMIN_PASSWORD=(.*)$') {" ^
    "            $hasPass = $true;" ^
    "            if ([string]::IsNullOrWhiteSpace($matches[1])) {" ^
    "                $newLines += 'ADMIN_PASSWORD=Admin123456!';" ^
    "                $updated = $true;" ^
    "            } else { $newLines += $line; }" ^
    "        } else {" ^
    "            $newLines += $line;" ^
    "        }" ^
    "    }" ^
    "    if (-not $hasSecret) { $newLines += 'SECRET_KEY=' + [System.Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(36)).Replace('+','-').Replace('/','_').TrimEnd('='); $updated = $true; }" ^
    "    if (-not $hasPass) { $newLines += 'ADMIN_PASSWORD=Admin123456!'; $updated = $true; }" ^
    "    if ($updated) { Set-Content $envFile $newLines -Encoding UTF8; Write-Host '[*] .env diperbarui dengan SECRET_KEY dan kata sandi admin otomatis.' }" ^
    "}"

:: 4. Buat folder penyimpanan persisten jika belum ada
if not exist "data" mkdir data
if not exist "recordings" mkdir recordings
if not exist "reports" mkdir reports
if not exist "logs" mkdir logs

:: 5. Evaluasi parameter masukan
set "PROFILES="
set "BUILD_ARG="
set "ACTION=up"

:parse_args
if "%~1"=="" goto after_args

if /i "%~1"=="--proxy" (
    set "PROFILES=--profile proxy"
    echo [*] Profil Caddy Reverse Proxy diaktifkan.
    shift
    goto parse_args
)
if /i "%~1"=="--tunnel" (
    set "PROFILES=--profile tunnel"
    echo [*] Profil Cloudflare Tunnel diaktifkan.
    shift
    goto parse_args
)
if /i "%~1"=="--build" (
    set "BUILD_ARG=--build"
    echo [*] Mode rebuild container image diaktifkan.
    shift
    goto parse_args
)
if /i "%~1"=="--down" (
    set "ACTION=down"
    shift
    goto parse_args
)
if /i "%~1"=="--logs" (
    set "ACTION=logs"
    shift
    goto parse_args
)
if /i "%~1"=="--status" (
    set "ACTION=status"
    shift
    goto parse_args
)
if /i "%~1"=="--ps" (
    set "ACTION=status"
    shift
    goto parse_args
)
if /i "%~1"=="--restart" (
    set "ACTION=restart"
    shift
    goto parse_args
)

shift
goto parse_args

:after_args

:: 6. Jalankan aksi Docker Compose
if /i "%ACTION%"=="down" (
    echo [*] Menghentikan container Prism...
    docker compose %PROFILES% down
    echo [OK] Container berhasil dihentikan.
    exit /b 0
)

if /i "%ACTION%"=="logs" (
    docker compose %PROFILES% logs -f
    exit /b 0
)

if /i "%ACTION%"=="status" (
    docker compose %PROFILES% ps
    exit /b 0
)

if /i "%ACTION%"=="restart" (
    echo [*] Merestart container Prism...
    docker compose %PROFILES% restart
    echo [OK] Container berhasil direstart.
    exit /b 0
)

:: Jalankan docker compose up -d
echo [*] Menjalankan container Prism via Docker Compose...
docker compose %PROFILES% up -d %BUILD_ARG%

if errorlevel 1 (
    echo.
    echo [ERROR] Gagal menjalankan Docker Compose.
    pause
    exit /b 1
)

:: 7. Tampilkan banner ringkasan akses & kredensial
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$envFile = '.env';" ^
    "$port = '3487';" ^
    "$pass = 'Admin123456!';" ^
    "if (Test-Path $envFile) {" ^
    "    foreach ($l in (Get-Content $envFile -Encoding UTF8)) {" ^
    "        if ($l -match '^SA_PORT=(.*)$') { $port = $matches[1].Trim() }" ^
    "        elseif ($l -match '^ADMIN_PASSWORD=(.*)$') { $pass = $matches[1].Trim() }" ^
    "    }" ^
    "}" ^
    "Write-Host '';" ^
    "Write-Host '======================================================================' -ForegroundColor Green;" ^
    "Write-Host '  [OK] DEPLOYMENT BERHASIL & PRISM AKTIF!' -ForegroundColor Green;" ^
    "Write-Host '======================================================================' -ForegroundColor Green;" ^
    "Write-Host ('  Dashboard Web:     http://localhost:' + $port);" ^
    "Write-Host '';" ^
    "Write-Host '  Kredensial Login Administrator:';" ^
    "Write-Host '    Username:        admin';" ^
    "Write-Host ('    Password:        ' + $pass) -ForegroundColor Yellow;" ^
    "Write-Host '';" ^
    "Write-Host '  Perintah Bantuan Singkat:';" ^
    "Write-Host '    Pantau log:      deploy.bat --logs';" ^
    "Write-Host '    Cek status:      deploy.bat --status';" ^
    "Write-Host '    Hentikan:        deploy.bat --down';" ^
    "Write-Host '======================================================================' -ForegroundColor Green;" ^
    "Write-Host '';"

exit /b 0
