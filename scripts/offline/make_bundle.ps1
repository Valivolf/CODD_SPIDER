# Offline update bundle builder for Python CODD_SPIDER (no browser, no AI)
# Automatically starts Docker Desktop if not running and builds lan-calendar-app-<stamp>.tar
# Usage:
#   .\scripts\offline\make_bundle.cmd
#   .\scripts\offline\make_bundle.cmd app [output_dir]

param(
    [Parameter(Position = 0)]
    [string]$What = "app",

    [Parameter(Position = 1)]
    [string]$OutDir = ""
)

$ErrorActionPreference = "Continue"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $RepoRoot

if ([string]::IsNullOrWhiteSpace($OutDir)) {
    $OutDir = Join-Path $RepoRoot "offline-out"
}
if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}
$OutDir = (Resolve-Path $OutDir).Path

function Test-DockerEngine {
    cmd /c "docker info >nul 2>nul"
    return ($LASTEXITCODE -eq 0)
}

function Ensure-DockerRunning {
    if (Test-DockerEngine) {
        Write-Host "[Docker] Engine is running." -ForegroundColor Green
        return
    }

    Write-Host "[Docker] Docker Engine is not running. Searching for Docker Desktop..." -ForegroundColor Yellow
    $Candidates = @(
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
        "C:\Program Files\Docker\Docker\Docker Desktop.exe",
        (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
    )
    foreach ($p in $Candidates) {
        if ($p -and (Test-Path $p)) {
            Write-Host "[Docker] Starting Docker Desktop: $p" -ForegroundColor Cyan
            Start-Process -FilePath $p
            break
        }
    }

    Write-Host "[Docker] Waiting for Docker Engine daemon (up to 120 seconds)..." -ForegroundColor Cyan
    for ($i = 1; $i -le 40; $i++) {
        Start-Sleep -Seconds 3
        if (Test-DockerEngine) {
            Write-Host ""
            Write-Host "[Docker] Docker Engine is online and ready!" -ForegroundColor Green
            return
        }
        Write-Host -NoNewline "."
    }

    Write-Host ""
    Write-Host "ERROR: Docker Engine did not start within 120 seconds. Start Docker Desktop manually." -ForegroundColor Red
    exit 1
}

function Build-AppBundle {
    $AppStamp = Get-Date -Format "yyyyMMdd-HHmm"
    $B = Join-Path $OutDir "lan-calendar-app"
    if (Test-Path $B) {
        Remove-Item -Path $B -Recurse -Force
    }
    New-Item -ItemType Directory -Path (Join-Path $B "docker") -Force | Out-Null

    $Dockerfile = "docker/Dockerfile"
    if (-not (Test-Path (Join-Path $RepoRoot $Dockerfile))) {
        $Dockerfile = "Dockerfile"
    }

    Write-Host "[app 1/3] Building Python app image lan-calendar:latest (without browser and AI)..." -ForegroundColor Cyan
    docker build -f $Dockerfile -t lan-calendar:latest .
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to build lan-calendar:latest" -ForegroundColor Red
        exit 1
    }

    # Update local containers if already running on this machine (without robot/ai profiles)
    cmd /c "docker inspect lan-calendar >nul 2>nul"
    if ($LASTEXITCODE -eq 0) {
        if (Test-Path (Join-Path $RepoRoot "docker\docker-compose.local.yml")) {
            docker compose -f docker/docker-compose.yml -f docker/docker-compose.local.yml up -d --no-build lan-calendar codd-bot
        } else {
            docker compose -f docker/docker-compose.yml up -d --no-build lan-calendar codd-bot
        }
    }

    Write-Host "[app 2/3] Saving image lan-calendar:latest..." -ForegroundColor Cyan
    docker save -o (Join-Path $B "lan-calendar.tar") lan-calendar:latest
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to save lan-calendar.tar" -ForegroundColor Red
        exit 1
    }

    Write-Host "[app 3/3] Packaging update archive (without browser and AI)..." -ForegroundColor Cyan
    $DockerSrc = Join-Path $RepoRoot "docker"
    if (Test-Path $DockerSrc) {
        Get-ChildItem -Path $DockerSrc -File | Where-Object {
            $_.Name -notmatch '\.tar(\.gz)?$' -and $_.Name -notmatch '\.bak'
        } | ForEach-Object {
            Copy-Item $_.FullName (Join-Path $B "docker\$($_.Name)") -Force
        }
    }

    Copy-Item (Join-Path $RepoRoot "scripts\offline\install_app.sh") (Join-Path $B "install.sh") -Force
    Copy-Item (Join-Path $RepoRoot "scripts\offline\selftest.sh") (Join-Path $B "selftest.sh") -Force
    $ReadmeSrc = Join-Path $RepoRoot "scripts\offline\README_app.txt"
    if (Test-Path $ReadmeSrc) {
        Copy-Item $ReadmeSrc (Join-Path $B "README.txt") -Force
    }

    $ArchiveName = "lan-calendar-app-$AppStamp.tar"
    $LinuxCmd = @"
