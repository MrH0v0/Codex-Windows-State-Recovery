# 对抗性验收清单

## 1. 边界与证据

- [ ] 已记录当前包版本、安装位置、Codex Home 和时间。
- [ ] 已区分耐久状态、历史事实、派生缓存和程序包。
- [ ] 审计未写入源文件；显式 `--output` 除外。
- [ ] 报告不含秘密、原始对话正文或可直接复用的凭据。
- [ ] 所有结论都有文件、哈希、数据库、日志或 UI 证据。

## 2. 备份与回滚

- [ ] 每个拟修改文件都有修改前镜像、大小和 SHA-256。
- [ ] SQLite 备份使用停止写入或 backup API，并考虑 WAL/SHM。
- [ ] 备份位置不在将被替换的目录中。
- [ ] 回滚目标使用绝对、已验证且受限于 Codex Home 的路径。
- [ ] 故障注入已证明中途失败会恢复 preimage。

## 3. 配置

- [ ] `config.toml` 非空、无 NUL、`tomllib` 解析通过。
- [ ] 未使用旧候选整文件覆盖。
- [ ] 版本耦合路径来自当前版本。
- [ ] sandbox、审批、执行命令和项目信任均有独立批准。
- [ ] 旧字段、服务固定值和 provider 经过当前版本验证。

## 4. 项目侧栏

- [ ] `local-projects` 是对象，项目 ID 唯一。
- [ ] `project-order` 是无重复字符串列表，集合与项目 ID 完全一致。
- [ ] 每个 rootPaths 项结构有效；缺失目录被显式报告。
- [ ] 没有把主目录、Desktop、Documents、汇总目录或临时 worktree 自动加入。
- [ ] 仅 rollout `cwd` 的候选没有被自动恢复。
- [ ] `--trust-projects` 只用于逐个批准的根目录。

## 5. 历史与数据库

- [ ] 两个数据库均通过 `PRAGMA quick_check`。
- [ ] `threads` 表可查询，计数已记录。
- [ ] 任何计数下降都被报告；超过基线 5% 的下降会阻断健康状态。
- [ ] 恢复前验证 snapshot hash、大小、允许路径和健康标记。
- [ ] 恢复时不会让旧 WAL/SHM 覆盖主数据库。
- [ ] 至少一个已知历史任务可从 UI 打开。
- [ ] `process_manager\chat_processes.json` 缺失或为有效对象数组；NUL/解析错误不会被基线采纳。

## 6. Plugin 与 runtime

- [ ] 只把当前存在或明确启用的缓存纳入基线。
- [ ] plugin manifest 有效。
- [ ] `latest` 的解析目标稳定且仍位于对应 plugin 根目录。
- [ ] 绝对 runtime 路径存在，不指向上一包版本。
- [ ] 缓存缺失和缓存损坏被区别处理。

## 7. Guard 与自动化

- [ ] 缺少/损坏基线时 guard 拒绝自动采纳当前状态。
- [ ] 仅在完整健康验收后显式刷新基线。
- [ ] 健康快照先在 `.partial` 完成，再原子发布。
- [ ] degraded/critical 运行不会覆盖 last-known-good 指针。
- [ ] 定时任务为 Limited 权限、IgnoreNew、有限执行时间。
- [ ] 定时任务只检测/快照，不自动恢复状态。
- [ ] 可选 fast-patch 集成未安装时不会影响纯状态 guard。

## 8. 重启与 UI

- [ ] 写入发生时 Codex 已停止，或由外部 executor 完成。
- [ ] Codex 由当前包 AUMID 重新启动，而非硬编码旧路径。
- [ ] 重复 Windows 设置提示不再出现，或仍出现时有明确证据。
- [ ] 预期项目全部显示且顺序合理。
- [ ] 新任务和已知旧任务均可打开。
- [ ] 再进行一次完整退出/重启后状态仍保持。

## 9. 开源交付

- [ ] `SKILL.md` frontmatter 仅含 `name` 和 `description`。
- [ ] UI metadata 能解析，默认 prompt 包含 `$repair-codex-windows-state`。
- [ ] Python 在 3.11+ 编译并通过测试。
- [ ] PowerShell 5.1 语法解析通过。
- [ ] 测试只在临时目录进行，不访问真实 `.codex`。
- [ ] 仓库不包含用户路径、任务 ID、配置备份、数据库、日志或凭据。
- [ ] README 的能力声明与实际脚本一致。
- [ ] MIT License 已包含。

只有所有适用项通过，才能将结果标记为“审查通过”。未观察到的 UI 项必须
标为“未验证”，不能由单元测试替代。
