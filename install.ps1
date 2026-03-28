#Requires -Version 5.1
<#
.SYNOPSIS
    JarvisLT Windows Installer — press-and-forget setup for Windows 10/11.

.DESCRIPTION
    Installs all prerequisites (Python, Node.js, Docker Desktop) via winget,
    sets up the Python environment, configures API keys, starts Docker services,
    installs Playwright, and runs Google OAuth.

    Safe to re-run after a reboot.

.PARAMETER SkipPrereqs
    Skip winget prerequisite installation (Python, Node.js, Docker Desktop).

.PARAMETER SkipDocker
    Skip Docker startup and 'docker compose up'.

.PARAMETER SkipOAuth
    Skip the Google OAuth step (Gmail/Calendar). Run 'pipenv run python auth_google.py' later.

.EXAMPLE
    # First run:
    powershell -ExecutionPolicy Bypass -File install.ps1

    # Re-run after reboot (prerequisites already installed):
    powershell -ExecutionPolicy Bypass -File install.ps1 -SkipPrereqs
#>

param(
    [switch]$SkipPrereqs,
    [switch]$SkipDocker,
    [switch]$SkipOAuth
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# ── Helpers ────────────────────────────────────────────────────────────────────

function Write-Step { param([string]$msg) Write-Host "`n  ▶  $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "     ✓  $msg" -ForegroundColor Green }
function Write-Warn { param([string]$msg) Write-Host "     ⚠  $msg" -ForegroundColor Yellow }
function Write-Fail { param([string]$msg) Write-Host "     ✗  $msg" -ForegroundColor Red }
function Write-Info { param([string]$msg) Write-Host "     $msg" -ForegroundColor Gray }

function Test-Cmd {
    param([string]$cmd)
    return [bool](Get-Command $cmd -ErrorAction SilentlyContinue)
}

function Invoke-Winget {
    param([string]$id, [string]$name)
    Write-Info "Installing $name via winget..."
    winget install --id $id --silent --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
    # Exit code -1978335212 (0x8A150014) = already installed — treat as success
    if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq -1978335212) {
        Write-OK "$name ready"
    } else {
        Write-Warn "$name install returned code $LASTEXITCODE — may already be installed or need reboot"
    }
}

function Refresh-Path {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
}

function Wait-ForDocker {
    Write-Info "Waiting for Docker daemon (up to 3 minutes)..."
    for ($i = 1; $i -le 36; $i++) {
        docker info 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-OK "Docker daemon is ready"
            return $true
        }
        Start-Sleep -Seconds 5
        Write-Info "  ...still waiting ($($i * 5)s / 180s)"
    }
    return $false
}

# ── Banner ─────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  ╔════════════════════════════════════════════╗" -ForegroundColor Magenta
Write-Host "  ║         JarvisLT  Windows  Installer       ║" -ForegroundColor Magenta
Write-Host "  ╚════════════════════════════════════════════╝" -ForegroundColor Magenta
Write-Host ""
Write-Info "Directory: $ScriptDir"
Write-Host ""

# ── Phase 1: Prerequisites ─────────────────────────────────────────────────────

if (-not $SkipPrereqs) {
    Write-Step "Phase 1/7 — Installing prerequisites"

    if (-not (Test-Cmd "winget")) {
        Write-Fail "winget not found."
        Write-Info "Install 'App Installer' from the Microsoft Store, then re-run this script."
        Write-Info "Direct link: https://apps.microsoft.com/store/detail/9NBLGGH4NNS1"
        exit 1
    }
    Write-OK "winget available"

    Invoke-Winget "Python.Python.3.10"  "Python 3.10"
    Invoke-Winget "OpenJS.NodeJS.LTS"   "Node.js LTS"
    Invoke-Winget "Docker.DockerDesktop" "Docker Desktop"

    # Refresh PATH so newly installed tools are visible in this session
    Refresh-Path

    # Verify Python
    if (-not (Test-Cmd "python")) {
        Write-Fail "Python still not in PATH after install."
        Write-Info "Please reboot, then re-run:"
        Write-Info "  powershell -ExecutionPolicy Bypass -File install.ps1 -SkipPrereqs"
        exit 1
    }
    $pyVer = (python --version 2>&1).ToString().Trim()
    Write-OK "$pyVer"

    # Verify Node
    if (-not (Test-Cmd "node")) {
        Write-Fail "Node.js still not in PATH after install."
        Write-Info "Please reboot, then re-run with -SkipPrereqs"
        exit 1
    }
    $nodeVer = (node --version 2>&1).ToString().Trim()
    Write-OK "Node.js $nodeVer"

} else {
    Write-Step "Phase 1/7 — Prerequisites (skipped)"
    Refresh-Path

    if (-not (Test-Cmd "python")) {
        Write-Fail "Python not found. Install Python 3.10 from python.org or via winget."
        exit 1
    }
    if (-not (Test-Cmd "node")) {
        Write-Fail "Node.js not found. Install from nodejs.org or via winget."
        exit 1
    }
    Write-OK "Python and Node.js found"
}

