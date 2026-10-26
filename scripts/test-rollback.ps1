#requires -Version 7
param([Parameter(Mandatory)][string]$Image)
$ErrorActionPreference = 'Stop'
$imageId = docker image inspect $Image --format '{{.Id}}'
if ($LASTEXITCODE -ne 0 -or $imageId -notmatch '\Asha256:[0-9a-f]{64}\z') {
    throw 'The exact rollback image must already be available locally.'
}
$old = [Environment]::GetEnvironmentVariable('RELAY_ROLLBACK_IMAGE', 'Process')
Push-Location (Split-Path $PSScriptRoot)
try {
    $env:RELAY_ROLLBACK_IMAGE = $imageId
    $raw = docker compose --env-file .env.example -f docker-compose.yml -f docker-compose.rollback.yml config --no-env-resolution --format json
    if ($LASTEXITCODE -ne 0) { throw 'Rollback Compose validation failed.' }
    $service = (($raw -join "`n") | ConvertFrom-Json).services.proxy
    if ($service.image -ne $imageId -or $service.volumes -or $service.read_only -ne $true -or
        $service.ports[0].host_ip -ne '127.0.0.1' -or
        ($service.healthcheck.test -join ' ') -notlike '*/claude/healthz*') {
        throw 'Rollback configuration does not preserve the deployment contract.'
    }
} finally {
    Pop-Location
    [Environment]::SetEnvironmentVariable('RELAY_ROLLBACK_IMAGE', $old, 'Process')
}
$owner = [guid]::NewGuid().ToString('N')
$name = 'relay-rollback-test-' + $owner
try {
    docker run -d --pull never --name $name --label "relay.test.owner=$owner" --network none `
        --read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m --cap-drop ALL `
        --security-opt no-new-privileges:true --memory 1g --pids-limit 128 `
        --env COPILOT_GITHUB_TOKEN=gho_synthetic_fixture --env COPILOT_API_BASE=http://127.0.0.1:9 `
        --env COPILOT_ALLOW_INSECURE_API_BASE=1 $imageId | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Rollback rehearsal container failed to start.' }
    $probe = @'
import json, os, time, urllib.request
assert os.getuid() != 0
for attempt in range(50):
    try:
        with urllib.request.urlopen("http://127.0.0.1:4141/claude/healthz", timeout=3) as response:
            assert response.status == 200
            assert json.load(response)["upstream_ok"] is False
        break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit("Legacy health route unavailable")
print("Offline rollback image starts non-root/read-only; legacy health route works.")
'@
    docker exec $name /app/.venv/bin/python -c $probe
    if ($LASTEXITCODE -ne 0) { throw 'Offline rollback health rehearsal failed.' }
    Write-Output 'No live rollback, credentials, or inference were used.'
} finally {
    $actual = docker inspect $name --format '{{index .Config.Labels "relay.test.owner"}}' 2>$null
    if ($LASTEXITCODE -eq 0) {
        if ($actual -ne $owner) { throw 'Refusing cleanup: rollback fixture ownership mismatch.' }
        docker rm -f $name | Out-Null
    }
}
