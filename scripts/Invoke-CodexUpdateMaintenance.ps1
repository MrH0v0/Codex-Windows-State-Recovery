[CmdletBinding()]
param(
  [switch]$EnableFastPatchIntegration,
  [switch]$ForceFastPatchCheck,
  [switch]$SkipFastPatchRepair
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$guardHome = Split-Path -Parent $MyInvocation.MyCommand.Path
$codexHome = Join-Path $env:USERPROFILE '.codex'
$guardScript = Join-Path $guardHome 'Invoke-CodexUpdateGuard.ps1'
$fastPatchScript = Join-Path $codexHome 'skills\codex-windows-fast-patch\scripts\install-computer-use-local.ps1'
$logsRoot = Join-Path $guardHome 'maintenance-logs'
$latestReportPath = Join-Path $guardHome 'maintenance-latest.json'
$runId = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$runRoot = Join-Path $logsRoot $runId
$utf8Bom = [System.Text.UTF8Encoding]::new($true)
$mutex = [System.Threading.Mutex]::new($false, 'Local\CodexUpdateMaintenance')
$mutexAcquired = $false

if (
  -not $EnableFastPatchIntegration -and
  ($ForceFastPatchCheck -or $SkipFastPatchRepair)
) {
  throw (
    '-ForceFastPatchCheck and -SkipFastPatchRepair require ' +
    '-EnableFastPatchIntegration'
  )
}

function Write-JsonWithBom {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)]$Value
  )

  $json = $Value | ConvertTo-Json -Depth 10
  $directory = Split-Path -Parent $Path
  New-Item -ItemType Directory -Path $directory -Force | Out-Null
  $temporary = Join-Path $directory (
    '.' + (Split-Path -Leaf $Path) + '.tmp-' +
    [guid]::NewGuid().ToString('N')
  )
  try {
    [System.IO.File]::WriteAllText(
      $temporary,
      $json + "`r`n",
      $utf8Bom
    )
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
      $replaceBackup = Join-Path $directory (
        '.' + (Split-Path -Leaf $Path) + '.replace-backup-' +
        [guid]::NewGuid().ToString('N') + '.tmp'
      )
      try {
        [System.IO.File]::Replace(
          $temporary,
          $Path,
          $replaceBackup,
          $true
        )
        if (Test-Path -LiteralPath $replaceBackup -PathType Leaf) {
          Remove-Item -LiteralPath $replaceBackup -Force
        }
      } catch {
        if (
          (Test-Path -LiteralPath $replaceBackup -PathType Leaf) -and
          -not (Test-Path -LiteralPath $Path -PathType Leaf)
        ) {
          [System.IO.File]::Move($replaceBackup, $Path)
        }
        throw
      }
    } else {
      [System.IO.File]::Move($temporary, $Path)
    }
  } finally {
    if (Test-Path -LiteralPath $temporary -PathType Leaf) {
      Remove-Item -LiteralPath $temporary -Force
    }
  }
}

function Invoke-CapturedPowerShell {
  param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$ScriptPath,
    [string[]]$ScriptArguments = @()
  )

  $stdoutPath = Join-Path $runRoot "$Name.stdout.log"
  $stderrPath = Join-Path $runRoot "$Name.stderr.log"
  $arguments = @(
    '-NoProfile',
    '-ExecutionPolicy',
    'Bypass',
    '-File',
    ('"' + $ScriptPath + '"')
  ) + $ScriptArguments
  $process = Start-Process `
    -FilePath 'powershell.exe' `
    -ArgumentList $arguments `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -Wait `
    -PassThru
  [pscustomobject]@{
    name = $Name
    exitCode = $process.ExitCode
    stdout = $stdoutPath
    stderr = $stderrPath
  }
}

