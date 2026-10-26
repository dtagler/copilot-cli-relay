#requires -Version 7
# Extract functions only. Never run the interactive menu or actual Docker.
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot '../../scripts/proxy.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Proxy helper failed to parse.' }
foreach ($name in @('Invoke-RelayDocker','Invoke-RelayContainerAction')) {
    $node = $ast.Find({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    . ([scriptblock]::Create($node.Extent.Text))
}
$passed = 0
function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "FAILED: $Message" }
    $script:passed++
}
$calls = [Collections.Generic.List[string]]::new()
$failAt = 0
$running = $true
function docker {
    $script:calls.Add(($args -join ' '))
    $global:LASTEXITCODE = if ($script:failAt -eq $script:calls.Count) { 1 } else { 0 }
    if (($args -join ' ') -eq 'compose ps -q proxy' -and $script:running) { 'synthetic-container' }
}
try {
    foreach ($action in @('start','stop','restart','status','rebuild')) {
        $calls.Clear()
        $failAt = 1
        $threw = $false
        try { Invoke-RelayContainerAction -Action $action } catch { $threw = $true }
        Assert-True $threw 'native failure must terminate the action'
        Assert-True ($calls.Count -eq 1) 'native failure prevents dependent calls'
    }
    $calls.Clear()
    $failAt = 2
    $threw = $false
    try { Invoke-RelayContainerAction -Action rebuild } catch { $threw = $true }
    Assert-True ($threw -and $calls.Count -eq 2) 'failed recreation does not report success'
    $calls.Clear()
    $failAt = 0
    Invoke-RelayContainerAction -Action rebuild
    Assert-True (($calls -join ';') -eq 'compose build proxy;compose up -d --no-build --force-recreate proxy;compose ps proxy') 'successful build permits explicit recreation'
    $calls.Clear()
    $running = $false
    Invoke-RelayContainerAction -Action restart
    Assert-True (($calls -join ';') -eq 'compose ps -q proxy;compose up -d proxy;compose ps proxy') 'absent container is started'
    $calls.Clear()
    $running = $true
    Invoke-RelayContainerAction -Action restart
    Assert-True (($calls -join ';') -eq 'compose ps -q proxy;compose restart proxy;compose ps proxy') 'running container is restarted'
} finally { Remove-Item Function:docker }
Write-Output "$passed lifecycle PowerShell assertions passed."
