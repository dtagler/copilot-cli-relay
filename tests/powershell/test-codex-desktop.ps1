# Run with PowerShell 7. No Pester, installs, network, credentials, or user-config writes.
#requires -Version 7
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\..\scripts\codex-desktop.ps1')
$passed = 0
function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "FAILED: $Message" }
    $script:passed++
}
function Assert-Throws([scriptblock]$Action, [string]$Message) {
    $threw = $false
    try { & $Action | Out-Null } catch { $threw = $true }
    Assert-True $threw $Message
}

$pwshPath = (Get-Command pwsh -CommandType Application | Select-Object -First 1).Source
$selected = Resolve-DesktopPowerShell -Candidates @($pwshPath, $pwshPath)
Assert-True ($selected -eq $pwshPath) 'multiple installed matches must not be concatenated'
Assert-Throws { Resolve-DesktopPowerShell -Candidates @() } 'missing executable fails'
Assert-Throws { Resolve-DesktopPowerShell -Candidates @('relative-pwsh.exe') } 'relative executable fails'

$config = Get-DesktopRelayConfig -Model 'gpt-6-astra' -ProviderId 'desktop_test' `
    -BaseUrl 'http://127.0.0.1:4141/codex/v1/' -PowerShellPath $pwshPath
Assert-True ($config.Contains('model_provider = "desktop_test"')) 'root selects separate provider'
Assert-True ($config.Contains('name = "GitHub Copilot for Codex in ChatGPT"')) 'display name identifies Codex within the unified app'
Assert-True ($config.Contains('[model_providers.desktop_test.auth]')) 'auth matches provider'
Assert-True ($config.Contains('model_catalog_url = "http://127.0.0.1:4141/codex/v1/models"')) 'explicit catalog'
Assert-True ($config.Contains('supports_websockets = false')) 'stateless SSE transport'
Assert-True ($config.Contains('-NoProfile') -and $config.Contains('-NonInteractive')) 'bounded helper invocation'
Assert-True ($config -notmatch '(?m)^(sandbox_mode|approval_policy|default_permissions|env_key)\s*=') 'no permission/credential policy changes'
$commandLine = ($config -split '\r?\n' | Where-Object { $_ -like 'command = *' })
$decoded = $commandLine.Substring('command = '.Length) | ConvertFrom-Json
Assert-True ($decoded -eq $pwshPath) 'auth command is one correctly escaped executable path'
$quoted = Get-DesktopRelayConfig -Model 'model-"quoted"\variant' -ProviderId 'desktop_test' `
    -BaseUrl 'http://127.0.0.1:4141/codex/v1' -PowerShellPath $pwshPath
$modelLine = $quoted -split '\r?\n' | Where-Object { $_ -like 'model = *' }
Assert-True (($modelLine.Substring('model = '.Length) | ConvertFrom-Json) -eq 'model-"quoted"\variant') 'model escaping'

foreach ($provider in @('openai', 'ollama', 'lmstudio', 'amazon-bedrock', 'bad.id', "bad`nkey")) {
    Assert-Throws {
        Get-DesktopRelayConfig -Model 'test' -ProviderId $provider `
            -BaseUrl 'http://127.0.0.1:4141/codex/v1' -PowerShellPath $pwshPath
    } 'unsafe or reserved provider rejected'
}
Assert-Throws {
    Get-DesktopRelayConfig -Model '' -ProviderId 'desktop_test' `
        -BaseUrl 'http://127.0.0.1:4141/codex/v1' -PowerShellPath $pwshPath
} 'empty model rejected'
foreach ($url in @(
    'https://external.example/codex/v1',
    'http://dummy@127.0.0.1:4141/codex/v1',
    'http://127.0.0.1:4141/v1',
    'http://127.0.0.1:4141/codex/v1?token=dummy',
    'http://127.0.0.1:4141/codex/v1#fragment',
    'not a URL'
)) {
    Assert-Throws { Get-DesktopRelayUri $url } 'unsafe or incorrect relay URL rejected'
}
foreach ($url in @(
    'http://127.0.0.1:4142/codex/v1',
    'http://localhost:4141/codex/v1',
    'http://[::1]:4141/codex/v1'
)) {
    Assert-True ([bool](Get-DesktopRelayUri $url)) 'supported loopback URL accepted'
}

$healthy = [pscustomobject]@{ok=$true; upstream_ok=$true; upstream_status=200; response_models=1}
$catalog = [pscustomobject]@{
    object='list'
    data=@([pscustomobject]@{id='test-model'})
    models=@([pscustomobject]@{slug='test-model'})
}
$requested = [Collections.Generic.List[string]]::new()
$fetch = {
    param($Uri)
    $requested.Add($Uri.AbsolutePath)
    if ($Uri.AbsolutePath -eq '/codex/healthz') { return $healthy }
    if ($Uri.AbsolutePath -eq '/codex/v1/models') { return $catalog }
    throw 'Unexpected endpoint'
}
$result = Test-DesktopRelay -BaseUrl 'http://127.0.0.1:4141/codex/v1' -Model 'test-model' -Fetch $fetch
Assert-True ($result.UpstreamHealthy -and $result.CodexModels -eq 1) 'healthy selected model'
Assert-True (($requested -join ',') -eq '/codex/healthz,/codex/v1/models') 'checks make only health/catalog reads'
Assert-Throws {
    Test-DesktopRelay -BaseUrl 'http://127.0.0.1:4141/codex/v1' -Model 'unavailable' -Fetch $fetch
} 'unknown model fails rather than substituting'
$healthy.upstream_ok = $false
Assert-Throws {
    Test-DesktopRelay -BaseUrl 'http://127.0.0.1:4141/codex/v1' -Model 'test-model' -Fetch $fetch
} 'HTTP 200 and ok=true do not mask unhealthy upstream'
$healthy.upstream_ok = $true
$healthy.response_models = 'invalid'
Assert-Throws {
    Test-DesktopRelay -BaseUrl 'http://127.0.0.1:4141/codex/v1' -Model 'test-model' -Fetch $fetch
} 'malformed health count rejected'
$healthy.response_models = 1
$catalog.models = [pscustomobject]@{slug='test-model'}
Assert-Throws {
    Test-DesktopRelay -BaseUrl 'http://127.0.0.1:4141/codex/v1' -Model 'test-model' -Fetch $fetch
} 'non-array catalog rejected'
$catalog.models = @()
Assert-Throws {
    Test-DesktopRelay -BaseUrl 'http://127.0.0.1:4141/codex/v1' -Model 'test-model' -Fetch $fetch
} 'empty Codex catalog rejected'
$catalog.models = @([pscustomobject]@{slug='test-model'})
$catalog.data = @()
Assert-Throws {
    Test-DesktopRelay -BaseUrl 'http://127.0.0.1:4141/codex/v1' -Model 'test-model' -Fetch $fetch
} 'empty OpenAI catalog rejected'
$fetchFailed = $false
try {
    Test-DesktopRelay -BaseUrl 'http://127.0.0.1:4141/codex/v1' -Model 'test-model' `
        -Fetch { throw 'sensitive-upstream-detail' } | Out-Null
} catch {
    $fetchFailed = $true
    Assert-True ($_.Exception.Message -notlike '*sensitive-upstream-detail*') 'upstream failure detail suppressed'
}
Assert-True $fetchFailed 'failed fetch must fail the check'
Write-Output "$passed PowerShell assertions passed."
