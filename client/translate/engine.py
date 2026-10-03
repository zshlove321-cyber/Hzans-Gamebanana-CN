"""DeepSeek 一键汉化引擎。

职责与边界：
  * 只调用 DeepSeek 的 OpenAI 兼容 `/chat/completions` 接口，密钥来自设置或环境变量；
  * 输入是「文本单元列表」，输出是同长度译文列表（顺序一一对应）；
  * 批处理 + 并发上限 + 重试 + 磁盘缓存；缓存键为原文哈希，避免重复计费；
  * 传输层可注入（`transport` 参数），因此可以在没有任何密钥、不联网的情况下完成自检。

术语保护由 `client/translate/extractor.py` 的跳过规则在提取阶段完成，
本模块只负责「把送进来的文本译成中文」，并再次校验：译文若与原文完全相同则视为保留项。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import requests

from ..settings import Settings, default_config_dir
from .glossary import Glossary, load_glossary

Transport = Callable[[str, dict, dict, int], dict]

SYSTEM_PROMPT = (
    "你是游戏模组社区（GameBanana 类站点）的中文本地化译者，把用户给出的文本翻译成{target}。\n"
    "严格遵守以下约束：\n"
    "1. 只输出译文，不解释、不加注释、不改动条目数量与顺序。\n"
    "2. 输入是 JSON 数组，输出必须是**同长度**的 JSON 数组，第 i 项是第 i 项的译文。\n"
    "3. **源语言可能是任何语言**（英语、俄语、西班牙语、德语、法语、日语、韩语等）。"
    "只要是{target}以外的语言，就必须翻译成{target}；不要因为「看不懂就不动」。\n"
    "4. **默认要翻译**。只有满足以下条件之一才原样返回：\n"
    "   (a) 内容已经是{target}；(b) 内容只有符号/数字/单个字母；"
    "(c) 内容是纯代码标识符、文件名或 URL。\n"
    "   像 \"P.N.C.R.\" 这种缩写，应保留缩写并补中文说明，不要原样丢回。\n"
    "5. 以下内容必须保留原样、不翻译、不改写：代码标识符、函数名、文件名、路径、URL、"
    "邮箱、版本号（如 1.20.1）、占位符（如 {{name}}、%s、${var}）、"
    "以及术语表中标注为「保留原文」的条目。\n"
    "6. 游戏名、模组名、角色名、平台名、引擎名保留英文原名；"
    "首次出现时可在中文后用括号附注原名。\n"
    "7. **社区缩写与俚语要按中文游戏社区的习惯处理**：常用缩写保留英文并附中文释义"
    "（如「毕业装备（BIS）」「过于强力（OP）」）；已被中文社区通用的直接用中文"
    "（如 OP→过于强力、nerf→削弱、buff→增强）；俚语译成自然中文，不要保留英文原词，"
    "也不要逐字硬译。\n"
    "8. 若提供了术语表，**术语表的优先级高于你自己的判断**，命中的术语必须照表处理。\n"
    "9. 译文要像中文母语者写的：简洁、口语自然、符合游戏社区语气；"
    "不逐字硬译，不加多余标点，不要机翻腔。\n"
    "10. 语气词、粗口、攻击性用语如实译出，不美化、不加注、不省略。"
)


def build_system_prompt(target_language: str) -> str:
    """构造系统提示词。

    注意：模板里出现的占位符示例（`{{name}}`、`${var}`）本身就是花括号，
    直接对整段做 str.format 会抛 KeyError。因此这里只做显式替换，不用 format。
    """
    return SYSTEM_PROMPT.replace("{target}", target_language)


# 提示词版本：参与缓存键。修改提示词或术语表后递增，
# 旧译文会自动失效并重新翻译（否则用户改了术语表却看不到效果）。
PROMPT_VERSION = "3"


def cache_signature(target_language: str, model: str) -> str:
    """缓存签名：模型 + 目标语言 + 提示词版本 + 术语表版本。"""
    return f"{model}\u0000{target_language}\u0000p{PROMPT_VERSION}\u0000g{_GLOSSARY_VERSION}"


_GLOSSARY_VERSION = "1"


def set_glossary_version(version: int | str) -> None:
    """由术语表加载方调用，把术语表版本并入缓存签名。"""
    global _GLOSSARY_VERSION
    _GLOSSARY_VERSION = str(version)


@dataclass
class TranslationStats:
    requested: int = 0
    translated: int = 0
    cached: int = 0
    kept_verbatim: int = 0
    failed: int = 0
    batches: int = 0
    deduplicated: int = 0
    retried: int = 0
    recovered: int = 0

    def summary(self) -> str:
        extra = f"、去重 {self.deduplicated}" if self.deduplicated else ""
        retry = f"、强化重试 {self.retried}（成功 {self.recovered}）" if self.retried else ""
        return (f"共 {self.requested} 条：新译 {self.translated}、缓存命中 {self.cached}、"
                f"保留原文 {self.kept_verbatim}、失败 {self.failed}{extra}{retry}"
                f"（{self.batches} 批）")


class TranslationError(RuntimeError):
    """翻译链路错误，消息面向用户可见（中文）。"""


class DiskCache:
    """原文哈希 → 译文的持久缓存，避免重复调用产生费用。"""

    def __init__(self, path: Path, flush_interval: float = 2.0) -> None:
        self.path = path
        self.flush_interval = flush_interval
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        self._dirty = False
        # 初始化为当前时间，避免首次 put 立刻写盘（见 DiskCache 中的说明）
        self._last_flush = time.time()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self._data = {str(k): str(v) for k, v in raw.items()}

    @staticmethod
    def key(text: str, language: str, model: str) -> str:
        return hashlib.sha256(f"{model}\u0000{language}\u0000{text}".encode("utf-8")).hexdigest()

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._data[key] = value
            self._dirty = True

    def flush(self, force: bool = False) -> None:
        """合并写盘：批量翻译时避免每条译文都重写整个缓存文件。

        实测 5000 条译文（468 KB）时单次写盘约 3 ms；批量翻译会连续命中，
        因此按 flush_interval 合并（默认 2 秒），并在批次结束时强制落盘。
        """
        with self._lock:
            if not self._dirty:
                return
            now = time.time()
            if not force and (now - self._last_flush) < self.flush_interval:
                return
            payload = json.dumps(self._data, ensure_ascii=False, indent=0) + "\n"
            self._dirty = False
            self._last_flush = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8", newline="\n")
            tmp.replace(self.path)
        except OSError:
            with self._lock:
                self._dirty = True

    def close(self) -> None:
        self.flush(force=True)

    def _mark_dirty(self) -> None:
        """仅供基准测试：把缓存标记为待落盘。"""
        with self._lock:
            self._dirty = True


def http_transport(base_url: str, payload: dict, headers: dict, timeout: int) -> dict:
    """默认传输层：真实调用 DeepSeek。"""
    url = base_url.rstrip("/") + "/chat/completions"
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    if response.status_code >= 400:
        detail = response.text[:300].replace("\n", " ")
        raise TranslationError(f"DeepSeek 返回 HTTP {response.status_code}：{detail}")
    return response.json()


class DeepSeekTranslator:
    def __init__(self, settings: Settings, transport: Transport | None = None,
                 cache: DiskCache | None = None, glossary: Glossary | None = None) -> None:
        self.settings = settings
        cache_dir = Path(settings.config_dir or default_config_dir())
        self.cache = cache or DiskCache(cache_dir / "translation-cache.json")
        self._transport = transport or http_transport
        self.last_stats = TranslationStats()
        self._last_error = ""
        try:
            self.glossary = glossary if glossary is not None else load_glossary()
        except (OSError, ValueError):
            # 术语表缺失或损坏不应阻断翻译
            self.glossary = Glossary()
        # 术语表版本并入缓存签名：改术语表后旧译文自动失效
        set_glossary_version(self.glossary.version)

    # --- 对外接口 ---
    def translate_many(self, texts: Sequence[str]) -> list[str]:
        """批量翻译；返回与输入等长的列表。未配置密钥时抛 TranslationError。"""
        if not texts:
            return []
        if not self.settings.resolved_api_key():
            raise TranslationError("未配置 DeepSeek API 密钥：请在「设置」里填写后重试。")

        results: list[str | None] = [None] * len(texts)
        pending: list[tuple[int, str]] = []
        stats = TranslationStats(requested=len(texts))

        for index, text in enumerate(texts):
            if not text.strip():
                results[index] = text
                stats.kept_verbatim += 1
                continue
            cached = self.cache.get(self._cache_key(text))
            if cached is not None:
                results[index] = cached
                stats.cached += 1
                continue
            pending.append((index, text))

        self._translate_pending(pending, results, stats)
        self._retry_untranslated(results, stats)
        self.cache.flush(force=True)  # 一批翻译结束后确保落盘
        self.last_stats = stats
        return [original if value is None else value for original, value in zip(texts, results)]

    def _retry_untranslated(self, results: list[str | None], stats: "TranslationStats") -> None:
        """对「原样返回」且确实需要翻译的条目做一次强化重试。

        模型偶尔会因为「看起来像专有名词」而把整条丢回原文。这里筛出真正需要翻译的
        （含拉丁/西里尔等字母、且不含中文），用一句更明确的指令单独重试一次；
        仍失败就保留原文，并在统计中计入，便于用户判断是否需要人工处理。
        """
        retry_items: list[tuple[int, str]] = []
        for index, value in enumerate(results):
            if value is None or not value.strip():
                continue
            if not _needs_translation(value):
                continue
            retry_items.append((index, value))
        if not retry_items:
            return

        stats.retried = len(retry_items)
        batch_size = max(1, self.settings.translate_batch_size)
        recovered = 0
        for start in range(0, len(retry_items), batch_size):
            chunk = retry_items[start:start + batch_size]
            try:
                pairs = self._translate_batch_forced(chunk)
            except TranslationError:
                continue
            for index, original, translated in pairs:
                if translated.strip() and translated.strip() != original.strip():
                    results[index] = translated
                    recovered += 1
                    stats.translated += 1
                    stats.kept_verbatim = max(0, stats.kept_verbatim - 1)
                    self.cache.put(self._cache_key(original), translated)
        stats.recovered = recovered

    def _cache_key(self, text: str) -> str:
        """缓存键包含提示词与术语表版本：改动后旧译文自动失效并重译。"""
        signature = cache_signature(self.settings.translate_target_language,
                                    self.settings.deepseek_model)
        return self.cache.key(text, signature, "")

    def _translate_batch_forced(self, batch: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
        """强化重试：显式指出哪些条目「必须给出中文」，不提供术语表以外的退路。"""
        sources = [text for _, text in batch]
        glossary_block = self.glossary.render(sources)
        instruction = (
            "以下条目尚未翻译，必须给出{target}译文。\n"
            "即使它们是专有名词、缩写或看起来像品牌名，也要给出{target}处理结果："
            "专有名词可保留原名但需补中文说明。\n"
            "只输出 JSON 数组，长度与输入一致。"
        ).replace("{target}", self.settings.translate_target_language)
        user_content = json.dumps(sources, ensure_ascii=False)
        if glossary_block:
            user_content = f"{glossary_block}\n\n{instruction}\n\n{user_content}"
        else:
            user_content = f"{instruction}\n\n{user_content}"

        payload = {
            "model": self.settings.deepseek_model,
            "messages": [
                {"role": "system",
                 "content": build_system_prompt(self.settings.translate_target_language)},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.resolved_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        last_error = ""
        for attempt in range(self.settings.translate_max_retries + 1):
            try:
                data = self._transport(self.settings.deepseek_base_url, payload, headers,
                                       self.settings.translate_timeout_seconds)
                translated = self._parse_response(data, len(sources))
                return [(batch[i][0], batch[i][1], translated[i]) for i in range(len(batch))]
            except TranslationError as exc:
                last_error = str(exc)
            except (KeyError, ValueError, IndexError) as exc:
                last_error = f"响应解析失败：{type(exc).__name__}: {exc}"
            if attempt < self.settings.translate_max_retries:
                continue
        # 保留真实原因，便于排查（不要用笼统消息覆盖）
        raise TranslationError(f"强化重试失败：{last_error}" if last_error else "强化重试失败")

    # --- 内部实现 ---
    def _translate_pending(self, pending: list[tuple[int, str]], results: list[str | None],
                           stats: TranslationStats) -> None:
        # 同一页里重复文本（如多个条目共用标签）只发一次请求，避免重复计费
        unique: dict[str, list[int]] = {}
        for index, text in pending:
            unique.setdefault(text, []).append(index)
        unique_items = list(unique.items())

        batch_size = self.settings.translate_batch_size
        # 每批仍是 (占位索引, 原文) 结构，_translate_batch 依赖该形状
        batches = [[(0, text) for text, _ in unique_items[i:i + batch_size]]
                   for i in range(0, len(unique_items), batch_size)]
        stats.batches = len(batches)
        concurrency = max(1, min(self.settings.translate_max_concurrency, len(batches) or 1))
        stats.deduplicated = len(pending) - len(unique_items)

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(self._translate_batch, batch) for batch in batches]
            for future in futures:
                try:
                    pairs = future.result()
                except TranslationError as exc:
                    stats.failed += batch_size
                    self._last_error = str(exc)
                    continue
                except Exception as exc:  # noqa: BLE001 - 兜底，避免线程异常静默
                    stats.failed += batch_size
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    continue
                for _index, original, translated in pairs:
                    # 同一原文的所有出现位置共享同一译文
                    for position in unique.get(original, []):
                        results[position] = translated
                    if translated == original:
                        stats.kept_verbatim += 1
                    else:
                        stats.translated += 1
                        self.cache.put(self._cache_key(original), translated)

    def _translate_batch(self, batch: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
        sources = [text for _, text in batch]
        # 只注入这批文本里真正出现的术语，避免提示词过长稀释注意力
        glossary_block = self.glossary.render(sources)
        user_content = json.dumps(sources, ensure_ascii=False)
        if glossary_block:
            user_content = f"{glossary_block}\n\n# 待翻译文本（JSON 数组）\n{user_content}"

        payload = {
            "model": self.settings.deepseek_model,
            "messages": [
                {"role": "system",
                 "content": build_system_prompt(self.settings.translate_target_language)},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.resolved_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        last_error = ""
        for attempt in range(self.settings.translate_max_retries + 1):
            try:
                data = self._transport(self.settings.deepseek_base_url, payload, headers,
                                       self.settings.translate_timeout_seconds)
                translated = self._parse_response(data, len(sources))
                return [(batch[i][0], batch[i][1], translated[i]) for i in range(len(batch))]
            except TranslationError as exc:
                last_error = str(exc)
            except (KeyError, ValueError, IndexError) as exc:
                last_error = f"响应解析失败：{type(exc).__name__}: {exc}"
            if attempt < self.settings.translate_max_retries:
                continue
        raise TranslationError(last_error or "翻译失败")

    @staticmethod
    def _parse_response(data: dict, expected: int) -> list[str]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise TranslationError("DeepSeek 响应缺少 choices 字段")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise TranslationError("DeepSeek 响应内容为空")
        parsed = _extract_json_array(content)
        if not isinstance(parsed, list):
            raise TranslationError("DeepSeek 响应不是 JSON 数组")
        items = [str(item) for item in parsed]
        if len(items) != expected:
            raise TranslationError(f"译文条数与原文不一致（期望 {expected}，实际 {len(items)}）")
        return items


def _extract_json_array(content: str) -> list | None:
    """容错解析：模型可能返回 {"translations": [...]} 或带 Markdown 代码块。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    try:
        value = json.loads(text)
    except ValueError:
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            return None
        try:
            value = json.loads(text[start:end + 1])
        except ValueError:
            return None
    if isinstance(value, dict):
        for candidate in ("translations", "result", "results", "data", "items"):
            if isinstance(value.get(candidate), list):
                return value[candidate]
        return None
    return value if isinstance(value, list) else None


def iter_translatable(items: Iterable[str]) -> list[str]:
    """过滤空白项，便于调用方先清洗再翻译。"""
    return [item for item in items if item and item.strip()]


_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LETTERS = re.compile(r"[A-Za-z\u0400-\u04ff\u00c0-\u024f\u0370-\u03ff\u3040-\u30ff\uac00-\ud7af]")


def _needs_translation(text: str) -> bool:
    """判断「原样返回」的文本是否其实需要翻译。

    需要翻译 = 含字母（拉丁/西里尔/希腊/假名/谚文等）且不含中日韩统一表意文字。
    纯符号、纯数字、已是中文的内容不重试，避免无意义的二次计费。
    """
    stripped = text.strip()
    if not stripped or _CJK.search(stripped):
        return False
    if not _LETTERS.search(stripped):
        return False
    # 单个字母或纯缩写点号形式（如 "A."）无翻译价值
    letters = _LETTERS.findall(stripped)
    return len(letters) >= 2
