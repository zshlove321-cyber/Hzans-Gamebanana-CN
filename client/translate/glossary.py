"""术语表：把社区术语、缩写、俚语与必须保留的专有名词交给翻译器参考。

设计要点：
  * 术语表是**单一事实来源**（`glossary.json`），与跳过规则（`skip_rules.json`）分工不同——
    跳过规则决定「哪些内容不送翻译」，术语表决定「送翻译的内容怎么译」；
  * 术语以「命中的术语必须照此处理」的强制性说明注入提示词（最高优先级），
    避免模型对缩写/俚语自行发挥；
  * 只注入**文本中确实出现**的术语，避免提示词过长稀释注意力。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

GLOSSARY_PATH = Path(__file__).with_name("glossary.json")

# 术语表分组 → 提示词中的呈现顺序与说明
SECTION_TITLES = {
    "abbreviations": "缩写（保留英文缩写并附中文释义，已被中文社区通用的直接写中文）",
    "slang": "社区俚语（译成中文社区自然说法，不要保留英文）",
    "game_terms": "游戏与站点术语（按此译法）",
    "keep_original": "必须保留原文（不翻译）",
}

MAX_TERMS_PER_REQUEST = 60


@dataclass
class Glossary:
    """术语表。sections 保留分组，便于按类别渲染。"""

    sections: dict[str, dict[str, str]] = field(default_factory=dict)
    style: dict[str, str] = field(default_factory=dict)
    version: int = 1

    # --- 查询 ---
    def total_terms(self) -> int:
        return sum(len(items) for items in self.sections.values())

    def all_terms(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for items in self.sections.values():
            merged.update(items)
        return merged

    def match(self, texts: list[str], limit: int = MAX_TERMS_PER_REQUEST) -> dict[str, dict[str, str]]:
        """挑出这批文本里真正出现的术语，按分组返回。

        匹配规则：大小写不敏感的词边界匹配；含点号或空格的术语（如 `e.g.`、`git gud`）
        用转义后的字面匹配。多词术语优先于单词术语命中。
        """
        blob = "\n".join(texts)
        lowered = blob.lower()
        matched: dict[str, dict[str, str]] = {}
        for section, items in self.sections.items():
            hits: dict[str, str] = {}
            # 长术语优先，避免 "mod" 抢先命中 "mod loader" 这类情况
            for term, value in sorted(items.items(), key=lambda kv: -len(kv[0])):
                if not term:
                    continue
                if len(hits) >= limit:
                    break
                if _contains_term(lowered, term.lower()):
                    hits[term] = value
            if hits:
                matched[section] = hits
        return matched

    def render(self, texts: list[str], limit: int = MAX_TERMS_PER_REQUEST) -> str:
        """渲染成提示词片段；没有命中任何术语时返回空串。"""
        matched = self.match(texts, limit=limit)
        if not matched:
            return ""
        lines = ["# 术语表（最高优先级：下列术语必须按此处理，不得自行发挥）"]
        for section, title in SECTION_TITLES.items():
            hits = matched.get(section)
            if not hits:
                continue
            lines.append(f"\n## {title}")
            for term, value in hits.items():
                if term == value:
                    lines.append(f"- {term} → 保留原文")
                else:
                    lines.append(f"- {term} → {value}")
        if self.style:
            lines.append("\n## 风格约定")
            for key, value in self.style.items():
                lines.append(f"- {value}")
        return "\n".join(lines)


def _contains_term(haystack_lower: str, term_lower: str) -> bool:
    """词边界匹配，避免 "op" 命中 "option"、"cd" 命中 "cdrom"。"""
    if any(ch in term_lower for ch in ".+-/ "):
        return term_lower in haystack_lower
    pattern = rf"(?<![a-z0-9]){re.escape(term_lower)}(?![a-z0-9])"
    return re.search(pattern, haystack_lower) is not None


def load_glossary(path: Path | None = None) -> Glossary:
    """读取术语表；分组键以下划线开头的视为注释忽略。"""
    raw = json.loads((path or GLOSSARY_PATH).read_text(encoding="utf-8"))
    sections: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if key.startswith("_") or not isinstance(value, dict):
            continue
        if key == "style":
            continue
        sections[key] = {k: v for k, v in value.items()
                         if not k.startswith("_") and isinstance(v, str)}
    style = {k: v for k, v in (raw.get("style") or {}).items()
             if not k.startswith("_") and isinstance(v, str)}
    return Glossary(sections=sections, style=style, version=int(raw.get("version") or 1))
