#requires -Version 7
# Synthetic text only. Do not invoke the config writer or read real user files.
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot '../../scripts/codex-config.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'CLI helper failed to parse.' }
foreach ($name in @('Remove-TomlTableTree','Remove-LegacyProfileTables')) {
    $node = $ast.Find({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    . ([scriptblock]::Create($node.Extent.Text))
}
$passed = 0
function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "FAILED: $Message" }
    $script:passed++
}
$snippetNode = $ast.Find({
    param($n)
    $n -is [Management.Automation.Language.AssignmentStatementAst] -and
    $n.Left.Extent.Text -eq '$providerSnippet'
}, $true)
$literal = $snippetNode.Right.Find({
    param($n)
    $n -is [Management.Automation.Language.StringConstantExpressionAst]
}, $true)
$snippet = $literal.Value
Assert-True ($snippet -match '(?s)\[model_providers\.copilot\][^\[]*capabilities\.remote_compaction = "unsupported"') 'local compaction belongs to the provider, not its auth table'
Assert-True ([regex]::Matches($snippet, '(?m)^capabilities\.remote_compaction\s*=').Count -eq 1) 'provider compaction capability is assigned exactly once'
foreach ($source in @('', "[model_providers.copilot]`nname = 'old'`n")) {
    $result = Remove-TomlTableTree -Text $source -TableName 'model_providers.copilot'
    Assert-True ($result -eq '') 'fresh, empty and provider-only setups supported'
    Assert-True ((Remove-LegacyProfileTables -Text $result) -eq '') 'empty intermediate text supported'
}
$preserved = "[model_providers.copilot_desktop_pilot]`nname = 'desktop'`n[unrelated]`nprofile = `"copilot`"`n"
foreach ($header in @('[model_providers.copilot] # comment', '  [ model_providers . copilot ]',
    '[model_providers."copilot"]', "['model_providers'.'copilot']")) {
    $source = "model = 'fixture'`n$header`nname = 'old'`n[model_providers.copilot.auth] # auth`ncommand = 'fixture'`n" + $preserved
    $result = Remove-TomlTableTree -Text $source -TableName 'model_providers.copilot'
    Assert-True ($result -ceq ("model = 'fixture'`n" + $preserved)) 'commented/quoted managed tables removed without touching desktop'
}
$source = "profile = 'copilot' # legacy root`n[profiles.copilot] # legacy`nmodel = 'fixture'`n" + $preserved
Assert-True ((Remove-LegacyProfileTables -Text $source) -ceq $preserved) 'legacy root and table removed; unrelated profile field retained'
foreach ($source in @(
    'model_providers.copilot.name = "inline"',
    "[model_providers]`ncopilot = {name='inline'}",
    "[[model_providers.copilot]]`nname = 'array'",
    '[model_providers."cop\u0069lot"]',
    "message = '''`n[model_providers.copilot]`n'''",
    "[model_providers.copilot] unexpected"
)) {
    $threw = $false
    try { Remove-TomlTableTree -Text $source -TableName 'model_providers.copilot' | Out-Null } catch { $threw = $true }
    Assert-True $threw 'ambiguous syntax fails before a write'
}
Write-Output "$passed CLI-config PowerShell assertions passed."
