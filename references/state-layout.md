# Codex Windows 状态布局

## 目的

在恢复前先区分“耐久状态、历史事实、派生缓存、程序包”。不同层的
证据权重和修复方式不同，不能互相覆盖。

## 状态层

| 路径或来源 | 角色 | 恢复原则 |
|---|---|---|
| `%USERPROFILE%\.codex\config.toml` | 用户配置、功能开关、项目信任、运行时引用 | 解析后按键并入；禁止用旧文件整份覆盖 |
| `%USERPROFILE%\.codex\.codex-global-state.json` | Desktop 侧栏项目及顺序等 UI 耐久状态 | 同时验证 `local-projects`、`project-order`、根目录 |
| `%USERPROFILE%\.codex\state_5.sqlite` | 一套任务/线程投影 | `PRAGMA quick_check`、表结构和计数均需验证 |
| `%USERPROFILE%\.codex\sqlite\state_5.sqlite` | 另一套任务/线程投影 | 允许小幅异步差异；显著差异需定位而非盲目合并 |
| `sessions\`、`archived_sessions\` | rollout 历史事实和任务元数据 | 可证明任务存在及其 `cwd`，不能单独证明侧栏固定关系 |
| `session_index.jsonl` | 辅助索引 | 视为可重建索引，不替代 rollout 或数据库 |
| `process_manager\chat_processes.json` | 当前/近期命令进程记录数组 | 属于可重建运行状态；损坏会造成重复通知解析错误，不应从旧版本盲目恢复 PID |

SQLite 数据库若存在 `-wal`、`-shm`，它们属于同一个当前状态单元。直接
复制主文件而丢弃 sidecar 可能得到旧视图或不一致备份。

## 派生层

| 路径或来源 | 角色 | 恢复原则 |
|---|---|---|
| `.codex\plugins\cache\...` | bundled plugin 的版本缓存与 `latest` 入口 | 校验 manifest 和解析后的稳定目标；优先由当前版本重建 |
| `config.toml` 中的绝对 runtime 路径 | 当前包/工具路径 | 更新后重新发现；旧绝对路径仅是候选证据 |
| Windows Package 日志 | UI、工作区和更新事件证据 | 只读提取，不把日志中的所有 `cwd` 自动变成项目 |
| `.codex\maintenance\update-guard` | 守护脚本、基线和报告 | 基线必须显式刷新；报告不能替代源状态 |
| `.codex\backups_state\update-guard` | 健康快照、故障证据和恢复前镜像 | 保留 manifest、哈希与来源；不自动恢复 |

## 程序层

`Get-AppxPackage -Name OpenAI.Codex` 返回当前 Store/MSIX 包版本和安装位置。
程序包内容会在更新时整体替换，不应作为用户耐久状态存储点。

任何 ASAR、native host、脚本或签名包修改都属于版本绑定的兼容层修复，
必须与状态恢复隔离，并在包版本变化后重新验证。

## 证据优先级

同一结论存在冲突时，按以下顺序解释：

1. 当前可解析的源文件、数据库完整性结果和真实目录；
2. 停止 Codex 后制作并带哈希的备份；
3. rollout/session 元数据和当前包日志；
4. 守护报告与 UI 截图；
5. 文件名、时间戳或未经验证的旧副本。

UI “看不见”不等于数据不存在；文件“还在”也不等于当前版本会加载它。
