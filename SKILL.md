---
name: repair-codex-windows-state
description: Diagnose, recover, and harden Codex Desktop state after an update, restart, reset, or configuration incident. Use the full automated workflow on Windows when config.toml or the chat process registry is empty, NUL-filled, unparseable, or regenerated; sidebar projects or task history disappear; project-order, SQLite projections, rollout cwd paths, bundled plugin caches, runtime paths, Windows sandbox settings, or package-version transitions drift; or a last-known-good guard and validation-only restore are needed. On macOS, use the bundled manual recovery guide for evidence-preserving backup, read-only inspection, hidden-history triage, and SQLite quarantine; do not run the Windows PowerShell guard.
---

# Repair Codex Windows State

## Platform routing

- On Windows, use the complete workflow below. Treat
  `%USERPROFILE%\.codex` as durable user state and the Store/MSIX package as
  replaceable program state.
- On macOS, read
  [references/macos-recovery.md](references/macos-recovery.md), or
  [references/macos-recovery.en.md](references/macos-recovery.en.md) for
  English. Do not run the PowerShell installer, Guard, scheduled-task, Appx, or
  ASAR repair steps.
- Stop if the platform is neither Windows nor macOS.

## Operating contract

Follow these rules:

1. Start read-only. Observe before changing any file, task, process, package, or
   cache.
2. Back up every file that may be changed, including SQLite `-wal` and `-shm`
   sidecars.
3. Never adopt the current machine as a healthy baseline automatically.
4. Never restore a snapshot automatically. Validation is the default; restore
   requires an explicit confirmation switch.
5. Never replace `config.toml` wholesale from an old candidate. Merge reviewed
   durable keys only.
6. Never infer sidebar membership from rollout `cwd` alone. Require sidebar-log
   evidence and human review.
7. Do not expose secrets, auth tokens, cookies, API keys, or raw conversation
   content in reports or commits.
8. If a repair must stop the Codex process that owns the current task, prepare
   and launch an external PowerShell executor, persist its log/report paths, and
   verify the result after Codex restarts.
9. Prefer the smallest reversible change. Keep package/ASAR patching separate
   from state recovery.

Read [references/state-layout.md](references/state-layout.md) before interpreting
files. Read [references/recovery-routing.md](references/recovery-routing.md) to
select a recovery flow. Before any write, read
[references/adversarial-checklist.md](references/adversarial-checklist.md).

## Core workflow

### 1. Establish scope and package identity

Record:

- current user and `%USERPROFILE%\.codex`;
- installed `OpenAI.Codex` package version and install location;
- running Codex package processes;
- current `config.toml`, global state, both SQLite databases, rollout roots,
  plugin-cache state, guard state, and scheduled tasks;
- whether the workspace or output repository is dirty.

Use read-only commands first:

```powershell
Get-AppxPackage -Name OpenAI.Codex |
  Sort-Object Version -Descending |
  Select-Object -First 1 Name, Version, InstallLocation, PackageFamilyName

python "$env:USERPROFILE\.codex\skills\repair-codex-windows-state\scripts\audit_codex_state.py" `
  --output "$env:TEMP\codex-state-audit.json"
```

Exit code `1` from `audit_codex_state.py` means degraded or critical evidence was
found; it is not permission to repair.

### 2. Preserve evidence

Create a timestamped backup outside any file that will be overwritten. At
minimum preserve:

- `config.toml`;
- `.codex-global-state.json` and its backup;
- `state_5.sqlite`, `sqlite\state_5.sqlite`, and present sidecars;
- `session_index.jsonl`;
- guard metadata and the relevant report;
- hashes, file sizes, package version, and collection time.

Use SQLite's backup API or a stopped Codex process for database copies. A plain
copy of a live database without its sidecars is not a verified backup.

### 3. Classify the incident

Choose one primary flow:

- invalid or reset config;
- missing sidebar projects;
- missing or divergent task history;
- corrupt chat process registry and repeated notification parse warnings;
- missing rollout working directories;
- Windows setup/sandbox loop;
- bundled plugin or runtime-path drift;
- guard/snapshot validation and manual restore;
- package/ASAR compatibility regression.

Use [references/recovery-routing.md](references/recovery-routing.md). Do not mix
all flows into one broad rewrite.

### 4. Build a dry-run result

For config candidates:

```powershell
python "$env:USERPROFILE\.codex\skills\repair-codex-windows-state\scripts\audit_config_candidates.py" `
  --search-root "$env:USERPROFILE\.codex" `
  --output "$env:TEMP\codex-config-candidates.json"
