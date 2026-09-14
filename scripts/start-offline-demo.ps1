<#
.SYNOPSIS
  Start the fully-offline downtime demo and warm both local models.

.DESCRIPTION
  One command for Scenario A of docs/runbook-demo.md: it checks the box is
  actually able to run offline, starts the mock HL7 integration engine and the
  downtime capture app, then loads the Whisper weights and the Ollama model into
  memory before anyone touches the UI.

  The warm-up is the point. Both models load lazily, so without it the first
  clinician action of the demo pays for a cold model load - tens of seconds for
  Whisper, up to a minute or more for a 26B model on CPU - with a patient (or an
  audience) waiting. This script moves that cost before the room, and pins
  keep_alive at 2h so the weights stay resident for the whole session.

.EXAMPLE
  .\scripts\start-offline-demo.ps1
  .\scripts\start-offline-demo.ps1 -Model qwen2.5:7b -WhisperModel small -NoStrict
#>
[CmdletBinding()]
param(
    [string] $Model = "gemma4:26b",
    [string] $WhisperModel = "medium",
    [int]    $Port = 8200,
    [int]    $EnginePort = 2575,
    [switch] $NoStrict
)

$ErrorActionPreference = "Stop"

$Root    = Split-Path -Parent $PSScriptRoot
$LogDir  = Join-Path $Root ".downtime-logs"
$Python  = Join-Path $Root ".venv\Scripts\python.exe"
$AppLog  = Join-Path $LogDir "app.log"
$EngLog  = Join-Path $LogDir "engine.log"
$PidFile = Join-Path $LogDir "pids.json"
$OllamaBase = if ($env:EPICVIBE_DOWNTIME_OLLAMA_BASE_URL) { $env:EPICVIBE_DOWNTIME_OLLAMA_BASE_URL } else { "http://localhost:11434" }

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# -- 1. preflight: the box has to be able to do this at all -------------------

Step "Checking the virtualenv"
if (-not (Test-Path $Python)) {
    Fail ".venv not found at $Python. Create it first: python -m venv .venv, then .\.venv\Scripts\pip install -e "".[audio]"""
}
Write-Host "    $Python"

Step "Checking Ollama at $OllamaBase"
try {
    $tags = Invoke-RestMethod -Uri "$OllamaBase/api/tags" -TimeoutSec 10
} catch {
    Fail "cannot reach the Ollama daemon at $OllamaBase ($($_.Exception.Message)). Start it with: ollama serve"
}
$names = @($tags.models | ForEach-Object { $_.name })
if ($names -notcontains $Model) {
    Write-Host "    models present: $($names -join ', ')" -ForegroundColor DarkGray
    Fail "model '$Model' is not on this box. Pull it while you still have a network:`n`n           ollama pull $Model`n"
}
Write-Host "    model '$Model' is present"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# -- 2. processes ------------------------------------------------------------

