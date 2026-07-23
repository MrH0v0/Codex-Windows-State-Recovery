[CmdletBinding()]
param(
  [switch]$ConfirmRestore,
  [switch]$ValidateOnly,
  [string]$SnapshotPath = '',
  [switch]$NoLaunch,
  [string]$CodexHome = (Join-Path $env:USERPROFILE '.codex'),
  [string]$GuardHome = '',
  [switch]$TestMode
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$defaultCodexHome = [System.IO.Path]::GetFullPath(
  (Join-Path $env:USERPROFILE '.codex')
).TrimEnd('\')
$codexHomeResolved = [System.IO.Path]::GetFullPath(
  $CodexHome
).TrimEnd('\')
if (-not $GuardHome) {
  $GuardHome = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$guardHomeResolved = [System.IO.Path]::GetFullPath(
  $GuardHome
).TrimEnd('\')
$utf8Bom = [System.Text.UTF8Encoding]::new($true)
$mutex = $null
$mutexAcquired = $false
$stagedFiles = @()

function Test-PathUnder {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Root
  )
  $pathFull = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
  $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
  return $pathFull.StartsWith(
    $rootFull + '\',
    [System.StringComparison]::OrdinalIgnoreCase
  )
}

function Write-JsonAtomically {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)]$Value
  )
  $directory = Split-Path -Parent $Path
  New-Item -ItemType Directory -Path $directory -Force | Out-Null
  $temporary = Join-Path $directory (
    '.' + (Split-Path -Leaf $Path) + '.tmp-' +
    [guid]::NewGuid().ToString('N')
  )
  try {
    [System.IO.File]::WriteAllText(
      $temporary,
      (($Value | ConvertTo-Json -Depth 12) + "`r`n"),
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

function Get-RelativeSnapshotPath {
  param(
    [Parameter(Mandatory = $true)]$ManifestEntry,
    [Parameter(Mandatory = $true)][string]$SnapshotRoot
  )
  if (
    $ManifestEntry.PSObject.Properties.Name -contains 'relative' -and
    [string]$ManifestEntry.relative
  ) {
    return ([string]$ManifestEntry.relative).Replace('/', '\')
  }
  if (
    $ManifestEntry.PSObject.Properties.Name -contains 'snapshot' -and
    [string]$ManifestEntry.snapshot
  ) {
    $absolute = [System.IO.Path]::GetFullPath(
      [string]$ManifestEntry.snapshot
    )
    if (-not (Test-PathUnder -Path $absolute -Root $SnapshotRoot)) {
      throw "Manifest file escapes snapshot root: $absolute"
    }
    return $absolute.Substring($SnapshotRoot.Length).TrimStart('\')
  }
  throw 'Manifest entry has no usable relative or snapshot path'
}

function Invoke-PythonStateValidation {
  param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [Parameter(Mandatory = $true)][string]$GlobalStatePath,
    [string[]]$DatabasePaths = @()
  )
  $python = Get-Command python.exe -ErrorAction SilentlyContinue
  if (-not $python) {
    throw 'python.exe is required for safe TOML/JSON/SQLite validation'
  }
  $code = @'
import json
from pathlib import Path
import sqlite3
import sys
import tomllib

config_path = Path(sys.argv[1])
state_path = Path(sys.argv[2])
config_raw = config_path.read_bytes()
state_raw = state_path.read_bytes()
if not config_raw or b"\x00" in config_raw:
    raise SystemExit("config-invalid")
if not state_raw or b"\x00" in state_raw:
    raise SystemExit("global-state-invalid")
tomllib.loads(config_raw.decode("utf-8-sig"))
state = json.loads(state_raw.decode("utf-8-sig"))
if not isinstance(state, dict) or not isinstance(state.get("local-projects"), dict):
    raise SystemExit("global-state-shape-invalid")
projects = state["local-projects"]
order = state.get("project-order")
if (
    not isinstance(order, list)
    or not all(isinstance(item, str) for item in order)
    or len(order) != len(set(order))
    or set(order) != set(projects)
):
    raise SystemExit("global-state-project-order-invalid")
for project_id, project in projects.items():
    if not isinstance(project_id, str) or not isinstance(project, dict):
        raise SystemExit("global-state-project-entry-invalid")
    roots = project.get("rootPaths")
    if (
        not isinstance(roots, list)
        or not roots
        or not all(isinstance(root, str) and "\x00" not in root for root in roots)
    ):
        raise SystemExit("global-state-project-roots-invalid")
for raw_path in sys.argv[3:]:
    path = Path(raw_path)
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=10,
    )
    try:
        row = connection.execute("PRAGMA quick_check").fetchone()
        if not row or row[0] != "ok":
            raise SystemExit(f"sqlite-invalid:{path}:{row}")
        connection.execute("SELECT COUNT(*) FROM threads").fetchone()
    finally:
        connection.close()
print(json.dumps({"ok": True, "databases": len(sys.argv[3:])}))
'@
  $validationRoot = Join-Path (
    [System.IO.Path]::GetTempPath()
  ) ('codex-restore-validation-' + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $validationRoot -Force | Out-Null
  $validatorPath = Join-Path $validationRoot 'validate.py'
  $stdoutPath = Join-Path $validationRoot 'stdout.log'
  $stderrPath = Join-Path $validationRoot 'stderr.log'
  try {
    [System.IO.File]::WriteAllText(
      $validatorPath,
      $code,
      [System.Text.UTF8Encoding]::new($false)
    )
    $arguments = @(
      ('"' + $validatorPath + '"'),
      ('"' + $ConfigPath + '"'),
      ('"' + $GlobalStatePath + '"')
    ) + @(
      $DatabasePaths | ForEach-Object { '"' + $_ + '"' }
    )
    $process = Start-Process `
      -FilePath $python.Source `
      -ArgumentList $arguments `
      -WindowStyle Hidden `
      -RedirectStandardOutput $stdoutPath `
      -RedirectStandardError $stderrPath `
      -Wait `
      -PassThru
    if ($process.ExitCode -ne 0) {
      $stderr = if (Test-Path -LiteralPath $stderrPath -PathType Leaf) {
        [System.IO.File]::ReadAllText(
          $stderrPath,
          [System.Text.Encoding]::UTF8
        ).Trim()
      } else {
        ''
      }
      $stdout = if (Test-Path -LiteralPath $stdoutPath -PathType Leaf) {
        [System.IO.File]::ReadAllText(
          $stdoutPath,
          [System.Text.Encoding]::UTF8
        ).Trim()
      } else {
        ''
      }
      throw (
        "Snapshot content validation failed: " +
        (($stderr, $stdout | Where-Object { $_ }) -join ' ')
      )
    }
  } finally {
    $validationRootFull = [System.IO.Path]::GetFullPath(
      $validationRoot
    )
    $tempRootFull = [System.IO.Path]::GetFullPath(
      [System.IO.Path]::GetTempPath()
    ).TrimEnd('\')
    if (
      $validationRootFull.StartsWith(
        $tempRootFull + '\',
        [System.StringComparison]::OrdinalIgnoreCase
      ) -and
      (Test-Path -LiteralPath $validationRootFull -PathType Container)
    ) {
      Remove-Item -LiteralPath $validationRootFull -Recurse -Force
    }
  }
}

function Copy-CurrentPreimage {
  param(
    [Parameter(Mandatory = $true)][string]$PreimageRoot,
    [Parameter(Mandatory = $true)][string[]]$RelativePaths
  )
  $files = @()
  foreach ($relative in $RelativePaths) {
    $source = Join-Path $codexHomeResolved $relative
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
      continue
    }
    $destination = Join-Path $PreimageRoot $relative
    New-Item `
      -ItemType Directory `
      -Path (Split-Path -Parent $destination) `
      -Force |
      Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
    $files += [ordered]@{
      relative = $relative
      size = (Get-Item -LiteralPath $destination).Length
      sha256 = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
    }
  }
  return @($files)
}

function Stage-SnapshotFile {
  param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Destination,
    [Parameter(Mandatory = $true)][string]$ExpectedHash
  )
  $destinationDirectory = Split-Path -Parent $Destination
  New-Item -ItemType Directory -Path $destinationDirectory -Force |
    Out-Null
  $temporary = Join-Path $destinationDirectory (
    '.' + (Split-Path -Leaf $Destination) + '.restore-' +
    [guid]::NewGuid().ToString('N') + '.tmp'
  )
  Copy-Item -LiteralPath $Source -Destination $temporary -Force
  $actualHash = (
    Get-FileHash -LiteralPath $temporary -Algorithm SHA256
  ).Hash
  if ($actualHash -ne $ExpectedHash) {
    Remove-Item -LiteralPath $temporary -Force
    throw "Staged restore hash mismatch: $Source"
  }
  return $temporary
}

function Commit-StagedFile {
  param(
    [Parameter(Mandatory = $true)][string]$Staged,
    [Parameter(Mandatory = $true)][string]$Destination
  )
  if (Test-Path -LiteralPath $Destination -PathType Leaf) {
    $replaceBackup = Join-Path (Split-Path -Parent $Destination) (
      '.' + (Split-Path -Leaf $Destination) + '.replace-backup-' +
      [guid]::NewGuid().ToString('N') + '.tmp'
    )
    try {
      [System.IO.File]::Replace(
        $Staged,
        $Destination,
        $replaceBackup,
        $true
      )
      if (Test-Path -LiteralPath $replaceBackup -PathType Leaf) {
        Remove-Item -LiteralPath $replaceBackup -Force
      }
    } catch {
      if (
        (Test-Path -LiteralPath $replaceBackup -PathType Leaf) -and
        -not (Test-Path -LiteralPath $Destination -PathType Leaf)
      ) {
        [System.IO.File]::Move($replaceBackup, $Destination)
      }
      throw
    }
  } else {
    [System.IO.File]::Move($Staged, $Destination)
  }
}

function Restore-Preimage {
  param(
    [Parameter(Mandatory = $true)][string]$PreimageRoot,
    [Parameter(Mandatory = $true)][string[]]$ManagedPaths,
    [Parameter(Mandatory = $true)][hashtable]$OriginallyPresent
  )
  foreach ($relative in $ManagedPaths) {
    $destination = Join-Path $codexHomeResolved $relative
    $preimage = Join-Path $PreimageRoot $relative
    if (Test-Path -LiteralPath $preimage -PathType Leaf) {
      $staged = Stage-SnapshotFile `
        -Source $preimage `
        -Destination $destination `
        -ExpectedHash (
          Get-FileHash -LiteralPath $preimage -Algorithm SHA256
        ).Hash
      Commit-StagedFile -Staged $staged -Destination $destination
    } elseif (
      $OriginallyPresent.ContainsKey($relative) -and
      -not $OriginallyPresent[$relative] -and
      (Test-Path -LiteralPath $destination -PathType Leaf)
    ) {
      if (-not (Test-PathUnder -Path $destination -Root $codexHomeResolved)) {
        throw "Refusing rollback delete outside Codex home: $destination"
      }
      Remove-Item -LiteralPath $destination -Force
    }
  }
}

if ($ValidateOnly -and $ConfirmRestore) {
  throw 'Choose either -ValidateOnly or -ConfirmRestore, not both'
}
if (-not $ConfirmRestore) {
  $ValidateOnly = $true
}

if ($TestMode) {
  $tempRoot = [System.IO.Path]::GetFullPath(
    [System.IO.Path]::GetTempPath()
  ).TrimEnd('\')
  if (-not (Test-PathUnder -Path $codexHomeResolved -Root $tempRoot)) {
    throw 'TestMode CodexHome must be below the system temporary directory'
  }
} elseif ($codexHomeResolved -ne $defaultCodexHome) {
  throw "Refusing a non-default CodexHome without -TestMode: $codexHomeResolved"
}

$healthyRoot = Join-Path $codexHomeResolved 'backups_state\update-guard\healthy'
$guardStatePath = Join-Path $guardHomeResolved 'guard-state.json'
if (-not $SnapshotPath) {
  if (-not (Test-Path -LiteralPath $guardStatePath -PathType Leaf)) {
    throw "Guard state does not exist: $guardStatePath"
  }
  $guardState = [System.IO.File]::ReadAllText(
    $guardStatePath,
    [System.Text.Encoding]::UTF8
  ) | ConvertFrom-Json
  $SnapshotPath = [string]$guardState.lastHealthySnapshot
}
if (-not $SnapshotPath) {
  throw 'No last-known-good snapshot is registered'
}
$snapshotRoot = [System.IO.Path]::GetFullPath($SnapshotPath).TrimEnd('\')
if (
  $snapshotRoot -eq [System.IO.Path]::GetFullPath($healthyRoot).TrimEnd('\') -or
  -not (Test-PathUnder -Path $snapshotRoot -Root $healthyRoot)
) {
  throw "Snapshot is outside the trusted healthy root: $snapshotRoot"
}
if (-not (Test-Path -LiteralPath $snapshotRoot -PathType Container)) {
  throw "Snapshot directory does not exist: $snapshotRoot"
}

$manifestPath = Join-Path $snapshotRoot 'manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
  throw "Snapshot manifest does not exist: $manifestPath"
}
$manifest = [System.IO.File]::ReadAllText(
  $manifestPath,
  [System.Text.Encoding]::UTF8
) | ConvertFrom-Json
if ($manifest.healthStatus -ne 'healthy') {
  throw "Refusing to restore a non-healthy snapshot: $snapshotRoot"
}

$allowedRelativePaths = @(
  'config.toml',
  '.codex-global-state.json',
  '.codex-global-state.json.bak',
  'session_index.jsonl',
  'state_5.sqlite',
  'sqlite\state_5.sqlite'
)
$snapshotFiles = @{}
foreach ($entry in @($manifest.files)) {
  $relative = Get-RelativeSnapshotPath `
    -ManifestEntry $entry `
    -SnapshotRoot $snapshotRoot
  if ($allowedRelativePaths -notcontains $relative) {
    throw "Manifest contains an unsupported restore path: $relative"
  }
  if ($snapshotFiles.ContainsKey($relative)) {
    throw "Manifest contains a duplicate restore path: $relative"
  }
  $source = Join-Path $snapshotRoot $relative
  if (-not (Test-PathUnder -Path $source -Root $snapshotRoot)) {
    throw "Snapshot file escapes snapshot root: $source"
  }
  if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "Snapshot file is missing: $source"
  }
  $expectedSize = [int64]$entry.size
  $expectedHash = ([string]$entry.sha256).ToUpperInvariant()
  $actualSize = (Get-Item -LiteralPath $source).Length
  $actualHash = (
    Get-FileHash -LiteralPath $source -Algorithm SHA256
  ).Hash
  if ($actualSize -ne $expectedSize -or $actualHash -ne $expectedHash) {
    throw "Snapshot manifest verification failed: $relative"
  }
  $snapshotFiles[$relative] = [ordered]@{
    source = $source
    size = $actualSize
    sha256 = $actualHash
  }
}
foreach ($required in @('config.toml', '.codex-global-state.json')) {
  if (-not $snapshotFiles.ContainsKey($required)) {
    throw "Snapshot is missing required file: $required"
  }
}

$databaseSnapshotPaths = @(
  'state_5.sqlite',
  'sqlite\state_5.sqlite'
) | Where-Object { $snapshotFiles.ContainsKey($_) } | ForEach-Object {
  [string]$snapshotFiles[$_].source
}
Invoke-PythonStateValidation `
  -ConfigPath ([string]$snapshotFiles['config.toml'].source) `
  -GlobalStatePath ([string]$snapshotFiles['.codex-global-state.json'].source) `
  -DatabasePaths @($databaseSnapshotPaths)

$validationReport = [ordered]@{
  schemaVersion = 2
  validatedAt = [DateTimeOffset]::UtcNow.ToString('o')
  valid = $true
  snapshot = $snapshotRoot
  packageVersionAtSnapshot = $manifest.packageVersion
  files = @(
    $snapshotFiles.GetEnumerator() |
      Sort-Object Name |
      ForEach-Object {
        [ordered]@{
          relative = $_.Key
          size = $_.Value.size
          sha256 = $_.Value.sha256
        }
      }
  )
}
if ($ValidateOnly) {
  $validationReport | ConvertTo-Json -Depth 8
  exit 0
}

$mutexName = if ($TestMode) {
  "Local\CodexUpdateMaintenance-Test-$PID"
} else {
  'Local\CodexUpdateMaintenance'
}
$mutex = [System.Threading.Mutex]::new(
  $false,
  $mutexName
)
$mutexAcquired = $mutex.WaitOne(0)
if (-not $mutexAcquired) {
  throw 'Codex maintenance is already running; restore was not started'
}

$package = Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction SilentlyContinue |
  Sort-Object Version -Descending |
  Select-Object -First 1
if (-not $TestMode -and $package) {
  $packageRoot = [System.IO.Path]::GetFullPath(
    $package.InstallLocation
  ).TrimEnd('\')
  $codexProcesses = Get-Process -Name ChatGPT, Codex -ErrorAction SilentlyContinue |
    Where-Object {
      $processPath = $null
      try {
        $processPath = $_.Path
      } catch {
        $processPath = $null
      }
      $processPath -and $processPath.StartsWith(
        $packageRoot + '\',
        [System.StringComparison]::OrdinalIgnoreCase
      )
    }
  if ($codexProcesses) {
    $codexProcesses | Stop-Process -Force
    $deadline = (Get-Date).AddSeconds(15)
    do {
      Start-Sleep -Milliseconds 250
      $remaining = @(
        $codexProcesses | Where-Object {
          Get-Process -Id $_.Id -ErrorAction SilentlyContinue
        }
      )
    } while ($remaining.Count -gt 0 -and (Get-Date) -lt $deadline)
    if ($remaining.Count -gt 0) {
      throw "Codex processes did not exit: $($remaining.Id -join ', ')"
    }
  }
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$preimageRoot = Join-Path $codexHomeResolved (
  "backups_state\update-guard\manual-restore-preimage\$stamp"
)
$managedPaths = @(
  $allowedRelativePaths +
  @(
    'state_5.sqlite-wal',
    'state_5.sqlite-shm',
    'sqlite\state_5.sqlite-wal',
    'sqlite\state_5.sqlite-shm'
  )
)
$originallyPresent = @{}
foreach ($relative in $managedPaths) {
  $originallyPresent[$relative] = Test-Path `
    -LiteralPath (Join-Path $codexHomeResolved $relative) `
    -PathType Leaf
}
$preimageFiles = Copy-CurrentPreimage `
  -PreimageRoot $preimageRoot `
  -RelativePaths $managedPaths
$preimageManifest = [ordered]@{
  schemaVersion = 2
  createdAt = [DateTimeOffset]::UtcNow.ToString('o')
  purpose = 'pre-restore rollback'
  sourceSnapshot = $snapshotRoot
  files = @($preimageFiles)
}
Write-JsonAtomically `
  -Path (Join-Path $preimageRoot 'manifest.json') `
  -Value $preimageManifest

try {
  foreach ($relative in @($snapshotFiles.Keys)) {
    $destination = Join-Path $codexHomeResolved $relative
    $staged = Stage-SnapshotFile `
      -Source ([string]$snapshotFiles[$relative].source) `
      -Destination $destination `
      -ExpectedHash ([string]$snapshotFiles[$relative].sha256)
    $stagedFiles += [ordered]@{
      relative = $relative
      staged = $staged
      destination = $destination
    }
  }

  foreach ($relative in @(
    'state_5.sqlite-wal',
    'state_5.sqlite-shm',
    'sqlite\state_5.sqlite-wal',
    'sqlite\state_5.sqlite-shm'
  )) {
    $path = Join-Path $codexHomeResolved $relative
    if (Test-Path -LiteralPath $path -PathType Leaf) {
      if (-not (Test-PathUnder -Path $path -Root $codexHomeResolved)) {
        throw "Refusing to remove SQLite sidecar outside Codex home: $path"
      }
      Remove-Item -LiteralPath $path -Force
    }
  }

  foreach ($stagedFile in $stagedFiles) {
    Commit-StagedFile `
      -Staged ([string]$stagedFile.staged) `
      -Destination ([string]$stagedFile.destination)
  }
  $stagedFiles = @()

  $restoredDatabasePaths = @(
    'state_5.sqlite',
    'sqlite\state_5.sqlite'
  ) | Where-Object { $snapshotFiles.ContainsKey($_) } | ForEach-Object {
    Join-Path $codexHomeResolved $_
  }
  Invoke-PythonStateValidation `
    -ConfigPath (Join-Path $codexHomeResolved 'config.toml') `
    -GlobalStatePath (Join-Path $codexHomeResolved '.codex-global-state.json') `
    -DatabasePaths @($restoredDatabasePaths)
} catch {
  foreach ($stagedFile in $stagedFiles) {
    if (Test-Path -LiteralPath $stagedFile.staged -PathType Leaf) {
      Remove-Item -LiteralPath $stagedFile.staged -Force
    }
  }
  Restore-Preimage `
    -PreimageRoot $preimageRoot `
    -ManagedPaths $managedPaths `
    -OriginallyPresent $originallyPresent
  throw "Restore failed and the preimage was reapplied: $($_.Exception.Message)"
}

$restoreReport = [ordered]@{
  schemaVersion = 2
  restoredAt = [DateTimeOffset]::UtcNow.ToString('o')
  snapshot = $snapshotRoot
  preimage = $preimageRoot
  validation = $validationReport
}
Write-JsonAtomically `
  -Path (Join-Path $preimageRoot 'restore-report.json') `
  -Value $restoreReport

if (-not $NoLaunch -and -not $TestMode -and $package) {
  $packageManifest = $package | Get-AppxPackageManifest
  $application = @(
    $packageManifest.Package.Applications.Application
  ) | Select-Object -First 1
  $applicationId = [string]$application.Id
  if ($applicationId) {
    $appUserModelId = "$($package.PackageFamilyName)!$applicationId"
    Start-Process `
      -FilePath 'explorer.exe' `
      -ArgumentList "shell:AppsFolder\$appUserModelId" `
      -WindowStyle Hidden
  }
}

$restoreReport | ConvertTo-Json -Depth 10

if ($mutexAcquired) {
  $mutex.ReleaseMutex()
  $mutexAcquired = $false
}
if ($mutex) {
  $mutex.Dispose()
}
