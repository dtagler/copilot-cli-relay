#requires -Version 7
param([Parameter(Mandatory)][string]$Image)
$ErrorActionPreference = 'Stop'
$imageId = docker image inspect $Image --format '{{.Id}}'
if ($LASTEXITCODE -ne 0 -or $imageId -notmatch '\Asha256:[0-9a-f]{64}\z') {
    throw 'Build the named production image first; this test never pulls images.'
}
$owner = [guid]::NewGuid().ToString('N')
$server = 'relay-drain-' + $owner
$client = 'relay-drain-client-' + $owner
$fixture = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../tests/integration/stream_shutdown_fixture.py'))
if (-not (Test-Path -LiteralPath $fixture -PathType Leaf) -or
    ((Get-Item -LiteralPath $fixture).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'Expected an ordinary repository fixture file.'
}
$mount = "type=bind,source=$fixture,target=/fixture.py,readonly"
$process = $null
$processStarted = $false
try {
    docker run -d --pull never --name $server --label "relay.test.owner=$owner" --network none `
        --read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m --cap-drop ALL `
        --security-opt no-new-privileges:true --memory 1g --pids-limit 128 --mount $mount `
        --env COPILOT_GITHUB_TOKEN=gho_synthetic_fixture `
        --env COPILOT_API_BASE=http://127.0.0.1:4142 --env COPILOT_ALLOW_INSECURE_API_BASE=1 $imageId | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Drain fixture container failed to start.' }
    docker exec -d $server /app/.venv/bin/python /fixture.py upstream
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic upstream failed to start.' }
    docker exec $server /app/.venv/bin/python /fixture.py probe
    if ($LASTEXITCODE -ne 0) { throw 'Drain fixture readiness failed.' }

    # The client is in a separate container, so stopping the server cannot kill
    # the observer before it verifies the final event. No host ports are exposed.
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = [Diagnostics.ProcessStartInfo]::new()
    $process.StartInfo.FileName = (Get-Command docker -CommandType Application | Select-Object -First 1).Source
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    foreach ($argument in @(
        'run','--rm','--pull','never','--name',$client,'--label',"relay.test.owner=$owner",
        '--network',"container:$server",'--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges:true','--memory','128m','--pids-limit','64',
        '--mount',$mount,'--entrypoint','/app/.venv/bin/python',$imageId,'/fixture.py','client'
    )) { $process.StartInfo.ArgumentList.Add($argument) }
    $processStarted = $process.Start()
    $stderr = [char[]]::new(4097)
    $errTask = $process.StandardError.ReadBlockAsync($stderr, 0, $stderr.Length)
    $started = $process.StandardOutput.ReadLineAsync()
    if (-not $started.Wait(10000) -or $started.Result -ne 'stream-started') {
        throw 'Client did not observe an active stream.'
    }
    docker stop --time 30 $server | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Graceful stop failed.' }
    if (-not $process.WaitForExit(10000)) { throw 'Drain observer did not finish.' }
    $completed = $process.StandardOutput.ReadLineAsync()
    if (-not $completed.Wait(1000) -or $completed.Result -ne 'stream-completed' -or $process.ExitCode -ne 0) {
        throw 'Active stream did not complete across shutdown.'
    }
    if (-not $errTask.Wait(1000) -or $errTask.Result -ne 0) { throw 'Drain observer reported an error.' }
    $exitCode = docker inspect $server --format '{{.State.ExitCode}}'
    if ($LASTEXITCODE -ne 0 -or $exitCode -ne '0') { throw 'Server did not exit cleanly after draining.' }
    Write-Output 'Network-isolated active stream completed through graceful shutdown; server exit 0.'
} finally {
    foreach ($name in @($client, $server)) {
        $actual = docker inspect $name --format '{{index .Config.Labels "relay.test.owner"}}' 2>$null
        if ($LASTEXITCODE -eq 0) {
            if ($actual -ne $owner) { throw 'Refusing cleanup: fixture ownership mismatch.' }
            docker rm -f $name | Out-Null
        }
    }
    if ($process) {
        if ($processStarted -and -not $process.HasExited) { $process.Kill(); $null = $process.WaitForExit(1000) }
        $process.Dispose()
    }
}
