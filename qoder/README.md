# 组会接力

组会结束后，进展在周报里，决定在聊天里，下一步又留在某个人的笔记中。组会接力把这些内容保存为有来源的项目记录：这次写下进展，下次接着未完成的任务，交接时再导出当前有效的事实。

这是可在本地运行的中文 Web 工具。支持创建独立项目、约定格式的 Markdown/CSV 导入、任务更新、四项复盘、修正与撤回，以及带来源的 Markdown 周报。PowerContext 保存项目进展、决定和报告偏好，重启后重新读取；运行时无需模型密钥。

## 安装与启动

实测环境为 Linux/WSL、Python3.12.3、Chrome156。先安装系统的 Python3.12 和 venv 支持，再在仓库的 qoder 目录执行：

```bash
bash scripts/setup.sh
bash scripts/start.sh
```

打开 http://127.0.0.1:4323 。setup 会创建本目录的 `.venv`，安装 `requirements.lock.txt` 中的全部固定依赖并运行 pip check。启动脚本使用该环境，默认监听本机回环地址、单 worker。Windows 请在 WSL 中执行上述命令。

需要自定义端口或数据位置时：

```bash
PORT=4323 DATA_DIR="$PWD/data" bash scripts/start.sh
```

Ctrl+C 停止服务。再次使用同一 DATA_DIR 启动即可接续。备份时先停止服务，再完整复制数据目录；开启新样例可改用一个新的空 DATA_DIR，保留旧目录。

## 用一次讨论接下一次工作

1. 新建并选中项目，到“导入”页选择海藻/课程样例，或上传下述格式的文件。
2. 在“任务”页添加任务、编辑标题/负责人/截止日期、标记完成。在“复盘”页填写已完成、困难、决定、下一步；错误事实用“修正”，失效记录用“撤回”。
3. 下次打开项目，从接续卡继续。卡片上的来源编号、时间和“查看来源”会打开保存于 PowerContext 的实际记录。
4. 在“偏好”页设置报告标题与输出重点，到“报告”页生成、复制或下载 Markdown。项目切换、记录或修订版变化会使旧报告失效，须重新生成。

两组完整操作及预期结果见[验证样例](samples/验证样例.md)。内置快捷按钮是简版样例；`samples/algae-experiment.md` 是含7任务、2复盘的文件版，彼此按稳定编号去重。

## 输入约定

任务包含稳定 task_id、标题、状态 `todo / doing / done`、负责人标签、可空的 YYYY-MM-DD 截止日期。表单复盘包含记录编号、日期、已完成、困难、决定、下一步；文本框支持换行。

Markdown 导入接受固定字段和五列任务表。每个字段值写在同一行，用单独的 `---` 行分隔多次复盘，不解析任意会议全文。

```markdown
# 复盘：第一周
记录编号: algae-week-1
日期: 2026-10-26
已完成: 配制三组培养基
困难: 蓝光灯需要重新标定
决定: 先固定盐度再调整光照
下一步: 记录七天的生长曲线

| ID | 标题 | 状态 | 负责人 | 截止日期 |
| --- | --- | --- | --- | --- |
| algae-light | 标定蓝光灯 | doing | 小林 | 2026-11-02 |
```

CSV 使用表头 `record_id,date,completed,blockers,decisions,next_steps,task_id,title,status,owner,due_date`；同一记录编号的复盘字段应一致，带逗号或换行的字段按CSV规则加引号。[CSV样例](samples/import-example.csv)可直接查看。

文件为 UTF-8（可带BOM），最大100KiB。错误编码、无效状态/日期、字段类型不符和空CSV给出错误，整次导入不部分写入。完整的已接受输入保存在来源记录的 raw_content 中。

## 保存与来源

每个项目对应 PowerContext scope，每次变更保存为不可变来源事件。应用按SDK真实游标分页读取，再按事件次序生成有效任务、复盘和偏好。当前任务引用最近一次修改来源；修正后的复盘替代旧版本，撤回事实退出接续卡和报告。来源ID、ISO时间和原文入口可逐条核对。导出文件的 `/api/projects/.../sources/...` 链接须在本地应用运行时从该应用地址打开。

每次业务写入携带 action_id 和 expected_revision。同动作同内容重试返回原回执；不同内容复用动作或过期版本返回冲突。前端禁用重复提交并在冲突后刷新状态。旧导入不会把已完成任务重新覆盖成待办，也不会恢复已撤回复盘；若需要新增复盘，请使用新的记录编号。

每项目最多200个任务、500个事件，最多20个项目。事件达到上限后拒绝新增，已有状态和报告仍可读取。每个数据目录仅允许一个服务进程持有，适合单机使用；没有登录和云端多人协作。localStorage只缓存选中的项目，不保存业务状态。

## 技术与核验

主要业务、界面、PowerContext接入和展示站由 Qoder CN CLI1.1.81 开发；Codex负责规格、环境、独立验收、少量脚本/文档集成和发布核验。[开发记录](development/README.md)提供账号UID、普通会话、代码差异和真实Qoder画面。[技术说明](TECHNICAL.md)说明状态选择和一致性处理。

PowerContext1.4.1、FastAPI0.115.14、Uvicorn0.34.3、Pydantic2.14.2和python-multipart0.0.20，全部传递依赖见锁文件。应用没有运行时LLM和外部账号要求。接口语义参考官方SDK及本仓库[已验证的R12接入说明](../minimax/README.md)，本应用独立实现。

[验收摘要](evidence/验收摘要.json)及配套原始记录包含真实SDK、HTTP、浏览器、重启、分页、并发与失败处理的结果。可在安装后复验：

```bash
.venv/bin/python tests/independent_contract.py
.venv/bin/python tests/edge_contract.py
.venv/bin/python tests/lifecycle_contract.py
.venv/bin/python scripts/verify_runtime.py
```

样例均为原创虚构，暂无真人使用统计。上述检查是固定样例上的工具实验，不作为用户规模或人工提效百分比。
