<#
.SYNOPSIS
  Print or write the Codex CLI config for this proxy.

.DESCRIPTION
  By default this script prints the config snippets. With -Write, it updates the
  managed copilot provider block in $env:USERPROFILE\.codex\config.toml and
  writes the copilot profile to $env:USERPROFILE\.codex\copilot.config.toml
  (Codex 0.134+ overlays `--profile copilot` from that separate file and no
  longer reads a legacy [profiles.copilot] table from config.toml). It also
  removes any legacy [profiles.copilot*] tables / `profile = "copilot"` selector
  from config.toml, and persists the local dummy CODEX_PROXY_API_KEY for new
  PowerShell sessions. All file/environment changes require an explicit YES.
#>
#requires -Version 7
[CmdletBinding()]
param(
  [switch]$Write,
  [switch]$Force
)

$ErrorActionPreference = 'Stop'

# Provider definition lives in the base config.toml (project-local configs can't
# override providers, and the profile file overlays on top of it).
$providerSnippet = @'
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
'@

# Profile overlay. Codex loads config.toml then overlays this file for
# `codex --profile copilot`. Keys are top-level here (NOT under [profiles.copilot]).
$profileSnippet = @'
# copilot-cli-relay Codex profile.
# Overlaid on ~/.codex/config.toml when you run: codex --profile copilot
model_provider = "copilot"
model = "gpt-5.5"
model_reasoning_effort = "medium"
'@

if (-not $Write) {
  Write-Output '# --- ~/.codex/config.toml (add/refresh this block) ---'
  $providerSnippet -split '\r?\n' | ForEach-Object { Write-Output $_ }
  Write-Output ''
  Write-Output '# --- ~/.codex/copilot.config.toml (whole file) ---'
  $profileSnippet -split '\r?\n' | ForEach-Object { Write-Output $_ }
  Write-Output ''
  Write-Output 'To write these files and persist the dummy key, run:'
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
  return [regex]::Replace($Text, $pattern, '')
}

function Remove-LegacyProfileTables {
  param(
    [Parameter(Mandatory)]
    [string]$Text
  )

  # Strip [profiles.copilot] and any subtable ([profiles.copilot.windows], etc.).
  $pattern = "(?ms)^\[profiles\.copilot(?:\.[^\]]+)?\]\r?\n.*?(?=^\[|\z)"
  $stripped = [regex]::Replace($Text, $pattern, '')
  # Strip a top-level `profile = "copilot"` selector line (no longer supported).
  $stripped = [regex]::Replace($stripped, '(?m)^\s*profile\s*=\s*"copilot"\s*\r?\n', '')
  return $stripped
}

$codexDir = Join-Path $env:USERPROFILE '.codex'
$configPath = Join-Path $codexDir 'config.toml'
$profilePath = Join-Path $codexDir 'copilot.config.toml'
$existing = ''
if (Test-Path $configPath) {
  $existing = Get-Content -Raw -Path $configPath
}

Write-Host "About to update Codex relay setup:" -ForegroundColor Cyan
Write-Host "  $configPath  (managed copilot provider block)"
Write-Host "  $profilePath  (copilot profile overlay)"
Write-Host ''
Write-Host "This writes a local relay provider/profile plus non-secret dummy CODEX_PROXY_API_KEY."
Write-Host "Any legacy [profiles.copilot*] tables in config.toml will be removed."
Write-Host "Type YES to confirm these file and user-environment changes."
$confirmation = Read-Host 'Confirmation'
if ($confirmation -ne 'YES') {
  Write-Host 'No changes made.' -ForegroundColor Yellow
  exit 1
}

New-Item -ItemType Directory -Force -Path $codexDir | Out-Null
if ($Force) {
  Write-Host "-Force is accepted for compatibility; -Write already refreshes the managed copilot sections." -ForegroundColor Yellow
}

# Refresh config.toml: drop the managed provider block plus any legacy profile
# tables/selector, then re-append the current provider block.
$updated = Remove-TomlTable -Text $existing -TableName 'model_providers.copilot'
$updated = Remove-LegacyProfileTables -Text $updated
$updated = $updated.Trim()
$configText = if ($updated) { $updated + "`r`n`r`n" + $providerSnippet } else { $providerSnippet }
Set-Content -Path $configPath -Value $configText -Encoding UTF8
Write-Host "Refreshed copilot provider in config.toml (legacy profile tables removed)." -ForegroundColor Green

# The profile file is fully managed by this script — write it wholesale.
Set-Content -Path $profilePath -Value $profileSnippet -Encoding UTF8
Write-Host "Wrote copilot profile to copilot.config.toml." -ForegroundColor Green

[Environment]::SetEnvironmentVariable('CODEX_PROXY_API_KEY', 'dummy', 'User')
$env:CODEX_PROXY_API_KEY = 'dummy'

$psProfilePath = $PROFILE.CurrentUserAllHosts
$psProfileDir = Split-Path -Parent $psProfilePath
New-Item -ItemType Directory -Force -Path $psProfileDir | Out-Null
$profileLine = '$env:CODEX_PROXY_API_KEY = "dummy"'
if (Test-Path $psProfilePath) {
  $profileText = Get-Content -Raw -Path $psProfilePath
  if ($profileText -notmatch 'CODEX_PROXY_API_KEY') {
    Add-Content -Path $psProfilePath -Value ("`r`n# Local dummy key for copilot-cli-relay Codex profile`r`n" + $profileLine) -Encoding UTF8
  }
} else {
  Set-Content -Path $psProfilePath -Value ("# Local dummy key for copilot-cli-relay Codex profile`r`n" + $profileLine) -Encoding UTF8
}

Write-Host "Persisted CODEX_PROXY_API_KEY for new PowerShell sessions." -ForegroundColor Green
Write-Host "Start Codex with: codex -p copilot"
Write-Host "Already-open terminals may still need: `$env:CODEX_PROXY_API_KEY = 'dummy'"
