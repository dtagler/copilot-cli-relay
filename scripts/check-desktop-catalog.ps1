#requires -Version 7
<#
Check that the installed bundled engine consumes relay-shaped catalog metadata.
Uses a temporary CODEX_HOME and loopback catalog server with a unique marker.
No inference, app UI automation, user-config writes, or installed-app changes.
#>
[CmdletBinding()]
param([string]$Model = 'gpt-6-astra')
$ErrorActionPreference = 'Stop'
$requestedModel = $Model
. (Join-Path $PSScriptRoot 'codex-desktop.ps1')
$Model = $requestedModel
$package = Get-DesktopPackage
if (-not $package) { throw 'Expected installed OpenAI.Codex package.' }
$engine = Join-Path $package.InstallLocation 'app/resources/codex.exe'
$version = Invoke-DesktopEngineVersion -Engine $engine
$powerShellPath = Resolve-DesktopPowerShell
$catalog = Get-DesktopDiagnosticCatalog -Uri 'http://127.0.0.1:4141/codex/v1/models'
$selected = @($catalog.models | Where-Object { $_.slug -ceq $Model })
if ($selected.Count -ne 1) { throw 'Expected exactly one selected model in the relay catalog.' }
$marker = 'relay-catalog-proof-' + [guid]::NewGuid().ToString('N')
$selected[0].display_name = $marker
$body = [Text.Encoding]::UTF8.GetBytes(($catalog | ConvertTo-Json -Depth 100 -Compress))
if ($body.Length -gt 4MB) { throw 'Catalog exceeds the diagnostic limit.' }
$parent = 'C:\temp'
if (-not (Test-Path -LiteralPath $parent)) { $null = New-Item -ItemType Directory -Path $parent }
if ((Get-Item -LiteralPath $parent).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'Refusing linked diagnostic parent.'
}
$root = Join-Path $parent ('relay-catalog-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $root
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$job = $null
$process = $null
$started = $false
try {
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $snippet = Get-DesktopRelayConfig -Model $Model -ProviderId 'qualification' `
        -BaseUrl "http://127.0.0.1:$port/codex/v1" -PowerShellPath $powerShellPath
    [IO.File]::WriteAllText((Join-Path $root 'config.toml'), $snippet)
    $job = Start-ThreadJob -ArgumentList $listener,$body,(Get-DesktopCatalogRequestPattern) -ScriptBlock {
        param($Listener, $Body, $RequestPattern)
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        $served = 0
        while ([DateTime]::UtcNow -lt $deadline) {
            if (-not $Listener.Pending()) { Start-Sleep -Milliseconds 50; continue }
            $client = $Listener.AcceptTcpClient()
            try {
                $stream = $client.GetStream()
                $stream.ReadTimeout = 3000
                $buffer = [byte[]]::new(16384)
                $length = 0
                do {
                    $count = $stream.Read($buffer, $length, $buffer.Length - $length)
                    if ($count -eq 0) { throw 'Incomplete diagnostic HTTP request.' }
                    $length += $count
                    $headers = [Text.Encoding]::ASCII.GetString($buffer, 0, $length)
                } while (-not $headers.Contains("`r`n`r`n") -and $length -lt $buffer.Length)
                if ($headers -notmatch $RequestPattern -or
                    -not $headers.Contains("`r`n`r`n")) {
                    throw 'Only the diagnostic model catalog GET is allowed.'
                }
                $response = [Text.Encoding]::ASCII.GetBytes(
                    "HTTP/1.1 200 OK`r`nContent-Type: application/json`r`nContent-Length: $($Body.Length)`r`nConnection: close`r`n`r`n"
                )
                $stream.Write($response)
                $stream.Write($Body)
                $served++
                break
            } finally { $client.Dispose() }
        }
        if ($served -ne 1) { throw 'The bundled engine did not request the diagnostic catalog.' }
        $served
    }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = [Diagnostics.ProcessStartInfo]::new()
    $process.StartInfo.FileName = $engine
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    $process.StartInfo.WorkingDirectory = $root
    # Do not inherit API keys, proxy credentials, or the normal user's caches.
    # These changes apply only to this diagnostic child, never the host/app.
    $process.StartInfo.Environment.Clear()
    foreach ($name in @('SystemRoot', 'WINDIR', 'PATH', 'ComSpec')) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value) { $process.StartInfo.Environment[$name] = $value }
    }
    foreach ($name in @('USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP')) {
        $process.StartInfo.Environment[$name] = $root
    }
    $process.StartInfo.Environment['CODEX_HOME'] = $root
    foreach ($argument in @('debug', 'models')) { $process.StartInfo.ArgumentList.Add($argument) }
    $started = $process.Start()
    $stdout = [char[]]::new(4MB + 1)
    $stderr = [char[]]::new(16385)
    $outTask = $process.StandardOutput.ReadBlockAsync($stdout, 0, $stdout.Length)
    $errTask = $process.StandardError.ReadBlockAsync($stderr, 0, $stderr.Length)
    if (-not $process.WaitForExit(30000)) { throw 'Bundled catalog probe timed out.' }
    if (-not $outTask.Wait(1000) -or -not $errTask.Wait(1000) -or
        $outTask.Result -gt 4MB -or $errTask.Result -gt 16384 -or $process.ExitCode -ne 0) {
        throw 'Bundled catalog probe failed; private diagnostic output was suppressed.'
    }
    try {
        $parsed = [string]::new($stdout, 0, $outTask.Result) | ConvertFrom-Json -Depth 100
    } catch {
        throw 'Bundled catalog output was not valid JSON; private diagnostic output was suppressed.'
    }
    $models = if ($parsed -is [Array]) { $parsed } else { $parsed.models }
    if (-not (Test-DesktopCatalogMarker -Catalog $parsed -Model $Model -Marker $marker)) {
        throw 'Catalog marker missing; bundled fallback is not proof of relay discovery.'
    }
    $null = Wait-Job $job -Timeout 3
    if ($job.State -ne 'Completed') { throw 'Diagnostic catalog server did not complete.' }
    $served = Receive-Job $job -ErrorAction Stop
    if ($served -ne 1) { throw 'Diagnostic catalog request count mismatch.' }
    Write-Output "Bundled engine: $version"
    Write-Output "Relay catalog marker consumed: model=$Model models=$(@($models).Count)"
    Write-Output 'Isolated config/catalog compatibility passed. No inference or GUI qualification was performed.'
} finally {
    if ($process) {
        if ($started -and -not $process.HasExited) { $process.Kill($true); $null = $process.WaitForExit(1000) }
        $process.Dispose()
    }
    if ($job) { Stop-Job $job; Remove-Job $job }
    $listener.Stop()
    $resolved = [IO.Path]::GetFullPath($root)
    if ([IO.Path]::GetDirectoryName($resolved) -ne [IO.Path]::GetFullPath($parent) -or
        [IO.Path]::GetFileName($resolved) -notmatch '\Arelay-catalog-[0-9a-f]{32}\z' -or
        ((Get-Item -LiteralPath $resolved).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Refusing cleanup outside the recorded task-specific diagnostic root.'
    }
    $entries = @(Get-ChildItem -LiteralPath $resolved -Force -Recurse)
    if (@($entries | Where-Object {
            ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            -not $_.FullName.StartsWith($resolved + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
        }).Count) {
        throw 'Refusing cleanup of linked or unexpected diagnostic paths.'
    }
    Remove-Item -LiteralPath $resolved -Recurse
}
