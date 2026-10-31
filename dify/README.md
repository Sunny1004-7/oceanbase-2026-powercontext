# 星际失物招领局

一家回收抽象失物的小店。玩家可以登记丢失的周末、勇气或笑话，决定把它原样找回，或改装成能够分享的纪念物，最后领取一张专属回执。招领员阿弧会回应玩家写下的理由；再次来访时，它会读出此前留下的选择和纪念物。

作品采用 Dify Chatflow 编排三幕体验，由 MiniMax 生成角色反应，PowerContext 保存玩家档案。入口使用 Dify 原生对话页面，也可调用正式应用 API。

## 三幕玩法

新对话中填写虚构玩家代号，例如 `Nova`，发送“开始”查看说明。玩家代号为3–24位英文字母、数字、下划线或短横线。

| 幕 | 示例输入 | 发生的变化 |
| --- | --- | --- |
| 登记失物 | `@a1 登记 周末 | 想把一个下午留给自己` | 登记失物，展示两种处理方式 |
| 作出选择 | `@a2 选A | 这次想独自休息` 或 `@a2 选B | 想分给朋友` | A选择个人找回，B选择分享改装，纪念物随之改变 |
| 领取回执 | `@a3 领取` | 输出结局、纪念物和保存来源，完成一份委托 |
| 下次来访 | 新对话用同一代号，发送 `@a4 再来` | 读回完成记录，开启下一份委托 |

每个新动作使用新的编号，例如 `a1`、`a2`；同一步因网络问题重试时，保留原编号和原指令。编号可包含英文字母、数字、下划线或短横线，最长32位。理由可省略，最长160字。委托结束后，下一份委托继续使用未用过的编号。

三种失物分别对应蓝色小行星、出发扣和等待开花的月亮。A与B有不同的固定纪念物，角色描写根据失物、选择和理由生成。模型生成的描写不会改变游戏规定的有效状态。

## 环境与版本

实测环境为 Linux x86_64、Ubuntu 24.04 / WSL2、Python 3.12.3。Dify 固定为 **1.18.0**，源码提交 `50abea97062110044f15d0e03ccecadd9ccc87ca`。11个所需服务的镜像 digest 见 [images.lock.json](deploy/images.lock.json)，启动脚本按 digest 拉取，未启用知识库、向量数据库、Agent 后端或协作服务。

- 配套适配器：PowerContext 1.4.1、FastAPI 0.115.14、Uvicorn 0.34.3；完整依赖见 `requirements.lock.txt`。
- 模型：MiniMax-M2.5，大陆官方地址 `https://api.minimax.cn/v1`。
- Dify 官方插件：`langgenius/openai_api_compatible` **0.0.73**，已附签名包、来源和散列，见 `vendor/plugin-lock.json`。包内依赖固定到具体版本，实际 Dify 验证签名分类为 `langgenius`。
- YAML DSL 版本：**0.7.0**，交付文件为 [星际失物招领局.yml](dsl/星际失物招领局.yml)，由运行中的 Dify 导出，并在另一新建应用中重新导入运行。

需要 Docker Engine、Docker Compose 2.24或以上、至少2核和4GiB内存。实际验收机器提供7.7GiB内存；首次下载包含约数GiB的容器镜像，建议预留15GiB磁盘。Docker 数据与绑定目录放在 Linux 文件系统中。

## 安装与启动

在 Linux 文件系统检出仓库，进入本目录。两个终端分别运行适配器与 Dify。以下命令不依赖仓库其他题目的目录。

```bash
bash scripts/setup.sh
.venv/bin/python scripts/deploy_dify.py
```

`setup.sh` 会建立本应用的 `.venv`，按锁文件安装依赖，并生成被 Git 忽略的 `.env.local`。只在该本地文件填写 `MINIMAX_API_KEY`，其他模型项使用模板中的大陆地址和模型。适配器 token 自动生成。已有 Dify 工作区可同时填写本地 `DIFY_ADMIN_EMAIL` / `DIFY_ADMIN_PASSWORD`；首次部署的新实例会创建本地演示管理员，信息只保存在被忽略的 `.runtime.local.json`。

启动 PowerContext 适配器：

```bash
.venv/bin/python scripts/run_adapter.py
```

保持这个进程运行，在另一终端导入交付 DSL、配置官方模型插件并发布本地应用：

```bash
.venv/bin/python scripts/provision.py
```

脚本会输出本地应用网址。默认控制台为 `http://localhost:8383`，适配器为4335端口，容器通过 `http://pc-host.internal:4335` 访问它。Compose 为该名称配置了明确的宿主网关，Dify 的受支持内网白名单只允许 `172.30.13.1/32`。首次部署会安装官方插件并验证模型凭据，然后绑定应用环境变量 `PC_BASE_URL` 与秘密变量 `PC_TOKEN`。公开 DSL 不包含 token 或模型密钥。

也可在 Dify 页面安装 `vendor/` 中的官方插件包，配置相同模型，导入 YAML，在工作流环境变量中绑定服务地址和本地 token，然后发布。无需知识库或额外预置记忆。

部署目录默认位于 `~/.local/share/stellar-lost-found/dify`，可用 `--runtime <Linux目录>` 指定。适配器数据目录由 `.env.local` 的 `E05_DATA_DIR` 决定，启动也支持 `--data <目录>`。同一数据目录只启动一个适配器进程。

## 验证样例

[验证样例](samples/验证样例.md) 提供两个玩家的输入顺序、预期结局、回访及重试方法。保存后的每条回复显示修订号和 Source ID，可在 PowerContext 记录与 Dify 节点 trace 中对应。

验收包含以下实际运行：

- Nova 选择A，获得“装着星期八的口袋票”；Orbit 对同一种失物选择B，获得“可分给朋友的半日星图”。
- 新 Dify conversation、适配器进程重启后，Nova 的回访输入引用了此前A选择及纪念物，Orbit 状态保持独立。
- 原生页面完成登记、选择、领取；1360px桌面和390px手机布局检查通过。
- 新源码目录、全新虚拟环境和空玩家状态，导入平台导出的 YAML，重新绑定服务后完成三幕。
- 无效选择、相同动作重试、同编号不同输入，以及受控模型服务失败和写入失败均有对应记录；写入失败时未返回保存成功，同编号重试只保存一次。

普通模型输出、节点状态与用量、PowerContext 读写、进程编号和浏览器截图见 `evidence/`。模型插件对自定义模型显示的价格字段为0，不能据此估算实际账户费用。描写是生成内容，每次体验文字可有差异；验证以状态、选择、纪念物和来源对应为准。

## 重置本题样例

先停止本题适配器，再运行：

```bash
.venv/bin/python scripts/reset_samples.py
.venv/bin/python scripts/run_adapter.py
```

脚本仅为本应用选择一个新的空数据目录，保留此前数据。在 Dify 开启新对话即可重新体验。它不会清理其他应用、Dify数据库或其他轮次的数据。

## 使用范围与来源

这是单机互动样例。玩家代号用于区分本机档案，未提供账号认证；请使用虚构代号。每名玩家最多500次已保存动作。正常游戏无需网络搜索或 embedding，模型调用需要已有 MiniMax 额度。

流程、玩法和适配器为本作品实现；Dify 和 PowerContext 沿用其开源许可，官方兼容插件包保持原样，许可见 `vendor/LICENSE-openai_api_compatible.txt`。部署配置基于 [Dify 1.18.0](https://github.com/langgenius/dify/tree/1.18.0/docker)，模型通过 [官方兼容插件](https://marketplace.dify.ai/plugin/langgenius/openai_api_compatible) 接入。暂无真人使用统计。
