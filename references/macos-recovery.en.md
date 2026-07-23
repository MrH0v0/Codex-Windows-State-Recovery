# Manual recovery on macOS

Use this guide when Codex Desktop develops local-state problems after an update,
an unexpected exit, or a configuration reset. It does not install the Windows
Guard and does not assume that macOS uses the Windows package layout.

Reports in the official `openai/codex` repository show that the default data
root is commonly `~/.codex`. It can contain `config.toml`, `state_5.sqlite`,
`session_index.jsonl`, `sessions/`, and `archived_sessions/`. If `CODEX_HOME` is
set, use that directory instead of hard-coding `~/.codex`.

## 1. Quit Codex

Quit Codex from the application menu. Do not copy or move SQLite files while
the database is still being written.

If the application is still named `Codex`, request a normal quit with:

```bash
if pgrep -x 'Codex' >/dev/null; then
  osascript -e 'tell application "Codex" to quit'
fi
sleep 2
pgrep -fl 'Codex' || true
```

If Codex processes remain, check whether they belong to active CLI jobs. Do not
start with `kill -9`.

## 2. Locate and back up the data directory

```bash
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
BACKUP_DIR="$HOME/Desktop/codex-backup-$(date +%Y%m%d-%H%M%S)"

test -d "$CODEX_DIR" || {
  printf 'Codex data directory not found: %s\n' "$CODEX_DIR" >&2
  exit 1
}

mkdir -p "$BACKUP_DIR"
ditto "$CODEX_DIR" "$BACKUP_DIR/.codex"
printf 'Backup: %s\n' "$BACKUP_DIR/.codex"
```

The backup may contain sign-in data, conversation content, project names, and
local paths. Keep it in a trusted location. Do not upload the full directory to
GitHub or an issue.

Record sizes and hashes for important files:

```bash
find "$CODEX_DIR" -maxdepth 2 -type f \
  \( -name 'config.toml' \
  -o -name '.codex-global-state.json' \
  -o -name 'session_index.jsonl' \
  -o -name '*.sqlite' \
  -o -name '*.sqlite-wal' \
  -o -name '*.sqlite-shm' \) \
  -print0 |
while IFS= read -r -d '' file; do
  stat -f '%z %N' "$file"
  shasum -a 256 "$file"
done
```

## 3. Run read-only checks

### Configuration

This check requires Python 3.11 or later:

```bash
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"

python3 - "$CODEX_DIR/config.toml" <<'PY'
from pathlib import Path
import sys
import tomllib

path = Path(sys.argv[1])
data = path.read_bytes()
if not data:
    raise SystemExit("config.toml is empty")
if b"\x00" in data:
    raise SystemExit("config.toml contains NUL bytes")
tomllib.loads(data.decode("utf-8-sig"))
print("config.toml: ok")
PY
```

If the configuration is damaged, do not replace it wholesale with an old copy.
Start from a configuration that the current release can generate and merge
only settings that are still valid. Rediscover absolute plugin, socket,
runtime, cache, and application-bundle paths.

### Global state

```bash
python3 -m json.tool \
  "$CODEX_DIR/.codex-global-state.json" >/dev/null &&
  echo "global state: ok"
```

Valid JSON does not prove that the project index is complete. Compare:

- `electron-saved-workspace-roots`;
- `project-order`;
- the local project map;
- project directories that still exist;
- thread `cwd` values in `state_5.sqlite`;
- rollout `cwd` values.

Do not create a project from one rollout `cwd` alone.

### SQLite

```bash
for db in \
  "$CODEX_DIR/state_5.sqlite" \
  "$CODEX_DIR/sqlite/state_5.sqlite"
do
  if [ -f "$db" ]; then
    printf '\n%s\n' "$db"
    sqlite3 "$db" 'PRAGMA quick_check;'
    sqlite3 "$db" 'SELECT COUNT(*) AS thread_count FROM threads;'
  fi
done
```

Treat the thread count as health evidence only when `quick_check` returns `ok`.
If both databases exist, a small difference may be asynchronous projection.
Investigate a clear drop or divergence against backups, logs, and the UI.

### Rollouts and the session index

