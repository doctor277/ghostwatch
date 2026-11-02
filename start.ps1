[CmdletBinding()]
param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $ProjectRoot ".venv"
$PythonPath = Join-Path $VenvPath "Scripts\python.exe"
$RequirementsPath = Join-Path $ProjectRoot "requirements.txt"
$RequirementsStamp = Join-Path $VenvPath ".ghostwatch-requirements.sha256"
$DashboardHost = "127.0.0.1"
$DashboardPort = 8765
$DashboardUrl = "http://${DashboardHost}:${DashboardPort}"

Set-Location -LiteralPath $ProjectRoot

function Get-PythonVersion {
    param([Parameter(Mandatory)][string]$Executable)
    $VersionParts = & $Executable -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Python could not be started: $Executable" }
    return [version]$VersionParts
}

function Assert-SupportedPython {
    param([Parameter(Mandatory)][string]$Executable)
    $Version = Get-PythonVersion -Executable $Executable
    if ($Version -lt [version]"3.11") {
        throw "Python 3.11 or newer is required; found $Version at $Executable"
    }
    Write-Host "[Ghostwatch] Using Python $Version" -ForegroundColor Cyan
}

function Test-LocalPortAvailable {
    param([Parameter(Mandatory)][int]$Port)
    $Listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
    try {
        $Listener.Start()
        return $true
    } catch [System.Net.Sockets.SocketException] {
        return $false
    } finally {
        $Listener.Stop()
    }
}

if (-not (Test-Path -LiteralPath $PythonPath)) {
    try {
        $SystemPython = Get-Command python -ErrorAction Stop
        Assert-SupportedPython -Executable $SystemPython.Source
    } catch {
        Write-Error "Python 3.11 or newer is required. Install it from https://www.python.org/downloads/windows/ and enable 'Add Python to PATH'. Details: $($_.Exception.Message)"
        exit 1
    }
    Write-Host "[Ghostwatch] Creating local virtual environment..." -ForegroundColor Cyan
    & $SystemPython.Source -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) { Write-Error "Virtual environment creation failed."; exit 1 }
}

try {
    Assert-SupportedPython -Executable $PythonPath
} catch {
    Write-Error $_.Exception.Message
    exit 1
}

$RequirementsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $RequirementsPath).Hash
$InstalledHash = if (Test-Path -LiteralPath $RequirementsStamp) {
    (Get-Content -Raw -LiteralPath $RequirementsStamp).Trim()
} else {
    ""
}
if ($InstalledHash -ne $RequirementsHash) {
    Write-Host "[Ghostwatch] Installing verified dependencies..." -ForegroundColor Cyan
    & $PythonPath -m pip install --disable-pip-version-check -r $RequirementsPath
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Dependency installation failed. Check package availability and your network connection."
        exit 1
    }
    Set-Content -LiteralPath $RequirementsStamp -Value $RequirementsHash -NoNewline
} else {
    Write-Host "[Ghostwatch] Dependencies already match requirements.txt; skipping installation." -ForegroundColor DarkGray
}

if (-not (Test-LocalPortAvailable -Port $DashboardPort)) {
    Write-Error "Port $DashboardPort on $DashboardHost is already occupied. Stop the existing process or change the configured port before retrying."
    exit 1
}

Write-Host "[Ghostwatch] Starting local dashboard at $DashboardUrl" -ForegroundColor Green
Write-Host "[Ghostwatch] Press Ctrl+C to stop. No data leaves this computer." -ForegroundColor DarkGray
$ServerArguments = @("-m", "uvicorn", "app.main:app", "--host", $DashboardHost, "--port", "$DashboardPort")
$ServerProcess = $null
try {
    $ServerProcess = Start-Process -FilePath $PythonPath -ArgumentList $ServerArguments -PassThru -NoNewWindow
    $Ready = $false
    for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
        if ($ServerProcess.HasExited) {
            throw "Ghostwatch exited during startup with code $($ServerProcess.ExitCode)."
        }
        try {
            $Response = Invoke-WebRequest -UseBasicParsing -Uri "$DashboardUrl/api/health" -TimeoutSec 1
            if ($Response.StatusCode -eq 200) { $Ready = $true; break }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $Ready) { throw "Ghostwatch did not become ready within 15 seconds." }

    if (-not $NoBrowser) {
        try {
            Start-Process $DashboardUrl
        } catch {
            Write-Warning "Ghostwatch is running, but the browser could not be opened: $($_.Exception.Message)"
        }
    }

    Wait-Process -Id $ServerProcess.Id
    $ServerProcess.Refresh()
    if ($ServerProcess.ExitCode -ne 0) {
        throw "Ghostwatch exited unexpectedly with code $($ServerProcess.ExitCode)."
    }
} catch {
    Write-Error $_.Exception.Message
    exit 1
} finally {
    if ($null -ne $ServerProcess -and -not $ServerProcess.HasExited) {
        Stop-Process -Id $ServerProcess.Id
    }
}
