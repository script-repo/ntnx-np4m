#requires -Version 5.1
<#
.SYNOPSIS
    NP4M self-contained Windows installer. No admin, no Start Menu, no
    %LOCALAPPDATA% \u2014 everything lives in the directory you run this from.

.DESCRIPTION
    Bundles Python 3.12 (embeddable distribution) into ./python/ and the repo
    into ./ntnx-np4m-main/, then writes np4m.cmd + _run_np4m.py at the top of
    the folder. To uninstall: delete the folder.

    Folder layout after install:

        <install dir>/
        ├── install.ps1                 (if you downloaded it; optional)
        ├── np4m.cmd                    launcher (double-click to run)
        ├── _run_np4m.py                tiny waitress bootstrap
        ├── python/                     embedded Python 3.12 + pip + site-packages
        └── ntnx-np4m-main/             repo source

    Re-running upgrades the source tree and dependencies in place.

.NOTES
    Usage (PowerShell, no admin):

        mkdir C:\Tools\NP4M
        cd C:\Tools\NP4M
        iwr -useb https://raw.githubusercontent.com/script-repo/ntnx-np4m/main/install.ps1 | iex

    Env overrides:
        $env:NP4M_DIR           Target install dir (default: current directory).
        $env:NP4M_PORT          Listen port (default 5000).
        $env:NP4M_NO_START      Set to '1' to skip auto-launch after install.
        $env:NP4M_PY_VERSION    Python embed version (default 3.12.7).
        $env:NP4M_REPO_ZIP      Source zip URL override.
#>

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

function Write-Log { param([string]$Msg) Write-Host "[np4m] $Msg" -ForegroundColor Cyan }
function Write-Err { param([string]$Msg) Write-Host "[np4m] $Msg" -ForegroundColor Red }

# --- Config ---------------------------------------------------------------
$InstallDir = if ($env:NP4M_DIR) { $env:NP4M_DIR } else { (Get-Location).Path }
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$PyVersion  = if ($env:NP4M_PY_VERSION) { $env:NP4M_PY_VERSION } else { '3.12.7' }
$WebPort    = if ($env:NP4M_PORT)       { [int]$env:NP4M_PORT } else { 5000 }
$RepoZipUrl = if ($env:NP4M_REPO_ZIP)   { $env:NP4M_REPO_ZIP } else { 'https://github.com/script-repo/ntnx-np4m/archive/refs/heads/main.zip' }

$arch = $env:PROCESSOR_ARCHITECTURE
switch ($arch) {
    'AMD64' { $pyArchTag = 'amd64' }
    'ARM64' { $pyArchTag = 'arm64' }
    default {
        Write-Err "Unsupported CPU architecture: $arch. Need AMD64 or ARM64."
        exit 1
    }
}

$PyEmbedUrl = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-$pyArchTag.zip"
$GetPipUrl  = 'https://bootstrap.pypa.io/get-pip.py'

$PythonDir  = Join-Path $InstallDir 'python'
$PythonExe  = Join-Path $PythonDir 'python.exe'
$SrcDirName = 'ntnx-np4m-main'
$SrcDir     = Join-Path $InstallDir $SrcDirName
$Launcher   = Join-Path $InstallDir 'np4m.cmd'
$RunPy      = Join-Path $InstallDir '_run_np4m.py'

Write-Log "Install dir:   $InstallDir"
Write-Log "Python version: $PyVersion ($pyArchTag)"
Write-Log "Listen port:    127.0.0.1:$WebPort"

if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
}