set -eu
B=/out/lan-calendar-app
echo '$AppStamp' > "`$B/BUILD"

for f in "`$B/install.sh" "`$B/selftest.sh" "`$B/README.txt" "`$B/BUILD" "`$B"/docker/*; do
  if [ -f "`$f" ]; then
    sed -i 's/\r$//' "`$f"
  fi
done
chmod +x "`$B/install.sh" "`$B/selftest.sh"
[ -f "`$B/docker/caddy-entrypoint.sh" ] && chmod +x "`$B/docker/caddy-entrypoint.sh"

gzip -3 -f "`$B/lan-calendar.tar"

cd /app
dirs=""
for d in app bot static templates; do
  [ -d "`$d" ] && dirs="`$dirs `$d"
done
: > "`$B/CODE.sha256"
if [ -n "`$dirs" ]; then
  find `$dirs -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum >> "`$B/CODE.sha256"
fi
for f in requirements.txt pyproject.toml main.py; do
  [ -f "`$f" ] && sha256sum "`$f" >> "`$B/CODE.sha256"
done

cd "`$B"
files="lan-calendar.tar.gz install.sh selftest.sh BUILD CODE.sha256"
[ -f README.txt ] && files="`$files README.txt"
for f in docker/*; do
  [ -f "`$f" ] && files="`$files `$f"
done
sha256sum `$files > SHA256SUMS

tar -C /out -cf "/out/$ArchiveName" lan-calendar-app
"@

    $LinuxCmdClean = $LinuxCmd -replace "`r", ""
    docker run --rm -v "${OutDir}:/out" lan-calendar:latest sh -c $LinuxCmdClean
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to finalize update archive inside container" -ForegroundColor Red
        exit 1
    }

    $ArchivePath = Join-Path $OutDir $ArchiveName
    $SizeMb = [math]::Round(((Get-Item $ArchivePath).Length / 1MB), 1)
    Write-Host ""
    Write-Host "==================================================================" -ForegroundColor Green
    Write-Host " Python update bundle ready (no browser, no AI): $ArchivePath ($SizeMb MB)" -ForegroundColor Green
    Write-Host " On Linux server run:" -ForegroundColor Green
    Write-Host "   tar -xf $ArchiveName && cd lan-calendar-app && sudo bash install.sh" -ForegroundColor Yellow
    Write-Host "==================================================================" -ForegroundColor Green
}

switch ($What.ToLower()) {
    "app"    { Ensure-DockerRunning; Build-AppBundle }
    "update" { Ensure-DockerRunning; Build-AppBundle }
    ""       { Ensure-DockerRunning; Build-AppBundle }
    default {
        Write-Host "Usage: .\make_bundle.cmd [app|update] [output_dir]" -ForegroundColor Yellow
        exit 1
    }
}