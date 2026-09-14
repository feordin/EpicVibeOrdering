<#
.SYNOPSIS
  Stop the offline downtime demo started by start-offline-demo.ps1.

.DESCRIPTION
  Kills the PIDs recorded in .downtime-logs/pids.json, then - because a PID file
  can be stale, and a half-stopped demo that still holds :8200 is worse than no
  demo - falls back to whatever is still listening on the two ports.
#>
[CmdletBinding()]
param(
    [int] $Port = 0,
    [int] $EnginePort = 0
)

$ErrorActionPreference = "Stop"

$Root    = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root ".downtime-logs\pids.json"

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

$state = $null
if (Test-Path $PidFile) {
    $state = Get-Content $PidFile -Raw | ConvertFrom-Json
}
if ($Port -eq 0)       { $Port       = if ($state) { [int]$state.port } else { 8200 } }
if ($EnginePort -eq 0) { $EnginePort = if ($state) { [int]$state.enginePort } else { 2575 } }

function Stop-Pid($label, $processId) {
    if (-not $processId) { return }
    $p = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if (-not $p) { Write-Host "    $label pid $processId already gone"; return }
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    Write-Host "    stopped $label pid $processId"
}

Step "Stopping recorded PIDs"
if ($state) {
    Stop-Pid "app"    ([int]$state.app)
    Stop-Pid "engine" ([int]$state.engine)
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
} else {
    Write-Host "    no $PidFile - falling back to port listeners"
}

Step "Clearing any remaining listeners on :$Port and :$EnginePort"
foreach ($prt in @($Port, $EnginePort)) {
    $conns = @()
    try { $conns = @(Get-NetTCPConnection -LocalPort $prt -State Listen -ErrorAction SilentlyContinue) } catch {}
    foreach ($processId in ($conns | Select-Object -ExpandProperty OwningProcess -Unique)) {
        Stop-Pid "listener on :$prt" ([int]$processId)
    }
}

Start-Sleep -Milliseconds 300
foreach ($prt in @($Port, $EnginePort)) {
    $still = @()
    try { $still = @(Get-NetTCPConnection -LocalPort $prt -State Listen -ErrorAction SilentlyContinue) } catch {}
    if ($still.Count) { Write-Host "    :$prt is STILL in use" -ForegroundColor Yellow }
    else              { Write-Host "    :$prt is free" -ForegroundColor Green }
}

Write-Host "STOPPED" -ForegroundColor Green
