<#
.SYNOPSIS
  Print or write the Claude Code config for this proxy.

.DESCRIPTION
  By default this script prints the settings.json snippet Claude Code needs.
  With -Write, it updates $env:USERPROFILE\.claude\settings.json only after
  an explicit YES confirmation. It also writes the same non-secret dummy auth
  values to the Windows user environment so new terminals inherit the proxy
  configuration.
#>
#requires -Version 7
[CmdletBinding()]
param(
  [switch]$Write
)

$ErrorActionPreference = 'Stop'

$baseUrl = 'http://127.0.0.1:4141/claude'
$authToken = 'sk-dummy'
$model = 'claude-opus-4-8'
$smallModel = 'claude-sonnet-4-6'

$snippet = @"
{
  "env": {
    "ANTHROPIC_BASE_URL": "$baseUrl",
    "ANTHROPIC_AUTH_TOKEN": "$authToken",
    "ANTHROPIC_MODEL": "$model",
    "ANTHROPIC_SMALL_FAST_MODEL": "$smallModel"
  },
  "model": "$model",
  "effortLevel": "medium"
}
"@

if (-not $Write) {
  $snippet -split '\r?\n' | ForEach-Object { Write-Output $_ }
  Write-Output ''
  Write-Output 'To write this to Claude Code settings, run:'
  Write-Output '  pwsh scripts\claude-config.ps1 -Write'
  exit 0
}

$claudeDir = Join-Path $env:USERPROFILE '.claude'
$settingsPath = Join-Path $claudeDir 'settings.json'

Write-Host "About to update Claude Code settings:" -ForegroundColor Cyan
Write-Host "  $settingsPath"
Write-Host ''
Write-Host "This writes only local relay settings and a dummy auth token."
Write-Host "Type YES to confirm this file and user-environment change."
$confirmation = Read-Host 'Confirmation'
if ($confirmation -ne 'YES') {
  Write-Host 'No changes made.' -ForegroundColor Yellow
  exit 1
}

New-Item -ItemType Directory -Force -Path $claudeDir | Out-Null
if (Test-Path $settingsPath) {
  $settings = Get-Content -Raw -Path $settingsPath | ConvertFrom-Json
} else {
  $settings = [pscustomobject]@{}
}
if (-not ($settings.PSObject.Properties.Name -contains 'env') -or $null -eq $settings.env) {
  $settings | Add-Member -NotePropertyName env -NotePropertyValue ([pscustomobject]@{}) -Force
}

$envUpdates = [ordered]@{
  ANTHROPIC_BASE_URL = $baseUrl
  ANTHROPIC_AUTH_TOKEN = $authToken
  ANTHROPIC_MODEL = $model
  ANTHROPIC_SMALL_FAST_MODEL = $smallModel
}
foreach ($key in $envUpdates.Keys) {
  $settings.env | Add-Member -NotePropertyName $key -NotePropertyValue $envUpdates[$key] -Force
  [Environment]::SetEnvironmentVariable($key, $envUpdates[$key], 'User')
  Set-Item -Path "Env:$key" -Value $envUpdates[$key]
}
$settings | Add-Member -NotePropertyName model -NotePropertyValue $model -Force
$settings | Add-Member -NotePropertyName effortLevel -NotePropertyValue 'medium' -Force
$settings | ConvertTo-Json -Depth 20 | Set-Content -Path $settingsPath -Encoding UTF8

Write-Host "Updated Claude settings and Windows user environment." -ForegroundColor Green
Write-Host "Restart Claude Code so it uses $baseUrl."
