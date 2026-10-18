<#
.SYNOPSIS
  Print relay configuration for Codex in the Windows ChatGPT app, or check prerequisites.
.DESCRIPTION
  The codex-desktop filename and OpenAI.Codex package ID are retained for compatibility.
  This configures local Codex model traffic, not ChatGPT Chat, Work, or cloud tasks.
  Never writes Codex configuration, credentials, profiles, or environment variables.
  -Check reads the local relay's health/model endpoints and installed app metadata.
  It does not start apps, install software, or make model inference requests.
#>
#requires -Version 7
[CmdletBinding()]
param(
    [switch]$Check,
    [string]$Model = 'gpt-6-astra',
    [string]$ProviderId = 'copilot_desktop',
    [string]$BaseUrl = 'http://127.0.0.1:4141/codex/v1'
)

function Resolve-DesktopPowerShell {
    param([string[]]$Candidates)
    if (-not $PSBoundParameters.ContainsKey('Candidates')) {
        $Candidates = @(Get-Command pwsh -CommandType Application -ErrorAction Stop |
            Select-Object -ExpandProperty Source)
    }
    # Get-Command with a command-type filter can return several installations.
    # Never stringify that array into a single, invalid auth command.
    $path = $Candidates | Select-Object -First 1
    if (-not $path -or -not [IO.Path]::IsPathFullyQualified($path) -or
        -not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw 'PowerShell 7 executable not found. No installation was attempted.'
    }
    return [string]$path
}

function Get-DesktopRelayUri {
    param([string]$Value)
    $uri = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri)) {
        throw 'BaseUrl must be an absolute local relay URL.'
    }
    $hostName = $uri.DnsSafeHost.Trim('[', ']').ToLowerInvariant()
    if ($uri.Scheme -notin @('http', 'https') -or
        $hostName -notin @('127.0.0.1', 'localhost', '::1') -or
        $uri.UserInfo -or $uri.Query -or $uri.Fragment -or
        $uri.AbsolutePath.TrimEnd('/') -ne '/codex/v1') {
        throw 'BaseUrl must use a loopback host and /codex/v1, without credentials, query, or fragment.'
    }
    return [Uri]$uri.AbsoluteUri.TrimEnd('/')
}

function Get-DesktopRelayConfig {
    param(
        [string]$Model,
        [string]$ProviderId,
        [string]$BaseUrl,
        [string]$PowerShellPath
    )
    if ([string]::IsNullOrWhiteSpace($Model) -or $Model -match '[\x00-\x1f\x7f]') {
        throw 'Model must be a nonempty model ID without control characters.'
    }
    if ($ProviderId -notmatch '^[A-Za-z][A-Za-z0-9_-]{0,63}$' -or
        $ProviderId -in @('openai', 'ollama', 'lmstudio', 'amazon-bedrock')) {
        throw 'Use a non-reserved provider ID containing only letters, digits, underscores, or hyphens.'
    }
    if (-not [IO.Path]::IsPathFullyQualified($PowerShellPath) -or
        -not (Test-Path -LiteralPath $PowerShellPath -PathType Leaf)) {
        throw 'The auth helper must be one existing absolute PowerShell executable path.'
    }
    $uri = Get-DesktopRelayUri $BaseUrl
    # JSON string escaping is also valid for these TOML basic strings.
    $modelLiteral = ConvertTo-Json -InputObject $Model -Compress
    $urlLiteral = ConvertTo-Json -InputObject $uri.AbsoluteUri.TrimEnd('/') -Compress
    $catalogLiteral = ConvertTo-Json -InputObject ($uri.AbsoluteUri.TrimEnd('/') + '/models') -Compress
    $commandLiteral = ConvertTo-Json -InputObject $PowerShellPath -Compress
    return @"
# Merge these two ROOT keys before the first TOML table. Do not duplicate keys.
# This shared default also affects CLI launches without an explicit profile.
model = $modelLiteral
model_provider = "$ProviderId"

[model_providers.$ProviderId]
name = "GitHub Copilot for Codex in ChatGPT"
base_url = $urlLiteral
model_catalog_url = $catalogLiteral
wire_api = "responses"
supports_websockets = false

# This is a non-secret local token. The relay supplies upstream authentication.
[model_providers.$ProviderId.auth]
command = $commandLiteral
args = ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "[Console]::Out.Write('dummy')"]
timeout_ms = 5000
refresh_interval_ms = 0
"@
}

