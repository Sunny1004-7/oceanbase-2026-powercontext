# E02 一页技术说明

**任务与结果。** 阅读清单原有状态过滤、排序和中文 JSON CLI。本次由 MiniMax-M2.5 真实生成新增 `--tag`、标签清理和与状态取交集的补丁。执行者审阅完整业务差异并运行固定测试，没有修改模型生成后的业务文件；21 项中基线 10 项业务失败，补丁后全通过。测试 SHA-256：`8d4bc3d947e82816392bb5e1e2f6b9dc7c2d538ec4d4c24088c9fbcea770e9c9`。

**工程上下文。** seekdb 1.4.0.0（PyPI `pylibseekdb==1.4.0.post2` 打包 server，revision `1-bf450b4c8deb3c45d85efb1bd5f43e282d101914`），通过 PyMySQL 1.2.0 写入 `project_sources`。每条来源含路径、类型、符号、版本、SHA-256 和当前标记。真实 SQL 按 task key/current/kind 查询，返回规格、代码、测试和初始数据，直接进入 MiniMax 输入；未使用 embedding API。v1→v2 事务更新 current 标记并保留历史，检索含 v2 的 TAG-03，旧版不再是当前来源。

**真实模型开发。** 大陆 MiniMax OpenAI 兼容 HTTPS 接口，固定请求型号 `MiniMax-M2.5`。`evidence/development-01/minimax-round-1/` 保存实际 messages、普通返回内容、usage、可获得的请求 ID、耗时、候选摘要、应用记录及测试。普通输出中的完整源码与 `example/after/reading_list.py` 字节一致。只允许更改业务文件，失败时最多反馈修复三轮；本次一轮通过。

**跨进程状态。** PyPI `powercontext[builtin]==1.4.1`，使用真实 `open_builtin_runtime`、`scopes.create`、`sources.for_scope().capture_working_note`、`records.for_scope().list_sources`。从空 SQLite 状态开始，写入基线结果后结束进程；新 PID 读取实际 SourceRecord，决定进入 `modify_with_current_v2_context`，将已验证事项加入模型上下文并跳过重复基线/导入步骤。完成后保存最终验证，新进程读取后选择 `deliver_verified_patch`。应用不直接操作该 SQLite 文件。

**复现与边界。** 独立 Python 3.12 虚拟环境，全部版本见锁文件；无需 `/opt`、旧项目、R11 二进制或额外生成模型。空目录离线回放 `clean-replay-01` 再次完成数据库导入/版本更新/状态重启/固定测试，明确不是新模型调用。规格、记录、前后快照与补丁全部交付。需要重新模型开发时才使用被忽略的 `.env.local`，不包含凭据和私有思维链。六项均为本地验收通过，官方评分未取得。