function Test-FastPatchMarkers {
  $cacheRoot = Join-Path $codexHome 'plugins\cache\openai-bundled'
  foreach ($pluginName in @('computer-use', 'browser', 'chrome')) {
    $manifest = Join-Path $cacheRoot "$pluginName\latest\.codex-plugin\plugin.json"
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
      return $false
    }
  }

  $chromeScripts = Join-Path $cacheRoot 'chrome\latest\scripts'
  $checks = @(
    [pscustomobject]@{
      Path = (Join-Path $chromeScripts 'open-chrome-window.js')
      Marker = 'valueName == null || match[1] === label'
    },
    [pscustomobject]@{
      Path = (Join-Path $chromeScripts 'installed-browsers.js')
      Marker = 'valueName == null || match[1] === label'
    },
    [pscustomobject]@{
      Path = (Join-Path $chromeScripts 'check-native-host-manifest.js')
      Marker = 'valueName === "(Default)" || match[1] === valueName'
    }
  )
  foreach ($check in $checks) {
    if (-not (Test-Path -LiteralPath $check.Path -PathType Leaf)) {
      return $false
    }
    $content = [System.IO.File]::ReadAllText(
      $check.Path,
      [System.Text.UTF8Encoding]::new($false)
    )
    if (-not $content.Contains($check.Marker)) {
      return $false
    }
  }
  return $true
}

function Get-InstalledPackageVersion {
  $package = Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction SilentlyContinue |
    Sort-Object Version -Descending |
    Select-Object -First 1
  if ($package) {
    return $package.Version.ToString()
  }
  return ''
}

function Read-PreviousMaintenanceReport {
  if (-not (Test-Path -LiteralPath $latestReportPath -PathType Leaf)) {
    return $null
  }
  try {
    return Get-Content -LiteralPath $latestReportPath -Raw |
      ConvertFrom-Json -ErrorAction Stop
  } catch {
    return $null
  }
}

