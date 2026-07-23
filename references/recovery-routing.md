# 恢复流路由

## 使用方式

先选一个主症状，只执行对应的最小恢复流。若证据跨层，先修复源状态，
再让派生层重建。

## A. `config.toml` 为空、含 NUL、解析失败或被重建

只读证据：

- 原始字节长度、NUL 分布、SHA-256、修改时间；
- 当前文件与备份候选的 `tomllib` 结果；
- 候选中的过期绝对路径、模型/服务固定值、功能开关和项目信任；
- 当前包版本和实际 runtime 路径。

流程：

1. 备份坏文件的原始字节，不先“修好”证据。
2. 用 `audit_config_candidates.py` 生成脱敏候选报告。
3. 以当前新生成配置为基底，按
   [config-merge-policy.md](config-merge-policy.md) 逐键并入。
4. 重新发现 runtime 路径，不复制旧包路径。
5. 通过 `tomllib`、严格配置加载、启动和一次重启验证。

禁止：

- 从报告中的 `<redacted>` 恢复秘密；
- 整份回滚到旧配置；
- 因提示消失而跳过安全策略复核。

## B. 侧栏项目全部或部分消失

只读证据：

- 当前 `local-projects` 与 `project-order`；
- `electron-saved-workspace-roots`；
- `source=sidebar_workspace_groups` 日志；
- rollout `cwd` 与目录是否存在；
- Git 工作区标记和候选路径范围。

流程：

1. 运行 `discover_project_candidates.py`。
2. 仅人工复核 `high` / `medium` 且当前存在的侧栏证据候选。
3. 排除用户主目录、Desktop、Documents、Codex 汇总目录和临时 worktree。
4. 构造最小 manifest。
5. 运行 `merge_recovered_projects.py` dry-run。
6. 停止 Codex；带 `--apply --confirm-codex-stopped` 应用。
7. 复核侧栏、顺序、目录、信任和第二次重启持久性。

rollout `cwd` 只能说明任务曾在该目录运行，不足以自动恢复侧栏。

## C. 历史任务消失或两个 SQLite 投影分歧

只读证据：

- 两个数据库的存在性、大小、哈希、`PRAGMA quick_check`；
- `threads` 表计数和关键已知任务 ID；
- WAL/SHM 状态；
- rollout 文件计数、任务 ID 和 provider/source 分布；
- UI 能否按已知 ID 打开任务。

流程：

1. 停止写入后用 SQLite backup API 制作一致备份。
2. 判断是 UI 投影/筛选问题、单库损坏、数据库被重置，还是 rollout 仍在但
   未被索引。
3. 优先修复当前版本的投影/索引。
4. 仅在 schema、完整性、任务计数、来源和 UI 验证均通过时考虑旧库恢复。
5. 恢复主数据库前同时处理当前 WAL/SHM，避免旧 sidecar 覆盖恢复结果。

禁止用“数据库能打开”代替 `quick_check` 和已知任务抽查。

## D. 任务存在，但原工作目录缺失

只读证据：

- rollout `session_meta.payload.cwd`；
- 当前磁盘、已挂载盘、Git worktree 列表和仓库远端；
- 任务是否可在新目录中只读打开。

流程：

1. 将问题标记为“任务历史可用、cwd 不可用”，不要把任务判定为丢失。
2. 恢复/重新克隆原仓库，或让用户选择新的可信 cwd。
3. 不篡改历史 rollout 来伪造原路径。
4. 在新任务中引用原任务证据继续工作。

## E. `chat_processes.json` 损坏或通知注册反复失败

只读证据：

- `process_manager\chat_processes.json` 的大小、NUL、哈希和 JSON 形状；
- 当前包中该记录的 schema 与空集合表示；
- Desktop 日志中的 `Failed to register chat process notification`；
- 文件修改时间和是否仍包含可能存活的 PID。

流程：

1. 备份原始字节并记录哈希。
2. 从当前安装包确认文件仍是“进程记录数组”，不要沿用旧版本假设。
3. 若文件完全不可解析、无可信备份且当前 schema 接受空数组，原子替换为
   `[]`；不要伪造历史 PID。
4. 等待下一次命令通知或重启，确认错误不再新增且文件可由当前版本重写。
5. 将该检查加入 guard，但不要把临时进程记录纳入 last-known-good 恢复。

## F. 更新后重复出现“完成 Windows 设置”或 sandbox 提示

只读证据：

- 当前 `windows.sandbox`、`sandbox_mode`、`approval_policy`；
- Windows 功能、账户权限和包日志中的失败原因；
- 提示是否只在包版本变化后出现；
- 新配置与旧配置的安全策略差异。

流程：

1. 先确认提示来自 Codex、Windows 系统，还是包初始化。
2. 解析当前配置并记录安全策略。
3. 只修改被证据证明不兼容的单一设置。
4. 明确记录安全权衡；不要把降级 sandbox 当作通用修复。
5. 启动、执行一次受控本地命令并重启验证。

## G. bundled plugin、`latest` 或 runtime 路径漂移

只读证据：

- 各版本目录的 plugin manifest；
- `latest` 入口是否存在、解析后是否仍在该插件根目录；
- config 中启用项和绝对命令路径；
- 当前包版本、缓存生成时间和严格 smoke test。

流程：

1. 区分“缓存未安装”和“已安装但 `latest` 失效”。
2. 优先使用当前 Codex 的发现/安装流程重建缓存。
3. 仅对明确启用或当前存在的缓存建立持久基线。
4. 用最小 smoke test 验证，不把旧版本目录强行标为 `latest`。

## H. 使用 last-known-good 快照

流程：

1. 查看 guard 最新报告和记录的快照路径。
2. 运行 `Restore-CodexLastHealthy.ps1 -ValidateOnly`。
3. 核对 manifest、文件集合、哈希、包版本、项目路径和数据库计数。
4. 确认恢复会覆盖哪些文件。
5. 仅在当前状态已有独立备份且用户明确批准后使用
   `-ConfirmRestore`。
6. 检查自动生成的 preimage 和恢复报告。
7. 完成文件、运行时和 UI 三层复核。

## I. 包/ASAR/native host 兼容回归

仅当状态层健康、故障可稳定复现且证据指向当前程序包时进入此流。

要求：

- 使用包版本门禁；
- 保留原文件哈希和可恢复副本；
- dry-run 展示目标与差异；
- 更新后重新检测，禁止跨版本盲目复用；
- 使用独立的 `codex-windows-fast-patch` 等版本化 skill；
- 不把程序补丁写入本 Skill 的自动状态恢复路径。
