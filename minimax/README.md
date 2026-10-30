# 基于有效规格的阅读清单开发上下文

本样例为阅读清单新增标签筛选，并与已有状态筛选取交集。seekdb 保存规格、代码、测试与版本；MiniMax 根据实际查询结果生成业务补丁；PowerContext 保存验证进展，并在进程退出后由新进程读取，改变后续上下文与处理阶段。

需要 Linux x86_64、Python 3.12、约 4 GB 可用内存和 5 GB 磁盘空间。Windows 可在 WSL 中运行。使用 PyPI 的 `pylibseekdb==1.4.0.post2` 内置官方 server，无内核编译、不依赖 `/opt`。默认端口 2895，PowerContext 只使用本地 SQLite 基础状态接口，无需额外模型或 API 密钥。

```bash
cd minimax
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
# 从空数据目录验收已交付 MiniMax 补丁，不发生新的模型调用：
.venv/bin/python run.py replay --data "$PWD/.data/replay-01" --run-id replay-01
```

使用新的数据目录与 run id 即可重置验证样例，不删除旧记录。已有目录中的历史证据会保留；重复 run id 会被拒绝。运行会启动并关闭自己的数据库子进程，不连接其他项目数据库。可用 `--port 2896` 更换冲突端口。

若要重新让 MiniMax 开发，先复制 `.env.example` 为 `.env.local`，填写独立 `MINIMAX_API_KEY`、`MINIMAX_BASE_URL`、`MINIMAX_MODEL`。大陆地址 `https://api.minimax.cn/v1`；国际地址 `https://api.minimax.io/v1`。密钥只留在被忽略的本地文件，先执行 `.venv/bin/python minimax_client.py` 小请求，再执行：

```bash
.venv/bin/python run.py develop --data "$PWD/.data/develop-02" --run-id develop-02
```

最多 3 轮开发调用，测试失败时将真实结果反馈给 MiniMax；不自动充值。此入口会更新 `example/after/reading_list.py`，每轮候选、输入、普通输出与测试均另存。`replay` 只复验交付补丁，记录 `new_model_call=false`。

直接运行最终业务：

```bash
.venv/bin/python example/after/reading_list.py example/books.json --status done --tag 数据库
READING_MODULE="$PWD/example/after/reading_list.py" .venv/bin/python -m unittest discover -s tests -v
```

`run.py` 输出位于 `evidence/<run-id>/`：包括 `context-v1.json`、`context-v2.json`、`context-used.json`、规格历史、真实 SQL/参数/结果、前后测试、业务 diff、状态写入/新进程读取/分支决定及 summary。`state_worker.py` 是单独的 PowerContext 状态入口；`context_store.py` 提供按任务键与当前版本查询的上下文模块。任意 task key 没有资料时会明确报错，不返回预制答案。

本次必要来源：`specs/v2.json` 的 TAG-01/02/03 与 COMPAT-01/02，`example/before/reading_list.py` 的 `select_books/main`，`tests/test_reading_list.py` 的 `RegressionTests/TargetTests`，以及 `example/books.json`。v1 的四个规格条目仍在数据库历史中但 `is_current=0`。v2 新增空白标签处理、缺少 tags 与重复标签规则；更新后的查询和 MiniMax 输入均为 v2。

已执行 `development-01` 的真实 MiniMax 开发和 `clean-replay-01` 的干净依赖/空状态回放：21 项测试保持同一 SHA-256，基线 9 项回归通过、10 项目标断言失败，最终 21 项通过。详见 [一页技术说明](TECHNICAL.md) 和 `evidence/`。这些是本地验收，正式成绩仍待比赛平台。

来源：原创建模样例、规格、语料和测试；依赖 [PowerContext](https://github.com/oceanbase/powercontext)、[seekdb](https://github.com/oceanbase/seekdb) 使用原发布包，未修改上游源码。真实模型 [MiniMax 官方接口](https://platform.minimax.cn/docs/api-reference/text-openai-api)；没有保存认证头或私有思维链。
