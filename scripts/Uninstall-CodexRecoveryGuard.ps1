[CmdletBinding()]
param(
  [switch]$ConfirmUninstall,
  [string]$TaskName = 'Codex State Recovery Guard'
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

if (-not $ConfirmUninstall) {
  throw (
    'Uninstallation removes the scheduled task and installed executables. ' +
    'Rerun with -ConfirmUninstall.'
  )
}

$codexHome = Join-Path $env:USERPROFILE '.codex'
$maintenanceRoot = Join-Path $codexHome 'maintenance'
$targetRoot = Join-Path $maintenanceRoot 'update-guard'
$targetFull = [System.IO.Path]::GetFullPath($targetRoot).TrimEnd('\')
$maintenanceFull = [System.IO.Path]::GetFullPath(
  $maintenanceRoot
).TrimEnd('\')
if (-not $targetFull.StartsWith(
  $maintenanceFull + '\',
  [System.StringComparison]::OrdinalIgnoreCase
)) {
  throw "Unsafe maintenance path: $targetFull"
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
  Unregister-ScheduledTask `
    -TaskName $TaskName `
    -Confirm:$false
}

$removed = [System.Collections.Generic.List[string]]::new()
foreach ($fileName in @(
  'codex_update_guard.py',
  'Invoke-CodexUpdateGuard.ps1',
  'Invoke-CodexUpdateMaintenance.ps1',
  'Restore-CodexLastHealthy.ps1'
)) {
  $path = Join-Path $targetRoot $fileName
  if (Test-Path -LiteralPath $path -PathType Leaf) {
    Remove-Item -LiteralPath $path -Force
    $removed.Add($path)
  }
}

[ordered]@{
  status = 'uninstalled'
  scheduledTaskRemoved = [bool]$task
  executableFilesRemoved = @($removed)
  preservedGuardMetadata = @(
    (Join-Path $targetRoot 'expected-state.json'),
    (Join-Path $targetRoot 'guard-state.json'),
    (Join-Path $targetRoot 'reports')
  )
  preservedRecoveryBackups = (
    Join-Path $codexHome 'backups_state\update-guard'
  )
} | ConvertTo-Json -Depth 5
