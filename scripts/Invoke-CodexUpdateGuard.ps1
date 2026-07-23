[CmdletBinding()]
param(
  [switch]$ForceSnapshot,
  [switch]$RefreshBaseline
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$guardHome = Split-Path -Parent $MyInvocation.MyCommand.Path
$guardScript = Join-Path $guardHome 'codex_update_guard.py'
$codexHome = Join-Path $env:USERPROFILE '.codex'
$env:PYTHONIOENCODING = 'utf-8'

$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
  throw 'python.exe was not found; Codex Update State Guard requires Python 3.11+'
}

$package = Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction SilentlyContinue |
  Sort-Object Version -Descending |
  Select-Object -First 1
$packageVersion = if ($package) {
  $package.Version.ToString()
} else {
  ''
}

$arguments = @(
  $guardScript,
  '--codex-home',
  $codexHome,
  '--guard-home',
  $guardHome
)
if ($packageVersion) {
  $arguments += @('--package-version', $packageVersion)
}
if ($ForceSnapshot) {
  $arguments += '--force-snapshot'
}
if ($RefreshBaseline) {
  $arguments += '--refresh-baseline'
}

& $pythonCommand.Source @arguments
exit $LASTEXITCODE