function Test-DesktopRelay {
    param(
        [string]$BaseUrl,
        [string]$Model,
        [scriptblock]$Fetch = {
            param($Uri)
            Invoke-RestMethod -Uri $Uri -NoProxy -TimeoutSec 15 -ErrorAction Stop
        }
    )
    $uri = Get-DesktopRelayUri $BaseUrl
    $healthUri = [Uri]::new($uri, '/codex/healthz')
    $modelsUri = [Uri]($uri.AbsoluteUri.TrimEnd('/') + '/models')
    try { $health = & $Fetch $healthUri } catch {
        throw 'Relay health request failed. Start/check the existing relay; no settings were changed.'
    }
    # A 200 HTTP status or ok=true alone does not establish upstream health.
    $count = $health.response_models
    if ($health.upstream_ok -ne $true -or $health.upstream_status -ne 200 -or
        ($count -isnot [int] -and $count -isnot [long]) -or $count -lt 1) {
        throw 'Relay is reachable but Copilot health or the Responses model count is not healthy.'
    }
    try { $catalog = & $Fetch $modelsUri } catch {
        throw 'Relay model discovery failed. Upstream error bodies are not printed.'
    }
    $data = @($catalog.data | Where-Object { $null -ne $_ })
    $models = @($catalog.models | Where-Object { $null -ne $_ })
    if ($catalog.object -ne 'list' -or $catalog.data -isnot [Array] -or
        $catalog.models -isnot [Array] -or -not $data.Count -or -not $models.Count) {
        throw 'The relay must return both nonempty OpenAI data and Codex models catalogs.'
    }
    if ($data.id -cnotcontains $Model -or $models.slug -cnotcontains $Model) {
        throw 'The selected model is not present in both relay catalogs. No fallback model was selected.'
    }
    return [pscustomobject]@{
        UpstreamHealthy = $true
        OpenAIModels = $data.Count
        CodexModels = $models.Count
        SelectedModel = $Model
    }
}

function Invoke-DesktopRelayHelper {
    param([bool]$Check, [string]$Model, [string]$ProviderId, [string]$BaseUrl)
    $powerShellPath = Resolve-DesktopPowerShell
    $snippet = Get-DesktopRelayConfig -Model $Model -ProviderId $ProviderId `
        -BaseUrl $BaseUrl -PowerShellPath $powerShellPath
    if (-not $Check) {
        Write-Output $snippet
        return
    }
    if (-not (Get-Command Get-AppxPackage -ErrorAction SilentlyContinue)) {
        throw 'Windows Desktop package discovery is unavailable. No installation was attempted.'
    }
    $package = Get-AppxPackage -Name OpenAI.Codex -ErrorAction Stop |
        Select-Object -First 1
    if (-not $package) {
        throw 'Expected Windows app package OpenAI.Codex not found. Check app packaging; no installation was attempted.'
    }
    $engine = Join-Path $package.InstallLocation 'app\resources\codex.exe'
    if (-not (Test-Path -LiteralPath $engine -PathType Leaf)) {
        throw 'Desktop bundled engine not found. Check the installed app layout.'
    }
    $version = & $engine --version
    if ($LASTEXITCODE -ne 0) { throw 'The installed Desktop engine version check failed.' }
    $result = Test-DesktopRelay -BaseUrl $BaseUrl -Model $Model
    Write-Output "ChatGPT app package (OpenAI.Codex): $($package.Version) ($($package.Architecture))"
    Write-Output "Bundled engine: $($version -join ' ')"
    Write-Output "Copilot healthy; model present in both catalogs ($($result.CodexModels) Codex entries)."
    Write-Output 'Prerequisite checks passed. This does NOT verify the active app Codex provider or inference.'
    Write-Output 'Check effective config/profile/catalog precedence, save drafts, restart the ChatGPT app, and run the local Codex pilot.'
}

# Dot-sourcing exposes pure helpers for the dependency-free PowerShell tests.
if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    try {
        Invoke-DesktopRelayHelper -Check ([bool]$Check) -Model $Model `
            -ProviderId $ProviderId -BaseUrl $BaseUrl
    } catch {
        Write-Error $_
        exit 1
    }
}
