# Codex Windows 状态恢复

[中文](README.md) | [English](README.en.md)

Codex 更新或异常退出后，项目、聊天记录或配置突然不见了，不一定是数据真的丢了。针对这个问题，我设计了这个 Skill 通过codex一般任务进行修复。这个skill会先检查本地文件和数据库，再根据实际问题选择恢复方法。

它主要处理这些情况：

- `config.toml` 变空、损坏、无法解析或被重新生成；
- 侧栏项目全部或部分消失；
- 聊天记录在界面里不见了，但数据库或 rollout 文件还在；
- SQLite 损坏，导致 Codex 无法启动或历史记录异常；
- Windows 更新后反复出现设置或 sandbox 提示；
- bundled plugin、manifest 或 runtime 路径在更新后失效；
- 需要保存一份确认可用的备份，方便以后检查或手工恢复。

> [!IMPORTANT]
> 本 Skill 默认只检查，不会自动覆盖配置、项目或数据库。需要写文件的步骤
> 必须明确确认，并且会先备份原文件。

## 平台支持

| 平台 | 支持范围 |
|---|---|
| Windows 10/11 | 完整脚本、项目恢复、Guard、健康快照和人工恢复 |
| macOS | 手工备份、只读检查、历史定位和 SQLite 应急恢复教程 |

Windows 脚本依赖 Appx、计划任务和 PowerShell，不能直接在 macOS 上运行。
macOS 用户请使用下面的手工流程，不要安装 Windows Guard。

## 设计目标

1. **证据优先**：先验证原始文件、数据库、rollout、日志和真实目录，再
   选择修复流。
2. **最小修改**：配置按键并入；项目按审核后的 manifest 增量恢复；不做
   无关重写。
3. **可回退**：写入前备份，原子替换，中途失败自动恢复 preimage。
4. **更新耐受**：将用户耐久状态与可替换的应用包、缓存和版本耦合路径
   分离。
5. **默认安全**：审计只读、报告脱敏、baseline 与 restore 都需要显式
   动作。
6. **可验证**：同时检查文件层、运行时层和 UI 层；单元测试不能替代
   重启后的界面验收。

## 工作原理

```mermaid
flowchart TD
    A["只读审计<br/>配置 / 项目 / DB / rollout / plugin"] --> B{"主故障类型"}
    B --> C["配置逐键并入"]
    B --> D["侧栏项目候选复核"]
    B --> E["历史投影与 cwd 定位"]
    B --> F["sandbox / plugin / runtime 定位"]
    C --> G["修改前 preimage"]
    D --> G
    E --> G
    F --> G
    G --> H["单一、显式、可回退修复"]
    H --> I["文件 + 运行时 + UI 验证"]
    I --> J{"完整健康？"}
    J -- "否" --> K["保留故障证据，不刷新基线"]
    J -- "是" --> L["显式建立基线与健康快照"]
    L --> M["定时检测漂移<br/>永不自动恢复"]
```

状态层说明、证据优先级和恢复路由分别见：

- [`references/state-layout.md`](references/state-layout.md)
- [`references/recovery-routing.md`](references/recovery-routing.md)
- [`references/config-merge-policy.md`](references/config-merge-policy.md)
- [`references/adversarial-checklist.md`](references/adversarial-checklist.md)
- [`references/macos-recovery.md`](references/macos-recovery.md)

## macOS 教程：先备份，再判断是哪一层出了问题

macOS 的 Codex 数据目录是 `$CODEX_HOME`；没有设置该变量时，默认使用
`~/.codex`。下面的命令只读取或备份数据。

### 1. 退出 Codex 并备份

先从菜单退出 Codex。然后打开 Terminal：

```bash
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
BACKUP_DIR="$HOME/Desktop/codex-backup-$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"
ditto "$CODEX_DIR" "$BACKUP_DIR/.codex"
printf 'Backup: %s\n' "$BACKUP_DIR/.codex"
```

备份中可能包含登录信息、聊天内容和本地路径。不要上传或公开整个备份。

### 2. 检查配置、数据库和会话文件

```bash
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"

python3 -m json.tool \
  "$CODEX_DIR/.codex-global-state.json" >/dev/null &&
  echo "global state: ok"

for db in \
  "$CODEX_DIR/state_5.sqlite" \
  "$CODEX_DIR/sqlite/state_5.sqlite"
do
  if [ -f "$db" ]; then
    printf '%s: ' "$db"
    sqlite3 "$db" 'PRAGMA quick_check;'
  fi
done

find \
  "$CODEX_DIR/sessions" \
  "$CODEX_DIR/archived_sessions" \
  -type f -name 'rollout-*.jsonl' 2>/dev/null | wc -l
```

如果 `state_5.sqlite` 和 rollout 仍在，而侧栏看不到历史，先按“界面或索引
问题”处理，不要删除数据库或批量重写 `project-order`。

### 3. Codex 因 SQLite 损坏无法启动

只有日志明确指出某个 SQLite 文件损坏，并且完整备份已经完成时，才将该
数据库和它的 `-wal`、`-shm` 文件一起移到隔离目录。不要直接删除。

