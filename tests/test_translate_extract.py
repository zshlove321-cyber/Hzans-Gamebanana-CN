"""翻译提取自检：离线、无网络、无 GUI。

验收内容：
  1. 提取出的单元文本集合与 `SKIP_MARKERS` 的期望完全一致；
  2. 任何 SKIP_MARKER_* 都不出现在提取结果里（代码/输入框/SVG/时间戳等被正确跳过）；
  3. URL、邮箱、占位符、形如标识符与函数调用的代码术语原样保留（未被当作翻译单元）。

运行：python tests/test_translate_extract.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from client.translate.extractor import extract_units, load_rules  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/translate_sample.html"

SKIP_MARKERS = [
    "SKIP_MARKER_SCRIPT",
    "SKIP_MARKER_PRE",
    "SKIP_MARKER_CLASS",
    "SKIP_MARKER_ATTR",
    "SKIP_MARKER_TIME",
    "SKIP_MARKER_TEXTAREA",
    "SKIP_MARKER_SVG",
]

EXPECTED_UNITS = [
    "Download Fast Furnaces",
    "Fast Furnaces",
    "This mod makes furnaces twice as fast and keeps the vanilla look.",
    "Install guide: run then restart the game.",
    "Search results",
    "Search mods",
    "Requires Minecraft 1.20 or newer",
    "Works with Forge",
    "Great mod, works perfectly on my server.",
    "Powered by GameBanana",
]

KEPT_VERBATIM = [
    "https://gamebanana.com/mods/680479",
    "author@example.com",
    "{{ placeholder_name }}",
    "fast_furnaces_79df6.zip",
    "translatePage()",
]


def main() -> int:
    rules = load_rules()
    html = FIXTURE.read_text(encoding="utf-8")
    units = extract_units(html, rules)
    texts = [unit.text for unit in units]

    failures: list[str] = []

    for marker in SKIP_MARKERS:
        if any(marker in text for text in texts):
            failures.append(f"跳过规则失效：{marker} 出现在翻译单元中")
        if marker in html and marker not in SKIP_MARKERS:  # pragma: no cover - 配置错误保护
            failures.append(f"fixture 标记未登记：{marker}")

    for expected in EXPECTED_UNITS:
        if expected not in texts:
            failures.append(f"缺少期望单元：{expected!r}")

    for kept in KEPT_VERBATIM:
        if kept in texts:
            failures.append(f"应原样保留却进入翻译单元：{kept!r}")

    unexpected = [t for t in texts if t not in EXPECTED_UNITS]
    for text in unexpected:
        failures.append(f"出现未预期单元：{text!r}")

    # 代码术语与占位符的判定必须稳定
    for kept in KEPT_VERBATIM:
        if not rules.is_kept_verbatim(kept):
            failures.append(f"is_kept_verbatim 未拦截：{kept!r}")
    for translatable in ["Battle", "Fast Furnaces", "Install guide"]:
        if rules.is_kept_verbatim(translatable):
            failures.append(f"普通文本被误判为代码术语：{translatable!r}")

    print(f"提取单元数：{len(texts)}")
    for text in texts:
        print(f"  - {text}")
    print(f"跳过标记：{len(SKIP_MARKERS)} 项全部未进入翻译单元" if not failures else "")

    if failures:
        print("\n自检未通过：", file=sys.stderr)
        for item in failures:
            print(f"  * {item}", file=sys.stderr)
        return 1
    print("\n自检通过：提取、跳过与保留规则全部符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())