function Remove-OldRunLogs {
  $rootFull = [System.IO.Path]::GetFullPath($logsRoot).TrimEnd('\')
  $oldRuns = @(
    Get-ChildItem -LiteralPath $logsRoot -Directory -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTimeUtc -Descending |
      Select-Object -Skip 96
  )
  foreach ($oldRun in $oldRuns) {
    $targetFull = [System.IO.Path]::GetFullPath($oldRun.FullName)
    if (-not $targetFull.StartsWith(
      $rootFull + '\',
      [System.StringComparison]::OrdinalIgnoreCase
    )) {
      throw "refusing to remove maintenance log outside root: $targetFull"
    }
    Remove-Item -LiteralPath $targetFull -Recurse -Force
  }
}

try {
  $mutexAcquired = $mutex.WaitOne(0)
  if (-not $mutexAcquired) {
    exit 3
  }

  New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
  $packageVersion = Get-InstalledPackageVersion
  $previous = Read-PreviousMaintenanceReport
  $markersHealthyBefore = if ($EnableFastPatchIntegration) {
    Test-FastPatchMarkers
  } else {
    $null
  }
  $lastStrictVerifiedAt = if (
    $previous -and
    $previous.PSObject.Properties.Name -contains 'strictVerifiedAt'
  ) {
    [string]$previous.strictVerifiedAt
  } else {
    ''
  }
  $lastStrictAgeHours = [double]::PositiveInfinity
  if ($lastStrictVerifiedAt) {
    try {
      $lastStrictAgeHours = (
        [DateTimeOffset]::UtcNow -
        [DateTimeOffset]::Parse($lastStrictVerifiedAt)
      ).TotalHours
    } catch {
      $lastStrictAgeHours = [double]::PositiveInfinity
    }
  }
  $previousPackageVersion = if (
    $previous -and
    $previous.PSObject.Properties.Name -contains 'packageVersion'
  ) {
    [string]$previous.packageVersion
  } else {
    ''
  }
  $previousStatus = if (
    $previous -and
    $previous.PSObject.Properties.Name -contains 'status'
  ) {
    [string]$previous.status
  } else {
    ''
  }
  $needsStrictCheck = $EnableFastPatchIntegration -and (
      $ForceFastPatchCheck -or
      -not $markersHealthyBefore -or
      -not (Test-Path -LiteralPath $fastPatchScript -PathType Leaf) -or
      $previousPackageVersion -ne $packageVersion -or
      $previousStatus -ne 'healthy' -or
      $lastStrictAgeHours -ge 24
    )

  $initialStrict = $null
  $repair = $null
  $finalStrict = $null
  if ($needsStrictCheck) {
    if (-not (Test-Path -LiteralPath $fastPatchScript -PathType Leaf)) {
      $finalStrict = [pscustomobject]@{
        name = 'fast-patch-missing'
        exitCode = 90
        stdout = $null
        stderr = $null
      }
    } else {
      $initialStrict = Invoke-CapturedPowerShell `
        -Name 'fast-patch-strict-before' `
        -ScriptPath $fastPatchScript `
        -ScriptArguments @('-StrictVerifyOnly')
      $finalStrict = $initialStrict
      if ($initialStrict.exitCode -ne 0 -and -not $SkipFastPatchRepair) {
        $repair = Invoke-CapturedPowerShell `
          -Name 'fast-patch-repair' `
          -ScriptPath $fastPatchScript `
          -ScriptArguments @('-VerifyOnly')
        $finalStrict = Invoke-CapturedPowerShell `
          -Name 'fast-patch-strict-after' `
          -ScriptPath $fastPatchScript `
          -ScriptArguments @('-StrictVerifyOnly')
      }
    }
  }

  $guard = Invoke-CapturedPowerShell `
    -Name 'state-guard' `
    -ScriptPath $guardScript
  $markersHealthyAfter = if ($EnableFastPatchIntegration) {
    Test-FastPatchMarkers
  } else {
    $null
  }
  $strictExit = if ($finalStrict) { [int]$finalStrict.exitCode } else { 0 }
  $status = if (
    $guard.exitCode -eq 0 -and
    (
      -not $EnableFastPatchIntegration -or
      ($strictExit -eq 0 -and $markersHealthyAfter)
    )
  ) {
    'healthy'
  } else {
    'degraded'
  }
  $checkedAt = [DateTimeOffset]::UtcNow.ToString('o')
  $strictVerifiedAt = if ($needsStrictCheck -and $strictExit -eq 0) {
    $checkedAt
  } else {
    $lastStrictVerifiedAt
  }
  $report = [ordered]@{
    schemaVersion = 2
    checkedAt = $checkedAt
    status = $status
    packageVersion = $packageVersion
    fastPatchIntegrationEnabled = [bool]$EnableFastPatchIntegration
    markerCheckPassedBefore = $markersHealthyBefore
    markerCheckPassed = $markersHealthyAfter
    strictCheckRequired = $needsStrictCheck
    strictVerifiedAt = $strictVerifiedAt
    initialStrict = $initialStrict
    repair = $repair
    finalStrict = $finalStrict
    stateGuard = $guard
    runLogs = $runRoot
  }
  Write-JsonWithBom -Path (Join-Path $runRoot 'maintenance-report.json') -Value $report
  Write-JsonWithBom -Path $latestReportPath -Value $report
  Remove-OldRunLogs
  $report | ConvertTo-Json -Depth 10
  if ($status -eq 'healthy') {
    exit 0
  }
  exit 1
} catch {
  $failure = [ordered]@{
    schemaVersion = 2
    checkedAt = [DateTimeOffset]::UtcNow.ToString('o')
    status = 'error'
    fastPatchIntegrationEnabled = [bool]$EnableFastPatchIntegration
    error = $_.Exception.Message
    runLogs = $runRoot
  }
  New-Item -ItemType Directory -Path $guardHome -Force | Out-Null
  Write-JsonWithBom -Path $latestReportPath -Value $failure
  Write-Error $_
  exit 99
} finally {
  if ($mutexAcquired) {
    $mutex.ReleaseMutex()
  }
  $mutex.Dispose()
}
