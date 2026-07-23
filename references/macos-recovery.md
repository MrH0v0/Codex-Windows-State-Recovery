# macOS 手工恢复教程

本教程用于 Codex Desktop 更新、异常退出或配置重建后出现的本地状态问题。
它不会安装 Windows Guard，也不会假设 macOS 与 Windows 的应用包结构相同。

官方 `openai/codex` 仓库中的 macOS 故障报告记录，默认数据根通常是
`~/.codex`，其中可能包含 `config.toml`、`state_5.sqlite`、
`session_index.jsonl`、`sessions/` 和 `archived_sessions/`。如果设置了
`CODEX_HOME`，应使用该目录，而不是硬编码 `~/.codex`。

## 1. 先退出 Codex

从 Codex 菜单退出应用。不要在数据库仍被写入时复制或移动 SQLite 文件。

如果应用名仍是 `Codex`，可以用下面的命令请求正常退出：

```bash
if pgrep -x 'Codex' >/dev/null; then
  osascript -e 'tell application "Codex" to quit'
fi
sleep 2
pgrep -fl 'Codex' || true
```

如果仍有 Codex 进程，先确认它们是否属于正在运行的 CLI 任务。不要直接
`kill -9`。

## 2. 确认数据目录并完整备份

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

备份可能包含登录信息、聊天内容、项目名称和本地路径。只保存在可信位置，
不要把整个目录上传到 GitHub 或 issue。

记录关键文件的大小和哈希：

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

## 3. 只读检查

### 配置

下面的检查需要 Python 3.11+：

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

如果配置损坏，不要用旧文件整份覆盖。以当前版本能生成的配置为基底，只并入
已经确认仍适用的模型偏好、功能开关和项目权限。旧的绝对路径、plugin cache、
socket、runtime 和应用包路径必须重新发现。

### Global state

```bash
python3 -m json.tool \
  "$CODEX_DIR/.codex-global-state.json" >/dev/null &&
  echo "global state: ok"
```

JSON 可以解析不代表项目索引一定完整。继续比较：

- `electron-saved-workspace-roots`；
- `project-order`；
- 本地项目映射；
- 真实存在的项目目录；
- `state_5.sqlite` 中线程的 `cwd`；
- rollout 中保存的 `cwd`。

不要仅凭一个 rollout 的 `cwd` 自动创建项目。

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

只有 `quick_check` 返回 `ok` 时，线程计数才可作为健康证据。若两个数据库都
存在，少量差异可能是异步投影；明显下降或分叉需要结合备份、日志和界面复核。

### Rollout 和索引

```bash
find \
  "$CODEX_DIR/sessions" \
  "$CODEX_DIR/archived_sessions" \
  -type f -name 'rollout-*.jsonl' 2>/dev/null |
sort > "$BACKUP_DIR/rollout-files.txt"

wc -l "$BACKUP_DIR/rollout-files.txt"
wc -l "$CODEX_DIR/session_index.jsonl" 2>/dev/null || true
```

rollout 仍在通常说明原始会话没有被完全删除。侧栏不显示时，优先判断为索引、
分页、项目归组或路径标准化问题。

## 4. 按症状选择恢复方式

### 项目或聊天在侧栏消失

1. 保留当前 global state、SQLite、session index 和 rollout。
2. 确认数据库中的线程仍未归档，且 rollout 文件存在。
3. 在 Codex 的搜索中尝试标题、正文片段或已知线程 ID。
4. 如果知道线程 ID，可先用 `codex resume <thread-id>` 验证 CLI 是否仍能读取。
5. 不要批量修改 `project-order`、`projectless-thread-ids` 或线程时间戳来“顶回”
   最近列表；这些操作可能掩盖真正的 UI/索引问题。

### `config.toml` 损坏或被重建

1. 保留损坏文件原始字节。
2. 让当前版本生成一份可解析配置。
3. 对比旧备份，逐项并入仍需要的设置。
4. 对每个绝对路径重新确认文件存在。
5. 重启 Codex，确认设置、plugin 和项目权限实际生效。

### SQLite 损坏，Codex 无法启动

先确认日志明确指向 `state_5.sqlite`、`logs_2.sqlite` 或其他具体数据库。不要
因为“启动失败”就移动所有 SQLite 文件。

尝试保存恢复 SQL：

```bash
sqlite3 "$CODEX_DIR/state_5.sqlite" \
  '.recover' > "$BACKUP_DIR/state_5.recover.sql"
```

如果应用仍完全无法启动，而且完整备份已经完成，可以隔离日志明确指出的
数据库及其 sidecar，让 Codex 新建数据库。先把 `CORRUPT_DB` 改成日志中的
确切路径；下面的检查会拒绝 `$CODEX_DIR` 之外或非 SQLite 的目标：

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

这一步会让应用以新数据库启动，旧历史可能暂时不显示。隔离文件不能删除；
后续应从备份、`.recover` 输出和 rollout 中判断哪些数据需要恢复。

如果错误明确指向 `logs_2.sqlite`，将 `CORRUPT_DB` 设为那个文件，只隔离
日志数据库及其 sidecar，不要同时移动 `state_5.sqlite`。

## 5. 重启后验收

```bash
open -a Codex
```

逐项确认：

- Codex 能正常启动；
- 项目列表与真实目录一致；
- 旧聊天可在侧栏、搜索或 CLI 中找到；
- 新建聊天可以持久化并在重启后再次出现；
- `config.toml` 中保留的设置实际生效；
- SQLite `quick_check` 仍为 `ok`；
- 没有新的解析、迁移或数据库错误。

只有这些检查通过后，才能把当前备份标记为已知可用。macOS 教程目前不提供
自动 LaunchAgent，也不会自动回滚项目或数据库。

## 6. 参考

- [OpenAI Codex README：macOS 安装与运行](https://github.com/openai/codex/blob/main/README.md)
- [openai/codex #24030：macOS 更新后 SQLite 损坏导致无法启动](https://github.com/openai/codex/issues/24030)
- [openai/codex #23979：更新后项目历史在界面中消失但本地数据仍在](https://github.com/openai/codex/issues/23979)
- [openai/codex #20864：客户端共享 `~/.codex/state_5.sqlite`](https://github.com/openai/codex/issues/20864)

GitHub issue 是用户报告，不是稳定的公开 API 规范。新版本发布后，应重新确认
数据目录、SQLite schema 和应用启动方式。
