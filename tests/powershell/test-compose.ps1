#requires -Version 7
# Synthetic Docker output only. No real credentials, containers, or builds.
$ErrorActionPreference = 'Stop'
$checker = Join-Path $PSScriptRoot '../../scripts/check-compose.ps1'
$source = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('relay-compose-test-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $fixture
$composeTestState = @{
    Passed = 0; Calls = 0; FailAt = 0; Unsafe = $false; WrongRollbackImage = $false
    WrongRollbackHealth = $false
    ScratchPaths = [Collections.Generic.List[string]]::new()
    Fixture = $fixture; Source = $source
}
function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "FAILED: $Message" }
    $composeTestState.Passed++
}
function docker {
    $composeTestState.Calls++
    $cwd = (Get-Location).Path
    $composeTestState.ScratchPaths.Add($cwd)
    Assert-True ($cwd -ne $composeTestState.Fixture -and $cwd -ne $composeTestState.Source) 'isolates validation from source'
    Assert-True ((Test-Path -LiteralPath '.env') -and (Get-Item -LiteralPath '.env').Length -eq 0) 'provides empty synthetic env_file for older Compose'
    Assert-True (($args -join ' ') -match 'config --no-env-resolution --format json') 'never resolves env_file or creates containers'
    $global:LASTEXITCODE = if ($composeTestState.FailAt -eq $composeTestState.Calls) { 1 } else { 0 }
    if ($global:LASTEXITCODE) { return }
    $development = $args -contains 'docker-compose.dev.yml'
    $rollback = $args -contains 'docker-compose.rollback.yml'
    if ($rollback) {
        Assert-True (Test-Path -LiteralPath 'docker-compose.rollback.yml') 'copies rollback override into isolated validation'
        Assert-True ($env:RELAY_ROLLBACK_IMAGE -eq ('sha256:' + ('a' * 64))) 'selects the requested exact rollback image'
    }
    @{
        name = if ($development) { 'relay-dev' } else { 'relay-prod' }
        services = @{
            proxy = @{
                build = @{target = if ($development) { 'development' } else { 'production' }}
                read_only = -not $composeTestState.Unsafe
                ports = @(@{host_ip = '127.0.0.1'; published = if ($development) { '4142' } else { '4141' }})
                cap_drop = @('ALL')
                security_opt = @('no-new-privileges:true')
                mem_limit = 1073741824
                pids_limit = 128
                logging = @{options = @{'max-size' = '10m'}}
                healthcheck = @{test = @('CMD', 'http://127.0.0.1:4141/livez')}
                tmpfs = @('/tmp:rw,nosuid,nodev,size=128m')
                image = if ($rollback -and -not $composeTestState.WrongRollbackImage) { $env:RELAY_ROLLBACK_IMAGE } else { 'different-image' }
            }
        }
    } | ForEach-Object {
        if ($rollback -and -not $composeTestState.WrongRollbackHealth) {
            $_.services.proxy.healthcheck.test = @('CMD', 'http://127.0.0.1:4141/claude/healthz')
        }
        $_ | ConvertTo-Json -Depth 8 -Compress
    }
}
try {
    Set-Content -LiteralPath (Join-Path $fixture '.gitignore') -Value '.env'
    foreach ($name in @('.env.example', 'docker-compose.yml', 'docker-compose.dev.yml', 'docker-compose.rollback.yml')) {
        Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $fixture $name)
    }
    $originalLocation = (Get-Location).Path
    & $checker -ProjectRoot $fixture
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $fixture '.env'))) 'does not create source .env in a clean checkout'
    Assert-True ($composeTestState.Calls -eq 2) 'validates both Compose stacks'
    Assert-True ((Get-Location).Path -eq $originalLocation) 'restores working directory'

    Set-Content -LiteralPath (Join-Path $fixture '.env') -Value 'synthetic-do-not-copy'
    $composeTestState.Calls = 0
    & $checker -ProjectRoot $fixture
    Assert-True ((Get-Content -LiteralPath (Join-Path $fixture '.env') -Raw).Trim() -eq 'synthetic-do-not-copy') 'preserves existing source env_file'

    foreach ($failure in @(1, 2)) {
        $composeTestState.Calls = 0
        $composeTestState.FailAt = $failure
        $threw = $false
        try { & $checker -ProjectRoot $fixture } catch { $threw = $true }
        Assert-True ($threw -and $composeTestState.Calls -eq $failure) 'native failure stops dependent validation'
        Assert-True ((Get-Location).Path -eq $originalLocation) 'failure restores working directory'
    }
    $composeTestState.FailAt = 0
    $composeTestState.Calls = 0
    $composeTestState.Unsafe = $true
    $threw = $false
    try { & $checker -ProjectRoot $fixture } catch { $threw = $true }
    Assert-True ($threw -and $composeTestState.Calls -eq 1) 'unsafe production configuration still fails closed'
    $composeTestState.Unsafe = $false
    $rollbackImage = 'sha256:' + ('a' * 64)
    $oldRollbackImage = [Environment]::GetEnvironmentVariable('RELAY_ROLLBACK_IMAGE', 'Process')
    try {
        Remove-Item -LiteralPath (Join-Path $fixture '.env')
        foreach ($existing in @($null, 'synthetic-existing-override')) {
            if ($existing) { Set-Content -LiteralPath (Join-Path $fixture '.env') -Value 'synthetic-do-not-copy' }
            if ($null -eq $existing) {
                [Environment]::SetEnvironmentVariable('RELAY_ROLLBACK_IMAGE', [NullString]::Value, 'Process')
            } else {
                [Environment]::SetEnvironmentVariable('RELAY_ROLLBACK_IMAGE', $existing, 'Process')
            }
            $composeTestState.Calls = 0
            & $checker -ProjectRoot $fixture -RollbackImage $rollbackImage
            Assert-True ($composeTestState.Calls -eq 3) 'validates production, development, and rollback'
            Assert-True ([Environment]::GetEnvironmentVariable('RELAY_ROLLBACK_IMAGE', 'Process') -eq $existing) 'restores existing or absent rollback environment'
            if ($existing) {
                Assert-True ((Get-Content -LiteralPath (Join-Path $fixture '.env') -Raw).Trim() -eq 'synthetic-do-not-copy') 'rollback preserves existing source env_file'
            } else {
                Assert-True (-not (Test-Path -LiteralPath (Join-Path $fixture '.env'))) 'rollback supports a clean checkout without creating source env_file'
            }
        }
        foreach ($failure in @('native', 'image', 'health')) {
            $composeTestState.Calls = 0
            $composeTestState.FailAt = if ($failure -eq 'native') { 3 } else { 0 }
            $composeTestState.WrongRollbackImage = $failure -eq 'image'
            $composeTestState.WrongRollbackHealth = $failure -eq 'health'
            $threw = $false
            try { & $checker -ProjectRoot $fixture -RollbackImage $rollbackImage } catch { $threw = $true }
            Assert-True ($threw -and $composeTestState.Calls -eq 3) 'rollback validation failures remain terminal'
            Assert-True ($env:RELAY_ROLLBACK_IMAGE -eq 'synthetic-existing-override') 'restores rollback environment on failure'
            Assert-True ((Get-Location).Path -eq $originalLocation) 'rollback failure restores working directory'
        }
        $composeTestState.Calls = 0
        $threw = $false
        try { & $checker -ProjectRoot $fixture -RollbackImage 'invalid-tag' } catch { $threw = $true }
        Assert-True ($threw -and $composeTestState.Calls -eq 0) 'rejects non-exact rollback image before Docker'
    } finally {
        if ($null -eq $oldRollbackImage) {
            [Environment]::SetEnvironmentVariable('RELAY_ROLLBACK_IMAGE', [NullString]::Value, 'Process')
        } else {
            [Environment]::SetEnvironmentVariable('RELAY_ROLLBACK_IMAGE', $oldRollbackImage, 'Process')
        }
    }
    foreach ($path in ($composeTestState.ScratchPaths | Select-Object -Unique)) {
        Assert-True (-not (Test-Path -LiteralPath $path)) 'cleans validation scratch after success and failure'
    }
} finally {
    Remove-Item Function:docker
    $global:LASTEXITCODE = 0
    $rootItem = Get-Item -LiteralPath $fixture -Force
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        $rootItem.FullName -ne [IO.Path]::GetFullPath($fixture)) {
        throw 'Refusing cleanup of a replaced fixture root.'
    }
    foreach ($name in @('.gitignore', '.env', '.env.example', 'docker-compose.yml', 'docker-compose.dev.yml', 'docker-compose.rollback.yml')) {
        $path = Join-Path $fixture $name
        if (Test-Path -LiteralPath $path) {
            $item = Get-Item -LiteralPath $path -Force
            if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'Refusing cleanup of an unexpected fixture file.'
            }
            Remove-Item -LiteralPath $path -Force
        }
    }
    [IO.Directory]::Delete($fixture, $false)
}
Write-Output "$($composeTestState.Passed) Compose validation assertions passed."
