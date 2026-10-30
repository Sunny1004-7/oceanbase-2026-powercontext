# 知页：浏览器本地知识库

导入中文文本，在浏览器 Dedicated Worker 中生成向量，向量与原文通过 seekdb-js 存入本地 seekdb。查询也在浏览器生成向量，由 seekdb 执行全文与向量混合检索并返回排序原文。没有聊天、登录或云端推理接口。

需要 Linux x86_64（Ubuntu 24.04 / WSL 已验证）、Python 3.12、4 GB 可用内存、约 5 GB 磁盘，以及支持现代 WebGPU 的 Chrome/Edge。没有可用 GPU 时会自动使用 WASM，核心导入/查询仍可运行。本次实际浏览器为 Windows Chrome 156，硬件 RTX 4060 Laptop GPU。

```bash
cd stepfun
bash scripts/setup.sh
export PATH="$PWD/.tools/node-v22.14.0-linux-x64/bin:$PATH"
.venv/bin/python scripts/run.py
```

打开 `http://localhost:4312`。选择知识库，粘贴文本或选择 `.txt/.json`，点击“导入当前知识库”；也可点击“载入示例笔记”。每篇笔记用空行分隔。JSON 格式为 `[{"id":"note-1","title":"标题","text":"正文"}]`。同一 ID 再次导入会更新原文，单批次重复 ID 会明确报错。搜索结果显示顺序、ID 与完整原文。处理期间显示加载/向量化/写入进度，并暂时禁用切库按钮。刷新页面后可继续搜索已有数据。

脚本下载固定 Node 22.14.0 并检查官方 SHA-256，建立独立 `.venv`，按 `package-lock.json` 与 `requirements.txt` 安装依赖，下载固定模型文件并核验每个文件 SHA-256。npm 的 `legacy-peer-deps` 避免 seekdb 发布依赖中的 `workspace:*` peer 声明；`ignore-scripts` 避免安装浏览器应用并不使用的 ONNX Runtime Node 推理二进制。生产推理只使用浏览器运行时。实际数据库采用 `pylibseekdb==1.4.0.post2` 自带、未经修改的官方 seekdb 1.4.0.0 server，无源码编译，不依赖其他项目的 `/opt` 或旧环境。

`.env.example` 是无密钥配置示例，只包含本地数据路径及两个端口。应用读取已导出的环境变量；可在启动前用 `set -a; source .env.example; set +a` 导出示例设置，不隐式读取用户其他项目的配置。

`scripts/run.py` 负责启动自己的 seekdb（默认端口 2894）、有界等待真实 SQL 就绪、建立 `test` 库并启动 Node 服务；Ctrl+C 会关闭自己的子进程。`npm start` 只启动 Node，适用于数据库已经按同样配置运行的情况。

使用全新目录即可重置，不删除历史数据：

```bash
APP_DATA_DIR="$PWD/.data/run-02" SEEKDB_PORT=2897 PORT=4313 .venv/bin/python scripts/run.py
```

不同知识库及模型方案映射到不同集合，身份包含模型 revision、精度、后端、维度、CLS 池化、归一化与查询规则。GPU/FP16 自动降级为 WASM/INT8 时只使用匹配方案的集合；若为空，重新导入文本建立该方案的索引。模型文件缓存与同一 Worker 的模型实例会复用，不能把不同向量方案的数据混在一起。

模型为公开 MIT 来源 `Xenova/bge-small-zh-v1.5`，完整 revision `75c43b069aac4d136ba6bc1122f995fedcfd2781`；使用上游公开 FP16 与 INT8 ONNX 变体，保留 FP32 基线。配置、实际文件字节数、散列及获取 URL 见 `model-manifest.json`，原模型与 ONNX 转换来源见 `model-provenance.json` 和 `licenses/`。下载资产可以联网；运行时全部模型/运行时资产由本地服务提供，`env.allowRemoteModels=false`。

实际接口：`GET /api/config` 返回固定模型身份；`GET /api/status?kb=notes&profile=gpu_fp16` 返回当前集合数量；`POST /api/import` 接受文本/元数据与浏览器生成的向量；`POST /api/search` 接受原始查询与浏览器向量，返回数据库混合检索结果。Node 仅存储/查询和记录状态，不生成 embedding。`embeddingFunction:null` 显式禁用 SDK 默认模型。

索引刷新使用 SDK `refresh_index()` 对应 `CALL dbms_index_manager.refresh()`，随后在 12 秒上限内检查写入原文可见性，超时返回错误。正常检索使用 `collection.hybridSearch`：数据库 `DBMS_HYBRID_SEARCH.GET_SQL` 生成含 `MATCH`、向量条件与 RRF 的 SQL，再由数据库执行；应用不拼接两路排名。源码中的 `_delegate._internal.execute` 仅为固定 SDK 1.4.0 加入 SQL 审计，不改检索算法，升级 SDK 时需重新核验该记录接口。

验收命令由执行者运行，评审可复现；不需要用户标注或测量。Windows 主流 Chrome 运行真实硬件测试时，在 Windows Node 中执行（Playwright 包本身是跨平台 JS）：

```powershell
$env:APP_URL='http://localhost:4312'
$env:RUN_ID='judge-run-01'
node tests/benchmark.cjs
node tests/ui-flow.cjs
# 关闭并以同一个 APP_DATA_DIR 重启服务后：
$env:RESTART_ONLY='1'
node tests/benchmark.cjs
```

`tests/benchmark.cjs` 每种方案进行三次完整导入、每条查询五次热运行，固定 50 篇原创 CC0 笔记和 20 条事先冻结标注；自动降级场景通过禁用 Chrome GPU 与软件光栅化实现，初始化请求仍为 `auto`。浏览器测试阻止所有非 localhost 请求并保存网络证据。硬件 GPU 必须是非软件 NVIDIA 适配器；换硬件可调整测试中的适配器厂商断言，不能伪造成功记录。`tests/ui-flow.cjs` 另验证真实点击导入、进度、错误提示、文本增量、搜索与页面刷新。独立空库执行 UI 验收，避免把已有笔记数量误当初始空状态。

原始样本见 `evidence/final-browser-raw.zip`，400 条热查询 CSV 见 `evidence/query-samples.csv`，实际 SDK SQL、写入向量与结果见 `evidence/database-audit.jsonl.gz`；`evidence/summary.json` 为可检查汇总，`scripts/summarize.py` 可在解压原始样本后重建。早期 0.75 的完整原始对比及一次失败的 GPU 禁用参数测试保留在 `evidence/preliminary-raw.zip`。本次通用中文全文条件清理/术语扩展后为 0.95，保留原始语义查询向量和所有标注。评测集参与了调试，指标不代表独立泛化能力。

查看 [一页技术说明](TECHNICAL.md) 的模型体积、分阶段延迟与比较边界。已完成独立干净依赖安装、空应用数据目录导入/查询、自动降级、增量/更新/切库、刷新和服务重启。全部结果为本地实测，正式分数待比赛平台。
