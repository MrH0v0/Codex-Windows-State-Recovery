[CmdletBinding()]
param(
  [switch]$ConfirmReset,
  [switch]$ConfirmedCurrentSchemaArray,
  [string]$CodexHome = (Join-Path $env:USERPROFILE '.codex'),
  [string]$OutputPath = '',
  [switch]$TestMode
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$defaultCodexHome = [System.IO.Path]::GetFullPath(
  (Join-Path $env:USERPROFILE '.codex')
).TrimEnd('\')
$codexHomeResolved = [System.IO.Path]::GetFullPath(
  $CodexHome
).TrimEnd('\')
if ($TestMode) {
  $tempRoot = [System.IO.Path]::GetFullPath(
    [System.IO.Path]::GetTempPath()
  ).TrimEnd('\')
  if (-not $codexHomeResolved.StartsWith(
    $tempRoot + '\',
    [System.StringComparison]::OrdinalIgnoreCase
  )) {
    throw 'TestMode CodexHome must be below the system temporary directory'
  }
} elseif ($codexHomeResolved -ne $defaultCodexHome) {
  throw "Refusing a non-default CodexHome without -TestMode: $codexHomeResolved"
}

$registryPath = Join-Path (
  Join-Path $codexHomeResolved 'process_manager'
) 'chat_processes.json'
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$utf8Bom = [System.Text.UTF8Encoding]::new($true)

function Write-OptionalReport {
  param([Parameter(Mandatory = $true)]$Value)

  if (-not $OutputPath) {
    return
  }
  $outputFull = [System.IO.Path]::GetFullPath($OutputPath)
  $parent = Split-Path -Parent $outputFull
  if ($parent) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
  }
  [System.IO.File]::WriteAllText(
    $outputFull,
    (($Value | ConvertTo-Json -Depth 8) + "`r`n"),
    $utf8Bom
  )
}

if (-not (Test-Path -LiteralPath $registryPath -PathType Leaf)) {
  $missingReport = [ordered]@{
    status = 'healthy-missing'
    path = $registryPath
    reason = 'The current loader treats ENOENT as an empty registry.'
    changed = $false
  }
  Write-OptionalReport -Value $missingReport
  $missingReport | ConvertTo-Json -Depth 5
  exit 0
}

$raw = [System.IO.File]::ReadAllBytes($registryPath)
$nulCount = @($raw | Where-Object { $_ -eq 0 }).Count
$allNul = $raw.Length -gt 0 -and $nulCount -eq $raw.Length
$jsonArrayValid = $false
$parseError = ''
try {
  $text = $utf8NoBom.GetString($raw)
  $trimmed = $text.Trim()
  $parsed = $trimmed | ConvertFrom-Json -ErrorAction Stop
  if ($trimmed.StartsWith('[') -and $trimmed.EndsWith(']')) {
    $jsonArrayValid = $true
  }
} catch {
  $parseError = $_.Exception.Message
}

$inspection = [ordered]@{
  path = $registryPath
  size = $raw.Length
  nulCount = $nulCount
  allNul = $allNul
  jsonArrayValid = $jsonArrayValid
  sha256 = (Get-FileHash -LiteralPath $registryPath -Algorithm SHA256).Hash
  parseError = $parseError
}
if ($jsonArrayValid) {
  $healthyReport = [ordered]@{
    status = 'healthy'
    changed = $false
    inspection = $inspection
  }
  Write-OptionalReport -Value $healthyReport
  $healthyReport | ConvertTo-Json -Depth 8
  exit 0
}

if (-not $ConfirmReset) {
  $dryRunReport = [ordered]@{
    status = 'repair-required'
    changed = $false
    inspection = $inspection
    requiredFlags = @(
      '-ConfirmReset',
      '-ConfirmedCurrentSchemaArray'
    )
  }
  Write-OptionalReport -Value $dryRunReport
  $dryRunReport | ConvertTo-Json -Depth 8
  exit 2
}
if (-not $ConfirmedCurrentSchemaArray) {
  throw (
    '-ConfirmReset also requires -ConfirmedCurrentSchemaArray after ' +
    'checking the currently installed package schema.'
  )
}
if (-not ($allNul -or $raw.Length -eq 0)) {
  throw (
    'Refusing reset because the invalid registry is not empty or all-NUL. ' +
    'Preserve and inspect it manually.'
  )
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$backupRoot = Join-Path (
  Join-Path $codexHomeResolved 'backups_state\process-manager-repair'
) $stamp
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
$originalBackup = Join-Path (
  $backupRoot
) 'chat_processes.json.invalid.original'
Copy-Item -LiteralPath $registryPath -Destination $originalBackup -Force

$temporary = Join-Path (Split-Path -Parent $registryPath) (
  '.chat_processes.json.repair-' +
  [guid]::NewGuid().ToString('N') +
  '.tmp'
)
$replaceBackup = Join-Path $backupRoot 'chat_processes.json.replace-backup'
try {
  [System.IO.File]::WriteAllText($temporary, "[]`n", $utf8NoBom)
  [System.IO.File]::Replace(
    $temporary,
    $registryPath,
    $replaceBackup,
    $true
  )
} finally {
  if (Test-Path -LiteralPath $temporary -PathType Leaf) {
    Remove-Item -LiteralPath $temporary -Force
  }
}

$repairedRaw = [System.IO.File]::ReadAllBytes($registryPath)
if ($utf8NoBom.GetString($repairedRaw).Trim() -ne '[]') {
  Copy-Item -LiteralPath $originalBackup -Destination $registryPath -Force
  throw 'Registry verification failed; original bytes were restored.'
}

$report = [ordered]@{
  status = 'repaired'
  changed = $true
  repairedAt = [DateTimeOffset]::UtcNow.ToString('o')
  schemaConfirmation = 'current package uses an array of chat process records'
  original = $inspection
  backup = $originalBackup
  replaceBackup = $replaceBackup
  current = [ordered]@{
    path = $registryPath
    size = $repairedRaw.Length
    nulCount = @($repairedRaw | Where-Object { $_ -eq 0 }).Count
    sha256 = (
      Get-FileHash -LiteralPath $registryPath -Algorithm SHA256
    ).Hash
    jsonShape = 'array'
    recordCount = 0
  }
}
Write-OptionalReport -Value $report
$report | ConvertTo-Json -Depth 8