# ── Phase 2: Docker ────────────────────────────────────────────────────────────

if (-not $SkipDocker) {
    Write-Step "Phase 2/7 — Docker"

    if (-not (Test-Cmd "docker")) {
        Write-Fail "Docker CLI not found in PATH."
        if (-not $SkipPrereqs) {
            Write-Info "Docker Desktop was just installed — a reboot is likely required."
        } else {
            Write-Info "Install Docker Desktop from https://www.docker.com/products/docker-desktop"
        }
        Write-Info ""
        Write-Info "After reboot, run:"
        Write-Info "  powershell -ExecutionPolicy Bypass -File install.ps1 -SkipPrereqs"
        exit 1
    }

    # Check if daemon is already running
    docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Info "Docker daemon not running — starting Docker Desktop..."

        $candidates = @(
            "${env:ProgramFiles}\Docker\Docker\Docker Desktop.exe",
            "${env:LOCALAPPDATA}\Docker\Docker Desktop.exe"
        )
        $dockerExe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1

        if ($dockerExe) {
            Start-Process $dockerExe
            $ready = Wait-ForDocker
            if (-not $ready) {
                Write-Fail "Docker did not start within 3 minutes."
                Write-Info "Start Docker Desktop manually, then re-run:"
                Write-Info "  powershell -ExecutionPolicy Bypass -File install.ps1 -SkipPrereqs"
                exit 1
            }
        } else {
            Write-Fail "Cannot locate Docker Desktop executable."
            Write-Info "Start Docker Desktop manually from the Start menu, then re-run with -SkipPrereqs"
            exit 1
        }
    } else {
        Write-OK "Docker daemon already running"
    }
} else {
    Write-Step "Phase 2/7 — Docker (skipped)"
}

# ── Phase 3: Python environment ────────────────────────────────────────────────

Write-Step "Phase 3/7 — Python environment"

# Install/verify pipenv
python -m pipenv --version 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Info "Installing pipenv..."
    python -m pip install pipenv --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "pip install pipenv failed."
        exit 1
    }
    Write-OK "pipenv installed"
} else {
    Write-OK "pipenv already installed"
}

Write-Info "Installing Python packages (this may take a few minutes on first run)..."
python -m pipenv install
if ($LASTEXITCODE -ne 0) {
    Write-Fail "pipenv install failed. See errors above."
    exit 1
}
Write-OK "Python packages installed"

# ── Phase 4: Configuration (.env) ──────────────────────────────────────────────

Write-Step "Phase 4/7 — Configuration"

$envFile = Join-Path $ScriptDir ".env"
$needConfigure = $true

if (Test-Path $envFile) {
    Write-Warn ".env already exists."
    $answer = Read-Host "     Reconfigure API keys? (y/N)"
    $needConfigure = ($answer -eq "y" -or $answer -eq "Y")
    if (-not $needConfigure) {
        Write-OK "Keeping existing .env"
    }
}