# --- 1. Embedded Python ---------------------------------------------------
if (-not (Test-Path $PythonExe)) {
    $pyZip = Join-Path $InstallDir '.tmp_python.zip'
    Write-Log "Downloading embeddable Python from $PyEmbedUrl ..."
    try {
        Invoke-WebRequest -Uri $PyEmbedUrl -OutFile $pyZip -UseBasicParsing
    } catch {
        Write-Err "Download failed: $($_.Exception.Message)"
        Write-Err "If $PyVersion isn't published yet, set `$env:NP4M_PY_VERSION to a known one (e.g. 3.12.7)."
        exit 1
    }
    if (Test-Path $PythonDir) { Remove-Item -Recurse -Force $PythonDir }
    New-Item -ItemType Directory -Force -Path $PythonDir | Out-Null
    Write-Log "Extracting Python to $PythonDir ..."
    Expand-Archive -Path $pyZip -DestinationPath $PythonDir -Force
    Remove-Item $pyZip -Force

    # Enable site-packages so pip works on the embeddable distribution.
    $pthFile = Get-ChildItem -Path $PythonDir -Filter 'python*._pth' -File | Select-Object -First 1
    if (-not $pthFile) {
        Write-Err "Could not find python*._pth in $PythonDir"
        exit 1
    }
    $pthContent = Get-Content $pthFile.FullName
    $pthContent = $pthContent | ForEach-Object { if ($_ -match '^\s*#\s*import site') { 'import site' } else { $_ } }
    Set-Content -Path $pthFile.FullName -Value $pthContent -Encoding ASCII

    Write-Log "Bootstrapping pip..."
    $getPip = Join-Path $PythonDir 'get-pip.py'
    Invoke-WebRequest -Uri $GetPipUrl -OutFile $getPip -UseBasicParsing
    & $PythonExe $getPip --no-warn-script-location --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) {
        Write-Err "pip bootstrap failed."
        exit 1
    }
    Remove-Item $getPip -Force
} else {
    Write-Log "Python already present at $PythonDir, skipping download."
}

# --- 2. Source tree --------------------------------------------------------
$repoZip = Join-Path $InstallDir '.tmp_repo.zip'
Write-Log "Downloading source zip from $RepoZipUrl ..."
Invoke-WebRequest -Uri $RepoZipUrl -OutFile $repoZip -UseBasicParsing

if (Test-Path $SrcDir) {
    Write-Log "Removing previous source tree at $SrcDir ..."
    Remove-Item -Recurse -Force $SrcDir
}
Write-Log "Extracting source to $InstallDir ..."
Expand-Archive -Path $repoZip -DestinationPath $InstallDir -Force
Remove-Item $repoZip -Force

# --- 3. Python deps -------------------------------------------------------
Write-Log "Installing Python dependencies (this may take a minute) ..."
& $PythonExe -m pip install --upgrade pip --no-warn-script-location --disable-pip-version-check --quiet
& $PythonExe -m pip install -r (Join-Path $SrcDir 'requirements.txt') --no-warn-script-location --disable-pip-version-check --quiet
& $PythonExe -m pip install waitress --no-warn-script-location --disable-pip-version-check --quiet

# --- 4. Bootstrap runner + launcher ---------------------------------------
$runContent = @'
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC  = os.path.join(HERE, 'ntnx-np4m-main')
sys.path.insert(0, SRC)
os.chdir(SRC)

from waitress import serve
from app import app

host = os.environ.get('WEB_HOST', '127.0.0.1')
port = int(os.environ.get('WEB_PORT', '5000'))
print(f'[np4m] serving on http://{host}:{port}/  (Ctrl+C to stop)')
serve(app, host=host, port=port)
'@
Set-Content -Path $RunPy -Value $runContent -Encoding ASCII

$launcherContent = @"
@echo off
title NP4M
set WEB_HOST=127.0.0.1
set WEB_PORT=$WebPort
start "" http://127.0.0.1:$WebPort/
"%~dp0python\python.exe" "%~dp0_run_np4m.py"
"@
Set-Content -Path $Launcher -Value $launcherContent -Encoding ASCII
Write-Log "Wrote launcher: $Launcher"

# --- 5. Optional auto-launch ----------------------------------------------
if ($env:NP4M_NO_START -ne '1') {
    Write-Log "Launching NP4M ..."
    Start-Process -FilePath $Launcher -WorkingDirectory $InstallDir
}

Write-Log ""
Write-Log "NP4M installed (self-contained) at $InstallDir"
Write-Log "  Launch:    double-click np4m.cmd in that folder"
Write-Log "  URL:       http://127.0.0.1:$WebPort/"
Write-Log "  Stop:      close the NP4M console window"
Write-Log "  Uninstall: delete the folder"
