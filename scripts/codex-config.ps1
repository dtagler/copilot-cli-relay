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
  from config.toml. Command-backed dummy auth is the tested relay discovery setup.
  All file changes require an explicit YES.
  For Codex in the ChatGPT desktop app, use codex-desktop.ps1 instead; this writer
  replaces the CLI profile and cleans up its legacy user-environment setup.
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
[model_providers.copilot]
# GitHub Copilot via copilot-cli-relay.
# Start the proxy first: docker compose up -d proxy
name = "GitHub Copilot via local relay"
base_url = "http://127.0.0.1:4141/codex/v1"
wire_api = "responses"
# Use harness-managed /compact, not the standalone /responses/compact endpoint.
capabilities.remote_compaction = "unsupported"
requires_openai_auth = false
request_max_retries = 4
stream_max_retries = 10
stream_idle_timeout_ms = 300000

# Command auth makes Codex refresh /models for this custom provider. The relay
# discards this non-secret local token and injects the Copilot OAuth token.
[model_providers.copilot.auth]
command = "pwsh"
args = ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "[Console]::Out.Write('dummy')"]
timeout_ms = 5000
refresh_interval_ms = 0
'@

# Profile overlay. Codex loads config.toml then overlays this file for
# `codex --profile copilot`. Keys are top-level here (NOT under [profiles.copilot]).
$profileSnippet = @'
# copilot-cli-relay Codex profile.
# Overlaid on ~/.codex/config.toml when you run: codex --profile copilot
model_provider = "copilot"
model = "gpt-5.6-sol"
model_reasoning_effort = "medium"
'@

if (-not $Write) {
  Write-Output '# --- ~/.codex/config.toml (add/refresh this block) ---'
  $providerSnippet -split '\r?\n' | ForEach-Object { Write-Output $_ }
  Write-Output ''
  Write-Output '# --- ~/.codex/copilot.config.toml (whole file) ---'
  $profileSnippet -split '\r?\n' | ForEach-Object { Write-Output $_ }
  Write-Output ''
  Write-Output 'To write these files, run:'
  Write-Output '  pwsh scripts\codex-config.ps1 -Write'
  exit 0
}