if ($needConfigure) {
    Write-Host ""
    Write-Host "     Enter your API keys. Press Enter to skip optional ones." -ForegroundColor White
    Write-Host ""

    # OpenAI — required
    $openaiKey = ""
    while ($openaiKey -eq "") {
        $openaiKey = (Read-Host "     OPENAI_API_KEY  (required, starts with sk-)").Trim()
        if ($openaiKey -eq "") { Write-Warn "OpenAI API key is required to run Jarvis." }
    }

    # Neo4j
    $neo4jRaw = (Read-Host "     NEO4J_PASSWORD  [jarvispass]").Trim()
    $neo4jPass = if ($neo4jRaw -eq "") { "jarvispass" } else { $neo4jRaw }

    # Optional
    $serperKey   = (Read-Host "     SERPER_API_KEY       (optional — Google web search, serper.dev)").Trim()
    $placesKey   = (Read-Host "     GOOGLE_PLACES_API_KEY (optional — restaurant/POI search)").Trim()
    $haToken     = (Read-Host "     HA_TOKEN              (optional — Home Assistant)").Trim()
    $googleEmail = (Read-Host "     GOOGLE_ACCOUNT_EMAIL  (optional — e.g. you@gmail.com)").Trim()

    $lines = @(
        "OPENAI_API_KEY=$openaiKey",
        "NEO4J_PASSWORD=$neo4jPass"
    )
    if ($serperKey)   { $lines += "SERPER_API_KEY=$serperKey" }
    if ($placesKey)   { $lines += "GOOGLE_PLACES_API_KEY=$placesKey" }
    if ($haToken)     { $lines += "HA_TOKEN=$haToken" }
    if ($googleEmail) { $lines += "GOOGLE_ACCOUNT_EMAIL=$googleEmail" }

    Set-Content -Path $envFile -Value ($lines -join "`n") -Encoding UTF8
    Write-OK ".env saved"
}

# ── Phase 5: Docker services ───────────────────────────────────────────────────

if (-not $SkipDocker) {
    Write-Step "Phase 5/7 — Starting Docker services"
    Write-Info "Pulling container images (first run ~2 GB download, subsequent runs are instant)..."

    docker compose up -d
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "docker compose up failed. Check the Docker Desktop logs."
        exit 1
    }
    Write-OK "Containers started: speaches (TTS), whisper (STT), openwakeword (wake word), neo4j (memory)"
    Write-Info "Whisper will download its language model (~500 MB) on first use — this is normal."
} else {
    Write-Step "Phase 5/7 — Docker services (skipped)"
}

# ── Phase 6: Playwright ────────────────────────────────────────────────────────

Write-Step "Phase 6/7 — Playwright browser"

python -m pipenv run python -m playwright install chromium 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warn "Playwright chromium install had issues — web browsing may not work."
    Write-Info "Retry manually: pipenv run python -m playwright install chromium"
} else {
    Write-OK "Playwright Chromium installed"
}

# ── Phase 7: Google OAuth ──────────────────────────────────────────────────────

if (-not $SkipOAuth) {
    Write-Step "Phase 7/7 — Google OAuth  (Gmail / Calendar access)"
    Write-Host ""
    Write-Host "     A browser window will open. Sign in and click Allow." -ForegroundColor White
    Write-Host "     This is a one-time step — credentials are saved locally." -ForegroundColor White
    Write-Host ""

    $go = Read-Host "     Open browser now? (Y/n)"
    if ($go -ne "n" -and $go -ne "N") {
        python -m pipenv run python auth_google.py
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "OAuth did not complete cleanly."
            Write-Info "Run manually later: pipenv run python auth_google.py"
        } else {
            Write-OK "Google OAuth complete"
        }
    } else {
        Write-Warn "Skipped. Run before using Gmail/Calendar: pipenv run python auth_google.py"
    }
} else {
    Write-Step "Phase 7/7 — Google OAuth (skipped)"
}

# ── Done ───────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  ╔════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║           Setup complete!  Ready.          ║" -ForegroundColor Green
Write-Host "  ╚════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  To start Jarvis:" -ForegroundColor White
Write-Host "    Double-click  start.bat" -ForegroundColor Cyan
Write-Host "    or run:       pipenv run python main.py" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Say 'Hey Jarvis' to activate." -ForegroundColor White
Write-Host ""
Write-Host "  Troubleshooting:" -ForegroundColor Gray
Write-Host "    No microphone?    Check Windows Settings → Privacy → Microphone" -ForegroundColor Gray
Write-Host "    MCP server error? Try: pipenv run python main.py  (run from JarvisLT folder)" -ForegroundColor Gray
Write-Host "    Docker not found? Reboot and re-run: install.ps1 -SkipPrereqs" -ForegroundColor Gray
Write-Host ""