```bash
find \
  "$CODEX_DIR/sessions" \
  "$CODEX_DIR/archived_sessions" \
  -type f -name 'rollout-*.jsonl' 2>/dev/null |
sort > "$BACKUP_DIR/rollout-files.txt"

wc -l "$BACKUP_DIR/rollout-files.txt"
wc -l "$CODEX_DIR/session_index.jsonl" 2>/dev/null || true
```

Existing rollouts usually mean the original conversation data was not fully
deleted. If the sidebar is empty, investigate indexing, pagination, project
grouping, and path normalization before treating it as data loss.

## 4. Choose a recovery path

### Projects or conversations disappeared from the sidebar

1. Preserve the current global state, SQLite files, session index, and rollouts.
2. Confirm that the database rows are not archived and that rollouts exist.
3. Search in Codex by title, content fragment, or a known thread ID.
4. If the thread ID is known, use `codex resume <thread-id>` to check whether the
   CLI can still read it.
5. Do not bulk-edit `project-order`, `projectless-thread-ids`, or thread
   timestamps to force items into a recent list. That can hide the real UI or
   indexing failure.

### `config.toml` is damaged or was regenerated

1. Preserve the damaged file as raw evidence.
2. Let the current release generate a parseable configuration.
3. Compare it with an old backup and merge required settings one by one.
4. Recheck every absolute path.
5. Restart Codex and verify that settings, plugins, and project permissions
   actually work.

### SQLite is corrupt and Codex cannot start

First confirm that the log names `state_5.sqlite`, `logs_2.sqlite`, or another
specific database. Do not move every SQLite file merely because startup fails.

Try to preserve a recovery script:

```bash
sqlite3 "$CODEX_DIR/state_5.sqlite" \
  '.recover' > "$BACKUP_DIR/state_5.recover.sql"
```

If the application still cannot start and the full backup is complete,
quarantine only the database named in the log and its sidecars so Codex can
create a new database. Set `CORRUPT_DB` to the exact path from the log first.
The check below rejects targets outside `$CODEX_DIR` and non-SQLite files:

```bash
CORRUPT_DB="$CODEX_DIR/state_5.sqlite"

case "$CORRUPT_DB" in
  "$CODEX_DIR"/*.sqlite|"$CODEX_DIR"/*/*.sqlite) ;;
  *)
    printf 'Refusing unexpected database path: %s\n' "$CORRUPT_DB" >&2
    exit 1
    ;;
esac

test -f "$CORRUPT_DB" || {
  printf 'Database not found: %s\n' "$CORRUPT_DB" >&2
  exit 1
}

STAMP="$(date +%Y%m%d-%H%M%S)"
QUARANTINE="$BACKUP_DIR/quarantine-$STAMP"
mkdir -p "$QUARANTINE"

for file in \
  "$CORRUPT_DB" \
  "$CORRUPT_DB-wal" \
  "$CORRUPT_DB-shm"
do
  if [ -e "$file" ]; then
    mv "$file" "$QUARANTINE/"
  fi
done

open -a Codex
```

The application will start with a new database, so old history may be hidden.
Do not delete the quarantined files. Use the backup, `.recover` output, and
rollouts to decide what still needs recovery.

If the error explicitly names `logs_2.sqlite`, set `CORRUPT_DB` to that file
and quarantine only the log database and its sidecars. Do not move
`state_5.sqlite` at the same time.

## 5. Validate after restart

```bash
open -a Codex
```

Check all of the following:

- Codex starts normally;
- the project list matches real directories;
- old conversations are available in the sidebar, search, or CLI;
- a new conversation persists across another restart;
- retained `config.toml` settings take effect;
- SQLite `quick_check` still returns `ok`;
- no new parse, migration, or database error appears.

Only then mark the backup as known-good. This macOS guide does not install an
automatic LaunchAgent and never rolls back projects or databases automatically.

## 6. References

- [OpenAI Codex README: install and run on macOS](https://github.com/openai/codex/blob/main/README.md)
- [openai/codex #24030: SQLite corruption blocks startup after a macOS update](https://github.com/openai/codex/issues/24030)
- [openai/codex #23979: project history disappears from the UI while local data remains](https://github.com/openai/codex/issues/23979)
- [openai/codex #20864: clients share `~/.codex/state_5.sqlite`](https://github.com/openai/codex/issues/20864)

GitHub issues are user reports, not a stable public API contract. Recheck the
data directory, SQLite schema, and application launch method after major
releases.
