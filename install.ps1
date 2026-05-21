#requires -Version 5.1
<#
.SYNOPSIS
    NP4M one-shot installer for Windows. No admin required.

.DESCRIPTION
    - Installs Python 3.12 via winget (user scope) if no Python 3.10+ is found.
    - Downloads the repo as a zip (no git required).
    - Creates a virtualenv under %LOCALAPPDATA%\NP4M.
    - Installs waitress and project dependencies.
    - Writes a launcher (np4m.cmd) plus Start Menu and Desktop shortcuts.
    - Binds to 127.0.0.1:5000 only. Closing the console window stops the app.

.NOTES
    Usage:
        iwr -useb https://raw.githubusercontent.com/script-repo/ntnx-np4m/main/install.ps1 | iex

    Env overrides:
        $env:NP4M_PORT      Listen port (default 5000)
        $env:NP4M_NO_START  Set to '1' to skip auto-launch after install
        $env:NP4M_REPO_ZIP  Override the source zip URL
#>

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

function Write-Log { param([string]$Msg) Write-Host "[np4m] $Msg" -ForegroundColor Cyan }
function Write-Err { param([string]$Msg) Write-Host "[np4m] $Msg" -ForegroundColor Red }

# --- Config ---------------------------------------------------------------
$InstallDir = Join-Path $env:LOCALAPPDATA 'NP4M'
$RepoZipUrl = if ($env:NP4M_REPO_ZIP) { $env:NP4M_REPO_ZIP } else { 'https://github.com/script-repo/ntnx-np4m/archive/refs/heads/main.zip' }
$RepoDirName = 'ntnx-np4m-main'
$WebPort = if ($env:NP4M_PORT) { [int]$env:NP4M_PORT } else { 5000 }

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# --- 1. Python detection / install ----------------------------------------
function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($ver in '3.12','3.11','3.10') {
            try {
                $out = & py "-$ver" --version 2>$null
                if ($LASTEXITCODE -eq 0) {
                    return [PSCustomObject]@{ Exe = 'py'; PreArgs = @("-$ver"); Version = $ver }
                }
            } catch {}
        }
    }
    foreach ($exe in 'python','python3') {
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            try {
                $out = & $exe --version 2>$null
                if ($LASTEXITCODE -eq 0 -and $out -match 'Python (\d+)\.(\d+)') {
                    $major = [int]$Matches[1]; $minor = [int]$Matches[2]
                    if ($major -eq 3 -and $minor -ge 10) {
                        return [PSCustomObject]@{ Exe = $exe; PreArgs = @(); Version = "$major.$minor" }
                    }
                }
            } catch {}
        }
    }
    $pyRoot = Join-Path $env:LOCALAPPDATA 'Programs\Python'
    if (Test-Path $pyRoot) {
        $candidates = Get-ChildItem $pyRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^Python3(\d+)$' } |
            Sort-Object Name -Descending
        foreach ($c in $candidates) {
            $exe = Join-Path $c.FullName 'python.exe'
            if (Test-Path $exe) {
                try {
                    $out = & $exe --version 2>$null
                    if ($LASTEXITCODE -eq 0 -and $out -match 'Python (\d+)\.(\d+)') {
                        $major = [int]$Matches[1]; $minor = [int]$Matches[2]
                        if ($major -eq 3 -and $minor -ge 10) {
                            return [PSCustomObject]@{ Exe = $exe; PreArgs = @(); Version = "$major.$minor" }
                        }
                    }
                } catch {}
            }
        }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Log "Python 3.10+ not found. Installing Python 3.12 via winget (user scope)..."
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Err "winget is not available on this box. Install Python 3.10+ from https://www.python.org/downloads/ and re-run."
        exit 1
    }
    & winget install --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Err "winget install failed (exit $LASTEXITCODE)."
        exit 1
    }
    $py = Find-Python
    if (-not $py) {
        Write-Err "winget reported success but no Python 3.10+ was found afterward."
        Write-Err "Open a new PowerShell window and re-run this installer."
        exit 1
    }
}
Write-Log "Using Python $($py.Version): $($py.Exe) $($py.PreArgs -join ' ')"

