#!/usr/bin/env python3
"""组会接力静态展示站构建脚本（仅 Python 标准库，可离线运行）。

用法:
    python3 site/build.py --source-sha <40位十六进制提交> [--site-version v1.0.0] [--facts facts.json]

行为:
    - 所有仓库链接锁定到 --source-sha 指定的提交，不使用浮动 main。
    - 未提供 --facts 时，验收区显示“验收记录见固定版本材料”，不产生任何检查结果。
    - 提供 --facts 时，只展示其中已核验的检查条数、结论与截图清单；
      未通过的检查按未通过展示，绝不改写为通过。
    - site/assets/ 中存在的真实截图才会被引用；缺失时显示
      “截图随发布版本补齐”占位，不生成假截图。
    - 输出 dist/index.html、style.css、site.js 及 assets/，无外部运行依赖。
"""

import argparse
import html
import json
import re
import shutil
import sys
from pathlib import Path

SITE_DIR = Path(__file__).resolve().parent
SRC_DIR = SITE_DIR / "src"
ASSETS_DIR = SITE_DIR / "assets"
DIST_DIR = SITE_DIR / "dist"

REPO_BASE = "https://github.com/Sunny1004-7/oceanbase-2026-powercontext"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,39}$")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}

SCREENSHOT_SLOTS = [
    {
        "key": "desktop-overview",
        "title": "桌面 · 项目总览",
        "alt": "组会接力桌面界面：项目总览与「从这里继续」接续卡",
        "mobile": False,
    },
    {
        "key": "report",
        "title": "桌面 · 周报 / 交接单",
        "alt": "组会接力桌面界面：带来源追溯的周报生成页",
        "mobile": False,
    },
    {
        "key": "mobile",
        "title": "移动端界面",
        "alt": "组会接力在窄屏手机浏览器中的任务与复盘界面",
        "mobile": True,
    },
]


def esc(value):
    return html.escape(str(value), quote=True)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="构建组会接力静态展示站")
    parser.add_argument(
        "--source-sha",
        required=True,
        help="40 位十六进制的源码提交 SHA，用于锁定全部仓库链接",
    )
    parser.add_argument(
        "--site-version",
        default="v0.1.0-draft",
        help="站点版本号（默认 v0.1.0-draft）",
    )
    parser.add_argument(
        "--facts",
        type=Path,
        default=None,
        help="已核验事实 JSON 路径；缺省时验收区显示待补齐提示",
    )
    args = parser.parse_args(argv)

    if not SHA_RE.match(args.source_sha):
        parser.error("--source-sha 必须是 40 位小写十六进制提交 SHA")
    if not VERSION_RE.match(args.site_version):
        parser.error("--site-version 只能包含字母、数字与 . _ + -，且不超过 40 字符")
    if args.facts is not None and not args.facts.is_file():
        parser.error(f"--facts 文件不存在: {args.facts}")
    return args


def load_facts(path):
    """读取并校验 facts。任何结构问题都直接报错，不做静默兜底。"""
    if path is None:
        return None
    try:
        facts = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        sys.exit(f"facts 不是合法的 UTF-8 JSON: {exc}")
    if not isinstance(facts, dict):
        sys.exit("facts 顶层必须是 JSON 对象")

    checks = facts.get("checks")
    if not isinstance(checks, dict):
        sys.exit('facts 缺少 "checks" 对象')
    for field in ("verified_total", "passed"):
        value = checks.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            sys.exit(f'facts.checks.{field} 必须是非负整数')
    if checks["passed"] > checks["verified_total"]:
        sys.exit("facts.checks.passed 不能大于 verified_total")

    conclusion = facts.get("conclusion")
    if not isinstance(conclusion, str) or not conclusion.strip():
        sys.exit('facts.conclusion 必须是非空字符串（如实描述验收结论）')

    details = checks.get("details", [])
    if not isinstance(details, list):
        sys.exit("facts.checks.details 必须是数组")
    for item in details:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) \
                or not isinstance(item.get("passed"), bool):
            sys.exit('facts.checks.details 每项需要 {"name": 字符串, "passed": 布尔}')
    if len(details) > checks["verified_total"]:
        sys.exit("facts.checks.details 条数不能超过 verified_total")

    screenshots = facts.get("screenshots", [])
    if not isinstance(screenshots, list):
        sys.exit("facts.screenshots 必须是数组")
    known_keys = {slot["key"] for slot in SCREENSHOT_SLOTS}
    for item in screenshots:
        if not isinstance(item, dict) or item.get("key") not in known_keys:
            sys.exit(f'facts.screenshots.key 必须是 {sorted(known_keys)} 之一')
        filename = item.get("file", "")
        if not isinstance(filename, str) or Path(filename).name != filename \
                or Path(filename).suffix.lower() not in IMAGE_SUFFIXES:
            sys.exit("facts.screenshots.file 必须是 assets 目录内的图片文件名")
        if "caption" in item and not isinstance(item["caption"], str):
            sys.exit("facts.screenshots.caption 必须是字符串")

    return facts