详细命令、配置并入方法和重启验收清单见
[`references/macos-recovery.md`](references/macos-recovery.md)。

## Windows：安装 Skill

要求：

- Windows 10 或 Windows 11；
- Windows PowerShell 5.1 或 PowerShell 7；
- Python 3.11+（使用标准库 `tomllib`）；
- 已运行过 Codex Desktop，并存在 `%USERPROFILE%\.codex`。

将仓库克隆到 Codex Skills 目录：

```powershell
git clone `
  https://github.com/MrH0v0/repair-codex-windows-state.git `
  "$env:USERPROFILE\.codex\skills\repair-codex-windows-state"
```

重新打开 Codex 后，可直接请求：

```text
使用 $repair-codex-windows-state 审计这台 Windows 电脑上的 Codex
更新后状态异常；先只读，不要自动恢复。
```

## 快速开始：只读审计

### 1. 审计当前状态

```powershell
python "$env:USERPROFILE\.codex\skills\repair-codex-windows-state\scripts\audit_codex_state.py" `
  --output "$env:TEMP\codex-state-audit.json"
```

检查范围包括：

- `config.toml` 的字节、TOML 和关键运行时引用；
- global state 的项目结构、`project-order` 和 chat process registry；
- 两个 SQLite 数据库的 `PRAGMA quick_check` 和线程计数；
- rollout 数量、来源分布和丢失的 `cwd`；
- bundled plugin manifest 与 `latest` 的稳定目标；
- 已安装 guard 的基线/状态元数据。

退出码 `1` 表示检测到 degraded/critical 证据，不代表脚本执行失败，也不
授权写入。

### 2. 审计配置候选

```powershell
python "$env:USERPROFILE\.codex\skills\repair-codex-windows-state\scripts\audit_config_candidates.py" `
  --search-root "$env:USERPROFILE\.codex" `
  --output "$env:TEMP\codex-config-candidates.json"
```

报告会隐藏敏感字段。候选分数只用于缩小人工复核范围，不能作为整文件覆盖
依据。

### 3. 发现侧栏项目候选

```powershell
python "$env:USERPROFILE\.codex\skills\repair-codex-windows-state\scripts\discover_project_candidates.py" `
  --output "$env:TEMP\codex-project-candidates.json"
```

脚本区分：

- 有侧栏日志且仍存在的 Git 工作区；
- 只有 rollout `cwd` 的低置信度路径；
- 用户主目录、Desktop、Documents 等过宽路径；
- Codex 临时 worktree；
- 已经存在于侧栏的项目。

它只生成候选，不修改 global state。

### 4. 检查 chat process registry

```powershell
& "$env:USERPROFILE\.codex\skills\repair-codex-windows-state\scripts\Repair-CodexChatProcessRegistry.ps1" `
  -OutputPath "$env:TEMP\codex-chat-process-registry.json"
```

默认只检查。只有文件为空或全 NUL，且已经从**当前安装包**确认该 schema
仍是记录数组时，才允许：

```text
-ConfirmReset -ConfirmedCurrentSchemaArray
```

脚本会保留原始字节和替换前镜像，再原子写入空数组；不会伪造历史 PID。

## 增量恢复侧栏项目

先将人工批准的项目写入 manifest：

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

默认 dry-run：

```powershell
python "$env:USERPROFILE\.codex\skills\repair-codex-windows-state\scripts\merge_recovered_projects.py" `
  --state "$env:USERPROFILE\.codex\.codex-global-state.json" `
  --config "$env:USERPROFILE\.codex\config.toml" `
  --manifest ".\approved-projects.json" `
  --output ".\project-merge-dry-run.json"
```

确认 diff、停止 Codex 并完成独立备份后，才允许：

```powershell
python "$env:USERPROFILE\.codex\skills\repair-codex-windows-state\scripts\merge_recovered_projects.py" `
  --state "$env:USERPROFILE\.codex\.codex-global-state.json" `
  --config "$env:USERPROFILE\.codex\config.toml" `
  --manifest ".\approved-projects.json" `
  --apply `
  --confirm-codex-stopped `
  --output ".\project-merge-applied.json"
```

`--trust-projects` 是独立的安全决策，默认关闭。脚本拒绝临时 Codex
worktree；主目录、Desktop、Documents 等宽泛根目录还需要额外的
`--allow-broad-roots`，不建议使用。

## 可选：安装持久化 Guard

只有当前机器已经通过
[`references/adversarial-checklist.md`](references/adversarial-checklist.md)
的完整验收后，才建立 baseline：

```powershell
& "$env:USERPROFILE\.codex\skills\repair-codex-windows-state\scripts\Install-CodexRecoveryGuard.ps1" `
  -ConfirmInstall