function Remove-TomlTableTree {
  param(
    [Parameter(Mandatory)]
    [AllowEmptyString()]
    [string]$Text,

    [Parameter(Mandatory)]
    [string]$TableName
  )

  # This is a deliberately limited editor, not a TOML parser. Refuse layouts
  # that can hide headers inside strings or define the provider inline instead
  # of guessing and risking unrelated configuration.
  if ($Text.Contains('"""') -or $Text.Contains("'''")) {
    throw 'Automatic CLI merge does not support multiline TOML strings. Use print-only mode and merge manually.'
  }
  $key = '(?:[A-Za-z0-9_-]+|"(?:[^"\\\r\n]|\\.)*"|''[^''\r\n]*'')'
  $headerPattern = '^[ \t]*\[(?<array>\[)?[ \t]*(?<path>' + $key + '(?:[ \t]*\.[ \t]*' + $key + ')*)[ \t]*\](?(array)\])[ \t]*(?:#[^\r\n]*)?\r?$'
  $headers = [regex]::Matches($Text, '(?m)^[ \t]*\[.*$')
  $target = $TableName.Split('.')
  $remove = [Collections.Generic.List[object]]::new()
  for ($index = 0; $index -lt $headers.Count; $index++) {
    $header = $headers[$index]
    $parsed = [regex]::Match($header.Value, $headerPattern)
    if (-not $parsed.Success) {
      throw 'Unsupported TOML table syntax; no configuration was written. Use print-only mode.'
    }
    $parts = @([regex]::Matches($parsed.Groups['path'].Value, $key) | ForEach-Object {
      $value = $_.Value
      if ($value.StartsWith('"') -or $value.StartsWith("'")) { $value.Substring(1, $value.Length - 2) } else { $value }
    })
    if ($parts[0].Contains('\') -or
        ($parts[0] -in @('model_providers','profiles') -and $parts.Count -gt 1 -and $parts[1].Contains('\'))) {
      throw 'Escaped managed TOML table names require a manual merge.'
    }
    if ($parts.Count -eq 1 -and $parts[0] -eq $target[0]) {
      throw 'Inline managed TOML tables require a manual merge.'
    }
    $match = $parts.Count -ge $target.Count
    for ($part = 0; $match -and $part -lt $target.Count; $part++) {
      $match = $parts[$part] -ceq $target[$part]
    }
    if ($match) {
      if ($parsed.Groups['array'].Success) { throw 'Managed array tables require a manual merge.' }
      $end = if ($index + 1 -lt $headers.Count) { $headers[$index + 1].Index } else { $Text.Length }
      $remove.Add(@($header.Index, ($end - $header.Index)))
    }
  }
  $rootEnd = if ($headers.Count) { $headers[0].Index } else { $Text.Length }
  $root = $Text.Substring(0, $rootEnd)
  $rootKeyPattern = '(?m)^[ \t]*' + $key + '[ \t]*(?:\.|=)'
  foreach ($assignment in [regex]::Matches($root, $rootKeyPattern)) {
    $firstKey = [regex]::Match($assignment.Value.TrimStart(), $key).Value.Trim('"', "'")
    if ($firstKey -eq $target[0] -or $firstKey.Contains('\')) {
      throw 'Dotted or inline root provider/profile keys require a manual merge.'
    }
  }
  for ($index = $remove.Count - 1; $index -ge 0; $index--) {
    $Text = $Text.Remove($remove[$index][0], $remove[$index][1])
  }
  return $Text
}

function Remove-LegacyProfileTables {
  param(
    [Parameter(Mandatory)]
    [AllowEmptyString()]
    [string]$Text
  )

  $stripped = Remove-TomlTableTree -Text $Text -TableName 'profiles.copilot'
  # Remove only a ROOT selector, never an unrelated table's profile field.
  $firstTable = [regex]::Match($stripped, '(?m)^[ \t]*\[')
  $rootEnd = if ($firstTable.Success) { $firstTable.Index } else { $stripped.Length }
  $root = [regex]::Replace($stripped.Substring(0, $rootEnd),
    '(?m)^[ \t]*profile[ \t]*=[ \t]*(?:"copilot"|''copilot'')[ \t]*(?:#[^\r\n]*)?(?:\r?\n|\z)', '')
  return $root + $stripped.Substring($rootEnd)
}

$codexDir = Join-Path $env:USERPROFILE '.codex'
$configPath = Join-Path $codexDir 'config.toml'
$profilePath = Join-Path $codexDir 'copilot.config.toml'
$existing = ''
if (Test-Path $configPath) {
  $existing = [string](Get-Content -Raw -Path $configPath)
}

Write-Host "About to update Codex relay setup:" -ForegroundColor Cyan
Write-Host "  $configPath  (managed copilot provider block)"
Write-Host "  $profilePath  (copilot profile overlay)"
Write-Host ''
Write-Host "This writes a local relay provider/profile and removes the obsolete dummy-key setup."
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
$updated = Remove-TomlTableTree -Text $existing -TableName 'model_providers.copilot'
$updated = Remove-LegacyProfileTables -Text $updated
$updated = $updated.Trim()
$configText = if ($updated) { $updated + "`r`n`r`n" + $providerSnippet } else { $providerSnippet }
Set-Content -Path $configPath -Value $configText -Encoding UTF8
Write-Host "Refreshed copilot provider in config.toml (legacy profile tables removed)." -ForegroundColor Green

# The profile file is fully managed by this script — write it wholesale.
Set-Content -Path $profilePath -Value $profileSnippet -Encoding UTF8
Write-Host "Wrote copilot profile to copilot.config.toml." -ForegroundColor Green

# Remove the environment setup written by older versions of this script.
[Environment]::SetEnvironmentVariable('CODEX_PROXY_API_KEY', $null, 'User')
Remove-Item Env:CODEX_PROXY_API_KEY -ErrorAction SilentlyContinue
$psProfilePath = $PROFILE.CurrentUserAllHosts
if (Test-Path $psProfilePath) {
  [string]$profileText = Get-Content -Raw -Path $psProfilePath
  $legacyPattern = '(?m)^# Local dummy key for copilot-cli-relay Codex profile\r?\n\$env:CODEX_PROXY_API_KEY\s*=\s*["'']dummy["'']\r?\n?'
  $cleanedProfile = [regex]::Replace($profileText, $legacyPattern, '')
  if ($cleanedProfile -ne $profileText) {
    Set-Content -Path $psProfilePath -Value $cleanedProfile.TrimEnd() -Encoding UTF8
  }
}

Write-Host "Removed obsolete CODEX_PROXY_API_KEY setup if present." -ForegroundColor Green
Write-Host "Start Codex with: codex -p copilot"
