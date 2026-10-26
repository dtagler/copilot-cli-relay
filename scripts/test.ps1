#requires -Version 7
param([string]$Image = 'copilot-cli-relay:dev-hardening')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot
$imageId = docker image inspect $Image --format '{{.Id}}'
if ($LASTEXITCODE -ne 0 -or $imageId -notmatch '\Asha256:[0-9a-f]{64}\z') {
    throw 'Build the development target first. This script does not install tools or pull images.'
}
$mounts = @(foreach ($item in @('src','tests','scripts','docs','assets','skill','README.md','ARCHITECTURE.md','GLOSSARY.md','LICENSE','pyproject.toml','uv.lock')) {
    '--mount'
    "type=bind,source=$(Join-Path $root $item),target=/app/$item,readonly"
})
docker run --rm --pull never --network none --read-only --tmpfs /tmp:rw,nosuid,nodev,size=128m `
    --env COVERAGE_FILE=/tmp/.coverage --env UV_CACHE_DIR=/tmp/uv-cache @mounts $imageId /bin/sh -c `
    'uv lock --check --offline && uv sync --check --frozen --offline --no-install-project --inexact && /app/.venv/bin/ruff check --no-cache src tests scripts && /app/.venv/bin/coverage run --source=copilot_cli_relay -m pytest tests/unit -q -p no:cacheprovider && /app/.venv/bin/coverage report --fail-under=98 --show-missing && /app/.venv/bin/python scripts/check-docs.py'
if ($LASTEXITCODE -ne 0) { throw 'Offline Python or documentation validation failed.' }
foreach ($test in Get-ChildItem (Join-Path $root 'tests/powershell') -Filter 'test-*.ps1' | Sort-Object Name) {
    & $test.FullName
    if (-not $?) { throw "PowerShell test failed: $($test.Name)" }
}
foreach ($file in @(Get-ChildItem (Join-Path $root 'scripts') -Filter '*.ps1') + @(Get-ChildItem (Join-Path $root 'tests/powershell') -Filter '*.ps1')) {
    $tokens = $null
    $errors = $null
    $null = [Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw "PowerShell syntax failed: $($file.Name)" }
}
Write-Output 'PowerShell syntax checks passed.'

# Parse actual helper output with the existing container's stdlib TOML parser.
# Only synthetic snippets cross stdin. No user configuration is read or mounted.
$cliAst = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $root 'scripts/codex-config.ps1'), [ref]$null, [ref]$null)
$providerNode = $cliAst.Find({
    param($node)
    $node -is [Management.Automation.Language.AssignmentStatementAst] -and
    $node.Left.Extent.Text -eq '$providerSnippet'
}, $true)
. (Join-Path $root 'scripts/codex-desktop.ps1')
$providerLiteral = $providerNode.Right.Find({
    param($node)
    $node -is [Management.Automation.Language.StringConstantExpressionAst]
}, $true)
$fixtures = @(@{provider='copilot'; config=$providerLiteral.Value})
$pwshPath = Resolve-DesktopPowerShell
foreach ($provider in @('copilot_desktop', 'copilot_desktop_pilot', 'custom_test')) {
    $fixtures += @{
        provider=$provider
        config=(Get-DesktopRelayConfig -Model 'fixture-"quoted"\variant' `
            -ProviderId $provider -BaseUrl 'http://127.0.0.1:4141/codex/v1' -PowerShellPath $pwshPath)
    }
}
$fixtures | ConvertTo-Json -Depth 5 -Compress |
    docker run --rm -i --pull never --network none --read-only `
        --mount "type=bind,source=$(Join-Path $root 'scripts/check-codex-config.py'),target=/app/scripts/check-codex-config.py,readonly" `
        $imageId /app/.venv/bin/python scripts/check-codex-config.py
if ($LASTEXITCODE -ne 0) { throw 'Generated Codex configuration semantic validation failed.' }