Step "Starting the mock integration engine on :$EnginePort"
$engine = Start-Process -FilePath $Python `
    -ArgumentList "-m", "epicvibe.downtime.mock_engine", "--port", "$EnginePort", "--inbox", ".downtime-inbox" `
    -WorkingDirectory $Root -RedirectStandardOutput $EngLog -RedirectStandardError "$EngLog.err" `
    -WindowStyle Hidden -PassThru
Write-Host "    pid $($engine.Id) -> $EngLog"

Step "Starting the downtime app on :$Port"
# Child processes inherit this session's environment, so the app's settings are
# set here rather than passed as flags - DowntimeSettings is env-driven.
$env:EPICVIBE_DOWNTIME_PROVIDER          = "ollama"
$env:EPICVIBE_DOWNTIME_OLLAMA_MODEL      = $Model
$env:EPICVIBE_DOWNTIME_WHISPER_MODEL     = $WhisperModel
$env:EPICVIBE_DOWNTIME_OFFLINE_STRICT    = if ($NoStrict) { "false" } else { "true" }
# The default 10m expires mid-demo and the next fill pays the load again.
$env:EPICVIBE_DOWNTIME_OLLAMA_KEEP_ALIVE = "2h"
$env:EPICVIBE_DOWNTIME_ENGINE_PORT       = "$EnginePort"
$env:EPICVIBE_DOWNTIME_PORT              = "$Port"

$app = Start-Process -FilePath $Python -ArgumentList "-m", "epicvibe.downtime" `
    -WorkingDirectory $Root -RedirectStandardOutput $AppLog -RedirectStandardError "$AppLog.err" `
    -WindowStyle Hidden -PassThru
Write-Host "    pid $($app.Id) -> $AppLog"

@{ engine = $engine.Id; app = $app.Id; port = $Port; enginePort = $EnginePort;
   model = $Model; whisper = $WhisperModel;
   started = (Get-Date).ToString("o") } | ConvertTo-Json | Set-Content -Path $PidFile -Encoding utf8

# -- 3. wait for the app -----------------------------------------------------

Step "Waiting for http://localhost:$Port/api/status"
$deadline = (Get-Date).AddSeconds(60)
$status = $null
while ((Get-Date) -lt $deadline) {
    if ($app.HasExited) { break }
    try { $status = Invoke-RestMethod -Uri "http://localhost:$Port/api/status" -TimeoutSec 3; break }
    catch { Start-Sleep -Milliseconds 500 }
}
if (-not $status) {
    Write-Host "    the app did not come up. Last lines of the app log:" -ForegroundColor Red
    foreach ($f in @($AppLog, "$AppLog.err")) {
        if (Test-Path $f) {
            Write-Host "--- $f ---" -ForegroundColor DarkGray
            Get-Content $f -Tail 40
        }
    }
    Fail "downtime app failed to start (strict offline mode refuses to start if anything is not local)"
}
Write-Host "    up: provider $($status.provider) | model $($status.model) | engine $($status.engine) | $($status.templates) templates"

# -- 4. warm-up --------------------------------------------------------------

Step "Warming both local models (this is the slow part - once, here, not in the room)"
try {
    # 10 minutes: a 26B model loading from cold disk is minutes, and failing the
    # demo setup on an impatient timeout helps nobody.
    $warm = Invoke-RestMethod -Method Post -Uri "http://localhost:$Port/api/warmup" -TimeoutSec 600
} catch {
    Fail "warm-up call failed: $($_.Exception.Message)"
}

$w = $warm.whisper
if ($w.loaded) {
    Write-Host ("    whisper   {0,-18} loaded in {1,6:N1}s" -f $w.model, $w.elapsed_s) -ForegroundColor Green
} else {
    Write-Host ("    whisper   {0,-18} NOT loaded - {1}" -f $w.model, $w.reason) -ForegroundColor Yellow
}

$p = $warm.provider
if ($p.loaded) {
    Write-Host ("    provider  {0,-18} loaded in {1,6:N1}s (keep_alive {2})" -f $p.provider, $p.elapsed_s, $p.keep_alive) -ForegroundColor Green
} else {
    $why = if ($p.error) { $p.error } else { $p.reason }
    Write-Host ("    provider  {0,-18} NOT loaded - {1}" -f $p.provider, $why) -ForegroundColor Yellow
}

# -- 5. the offline claim, checked ------------------------------------------

Step "Offline checklist (GET /api/offline)"
$off = Invoke-RestMethod -Uri "http://localhost:$Port/api/offline" -TimeoutSec 10
$labels = [ordered]@{
    provider_local       = "inference runs on this box"
    whisper_installed    = "faster-whisper installed"
    whisper_model_cached = "Whisper weights cached on disk"
    engine_reachable     = "integration engine on a private address"
}
foreach ($k in $labels.Keys) {
    $ok = [bool]$off.$k
    $mark = if ($ok) { [char]0x2713 } else { [char]0x2717 }
    $colour = if ($ok) { "Green" } else { "Red" }
    Write-Host ("    {0} {1}" -f $mark, $labels[$k]) -ForegroundColor $colour
    if (-not $ok) { Write-Host ("      {0}" -f $off.detail.$k) -ForegroundColor DarkGray }
}
if ($off.all_local) {
    $strict = if ($off.strict) { " (STRICT)" } else { "" }
    Write-Host "    all_local: true$strict" -ForegroundColor Green
} else {
    Write-Host "    all_local: FALSE - not_local: $($off.not_local -join ', ')" -ForegroundColor Yellow
}

# -- 6. ready ----------------------------------------------------------------

Write-Host ""
Write-Host "READY - open http://localhost:$Port - you can disconnect the network now" -ForegroundColor Green
Write-Host "  engine pid $($engine.Id) (:$EnginePort)   app pid $($app.Id) (:$Port)"
Write-Host "  logs $LogDir   pids $PidFile"
Write-Host "  stop with: .\scripts\stop-offline-demo.ps1"