def repo_links(sha):
    return {
        "LINK_CODE": f"{REPO_BASE}/tree/{sha}/qoder",
        "LINK_README": f"{REPO_BASE}/blob/{sha}/qoder/README.md",
        "LINK_SAMPLES": f"{REPO_BASE}/blob/{sha}/qoder/samples/验证样例.md",
        "LINK_EVIDENCE": f"{REPO_BASE}/blob/{sha}/qoder/evidence/验收摘要.json",
        "LINK_DEVELOPMENT": f"{REPO_BASE}/blob/{sha}/qoder/development/README.md",
        "LINK_COMMIT": f"{REPO_BASE}/commit/{sha}",
    }


PH_ICON = (
    '<svg class="ph-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
    '<rect x="3" y="5" width="18" height="14" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.6"/>'
    '<circle cx="9" cy="10" r="1.6" fill="currentColor"/>'
    '<path d="M5 17l4.5-4.5 3 3L16 12l3 3.5" fill="none" stroke="currentColor" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def render_screenshots(facts, copied_assets):
    listed = {}
    if facts is not None:
        for item in facts.get("screenshots", []):
            listed[item["key"]] = item

    figures = []
    for slot in SCREENSHOT_SLOTS:
        entry = listed.get(slot["key"])
        caption = slot["title"]
        frame_cls = "shot-frame shot-mobile" if slot["mobile"] else "shot-frame"
        if entry is not None and entry["file"] in copied_assets:
            alt = entry.get("alt") or slot["alt"]
            if entry.get("caption"):
                caption = entry["caption"]
            body = (
                f'<div class="{frame_cls}">'
                f'<a href="assets/{esc(entry["file"])}" target="_blank" rel="noopener noreferrer" aria-label="{esc(caption)}原图">'
                f'<img src="assets/{esc(entry["file"])}" alt="{esc(alt)}" loading="lazy">'
                f'</a>'
                f"</div>"
            )
        else:
            note = "截图随发布版本补齐"
            if entry is not None:
                note = "截图已登记，文件随发布版本补齐"
            body = (
                f'<div class="shot-placeholder" role="img" '
                f'aria-label="{esc(slot["title"])}：{esc(note)}，暂无真实截图">'
                f"{PH_ICON}<strong>{esc(slot["title"])}</strong><span>{esc(note)}</span></div>"
            )
        figures.append(f"<figure>{body}<figcaption>{esc(caption)}</figcaption></figure>")
    return '<div class="shot-grid">' + "".join(figures) + "</div>"


def render_acceptance(facts):
    if facts is None:
        return (
            '<div class="acceptance-box acceptance-pending">'
            "<h3>验收记录见固定版本材料</h3>"
            "<p>本站尚未注入本次发布的核验结果，因此不在此展示任何检查数字或结论。"
            "独立测试与发布核验由 Codex 执行，最终记录以固定版本仓库中的 "
            "<code>evidence/验收摘要.json</code> 为准。</p>"
            '<p class="acceptance-footnote">自动化验收是在固定虚构样例上的工具实验，'
            "不构成真人使用统计或提效百分比。</p>"
            "</div>"
        )

    checks = facts["checks"]
    total = checks["verified_total"]
    passed = checks["passed"]
    all_passed = passed == total and total > 0
    verdict_cls = "verdict-pass" if all_passed else "verdict-fail"
    verdict_text = "已核验检查全部通过" if all_passed else "存在未通过项，如实展示"

    parts = [
        '<div class="acceptance-box">',
        "<h3>本次发布的已核验结果</h3>",
        '<dl class="acceptance-stats">',
        f"<div><dt>已核验检查</dt><dd>{total} 条</dd></div>",
        f'<div><dt>通过</dt><dd class="verdict-pass">{passed} 条</dd></div>',
        f'<div><dt>未通过</dt><dd class="{"verdict-pass" if passed == total else "verdict-fail"}">{total - passed} 条</dd></div>',
        f'<div><dt>结论</dt><dd class="{verdict_cls}">{esc(verdict_text)}</dd></div>',
        "</dl>",
        f"<p>{esc(facts['conclusion'])}</p>",
    ]
    details = checks.get("details", [])
    if details:
        items = []
        for item in details:
            cls = "ck-ok" if item["passed"] else "ck-bad"
            state = "通过" if item["passed"] else "未通过"
            line = f'{esc(item["name"])}（{state}'
            if not item["passed"] and isinstance(item.get("note"), str) and item["note"].strip():
                line += f'：{esc(item["note"].strip())}'
            line += "）"
            items.append(f'<li class="{cls}">{line}</li>')
        parts.append('<ul class="check-detail-list">' + "".join(items) + "</ul>")
    parts.append(
        '<p class="acceptance-footnote">以上数字来自发布时注入的核验事实文件，'
        "对应固定虚构样例上的自动化验收，不代表真人使用统计。完整记录见仓库 "
        "<code>evidence/验收摘要.json</code>。</p>"
    )
    parts.append("</div>")
    return "".join(parts)


def collect_assets(facts):
    """返回 (要复制的文件名集合)。有 facts 时只复制清单内文件，否则复制全部图片。"""
    if not ASSETS_DIR.is_dir():
        return set()
    available = {
        p.name for p in ASSETS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    }
    if facts is None:
        return available
    listed = {item["file"] for item in facts.get("screenshots", [])}
    missing = listed - available
    if missing:
        print(f"提示: facts 登记的截图暂缺，将显示占位: {', '.join(sorted(missing))}")
    return listed & available


def build(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    facts = load_facts(args.facts)

    template = (SRC_DIR / "index.html").read_text(encoding="utf-8")
    copied_assets = collect_assets(facts)

    replacements = repo_links(args.source_sha)
    replacements.update(
        {
            "SOURCE_SHA": args.source_sha,
            "SITE_VERSION": esc(args.site_version),
            "SCREENSHOTS": render_screenshots(facts, copied_assets),
            "ACCEPTANCE": render_acceptance(facts),
        }
    )
    page = template
    for key, value in replacements.items():
        page = page.replace("{{" + key + "}}", value)
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", page)
    if leftover:
        sys.exit(f"模板存在未替换占位符: {sorted(set(leftover))}")

    if DIST_DIR.resolve().parent != SITE_DIR.resolve() or DIST_DIR.name != "dist":
        sys.exit("构建输出目录越界，拒绝清理")
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    DIST_DIR.mkdir(parents=True)
    (DIST_DIR / "index.html").write_text(page, encoding="utf-8")
    shutil.copy2(SRC_DIR / "style.css", DIST_DIR / "style.css")
    shutil.copy2(SRC_DIR / "site.js", DIST_DIR / "site.js")
    if copied_assets:
        (DIST_DIR / "assets").mkdir()
        for name in sorted(copied_assets):
            shutil.copy2(ASSETS_DIR / name, DIST_DIR / "assets" / name)

    mode = "facts 已注入" if facts is not None else "无 facts（验收待补齐）"
    print(f"构建完成: {DIST_DIR}")
    print(f"  模式: {mode}")
    print(f"  站点版本: {args.site_version}")
    print(f"  source SHA: {args.source_sha}")
    print(f"  截图: {len(copied_assets)} 张真实图片, {len(SCREENSHOT_SLOTS) - len(copied_assets)} 个占位")
    return 0


if __name__ == "__main__":
    sys.exit(build())
