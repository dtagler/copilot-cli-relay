#requires -Version 7
param([Parameter(Mandatory)][string]$Image)
$ErrorActionPreference = 'Stop'
$imageId = docker image inspect $Image --format '{{.Id}}'
if ($LASTEXITCODE -ne 0 -or $imageId -notmatch '\Asha256:[0-9a-f]{64}\z') {
    throw 'Build the named production image first; no image is pulled by this test.'
}
$name = 'relay-smoke-' + [guid]::NewGuid().ToString('N')
$owner = [guid]::NewGuid().ToString('N')
$created = $false
try {
    $id = docker run -d --pull never --name $name --label "relay.test.owner=$owner" --network none `
        --read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m `
        --cap-drop ALL --security-opt no-new-privileges:true --memory 1g --pids-limit 128 `
        --env COPILOT_GITHUB_TOKEN=gho_synthetic_fixture $imageId
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic smoke container failed to start.' }
    $created = $true
    $probe = @'
import importlib.util, json, os, pathlib, shutil, time, urllib.request
assert os.getuid() != 0
assert not pathlib.Path("/app/src").exists()
assert not pathlib.Path("/app/tests").exists()
assert shutil.which("uv") is None
for module in ("pytest", "ruff", "coverage"):
    assert importlib.util.find_spec(module) is None, module
for attempt in range(50):
    try:
        with urllib.request.urlopen("http://127.0.0.1:4141/livez", timeout=1) as response:
            data = json.load(response)
            assert response.status == 200 and data["ok"] is True
        break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit("liveness did not become ready")
print("Non-root, runtime-only, network-isolated image smoke passed.")
'@
    docker exec $id /app/.venv/bin/python -c $probe
    if ($LASTEXITCODE -ne 0) { throw 'Production image smoke failed.' }
    $clock = [Diagnostics.Stopwatch]::StartNew()
    docker stop --time 30 $id | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Smoke container shutdown failed.' }
    $exitCode = docker inspect $id --format '{{.State.ExitCode}}'
    if ($LASTEXITCODE -ne 0 -or $exitCode -ne '0' -or $clock.Elapsed.TotalSeconds -gt 30) {
        throw 'Production image did not shut down cleanly within the grace period.'
    }
    Write-Output 'Production image graceful shutdown passed.'
} finally {
    if ($created) {
        $actual = docker inspect $name --format '{{index .Config.Labels "relay.test.owner"}}'
        if ($LASTEXITCODE -eq 0 -and $actual -eq $owner) {
            docker rm -f $name | Out-Null
        } else { throw 'Refusing cleanup: test container ownership could not be verified.' }
    }
}
