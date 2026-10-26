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
Assert-True ($config -match '(?s)\[model_providers\.desktop_test\][^\[]*capabilities\.remote_compaction = "unsupported"') 'local compaction belongs to the selected provider, not its auth table'
Assert-True ([regex]::Matches($config, '(?m)^capabilities\.remote_compaction\s*=').Count -eq 1) 'provider compaction capability is assigned exactly once'
Assert-True ($config.Contains('-NoProfile') -and $config.Contains('-NonInteractive')) 'bounded helper invocation'
Assert-True ($config -notmatch '(?m)^(sandbox_mode|approval_policy|default_permissions|env_key)\s*=') 'no permission/credential policy changes'
$commandLine = ($config -split '\r?\n' | Where-Object { $_ -like 'command = *' })
$decoded = $commandLine.Substring('command = '.Length) | ConvertFrom-Json
Assert-True ($decoded -eq $pwshPath) 'auth command is one correctly escaped executable path'
$quoted = Get-DesktopRelayConfig -Model 'model-"quoted"\variant' -ProviderId 'desktop_test' `
    -BaseUrl 'http://127.0.0.1:4141/codex/v1' -PowerShellPath $pwshPath
$modelLine = $quoted -split '\r?\n' | Where-Object { $_ -like 'model = *' }
Assert-True (($modelLine.Substring('model = '.Length) | ConvertFrom-Json) -eq 'model-"quoted"\variant') 'model escaping'

foreach ($provider in @('copilot_desktop', 'copilot_desktop_pilot')) {
    $compatible = Get-DesktopRelayConfig -Model 'gpt-6-astra' -ProviderId $provider `
        -BaseUrl 'http://127.0.0.1:4141/codex/v1' -PowerShellPath $pwshPath
    Assert-True ($compatible.Contains("model_provider = `"$provider`"")) 'current and legacy provider IDs remain selectable'
    Assert-True ($compatible.Contains("[model_providers.$provider]") -and
        $compatible.Contains("[model_providers.$provider.auth]")) 'current and legacy provider tables retain matching identities'
    Assert-True ($compatible.Contains('name = "GitHub Copilot for Codex in ChatGPT"')) 'both provider IDs use the supported display name'
}

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
foreach ($provider in @('', "valid`n", "valid`r`n", ('a' * 65), "valid`t")) {
    Assert-Throws {
        Get-DesktopRelayConfig -Model 'test' -ProviderId $provider `
            -BaseUrl 'http://127.0.0.1:4141/codex/v1' -PowerShellPath $pwshPath
    } 'provider must occupy the entire string and fit the length bound'
}
foreach ($provider in @('a', ('a' * 64))) {
    Assert-True ([bool](Get-DesktopRelayConfig -Model 'test' -ProviderId $provider `
        -BaseUrl 'http://127.0.0.1:4141/codex/v1' -PowerShellPath $pwshPath)) 'provider length boundary accepted'
}
$catalog.data = @([pscustomobject]@{id='test-model'})
foreach ($bad in @('true', $null, @($true), [pscustomobject]@{value=$true})) {
    $healthy.upstream_ok = $bad
    Assert-Throws { Test-DesktopRelay -BaseUrl 'http://127.0.0.1:4141/codex/v1' -Model 'test-model' -Fetch $fetch } 'health requires a Boolean'
}
$healthy.upstream_ok = $true
foreach ($bad in @('200', $null, @(200), [pscustomobject]@{value=200})) {
    $healthy.upstream_status = $bad
    Assert-Throws { Test-DesktopRelay -BaseUrl 'http://127.0.0.1:4141/codex/v1' -Model 'test-model' -Fetch $fetch } 'health requires an integer status'
}
$healthy.upstream_status = 200

# Orchestration tests inject all OS/network dependencies. Print mode must use none.
$invokeArgs = @{
    Check=$false; Model='test-model'; ProviderId='copilot_desktop'; BaseUrl='http://127.0.0.1:4141/codex/v1'
    ResolvePowerShell={ $pwshPath }; FindPackage={ throw 'unexpected discovery' }
    ProbeEngine={ throw 'unexpected probe' }; CheckRelay={ throw 'unexpected network' }
    RunningState={ throw 'unexpected process inspection' }
}
Assert-True ((Invoke-DesktopRelayHelper @invokeArgs) -like '*GitHub Copilot for Codex in ChatGPT*') 'print mode is isolated'
$invokeArgs.Check = $true
Assert-Throws { Invoke-DesktopRelayHelper @invokeArgs } 'unavailable package discovery fails'
$invokeArgs.FindPackage = { $null }
Assert-Throws { Invoke-DesktopRelayHelper @invokeArgs } 'absent package fails'
$invokeArgs.FindPackage = { [pscustomobject]@{InstallLocation=$PSScriptRoot; Version='test'; Architecture='test'} }
Assert-Throws { Invoke-DesktopRelayHelper @invokeArgs } 'engine probe failure propagates'
$invokeArgs.ProbeEngine = { 'codex-cli test' }
Assert-Throws { Invoke-DesktopRelayHelper @invokeArgs } 'unhealthy relay fails'
$invokeArgs.CheckRelay = { [pscustomobject]@{CodexModels=1} }
$invokeArgs.RunningState = { 'installed-running-mismatch' }
$output = (Invoke-DesktopRelayHelper @invokeArgs) -join "`n"
Assert-True ($output.Contains('installed-running-mismatch')) 'running package mismatch is visible'
Assert-True ($output.Contains('does NOT verify')) 'check does not claim inference'
Assert-True ($output.Contains('verify a local Codex chat') -and $output -notmatch '\bpilot\b') 'supported workflow guidance'

$installed = [IO.Path]::GetFullPath($PSScriptRoot)
$matching = [pscustomobject]@{Path=(Join-Path $installed 'ChatGPT.exe')}
$mismatching = [pscustomobject]@{Path=(Join-Path (Split-Path $installed) 'ChatGPT.exe')}
$unreadable = [pscustomobject]@{Path=$null}
foreach ($case in @(
    @{Items=@(); Expected='not-running-or-unavailable'},
    @{Items=@($matching); Expected='matches-installed-package'},
    @{Items=@($mismatching); Expected='installed-running-mismatch'},
    @{Items=@($unreadable); Expected='running-path-unavailable'},
    @{Items=@($matching,$unreadable); Expected='running-path-unavailable'},
    @{Items=@($mismatching,$unreadable); Expected='installed-running-mismatch'}
)) {
    $state = Get-DesktopRunningState -InstallLocation $installed -ListProcesses { $case.Items }
    Assert-True ($state -eq $case.Expected) 'running-state diagnostics preserve unknown paths'
}

Assert-Throws { Invoke-DesktopEngineVersion -Engine (Join-Path $PSScriptRoot 'absent.exe') } 'missing engine fails'
$probeArgs = @('-NoProfile', '-NonInteractive', '-Command', "[Console]::Out.Write('codex-cli fixture')")
Assert-True ((Invoke-DesktopEngineVersion -Engine $pwshPath -Arguments $probeArgs) -eq 'codex-cli fixture') 'bounded probe returns version'
Assert-Throws { Invoke-DesktopEngineVersion -Engine $pwshPath -Arguments @('-NoProfile','-Command','exit 2') } 'nonzero engine fails'
Assert-Throws { Invoke-DesktopEngineVersion -Engine $pwshPath -TimeoutMs 250 -Arguments @('-NoProfile','-Command','Start-Sleep 30') } 'engine timeout is bounded'
Assert-Throws { Invoke-DesktopEngineVersion -Engine $pwshPath -Arguments @('-NoProfile','-Command',"[Console]::Out.Write(('x' * 4097))") } 'oversized output fails'
Assert-Throws { Invoke-DesktopEngineVersion -Engine $pwshPath -Arguments @('-NoProfile','-Command',"[Console]::Out.Write('unexpected')") } 'unrecognized output is not printed'

# Real local HTTP redirect regression: both listeners belong to this test.
$source = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$target = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$source.Start()
$target.Start()
$job = $null
try {
    $sourcePort = $source.LocalEndpoint.Port
    $targetPort = $target.LocalEndpoint.Port
    $job = Start-ThreadJob -ArgumentList $source,$targetPort -ScriptBlock {
        param($Listener, $Port)
        $accept = $Listener.AcceptTcpClientAsync()
        if (-not $accept.Wait(10000)) { throw 'No diagnostic request arrived.' }
        $connection = $accept.Result
        try {
            $stream = $connection.GetStream()
            $reply = [Text.Encoding]::ASCII.GetBytes("HTTP/1.1 302 Found`r`nLocation: http://127.0.0.1:$Port/codex/healthz`r`nContent-Length: 0`r`nConnection: close`r`n`r`n")
            $stream.Write($reply)
        } finally { $connection.Dispose() }
    }
    Assert-Throws { Test-DesktopRelay -BaseUrl "http://127.0.0.1:$sourcePort/codex/v1" -Model 'test-model' } 'redirects are rejected'
    $null = Wait-Job $job -Timeout 15
    if ($job.State -ne 'Completed') { throw 'Redirect fixture did not complete.' }
    Receive-Job $job -ErrorAction Stop | Out-Null
    Assert-True (-not $target.Pending()) 'redirect target receives no request'
} finally {
    if ($job) { Stop-Job $job; Remove-Job $job }
    $source.Stop()
    $target.Stop()
}
$pattern = Get-DesktopCatalogRequestPattern
foreach ($line in @(
    "GET /codex/v1/models HTTP/1.1`r`n",
    "GET /codex/v1/models?client_version=fixture HTTP/1.1`r`n"
)) { Assert-True ($line -match $pattern) 'catalog GET accepts client-version queries' }
foreach ($line in @(
    "POST /codex/v1/models HTTP/1.1`r`n",
    "GET /codex/v1/responses HTTP/1.1`r`n",
    "GET /codex/v1/models/other HTTP/1.1`r`n"
)) { Assert-True ($line -notmatch $pattern) 'catalog fixture rejects other methods/routes' }
$marked = [pscustomobject]@{slug='fixture'; display_name='unique-marker'}
$unmarked = [pscustomobject]@{slug='fixture'; display_name='bundled-name'}
Assert-True (Test-DesktopCatalogMarker -Catalog @($marked) -Model 'fixture' -Marker 'unique-marker') 'array catalog marker'
Assert-True (Test-DesktopCatalogMarker -Catalog ([pscustomobject]@{models=@($marked)}) -Model 'fixture' -Marker 'unique-marker') 'wrapped catalog marker'
Assert-True (-not (Test-DesktopCatalogMarker -Catalog @($unmarked) -Model 'fixture' -Marker 'unique-marker')) 'bundled fallback is not proof'
Assert-True (-not (Test-DesktopCatalogMarker -Catalog @($marked,$unmarked) -Model 'fixture' -Marker 'unique-marker')) 'ambiguous model identity rejected'
foreach ($length in @(4, 5)) {
    $stream = [IO.MemoryStream]::new([byte[]]::new($length))
    if ($length -eq 4) {
        $bytes = Read-DesktopDiagnosticBytes -Stream $stream -MaxBytes 4
        Assert-True ($bytes.Length -eq 4) 'diagnostic exact byte limit is accepted'
    } else {
        Assert-Throws { Read-DesktopDiagnosticBytes -Stream $stream -MaxBytes 4 } 'diagnostic oversized body is rejected before parsing'
    }
    Assert-True (-not $stream.CanRead) 'diagnostic stream is closed on success and failure'
}
Assert-Throws { Get-DesktopDiagnosticCatalog -Uri 'https://external.example/codex/v1/models' } 'diagnostic fetch stays local'

foreach ($kind in @('compressed', 'oversized')) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $server = Start-ThreadJob -ArgumentList $listener,$kind -ScriptBlock {
        param($Listener, $Kind)
        $accept = $Listener.AcceptTcpClientAsync()
        if (-not $accept.Wait(5000)) { throw 'No bounded diagnostic request arrived.' }
        $connection = $accept.Result
        try {
            $stream = $connection.GetStream()
            $stream.ReadTimeout = 3000
            $buffer = [byte[]]::new(4096)
            $null = $stream.Read($buffer)
            $headers = if ($Kind -eq 'compressed') {
                "Content-Encoding: gzip`r`nContent-Length: 1"
            } else {
                'Content-Length: 4194305'
            }
            $response = [Text.Encoding]::ASCII.GetBytes("HTTP/1.1 200 OK`r`n$headers`r`nConnection: close`r`n`r`n")
            $stream.Write($response)
        } finally { $connection.Dispose() }
    }
    try {
        Assert-Throws { Get-DesktopDiagnosticCatalog -Uri "http://127.0.0.1:$port/codex/v1/models" } 'oversized/compressed diagnostic rejected without body allocation'
        $null = Wait-Job $server -Timeout 8
        if ($server.State -ne 'Completed') { throw 'Diagnostic response fixture did not complete.' }
        Receive-Job $server -ErrorAction Stop | Out-Null
    } finally {
        Stop-Job $server
        Remove-Job $server
        $listener.Stop()
    }
}
Write-Output "$passed PowerShell assertions passed."
