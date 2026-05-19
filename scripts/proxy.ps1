<#
.SYNOPSIS
  Interactive menu for managing the copilot-cli-relay Docker container.

.DESCRIPTION
  Run with no arguments. Pick an action from the numbered list and the script
  performs it then exits. Includes explicit Claude and Codex route checks.
  For scripted/CI use, prefer plain `docker compose`.
#>
#requires -Version 7
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot

function Get-ItemCount {
  param([object]$Value)

  if ($null -eq $Value) {
    return 0
  }
  if ($Value -is [array]) {
    return $Value.Count
  }
  return 1
}

function Test-RelayNamespace {
  param(
    [Parameter(Mandatory)]
    [string]$Name,

    [Parameter(Mandatory)]
    [string]$HealthUrl,

    [Parameter(Mandatory)]
    [string]$ModelsUrl,

    [switch]$HasCodexCatalog
  )

  Write-Host "$Name health: GET $HealthUrl" -ForegroundColor Cyan
  try {
    $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5
    $health | ConvertTo-Json -Compress
    if (-not $health.upstream_ok) {
      Write-Host "WARNING: $Name upstream_ok=false. Token may be stale; run 'pwsh scripts\extract-token.ps1' then restart." -ForegroundColor Yellow
      exit 1
    }
  } catch {
    Write-Host "$Name health check failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Is the container running? Pick option 'start' next time." -ForegroundColor Yellow
    exit 1
  }

  Write-Host "$Name models: GET $ModelsUrl" -ForegroundColor Cyan
  try {
    $models = Invoke-RestMethod -Uri $ModelsUrl -TimeoutSec 5
    $dataCount = Get-ItemCount $models.data
    if ($dataCount -lt 1) {
      throw "$Name model catalog returned zero data entries"
    }
    if ($HasCodexCatalog) {
      $catalogCount = Get-ItemCount $models.models
      if ($catalogCount -lt 1) {
        throw "$Name Codex model catalog returned zero models entries"
      }
      Write-Host "$Name model catalog reachable: data=$dataCount models=$catalogCount" -ForegroundColor Green
    } else {
      Write-Host "$Name model catalog reachable: data=$dataCount" -ForegroundColor Green
    }
  } catch {
    Write-Host "$Name model catalog check failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Is the container running? Pick option 'start' next time." -ForegroundColor Yellow
    exit 1
  }
}

$actions = @(
  @{ Key = 'start';   Desc = 'Start container (build only if image missing)' }
  @{ Key = 'stop';    Desc = 'Stop and remove container + network' }
  @{ Key = 'restart'; Desc = 'Restart to pick up src/ edits (starts if down)' }
  @{ Key = 'status';  Desc = 'Show container status + port bind' }
  @{ Key = 'rebuild'; Desc = 'Rebuild image and recreate container' }
  @{ Key = 'claude';  Desc = 'Check Claude routes: /claude/healthz + /claude/v1/models' }
  @{ Key = 'codex';   Desc = 'Check Codex routes: /codex/healthz + /codex/v1/models' }
  @{ Key = 'quit';    Desc = 'Exit without doing anything' }
)

Push-Location $repoRoot
try {
  if (-not (Test-Path '.env')) {
    Write-Host "WARNING: .env not found. Run 'pwsh scripts\extract-token.ps1' first." -ForegroundColor Yellow
    Write-Host ''
  }

  Write-Host 'copilot-cli-relay - pick an action:' -ForegroundColor Cyan
  for ($i = 0; $i -lt $actions.Count; $i++) {
    $n = $i + 1
    $a = $actions[$i]
    Write-Host ("  {0}) {1,-8}  {2}" -f $n, $a.Key, $a.Desc)
  }
  Write-Host ''

  $choice = Read-Host "Enter choice (1-$($actions.Count))"
  if (-not ($choice -match '^\d+$')) {
    Write-Host "Not a number - aborting." -ForegroundColor Red
    exit 1
  }
  $idx = [int]$choice - 1
  if ($idx -lt 0 -or $idx -ge $actions.Count) {
    Write-Host "Out of range - aborting." -ForegroundColor Red
    exit 1
  }
  $action = $actions[$idx].Key
  Write-Host ''

  switch ($action) {
    'start' {
      Write-Host "Starting proxy..." -ForegroundColor Cyan
      docker compose up -d proxy
      Start-Sleep -Seconds 2
      docker compose ps proxy
    }

    'stop' {
      Write-Host "Stopping proxy..." -ForegroundColor Cyan
      docker compose down
    }

    'restart' {
      $running = (docker compose ps -q proxy 2>$null | Measure-Object).Count
      if ($running -eq 0) {
        Write-Host "Container not running - starting fresh..." -ForegroundColor Cyan
        docker compose up -d proxy
      } else {
        Write-Host "Restarting proxy (picks up src/ edits)..." -ForegroundColor Cyan
        docker compose restart proxy
      }
      docker compose ps proxy
    }

    'status' {
      docker compose ps proxy
    }

    'rebuild' {
      Write-Host "Rebuilding proxy image and recreating container..." -ForegroundColor Cyan
      docker compose build proxy
      docker compose up -d --force-recreate proxy
      docker compose ps proxy
    }

    'claude' {
      Test-RelayNamespace `
        -Name 'Claude' `
        -HealthUrl 'http://127.0.0.1:4141/claude/healthz' `
        -ModelsUrl 'http://127.0.0.1:4141/claude/v1/models'
    }

    'codex' {
      Test-RelayNamespace `
        -Name 'Codex' `
        -HealthUrl 'http://127.0.0.1:4141/codex/healthz' `
        -ModelsUrl 'http://127.0.0.1:4141/codex/v1/models' `
        -HasCodexCatalog
    }

    'quit' {
      Write-Host 'Bye.' -ForegroundColor Cyan
    }
  }
} finally {
  Pop-Location
}
