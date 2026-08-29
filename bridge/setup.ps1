# One-time (and re-runnable) setup for the browser bridge, for Windows shells
# without bash. Same contract as setup.sh — see the comments there for why the
# state directory and the extension copy live outside the plugin.
#
#   .\setup.ps1              # set up, keep the existing token
#   .\setup.ps1 -Rotate      # new token (re-paste it into every profile popup)
param([switch]$Rotate)
$ErrorActionPreference = "Stop"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$HomeDir = if ($env:BROWSER_BRIDGE_HOME) { $env:BROWSER_BRIDGE_HOME } else { Join-Path $HOME ".claude-browser-bridge" }
$TokenFile = Join-Path $HomeDir "token"
$ExtDir = Join-Path $HomeDir "extension"

New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null

if ((Test-Path $TokenFile) -and (-not $Rotate)) {
  Write-Host "token: already set ($TokenFile)"
} else {
  $bytes = New-Object byte[] 24
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $token = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
  [IO.File]::WriteAllText($TokenFile, $token)
  Write-Host "token: generated ($TokenFile)"
}

if (Test-Path $ExtDir) { Remove-Item -Recurse -Force $ExtDir }
New-Item -ItemType Directory -Force -Path $ExtDir | Out-Null
Copy-Item (Join-Path $Here "extension\*") $ExtDir
Write-Host "extension: synced to $ExtDir"

[IO.File]::ReadAllText($TokenFile).Trim() | Set-Clipboard
Write-Host "token: copied to clipboard"

Write-Host @"

Next, in the Chrome profile you want Claude to drive:
  1. open  chrome://extensions  and turn on Developer mode
  2. Load unpacked -> $ExtDir
  3. click the Claude Bridge toolbar icon, paste the token, give this profile a
     label (e.g. "private"), Save

Repeat 2-3 for every Chrome profile you want to expose. Same token everywhere,
one distinct label each.
"@