```

Apply the policy in
[references/config-merge-policy.md](references/config-merge-policy.md).

For sidebar candidates:

```powershell
python "$env:USERPROFILE\.codex\skills\repair-codex-windows-state\scripts\discover_project_candidates.py" `
  --output "$env:TEMP\codex-project-candidates.json"
```

Convert only manually approved, existing project roots into a manifest:

```json
{
  "projects": [
    {
      "name": "Example",
      "rootPath": "C:\\absolute\\path\\to\\Example",
      "createdAt": 1760000000000
    }
  ]
}
```

Preview the merge without writes:

```powershell
python "$env:USERPROFILE\.codex\skills\repair-codex-windows-state\scripts\merge_recovered_projects.py" `
  --state "$env:USERPROFILE\.codex\.codex-global-state.json" `
  --config "$env:USERPROFILE\.codex\config.toml" `
  --manifest ".\approved-projects.json" `
  --output ".\project-merge-dry-run.json"
```

Do not add `--trust-projects` unless each root was separately reviewed.

For repeated chat-process notification parse errors, inspect without writes:

```powershell
& "$env:USERPROFILE\.codex\skills\repair-codex-windows-state\scripts\Repair-CodexChatProcessRegistry.ps1" `
  -OutputPath "$env:TEMP\codex-chat-process-registry.json"
```

Reset only when the file is empty/all-NUL and the currently installed package
was inspected to confirm that the registry schema is still an array:

```text
-ConfirmReset -ConfirmedCurrentSchemaArray
```

### 5. Apply one bounded repair

Stop Codex before mutating global state or SQLite. For a sidebar merge, rerun the
reviewed command with:

```text
--apply --confirm-codex-stopped
```

Add `--trust-projects` only with explicit trust approval. The script creates a
preimage and rolls both global state and config back if verification fails.

For config recovery, edit by key according to the merge policy. Revalidate with
Python `tomllib` and, when available, the installed Codex CLI's strict-config
path. Do not copy stale runtime paths from an older package version.

For database recovery, prefer product-generated current databases. Restore an
older database only after integrity, schema, thread count, provenance, and UI
projection checks pass.

### 6. Restart and verify at three layers

After the external executor finishes, verify:

1. Files: hashes, TOML/JSON parse, project-order consistency, SQLite
   `PRAGMA quick_check`, database counts, cache manifests and stable `latest`
   targets.
2. Runtime: Codex starts, no repeating setup prompt appears, and required
   plugins/runtime paths resolve.
3. UI: expected projects are visible, a known historical task opens, a current
   task persists across one more clean restart, and no unexpected project was
   trusted.

Record baseline diagnostics separately from post-repair evidence.

## Install the optional durability guard

Only install after the machine has passed the full verification gate:

```powershell
& "$env:USERPROFILE\.codex\skills\repair-codex-windows-state\scripts\Install-CodexRecoveryGuard.ps1" `
  -ConfirmInstall
```

The guard:

- requires an explicit initial healthy baseline;
- snapshots only healthy state;
- records degraded evidence without overwriting last-known-good state;
- treats any thread-count decrease as drift and a decrease beyond the stored
  95% floor as critical;
- validates project IDs, roots, order, config invariants, SQLite integrity,
  chat process registry, runtime paths, and relevant installed plugin caches;
- never performs an automatic restore.

Enable optional integration with `codex-windows-fast-patch` only when that skill
is installed and separately reviewed:

```text
-EnableFastPatchIntegration
```

Do not enable it merely to silence a state-health warning.

Validate a snapshot without restoring:

```powershell
& "$env:USERPROFILE\.codex\maintenance\update-guard\Restore-CodexLastHealthy.ps1" `
  -ValidateOnly
```

Perform a restore only after reviewing the selected snapshot and preimage path:

```powershell
& "$env:USERPROFILE\.codex\maintenance\update-guard\Restore-CodexLastHealthy.ps1" `
  -ConfirmRestore
```

Uninstalling executables and the scheduled task preserves evidence and backups:

```powershell
& "$env:USERPROFILE\.codex\skills\repair-codex-windows-state\scripts\Uninstall-CodexRecoveryGuard.ps1" `
  -ConfirmUninstall
```

## Escalation boundary

State recovery does not patch signed package files, ASAR bundles, browser-native
host code, or Windows security policy. If evidence isolates the fault to those
layers, preserve the state audit, read the package-specific skill, and use its
own dry-run and version gate. Never transplant a patch across package versions.

## Completion gate

Do not report success until every applicable item in
[references/adversarial-checklist.md](references/adversarial-checklist.md)
passes. If a UI assertion cannot be observed, label it unverified. If a
required directory, secret decision, package-specific patch, or user trust
choice is missing, stop and report the precise blocker.
