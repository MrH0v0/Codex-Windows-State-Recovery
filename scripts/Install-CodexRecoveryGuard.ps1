[CmdletBinding()]
param(
  [switch]$ConfirmInstall,
  [switch]$EnableFastPatchIntegration,
  [switch]$SkipScheduledTask,
  [string]$TaskName = 'Codex State Recovery Guard'
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if (-not $ConfirmInstall) {
  throw (
    'Installation is write-enabled. Review the scripts, then rerun with ' +
    '-ConfirmInstall.'
  )
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$codexHome = Join-Path $env:USERPROFILE '.codex'
$maintenanceRoot = Join-Path $codexHome 'maintenance'
$targetRoot = Join-Path $maintenanceRoot 'update-guard'
$guardWrapper = Join-Path $targetRoot 'Invoke-CodexUpdateGuard.ps1'
$maintenanceWrapper = Join-Path $targetRoot 'Invoke-CodexUpdateMaintenance.ps1'
$fastPatchScript = Join-Path (
  Join-Path $codexHome 'skills\codex-windows-fast-patch\scripts'
) 'install-computer-use-local.ps1'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$backupRoot = Join-Path (
  Join-Path $codexHome 'backups_state\update-guard-install'
) $stamp
$targetBackup = Join-Path $backupRoot 'previous-install'
$taskBackup = Join-Path $backupRoot 'scheduled-task.xml'
$utf8Bom = [System.Text.UTF8Encoding]::new($true)
$hadTarget = Test-Path -LiteralPath $targetRoot -PathType Container
$hadTask = $false
$taskRegistered = $false

$installedFiles = @(
  'codex_update_guard.py',
  'Invoke-CodexUpdateGuard.ps1',
  'Invoke-CodexUpdateMaintenance.ps1',
  'Restore-CodexLastHealthy.ps1'
)

function Assert-PathWithin {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Parent
  )

  $pathFull = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
  $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\')
  if (-not $pathFull.StartsWith(
    $parentFull + '\',
    [System.StringComparison]::OrdinalIgnoreCase
  )) {
    throw "Unsafe path outside expected parent: $pathFull"
  }
}

function Backup-ExistingState {
  New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
  if ($script:hadTarget) {
    Copy-Item `
      -LiteralPath $targetRoot `
      -Destination $targetBackup `
      -Recurse `
      -Force
  }
  $existingTask = Get-ScheduledTask `
    -TaskName $TaskName `
    -ErrorAction SilentlyContinue
  if ($existingTask) {
    $script:hadTask = $true
    $xml = Export-ScheduledTask -TaskName $TaskName
    [System.IO.File]::WriteAllText(
      $taskBackup,
      $xml,
      $utf8Bom
    )
  }
}

function Restore-PreviousState {
  Assert-PathWithin -Path $targetRoot -Parent $maintenanceRoot
  if ($script:hadTarget) {
    foreach ($fileName in $installedFiles) {
      $previous = Join-Path $targetBackup $fileName
      $current = Join-Path $targetRoot $fileName
      if (Test-Path -LiteralPath $previous -PathType Leaf) {
        Copy-Item -LiteralPath $previous -Destination $current -Force
      } elseif (Test-Path -LiteralPath $current -PathType Leaf) {
        Remove-Item -LiteralPath $current -Force
      }
    }
    foreach ($metadataName in @(
      'expected-state.json',
      'guard-state.json'
    )) {
      $previous = Join-Path $targetBackup $metadataName
      $current = Join-Path $targetRoot $metadataName
      if (Test-Path -LiteralPath $previous -PathType Leaf) {
        Copy-Item -LiteralPath $previous -Destination $current -Force
      } elseif (Test-Path -LiteralPath $current -PathType Leaf) {
        Remove-Item -LiteralPath $current -Force
      }
    }
  } else {
    foreach ($fileName in $installedFiles) {
      $current = Join-Path $targetRoot $fileName
      if (Test-Path -LiteralPath $current -PathType Leaf) {
        Remove-Item -LiteralPath $current -Force
      }
    }
    foreach ($metadataName in @(
      'expected-state.json',
      'guard-state.json'
    )) {
      $current = Join-Path $targetRoot $metadataName
      if (Test-Path -LiteralPath $current -PathType Leaf) {
        Remove-Item -LiteralPath $current -Force
      }
    }
  }

  if ($script:taskRegistered -and -not $script:hadTask) {
    Unregister-ScheduledTask `
      -TaskName $TaskName `
      -Confirm:$false `
      -ErrorAction SilentlyContinue
  } elseif ($script:hadTask -and (Test-Path -LiteralPath $taskBackup)) {
    Register-ScheduledTask `
      -TaskName $TaskName `
      -Xml (Get-Content -LiteralPath $taskBackup -Raw) `
      -Force |
      Out-Null
  }
}

function Register-GuardTask {
  $actionArguments = (
    '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' +
    $maintenanceWrapper +
    '"'
  )
  if ($EnableFastPatchIntegration) {
    $actionArguments += ' -EnableFastPatchIntegration'
  }
  $action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument $actionArguments
  $userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
  $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
  $periodicTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(5) `
    -RepetitionInterval (New-TimeSpan -Minutes 30)
  $settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew
  $principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited
  Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($logonTrigger, $periodicTrigger) `
    -Settings $settings `
    -Principal $principal `
    -Description (
      'Validate durable Codex state and capture healthy snapshots. ' +
      'Never auto-restore configuration, projects, or databases.'
    ) `
    -Force |
    Out-Null
  $script:taskRegistered = $true
}

try {
  if (-not (Test-Path -LiteralPath $codexHome -PathType Container)) {
    throw "Codex home does not exist: $codexHome"
  }
  $python = Get-Command python.exe -ErrorAction SilentlyContinue
  if (-not $python) {
    throw 'python.exe was not found; Python 3.11+ is required.'
  }
  & $python.Source -c (
    'import sys; assert sys.version_info >= (3, 11), ' +
    'sys.version; print(sys.version)'
  )
  if ($LASTEXITCODE -ne 0) {
    throw 'Python 3.11+ validation failed.'
  }
  foreach ($fileName in $installedFiles) {
    $source = Join-Path $scriptRoot $fileName
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
      throw "Required installation file is missing: $source"
    }
  }
  if (
    $EnableFastPatchIntegration -and
    -not (Test-Path -LiteralPath $fastPatchScript -PathType Leaf)
  ) {
    throw (
      'Fast-patch integration was requested, but the optional ' +
      "codex-windows-fast-patch script is missing: $fastPatchScript"
    )
  }

  Assert-PathWithin -Path $targetRoot -Parent $maintenanceRoot
  Backup-ExistingState
  New-Item -ItemType Directory -Path $targetRoot -Force | Out-Null
  foreach ($fileName in $installedFiles) {
    Copy-Item `
      -LiteralPath (Join-Path $scriptRoot $fileName) `
      -Destination (Join-Path $targetRoot $fileName) `
      -Force
  }

  & powershell.exe `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File $guardWrapper `
    -RefreshBaseline `
    -ForceSnapshot
  if ($LASTEXITCODE -ne 0) {
    throw "Initial guard baseline was rejected with exit code $LASTEXITCODE."
  }

  if (-not $SkipScheduledTask) {
    Register-GuardTask
  }

  [ordered]@{
    status = 'installed'
    installedPath = $targetRoot
    backupPath = $backupRoot
    scheduledTask = if ($SkipScheduledTask) { $null } else { $TaskName }
    fastPatchIntegration = [bool]$EnableFastPatchIntegration
    restoreBehavior = 'manual-only'
    baselineBehavior = 'explicit-install-time-only'
  } | ConvertTo-Json -Depth 5
  exit 0
} catch {
  try {
    Restore-PreviousState
  } catch {
    Write-Warning "Rollback also failed: $($_.Exception.Message)"
  }
  Write-Error $_
  exit 1
}
