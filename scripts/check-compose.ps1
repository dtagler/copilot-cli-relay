#requires -Version 7
# Read-only: never resolve the credential-bearing env_file or create containers.
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot)
try {
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
    Write-Output 'Production and development Compose safety checks passed.'
} finally { Pop-Location }
