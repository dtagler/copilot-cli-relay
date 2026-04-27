<#
.SYNOPSIS
  Extracts the GitHub Copilot CLI's OAuth token from Windows Credential Manager
  and writes it into .env so the docker-compose proxy service can use it.

.DESCRIPTION
  The standalone `copilot` CLI stores its OAuth token in Credential Manager under
  target `copilot-cli/https://github.com:<login>` as a 40-byte UTF-8 ASCII blob.
  Linux containers cannot read DPAPI-protected credentials directly, so we extract
  on the host and pass it via .env.

  Re-run this script after the `copilot` CLI rotates the token (typically after
  re-authenticating).

.PARAMETER EnvFile
  Path to the .env file to update. Defaults to .\.env in the script's parent dir.

.PARAMETER Login
  GitHub login. Defaults to whatever is in ~\.copilot\config.json.
#>
[CmdletBinding()]
param(
  [string]$EnvFile,
  [string]$Login
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) { $EnvFile = Join-Path $repoRoot '.env' }

if (-not $Login) {
  $cfgPath = Join-Path $env:USERPROFILE '.copilot\config.json'
  if (-not (Test-Path $cfgPath)) {
    throw "Could not find $cfgPath. Pass -Login <your-github-login> manually."
  }
  $cfg = Get-Content -Raw $cfgPath | ConvertFrom-Json
  $Login = $cfg.lastLoggedInUser.login
  if (-not $Login) { throw "config.json missing lastLoggedInUser.login. Pass -Login manually." }
}

$target = "copilot-cli/https://github.com:$Login"
Write-Host "Reading credential: $target" -ForegroundColor Cyan

$src = @"
using System;
using System.Runtime.InteropServices;
public class _CM {
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct C {
    public uint Flags; public uint Type; public IntPtr T; public IntPtr Cm;
    public System.Runtime.InteropServices.ComTypes.FILETIME LW;
    public uint Sz; public IntPtr B;
    public uint Persist; public uint AC; public IntPtr Attr; public IntPtr TA; public IntPtr UN;
  }
  [DllImport("Advapi32.dll", EntryPoint="CredReadW", CharSet=CharSet.Unicode, SetLastError=true)]
  public static extern bool R(string t, uint y, uint f, out IntPtr p);
  [DllImport("Advapi32.dll", EntryPoint="CredFree")]
  public static extern void F(IntPtr p);
  public static byte[] Read(string t) {
    IntPtr p;
    if (!R(t, 1, 0, out p)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    try {
      var c = (C)Marshal.PtrToStructure(p, typeof(C));
      var b = new byte[c.Sz];
      Marshal.Copy(c.B, b, 0, (int)c.Sz);
      return b;
    } finally { F(p); }
  }
}
"@
if (-not ([System.Management.Automation.PSTypeName]'_CM').Type) {
  Add-Type -TypeDefinition $src -Language CSharp
}

try {
  $bytes = [_CM]::Read($target)
} catch {
  throw "Credential not found ($target). Have you run ``copilot`` and authenticated? Underlying error: $($_.Exception.Message)"
}

$token = [System.Text.Encoding]::UTF8.GetString($bytes)
if (-not ($token.StartsWith('gho_') -or $token.StartsWith('ghu_'))) {
  throw "Credential blob doesn't look like a GitHub OAuth token (got $($token.Substring(0,[Math]::Min(8,$token.Length)))…)"
}
Write-Host ("Token OK: prefix={0} length={1}" -f $token.Substring(0,4), $token.Length) -ForegroundColor Green

$lines = @()
if (Test-Path $EnvFile) {
  $lines = Get-Content $EnvFile
}
$found = $false
$out = foreach ($line in $lines) {
  if ($line -match '^\s*COPILOT_GITHUB_TOKEN\s*=') {
    $found = $true
    "COPILOT_GITHUB_TOKEN=$token"
  } else {
    $line
  }
}
if (-not $found) {
  $out = @($out) + "COPILOT_GITHUB_TOKEN=$token"
}

$tmp = "$EnvFile.tmp"
# Create the temp file empty first so we can lock down its ACL BEFORE the
# token bytes ever hit disk. This prevents other Windows users on the host
# (or stale group permissions inherited from the parent dir) from reading
# the OAuth token between write and rename.
New-Item -Path $tmp -ItemType File -Force | Out-Null
$aclHardened = $false
try {
  $acl = New-Object System.Security.AccessControl.FileSecurity
  $acl.SetAccessRuleProtection($true, $false)  # disable inheritance, drop inherited ACEs
  $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $me, 'FullControl', 'Allow'
  )
  $acl.AddAccessRule($rule)
  # SYSTEM keeps full control so backups/AV still work.
  $system = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
  $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    $system, 'FullControl', 'Allow'
  )))
  Set-Acl -Path $tmp -AclObject $acl
  $aclHardened = $true
} catch {
  Remove-Item -Force $tmp -ErrorAction SilentlyContinue
  throw "Refusing to write token: could not harden ACL on $tmp ($($_.Exception.Message)). " +
        "Re-run from an elevated PowerShell, or set the token manually in $EnvFile " +
        "after locking it down with `icacls $EnvFile /inheritance:r /grant:r `"$($env:USERNAME):(R,W)`"`."
}
$out -join "`n" | Set-Content -NoNewline -Encoding ascii -Path $tmp
Move-Item -Force $tmp $EnvFile
if ($aclHardened) {
  Write-Host "Wrote $EnvFile (ACL: inheritance disabled; explicit user + SYSTEM ACEs; BUILTIN\Administrators retains FullControl in practice when run by an account in that group)" -ForegroundColor Green
} else {
  Write-Host "Wrote $EnvFile (WARNING: ACL not hardened)" -ForegroundColor Yellow
}
Write-Host "Now run: docker compose up --build" -ForegroundColor Yellow
