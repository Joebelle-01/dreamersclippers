[CmdletBinding()]
param(
    [int]$Port = 8430,
    [int]$FrontendPort = 5174,
    [switch]$Dev,
    [switch]$NoBrowser,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -Path $ProjectRoot

# Prioritize full WinGet Gyan.FFmpeg binaries ahead of stripped shims in ~/.local/bin
$WinGetPackages = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
if (Test-Path $WinGetPackages) {
    $FfmpegBinDirs = Get-ChildItem -Path $WinGetPackages -Directory -Filter "Gyan.FFmpeg*" -ErrorAction SilentlyContinue |
        ForEach-Object {
            Get-ChildItem -Path $_.FullName -Recurse -Filter "ffmpeg.exe" -File -ErrorAction SilentlyContinue
        } |
        Sort-Object FullName -Descending |
        Select-Object -ExpandProperty DirectoryName -Unique
    if ($FfmpegBinDirs) {
        $FirstBin = @($FfmpegBinDirs)[0]
        $env:PATH = "$FirstBin;$env:PATH"
    }
}

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$UvCmd = Get-Command uv -ErrorAction SilentlyContinue
$UvFallback = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
$UvExe = if ($UvCmd) { $UvCmd.Source } elseif (Test-Path $UvFallback) { $UvFallback } else { $null }

if (-not (Test-Path $VenvPython)) {
    if ($UvExe) {
        Write-Host "[StreamClippers] Creating virtual environment via uv sync..."
        & $UvExe sync
    } else {
        throw "Virtual environment not found at $VenvPython and 'uv' was not found on PATH or at $UvFallback. Install uv from https://docs.astral.sh/uv/ and try again."
    }
}

if ($CheckOnly) {
    & $VenvPython -c "import backend.config, backend.security, backend.models, backend.db, backend.main; print('StreamClippers backend imports OK')"
    exit $LASTEXITCODE
}

# Stop any previous StreamClippers listener on $Port so 1-click relaunches never fail with EADDRINUSE
$ExistingConns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($ExistingConns) {
    foreach ($Conn in $ExistingConns) {
        if ($Conn.OwningProcess -and ($Conn.OwningProcess -ne $PID)) {
            Write-Host "[StreamClippers] Stopping previous server on port $Port (PID $($Conn.OwningProcess))..."
            Stop-Process -Id $Conn.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 400
}

# Clean up any leftover partial analyze/upload temp directories from interrupted sessions
$BuffersDir = Join-Path $ProjectRoot "data\buffers"
if (Test-Path $BuffersDir) {
    Get-ChildItem -Path $BuffersDir -Directory -Filter "analyze_*" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Get-ChildItem -Path $BuffersDir -Directory -Filter "upload_*" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

$FrontendDir = Join-Path $ProjectRoot "frontend"
$FrontendDistIndex = Join-Path $FrontendDir "dist\index.html"
$FrontendPkgJson = Join-Path $FrontendDir "package.json"
$UseViteDev = $Dev.IsPresent -or ((-not (Test-Path $FrontendDistIndex)) -and (Test-Path $FrontendPkgJson))

$BackendArgs = @(
    "-m", "uvicorn",
    "backend.main:app",
    "--host", "127.0.0.1",
    "--port", "$Port"
)

$BackendProc = $null
$FrontendProc = $null

try {
    Write-Host "[StreamClippers] Starting FastAPI backend on http://127.0.0.1:$Port"
    $BackendProc = Start-Process -FilePath $VenvPython -ArgumentList $BackendArgs -WorkingDirectory $ProjectRoot -PassThru -NoNewWindow

    $Ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        if ($BackendProc.HasExited) {
            throw "Backend exited prematurely with code $($BackendProc.ExitCode)."
        }
        try {
            $Resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/system/status" -UseBasicParsing -TimeoutSec 2
            if ($Resp.StatusCode -eq 200) {
                $Ready = $true
                break
            }
        } catch {
            continue
        }
    }

    if (-not $Ready) {
        throw "Backend did not respond on http://127.0.0.1:$Port within timeout."
    }

    $TargetUrl = "http://127.0.0.1:$Port"

    if ($UseViteDev -and (Test-Path $FrontendPkgJson)) {
        $NpmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
        if (-not $NpmCmd) {
            throw "npm.cmd not found in PATH while attempting to start Vite dev server."
        }
        if (-not (Test-Path (Join-Path $FrontendDir "node_modules"))) {
            Write-Host "[StreamClippers] Installing frontend npm dependencies..."
            Push-Location $FrontendDir
            try {
                & $NpmCmd.Source install
            } finally {
                Pop-Location
            }
        }
        Write-Host "[StreamClippers] Starting Vite frontend on http://127.0.0.1:$FrontendPort"
        $FrontendArgs = @("run", "dev", "--", "--host", "127.0.0.1", "--port", "$FrontendPort")
        $FrontendProc = Start-Process -FilePath $NpmCmd.Source -ArgumentList $FrontendArgs -WorkingDirectory $FrontendDir -PassThru -NoNewWindow
        $TargetUrl = "http://127.0.0.1:$FrontendPort"
    }

    if (-not $NoBrowser) {
        Start-Process $TargetUrl | Out-Null
    }

    Write-Host "[StreamClippers] Studio running at $TargetUrl. Press Ctrl+C to stop."
    while ($true) {
        if ($BackendProc -and $BackendProc.HasExited) {
            Write-Host "[StreamClippers] Backend process exited with code $($BackendProc.ExitCode)."
            break
        }
        if ($FrontendProc -and $FrontendProc.HasExited) {
            Write-Host "[StreamClippers] Frontend process exited with code $($FrontendProc.ExitCode)."
            break
        }
        Start-Sleep -Seconds 1
    }
} finally {
    if ($FrontendProc -and (-not $FrontendProc.HasExited) -and ($FrontendProc.Id -ne $PID)) {
        Write-Host "[StreamClippers] Stopping Vite frontend (PID $($FrontendProc.Id))..."
        Stop-Process -Id $FrontendProc.Id -Force -ErrorAction SilentlyContinue
    }
    if ($BackendProc -and (-not $BackendProc.HasExited) -and ($BackendProc.Id -ne $PID)) {
        Write-Host "[StreamClippers] Stopping FastAPI backend (PID $($BackendProc.Id))..."
        Stop-Process -Id $BackendProc.Id -Force -ErrorAction SilentlyContinue
    }
}
