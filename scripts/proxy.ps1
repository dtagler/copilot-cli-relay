<#
.SYNOPSIS
  Interactive menu for managing the claude-copilot-cli-relay Docker container.

.DESCRIPTION
  Run with no arguments. Pick an action from the numbered list and the script
  performs it then exits. For scripted/CI use, prefer plain `docker compose`.
#>
#requires -Version 7
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot

$actions = @(
  @{ Key = 'start';   Desc = 'Start container (build only if image missing)' }
  @{ Key = 'stop';    Desc = 'Stop and remove container + network' }
  @{ Key = 'restart'; Desc = 'Restart to pick up src/ edits (starts if down)' }
  @{ Key = 'status';  Desc = 'Show container status + port bind' }
  @{ Key = 'rebuild'; Desc = 'Rebuild image and recreate container' }
  @{ Key = 'health';  Desc = 'GET /healthz on localhost:4141' }
  @{ Key = 'quit';    Desc = 'Exit without doing anything' }
)

Push-Location $repoRoot
try {
  if (-not (Test-Path '.env')) {
    Write-Host "WARNING: .env not found. Run 'pwsh scripts\extract-token.ps1' first." -ForegroundColor Yellow
    Write-Host ''
  }

  Write-Host 'claude-copilot-cli-relay — pick an action:' -ForegroundColor Cyan
  for ($i = 0; $i -lt $actions.Count; $i++) {
    $n = $i + 1
    $a = $actions[$i]
    Write-Host ("  {0}) {1,-8}  {2}" -f $n, $a.Key, $a.Desc)
  }
  Write-Host ''

  $choice = Read-Host "Enter choice (1-$($actions.Count))"
  if (-not ($choice -match '^\d+$')) {
    Write-Host "Not a number — aborting." -ForegroundColor Red
    exit 1
  }
  $idx = [int]$choice - 1
  if ($idx -lt 0 -or $idx -ge $actions.Count) {
    Write-Host "Out of range — aborting." -ForegroundColor Red
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
        Write-Host "Container not running — starting fresh..." -ForegroundColor Cyan
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

    'health' {
      $url = 'http://localhost:4141/healthz'
      Write-Host "GET $url" -ForegroundColor Cyan
      try {
        $resp = Invoke-RestMethod -Uri $url -TimeoutSec 5
        $resp | ConvertTo-Json -Compress
        if (-not $resp.upstream_ok) {
          Write-Host "WARNING: upstream_ok=false. Token may be stale; run 'pwsh scripts\extract-token.ps1' then restart." -ForegroundColor Yellow
          exit 1
        }
      } catch {
        Write-Host "Health check failed: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Is the container running? Pick option 'start' next time." -ForegroundColor Yellow
        exit 1
      }
    }

    'quit' {
      Write-Host 'Bye.' -ForegroundColor Cyan
    }
  }
} finally {
  Pop-Location
}