# --- 2. Download repo zip --------------------------------------------------
$zipPath = Join-Path $InstallDir 'np4m.zip'
Write-Log "Downloading $RepoZipUrl ..."
Invoke-WebRequest -Uri $RepoZipUrl -OutFile $zipPath -UseBasicParsing

$srcDir = Join-Path $InstallDir $RepoDirName
if (Test-Path $srcDir) {
    Write-Log "Removing previous source tree at $srcDir ..."
    Remove-Item -Recurse -Force $srcDir
}
Write-Log "Extracting to $InstallDir ..."
Expand-Archive -Path $zipPath -DestinationPath $InstallDir -Force
Remove-Item $zipPath -Force

# --- 3. Virtualenv + dependencies -----------------------------------------
$venvDir = Join-Path $InstallDir '.venv'
$venvPy  = Join-Path $venvDir 'Scripts\python.exe'

if (-not (Test-Path $venvPy)) {
    Write-Log "Creating virtualenv at $venvDir ..."
    $venvArgs = $py.PreArgs + @('-m','venv',$venvDir)
    & $py.Exe @venvArgs
    if ($LASTEXITCODE -ne 0) { Write-Err "venv creation failed."; exit 1 }
}

Write-Log "Installing Python dependencies (this may take a minute)..."
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $srcDir 'requirements.txt') --quiet
& $venvPy -m pip install waitress --quiet

# --- 4. Launcher .cmd ------------------------------------------------------
$launcher = Join-Path $InstallDir 'np4m.cmd'
$launcherContent = @"
@echo off
title NP4M
cd /d "$srcDir"
set WEB_HOST=127.0.0.1
set WEB_PORT=$WebPort
start "" http://127.0.0.1:$WebPort/
"$venvDir\Scripts\waitress-serve.exe" --host=127.0.0.1 --port=$WebPort app:app
"@
Set-Content -Path $launcher -Value $launcherContent -Encoding ASCII
Write-Log "Wrote launcher: $launcher"

# --- 5. Shortcuts (Start Menu + Desktop) ----------------------------------
function New-NP4MShortcut {
    param(
        [string]$Path,
        [string]$Target,
        [string]$WorkingDir,
        [string]$IconLocation
    )
    $wsh = New-Object -ComObject WScript.Shell
    $lnk = $wsh.CreateShortcut($Path)
    $lnk.TargetPath       = $Target
    $lnk.WorkingDirectory = $WorkingDir
    $lnk.IconLocation     = $IconLocation
    $lnk.Description      = 'Launch NP4M'
    $lnk.WindowStyle      = 1
    $lnk.Save()
}

$icon = "$env:SystemRoot\System32\SHELL32.dll,17"
$startMenuDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
if (-not (Test-Path $startMenuDir)) {
    New-Item -ItemType Directory -Force -Path $startMenuDir | Out-Null
}
$startLnk   = Join-Path $startMenuDir 'NP4M.lnk'
$desktopDir = [Environment]::GetFolderPath('Desktop')
$desktopLnk = Join-Path $desktopDir 'NP4M.lnk'

New-NP4MShortcut -Path $startLnk   -Target $launcher -WorkingDir $InstallDir -IconLocation $icon
New-NP4MShortcut -Path $desktopLnk -Target $launcher -WorkingDir $InstallDir -IconLocation $icon
Write-Log "Shortcuts created (Start Menu + Desktop)."

# --- 6. Optional auto-launch ----------------------------------------------
if ($env:NP4M_NO_START -ne '1') {
    Write-Log "Launching NP4M ..."
    Start-Process -FilePath $launcher
}

Write-Log ""
Write-Log "NP4M installed at $InstallDir"
Write-Log "  Launch:  Start Menu -> NP4M   (or Desktop shortcut)"
Write-Log "  URL:     http://127.0.0.1:$WebPort/"
Write-Log "  Stop:    close the NP4M console window"