```

安装器会：

- 备份已有 guard 和同名计划任务；
- 安装检测/快照/人工恢复脚本；
- 对当前配置、项目结构、chat process registry、两套数据库和相关
  plugin cache 做严格校验；
- 显式建立 schema v2 baseline；
- 创建一个 Limited 权限、每 30 分钟运行、`IgnoreNew` 的计划任务；
- 安装失败时恢复原脚本、基线和计划任务。

Guard 的关键语义：

- 缺失 baseline 时拒绝运行，不自动采纳当前状态；
- 任意任务计数下降都会产生 drift 警告；
- 下降超过 baseline 的 5% 会进入 critical；
- 只对健康状态生成可恢复快照；
- degraded/critical 只保存证据，不更新 last-known-good；
- plugin 基线只包含当前有效存在或明确启用的缓存；
- 永不自动恢复。

如需与独立的 `codex-windows-fast-patch` Skill 集成，必须显式添加：

```text
-EnableFastPatchIntegration
```

未安装该 Skill 时，通用状态 Guard 仍可独立工作。

### 验证与人工恢复

默认只验证 last-known-good：

```powershell
& "$env:USERPROFILE\.codex\maintenance\update-guard\Restore-CodexLastHealthy.ps1" `
  -ValidateOnly
```

人工确认覆盖范围后：

```powershell
& "$env:USERPROFILE\.codex\maintenance\update-guard\Restore-CodexLastHealthy.ps1" `
  -ConfirmRestore
```

恢复器会校验允许路径、manifest、大小、SHA-256、TOML、JSON 和 SQLite；
停止当前 Codex 包进程；保留恢复前镜像；清理旧 WAL/SHM；原子替换；失败
时自动回滚；再使用当前包 AUMID 启动 Codex。

### 卸载自动化

```powershell
& "$env:USERPROFILE\.codex\skills\repair-codex-windows-state\scripts\Uninstall-CodexRecoveryGuard.ps1" `
  -ConfirmUninstall
```

卸载器移除计划任务和可执行脚本，但保留 baseline、报告和恢复快照，便于
审计或手工取回。

## 脚本清单

| 脚本 | 默认行为 | 写入门槛 |
|---|---|---|
| `audit_codex_state.py` | 只读状态审计 | 仅显式 `--output` 写报告 |
| `audit_config_candidates.py` | 只读、脱敏配置候选审计 | 仅显式 `--output` 写报告 |
| `discover_project_candidates.py` | 只读侧栏候选发现 | 仅显式 `--output` 写报告 |
| `Repair-CodexChatProcessRegistry.ps1` | 只读检查 | 两个显式确认参数 |
| `merge_recovered_projects.py` | dry-run | `--apply --confirm-codex-stopped` |
| `codex_update_guard.py` | 检测、健康快照或故障证据 | baseline 需显式刷新 |
| `Invoke-CodexUpdateGuard.ps1` | Windows 包装器 | 透传显式参数 |
| `Invoke-CodexUpdateMaintenance.ps1` | Guard 定时入口 | fast-patch 集成默认关闭 |
| `Restore-CodexLastHealthy.ps1` | validation-only | `-ConfirmRestore` |
| `Install-CodexRecoveryGuard.ps1` | 拒绝安装 | `-ConfirmInstall` |
| `Uninstall-CodexRecoveryGuard.ps1` | 拒绝卸载 | `-ConfirmUninstall` |

## 安全模型与非目标

本项目假设当前 Windows 用户账户本身可信。SHA-256 用于检测意外损坏和
快照漂移，不是用来抵抗已控制该账户、且能同时篡改 manifest 的攻击者。

本项目不会：

- 绕过 Windows 安全策略或组织管理；
- 修改已签名 MSIX 包、ASAR 或 native host；
- 猜测或恢复凭据；
- 从所有历史 `cwd` 自动生成侧栏；
- 把 sandbox 降级当作通用修复；
- 承诺不同 Codex 版本之间的私有状态 schema 永远兼容。

版本更新后应先运行只读审计。若状态层健康而故障明确位于程序包，再使用
独立、版本门控、可回滚的 package patch workflow。

## 开发与验证

运行 Python 测试：

```powershell
python -m unittest discover -s tests -v
```

编译所有 Python 文件：

```powershell
python -m compileall -q scripts tests
```

用 Windows PowerShell 5.1 解析脚本：

```powershell
Get-ChildItem .\scripts\*.ps1 | ForEach-Object {
  $parseTokens = $null
  $parseErrors = $null
  [void][System.Management.Automation.Language.Parser]::ParseFile(
    $_.FullName,
    [ref]$parseTokens,
    [ref]$parseErrors
  )
  if ($parseErrors) {
    $parseErrors
    throw "PowerShell parse failed: $($_.Name)"
  }
}
```

测试必须使用临时 Codex Home，不得读写真实 `%USERPROFILE%\.codex`。

## 发布前检查

- 运行完整故障注入测试；
- 在一台可丢弃的 Windows 用户环境中完成安装、检测、验证型恢复和卸载；
- 检查仓库不含用户绝对路径、配置备份、数据库、日志、任务 ID 或凭据；
- 对新 Codex 包版本重新验证状态 schema、进程定位、AUMID 和 plugin
  cache 布局；
- 仅在适用项全部通过后标记 release。

## License

本项目使用 [MIT License](LICENSE)。

Copyright (c) Codex Windows Recovery contributors.
