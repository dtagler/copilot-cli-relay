<#
.SYNOPSIS
  Print or write the Codex CLI config for this proxy.

.DESCRIPTION
  By default this script prints a config snippet. With -Write, it updates the
  managed copilot provider/profile blocks in $env:USERPROFILE\.codex\config.toml
  only after an explicit YES confirmation, and persists the local dummy
  CODEX_PROXY_API_KEY for new PowerShell sessions.
#>
#requires -Version 7
[CmdletBinding()]
param(
  [switch]$Write,
  [switch]$Force
)

$ErrorActionPreference = 'Stop'

$snippet = @'
# GitHub Copilot via copilot-cli-relay.
# Start the proxy first: docker compose up -d proxy
# Codex must have a local dummy key in the environment:
#   $env:CODEX_PROXY_API_KEY = "dummy"

[model_providers.copilot]
name = "GitHub Copilot via local relay"
base_url = "http://127.0.0.1:4141/codex/v1"
wire_api = "responses"
env_key = "CODEX_PROXY_API_KEY"
requires_openai_auth = false
request_max_retries = 4
stream_max_retries = 10
stream_idle_timeout_ms = 300000

[profiles.copilot]
model_provider = "copilot"
model = "gpt-5.5"
model_reasoning_effort = "medium"
'@

if (-not $Write) {
  $snippet -split '\r?\n' | ForEach-Object { Write-Output $_ }
  Write-Output ''
  Write-Output 'To write this profile and persist the dummy key, run:'
  Write-Output '  pwsh scripts\codex-config.ps1 -Write'
  exit 0
}

function Remove-TomlTable {
  param(
    [Parameter(Mandatory)]
    [string]$Text,

    [Parameter(Mandatory)]
    [string]$TableName
  )

  $escaped = [regex]::Escape($TableName)
  $pattern = "(?ms)^\[$escaped\]\r?\n.*?(?=^\[|\z)"
  return [regex]::Replace($Text, $pattern, '').Trim()
}

$codexDir = Join-Path $env:USERPROFILE '.codex'
$configPath = Join-Path $codexDir 'config.toml'
$existing = ''
if (Test-Path $configPath) {
  $existing = Get-Content -Raw -Path $configPath
}

Write-Host "About to update Codex relay setup:" -ForegroundColor Cyan
Write-Host "  $configPath"
Write-Host ''
Write-Host "This writes a local relay profile plus non-secret dummy CODEX_PROXY_API_KEY."
Write-Host "Type YES to confirm this file and user-environment change."
$confirmation = Read-Host 'Confirmation'
if ($confirmation -ne 'YES') {
  Write-Host 'No changes made.' -ForegroundColor Yellow
  exit 1
}

New-Item -ItemType Directory -Force -Path $codexDir | Out-Null
if ($Force) {
  Write-Host "-Force is accepted for compatibility; -Write already refreshes the managed copilot sections." -ForegroundColor Yellow
}

$hasManagedSections = $existing -match '(?m)^\[model_providers\.copilot\]' -or $existing -match '(?m)^\[profiles\.copilot\]'
if ($hasManagedSections) {
  $updated = Remove-TomlTable -Text $existing -TableName 'model_providers.copilot'
  $updated = Remove-TomlTable -Text $updated -TableName 'profiles.copilot'
  $configText = if ($updated.Trim()) { $updated.TrimEnd() + "`r`n`r`n" + $snippet } else { $snippet }
  Set-Content -Path $configPath -Value $configText -Encoding UTF8
  Write-Host "Refreshed existing Codex profile." -ForegroundColor Green
} else {
  $prefix = if ($existing.Trim()) { "`r`n`r`n" } else { "" }
  Add-Content -Path $configPath -Value ($prefix + $snippet) -Encoding UTF8
  Write-Host "Appended Codex profile." -ForegroundColor Green
}

[Environment]::SetEnvironmentVariable('CODEX_PROXY_API_KEY', 'dummy', 'User')
$env:CODEX_PROXY_API_KEY = 'dummy'

$profilePath = $PROFILE.CurrentUserAllHosts
$profileDir = Split-Path -Parent $profilePath
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
$profileLine = '$env:CODEX_PROXY_API_KEY = "dummy"'
if (Test-Path $profilePath) {
  $profileText = Get-Content -Raw -Path $profilePath
  if ($profileText -notmatch 'CODEX_PROXY_API_KEY') {
    Add-Content -Path $profilePath -Value ("`r`n# Local dummy key for copilot-cli-relay Codex profile`r`n" + $profileLine) -Encoding UTF8
  }
} else {
  Set-Content -Path $profilePath -Value ("# Local dummy key for copilot-cli-relay Codex profile`r`n" + $profileLine) -Encoding UTF8
}

Write-Host "Persisted CODEX_PROXY_API_KEY for new PowerShell sessions." -ForegroundColor Green
Write-Host "Start Codex with: codex -p copilot"
Write-Host "Already-open terminals may still need: `$env:CODEX_PROXY_API_KEY = 'dummy'"
