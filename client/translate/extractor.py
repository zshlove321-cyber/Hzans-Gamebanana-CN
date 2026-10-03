"""一键汉化的文本提取与跳过规则。

设计约束（与 `skip_rules.json` 同源）：
  * 跳过 code/pre/kbd/samp/var/tt、script/style/noscript、输入控件与 SVG/Canvas 等整棵子树；
  * 跳过 `translate="no"`、`.notranslate`、类名命中代码/时间戳/用户名等的元素；
  * 整段匹配 URL/邮箱/路径/占位符，或形如标识符、函数调用、点分路径的代码术语，原样保留；
  * 内联元素（a/b/strong/i/span…）的文本并入同一翻译单元，块级元素各自成单元，避免拆句错序。

本模块只用标准库，便于在无 GUI 与无网络环境下自检。
浏览器端 `client/webpage/translate.js` 实现同一套规则，二者以 skip_rules.json 为准。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

RULES_PATH = Path(__file__).with_name("skip_rules.json")
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


@dataclass(frozen=True)
class Unit:
    """一个可翻译文本单元。node_ids 指向组成它的文本节点，便于原样还原。"""

    id: str
    text: str
    node_ids: tuple[int, ...]
    tag: str


@dataclass(frozen=True)
class SkipRules:
    skip_tags: frozenset[str]
    skip_attributes: tuple[str, ...]
    skip_attribute_values: frozenset[str]
    skip_class_substrings: tuple[str, ...]
    skip_role: frozenset[str]
    keep_patterns: tuple[re.Pattern[str], ...]
    code_patterns: tuple[re.Pattern[str], ...]
    inline_tags: frozenset[str]
    min_length: int
    max_unit_chars: int

    def skips_tag(self, tag: str) -> bool:
        return tag in self.skip_tags

    def skips_element(self, tag: str, attrs: dict[str, str]) -> bool:
        for name in self.skip_attributes:
            value = (attrs.get(name) or "").strip().lower()
            if value and value in self.skip_attribute_values:
                return True
            if name == "translate" and value == "no":
                return True
        classes = f"{attrs.get('class', '')} {attrs.get('id', '')}"
        lowered = classes.lower()
        if any(token in lowered for token in self.skip_class_substrings):
            return True
        role = (attrs.get("role") or "").strip().lower()
        if role and role in self.skip_role:
            return True
        if "contenteditable" in attrs:
            return True
        return False

    def is_kept_verbatim(self, text: str) -> bool:
        """整段命中占位符/URL/代码形态时保留原文。"""
        stripped = text.strip()
        if len(stripped) < self.min_length:
            return True
        if any(pattern.match(stripped) for pattern in self.keep_patterns):
            return True
        if any(pattern.match(stripped) for pattern in self.code_patterns):
            return True
        return False


def load_rules(path: Path | None = None) -> SkipRules:
    raw = json.loads((path or RULES_PATH).read_text(encoding="utf-8"))
    return SkipRules(
        skip_tags=frozenset(raw["skip_tags"]),
        skip_attributes=tuple(raw["skip_attributes"]),
        skip_attribute_values=frozenset(raw["skip_attribute_values"]),
        skip_class_substrings=tuple(raw["skip_class_substrings"]),
        skip_role=frozenset(raw["skip_role"]),
        keep_patterns=tuple(re.compile(p) for p in raw["keep_patterns"]),
        code_patterns=tuple(re.compile(p) for p in raw["code_like_heuristics"]),
        inline_tags=frozenset(raw["inline_tags"]),
        min_length=int(raw["min_length"]),
        max_unit_chars=int(raw["max_unit_chars"]),
    )


@dataclass
class _TextNode:
    node_id: int
    text: str


@dataclass
class _Element:
    tag: str
    attrs: dict[str, str]
    parent: "_Element | None"
    skipped: bool
    children: list[Any]  # _Element | _TextNode
    text_ids: list[int]


class _TreeBuilder(HTMLParser):
    def __init__(self, rules: SkipRules) -> None:
        super().__init__(convert_charrefs=True)
        self.rules = rules
        self.nodes: list[_TextNode] = []
        self.root = _Element("#document", {}, None, False, [], [])
        self._current = self.root
        self._skip_depth = 0

    # --- 解析回调 ---
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attribute_map = {name.lower(): (value or "") for name, value in attrs}
        inherited = self._skip_depth > 0
        own_skip = self.rules.skips_tag(tag) or self.rules.skips_element(tag, attribute_map)
        # br 作为软换行并入当前单元
        if tag == "br" and not inherited:
            self._append_text("\n")
            return
        element = _Element(tag, attribute_map, self._current, inherited or own_skip, [], [])
        self._current.children.append(element)
        if tag not in VOID_TAGS:
            self._current = element
            if element.skipped:
                self._skip_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br":
            self._append_text("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in VOID_TAGS:
            return
        node = self._current
        while node is not self.root and node.tag != tag:
            node = node.parent or self.root
        if node is self.root:
            return
        if node.skipped:
            self._skip_depth = max(0, self._skip_depth - 1)
        self._current = node.parent or self.root

    def handle_data(self, data: str) -> None:
        self._append_text(data)

    def _append_text(self, data: str) -> None:
        if self._skip_depth > 0 or not data.strip():
            return
        node_id = len(self.nodes)
        self.nodes.append(_TextNode(node_id, data))
        self._current.children.append(self.nodes[-1])
        self._current.text_ids.append(node_id)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _collect_text(element: _Element, rules: SkipRules, block_tags: frozenset[str]) -> tuple[str, list[int]]:
    """收集元素自身及其内联后代的可翻译文本；遇到块级后代即停（由块级层各自成单元）。

    被跳过规则命中的子树整体忽略，因此 code/pre 等内部文本不会混进句子。
    """
    parts: list[str] = []
    ids: list[int] = []

    def walk(node: _Element) -> None:
        for child in node.children:
            if isinstance(child, _TextNode):
                parts.append(child.text)
                ids.append(child.node_id)
                continue
            if child.skipped or child.tag in block_tags:
                continue
            parts.append(" ")
            walk(child)

    walk(element)
    return _collapse("".join(parts)), ids


def load_block_tags(path: Path | None = None) -> frozenset[str]:
    raw = json.loads((path or RULES_PATH).read_text(encoding="utf-8"))
    return frozenset(raw["block_tags"])


def extract_units(html: str, rules: SkipRules | None = None,
                  block_tags: Iterable[str] | None = None) -> list[Unit]:
    """从 HTML 提取可翻译单元；跳过规则命中的内容不会出现在结果里。"""
    rules = rules or load_rules()
    blocks = frozenset(t.lower() for t in block_tags) if block_tags is not None else load_block_tags()

    parser = _TreeBuilder(rules)
    parser.feed(html)
    parser.close()

    units: list[Unit] = []
    counter = 0

    def emit(text: str, ids: tuple[int, ...], tag: str) -> None:
        nonlocal counter
        if not text or rules.is_kept_verbatim(text):
            return
        units.append(Unit(f"u{counter}", text, ids, tag))
        counter += 1

    def visit(element: _Element) -> None:
        """每个块级元素产出一个单元（正文合并、内联并入）；容器继续下钻。"""
        if element.skipped:
            return
        if element.tag in blocks:
            text, ids = _collect_text(element, rules, blocks)
            emit(text, tuple(ids), element.tag)
        for child in element.children:
            if isinstance(child, _Element):
                visit(child)
    visit(parser.root)
    # 超长单元按句子边界切分，避免单次请求过大
    return _split_long_units(units, rules)


def _split_long_units(units: list[Unit], rules: SkipRules) -> list[Unit]:
    result: list[Unit] = []
    for unit in units:
        if len(unit.text) <= rules.max_unit_chars:
            result.append(unit)
            continue
        chunks = _chunk_text(unit.text, rules.max_unit_chars)
        for index, chunk in enumerate(chunks):
            result.append(Unit(f"{unit.id}.{index}", chunk, unit.node_ids, unit.tag))
    return result


def _chunk_text(text: str, limit: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?。！？；;])\s+", text)
    chunks: list[str] = []
    buffer = ""
    for sentence in sentences:
        candidate = f"{buffer} {sentence}".strip() if buffer else sentence
        if len(candidate) <= limit:
            buffer = candidate
            continue
        if buffer:
            chunks.append(buffer)
        while len(sentence) > limit:
            chunks.append(sentence[:limit])
            sentence = sentence[limit:]
        buffer = sentence
    if buffer:
        chunks.append(buffer)
    return chunks
