# 组会接力 · 公开展示站

「组会接力」项目的静态展示入口，面向 Qoder Sites 发布。原生 HTML/CSS/少量 JS，构建脚本只用 Python 标准库，可完全离线构建，产物无外部运行依赖（无 CDN、无字体外链、无 npm）。

本站是**静态展示站**：不连接本地 PowerContext，不提供在线体验；应用在用户本机运行。

## 目录

```
site/
├── src/            # 页面源码
│   ├── index.html  # 模板，含 {{...}} 占位符，由 build.py 注入
│   ├── style.css
│   └── site.js     # 仅做滚动浮现动效，尊重 prefers-reduced-motion
├── assets/         # 真实应用截图（发布时由 Codex 加入已验收真图）
├── build.py        # 构建脚本
├── dist/           # 构建产物（被项目 .gitignore 忽略）
└── README.md
```

## 构建

```bash
# 草稿构建：无核验事实，验收区显示“验收记录见固定版本材料”
python3 site/build.py --source-sha <40位十六进制提交SHA>

# 发布构建：注入站点版本与已核验事实
python3 site/build.py \
  --source-sha <40位十六进制提交SHA> \
  --site-version v1.0.0 \
  --facts /path/to/facts.json
```

参数说明：

- `--source-sha`（必填）：40 位小写十六进制提交 SHA。所有仓库链接（README、样例、验收摘要、开发记录、代码入口、页脚提交链接）都锁定到该提交，不使用浮动 main。
- `--site-version`：站点版本号，默认 `v0.1.0-draft`，显示在页脚。
- `--facts`：已核验事实 JSON。**不提供时不产生任何检查数字或结论**，只显示待补齐提示。

产物写入 `site/dist/`：`index.html`、`style.css`、`site.js`，以及存在真实截图时的 `assets/`。每次构建会清空重建 dist。

## facts JSON 格式

facts 由发布核验流程（Codex）产出，只应包含已核验的内容。结构不合法时构建直接报错，不会静默兜底。

```json
{
  "conclusion": "如实描述本次验收结论的一句话或一段话",
  "checks": {
    "verified_total": 9,
    "passed": 9,
    "details": [
      { "name": "检查名称", "passed": true },
      { "name": "另一项检查", "passed": false, "note": "未通过原因（可选）" }
    ]
  },
  "screenshots": [
    { "key": "desktop-overview", "file": "desktop-overview.png", "caption": "可选图注", "alt": "可选替代文本" },
    { "key": "report", "file": "report.png" },
    { "key": "mobile", "file": "mobile.png" }
  ]
}
```

规则：

- `checks.verified_total` / `checks.passed`：非负整数，`passed ≤ verified_total`。未通过的检查按未通过展示，页面不会把失败写成通过；只有全部通过时结论才显示“全部通过”。
- `checks.details`：可选，条数不超过 `verified_total`。
- `screenshots[].key`：只能是 `desktop-overview`（桌面总览）、`report`（报告页）、`mobile`（移动界面）。
- `screenshots[].file`：必须是 `site/assets/` 内的图片文件名（png/jpg/jpeg/webp/gif/avif），不允许路径分隔符。登记了但文件暂缺时显示“截图已登记，文件随发布版本补齐”占位。
- 提供 facts 时只复制清单内的图片；未提供 facts 时复制 assets 下全部图片并按槽位文件名匹配。

## 截图约定

无 facts 时，三个槽位按以下文件名在 `site/assets/` 中查找：

| 槽位 | 文件名 |
| --- | --- |
| 桌面总览 | `desktop-overview.png` |
| 报告页 | `report.png` |
| 移动界面 | `mobile.png` |

assets 缺失时页面显示明确的“截图随发布版本补齐”占位，**不生成假截图**。首屏便签与时间线均标注“手绘示意 · 非产品截图”。

## 内容红线

- 不声称“已全部通过”，除非 facts 如实注入且全部通过。
- 暂无真人使用统计；自动化验收是固定虚构样例上的工具实验，不当作用户数或提效百分比。
- 分工如实表述：主要业务、界面与 PowerContext 接入由 Qoder CN CLI 开发；Codex 负责规格、环境、独立测试、发布核验与材料整理。
- 不输出 API key、UID 猜测或认证链接；不提供 localhost 公网体验链接。