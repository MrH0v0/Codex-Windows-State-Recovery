# `config.toml` 并入策略

## 核心原则

以当前版本能生成并解析的配置为基底。旧配置是证据集合，不是可直接覆盖的
镜像。任何并入都应形成逐键差异、验证结果和回退副本。

## 可优先评估的耐久项

这些项目通常表达用户长期意图，但仍需验证当前版本支持：

- `personality`、推理强度等交互偏好；
- 明确启用的 feature/plugin；
- marketplace 声明；
- 已逐个确认的项目 `trust_level`；
- memory、desktop 等非版本路径偏好；
- 用户主动选择的审批策略。

## 必须重新发现的版本耦合项

不要从旧配置原样复制：

- 指向 WindowsApps、包内 resources 或缓存版本目录的绝对路径；
- `notify` 可执行文件；
- MCP server 的 Node/Python/CLI 绝对命令；
- `NODE_REPL_NODE_PATH`、`CODEX_CLI_PATH`；
- bundled plugin 的 `latest` 目标；
- 当前版本已删除或重命名的字段。

先从当前包、当前 PATH 或当前 plugin manifest 重新发现，再写入。

## 必须单独批准的安全项

以下变化不能作为“恢复丢失项目”的附带操作：

- `sandbox_mode`；
- `windows.sandbox`；
- `approval_policy`；
- 工作区写权限或网络权限；
- 项目 `trust_level`；
- 任意执行命令、hook、MCP server；
- auth provider、base URL 或 credential 来源。

报告旧值、当前值、拟议值和安全影响，得到明确批准后再改。

## 必须重新验证的服务项

模型名、provider、service tier、wire API、实验字段可能随版本或账户变化。
旧值只能作为候选。使用当前 CLI/产品支持面验证后再保留。

## 秘密处理

- 不在审计报告中输出秘密值；
- 不把 `.codex` 备份、auth 文件、cookie 或原始对话提交到 Git；
- 不从 `<redacted>`、哈希或截图“推断”秘密；
- 若当前配置缺少必要秘密，让用户通过产品的受支持登录/密钥流程重新提供。

## 最小并入记录

每个键至少记录：

```text
key:
source:
old_value_class:
new_value_class:
reason:
current_version_validation:
rollback_file:
```

`value_class` 表示值类型或脱敏摘要，不记录秘密原文。

## 验证门槛

1. 文件非空、无 NUL、UTF-8 可读；
2. Python `tomllib` 解析通过；
3. 当前 Codex 严格配置加载不报告 unknown field；
4. 所有绝对执行路径存在且属于预期版本；
5. 安全项与批准一致；
6. Codex 正常启动；
7. 一次完整退出和重启后值仍保持；
8. 项目、任务、插件没有因并入发生无关回归。
