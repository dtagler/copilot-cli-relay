#requires -Version 7
param(
    [string]$ProjectRoot = (Split-Path $PSScriptRoot),
    [ValidatePattern('\Asha256:[0-9a-f]{64}\z')]
    [string]$RollbackImage
)
# Never read the real .env or create containers. Some Compose versions still
# require env_file to exist with --no-env-resolution, so use a disposable copy.
$ErrorActionPreference = 'Stop'
$scratch = Join-Path ([IO.Path]::GetTempPath()) ('relay-compose-check-' + [guid]::NewGuid().ToString('N'))
$copyFiles = @('.env.example', 'docker-compose.yml', 'docker-compose.dev.yml')
if ($RollbackImage) { $copyFiles += 'docker-compose.rollback.yml' }
$ownedFiles = @('.gitignore', '.env') + $copyFiles
$pushed = $false
if (Test-Path -LiteralPath $scratch) { throw 'Validation directory unexpectedly exists.' }
$null = New-Item -ItemType Directory -Path $scratch
try {
    Set-Content -LiteralPath (Join-Path $scratch '.gitignore') -Value '.env'
    Set-Content -LiteralPath (Join-Path $scratch '.env') -Value '' -NoNewline
    foreach ($name in $copyFiles) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $name) -Destination (Join-Path $scratch $name)
    }
    Push-Location $scratch
    $pushed = $true
    $raw = docker compose --env-file .env.example -f docker-compose.yml config --no-env-resolution --format json
    if ($LASTEXITCODE -ne 0) { throw 'Production Compose validation failed.' }
    $config = ($raw -join "`n") | ConvertFrom-Json
    $service = $config.services.proxy
    if ($service.build.target -ne 'production' -or $service.volumes -or
        $service.read_only -ne $true -or $service.ports.Count -ne 1 -or
        $service.ports[0].host_ip -ne '127.0.0.1' -or
        $service.cap_drop -notcontains 'ALL' -or
        $service.security_opt -notcontains 'no-new-privileges:true' -or
        -not $service.mem_limit -or -not $service.pids_limit -or
        -not $service.logging.options.'max-size' -or
        ($service.healthcheck.test -join ' ') -notlike '*/livez*') {
        throw 'Production deployment safety contract failed.'
    }
    $raw = docker compose --env-file .env.example -f docker-compose.dev.yml config --no-env-resolution --format json
    if ($LASTEXITCODE -ne 0) { throw 'Development Compose validation failed.' }
    $dev = ($raw -join "`n") | ConvertFrom-Json
    if ($dev.name -eq $config.name -or $dev.services.proxy.build.target -ne 'development' -or
        $dev.services.proxy.ports[0].host_ip -ne '127.0.0.1' -or
        $dev.services.proxy.ports[0].published -eq $service.ports[0].published) {
        throw 'Development and production must be separate stacks and ports.'
    }
    if (@($dev.services.proxy.tmpfs).Count -ne 1 -or
        $dev.services.proxy.tmpfs[0] -ne '/tmp:rw,nosuid,nodev,size=128m') {
        throw 'Development scratch mount must be one bounded tmpfs specification.'
    }
    if ($RollbackImage) {
        $oldRollbackImage = [Environment]::GetEnvironmentVariable('RELAY_ROLLBACK_IMAGE', 'Process')
        try {
            $env:RELAY_ROLLBACK_IMAGE = $RollbackImage
            $raw = docker compose --env-file .env.example -f docker-compose.yml -f docker-compose.rollback.yml config --no-env-resolution --format json
            if ($LASTEXITCODE -ne 0) { throw 'Rollback Compose validation failed.' }
            $rollback = (($raw -join "`n") | ConvertFrom-Json).services.proxy
            if ($rollback.image -ne $RollbackImage -or $rollback.volumes -or
                $rollback.read_only -ne $true -or $rollback.ports.Count -ne 1 -or
                $rollback.ports[0].host_ip -ne '127.0.0.1' -or
                $rollback.cap_drop -notcontains 'ALL' -or
                $rollback.security_opt -notcontains 'no-new-privileges:true' -or
                -not $rollback.mem_limit -or -not $rollback.pids_limit -or
                -not $rollback.logging.options.'max-size' -or
                ($rollback.healthcheck.test -join ' ') -notlike '*/claude/healthz*') {
                throw 'Rollback configuration does not preserve the deployment contract.'
            }
        } finally {
            if ($null -eq $oldRollbackImage) {
                [Environment]::SetEnvironmentVariable('RELAY_ROLLBACK_IMAGE', [NullString]::Value, 'Process')
            } else {
                [Environment]::SetEnvironmentVariable('RELAY_ROLLBACK_IMAGE', $oldRollbackImage, 'Process')
            }
        }
        Write-Output 'Rollback Compose safety checks passed.'
    }
    Write-Output 'Production and development Compose safety checks passed.'
} finally {
    if ($pushed) { Pop-Location }
    # Only remove our explicitly named files, never recurse through a directory.
    # Refuse cleanup if a link or an unexpected directory has replaced the root.
    $item = Get-Item -LiteralPath $scratch -Force
    if (-not $item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        $item.FullName -ne [IO.Path]::GetFullPath($scratch)) {
        throw 'Refusing cleanup of an unexpected validation directory.'
    }
    foreach ($name in $ownedFiles) {
        $path = Join-Path $scratch $name
        if (Test-Path -LiteralPath $path) {
            $file = Get-Item -LiteralPath $path -Force
            if ($file.PSIsContainer -or ($file.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'Refusing cleanup of an unexpected validation file.'
            }
            Remove-Item -LiteralPath $path -Force
        }
    }
    # An unexpected extra file prevents removal rather than broadening cleanup.
    [IO.Directory]::Delete($scratch, $false)
}
