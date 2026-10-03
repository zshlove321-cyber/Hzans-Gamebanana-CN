"""QWebChannel 翻译桥：网页脚本 ↔ Python 翻译引擎。

只依赖 QtCore，因此即使没有安装 PySide6-Addons（QtWebEngine）也能单独测试。
翻译与密钥全部留在 Python 侧，页面脚本只负责提取文本与回填译文。
"""
from __future__ import annotations

import json

from PySide6.QtCore import QObject, Signal, Slot

from ..translate import DeepSeekTranslator, TranslationError


class TranslationBridge(QObject):
    """暴露给网页脚本的翻译通道。"""

    translations_ready = Signal(str, str)  # request_id, payload_json
    failed = Signal(str)

    def __init__(self, translator: DeepSeekTranslator, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.translator = translator
        self._busy = False

    @Slot(str, str)
    def requestTranslations(self, request_id: str, items_json: str) -> None:
        """参数是 [{id, text}] 的 JSON 字符串；结果通过 translations_ready 回传。"""
        if self._busy:
            self._reply(request_id, {"error": "已有翻译任务在进行中，请稍后重试"})
            return
        try:
            items = json.loads(items_json)
        except ValueError as exc:
            self._reply(request_id, {"error": f"请求解析失败：{exc}"})
            return
        if not isinstance(items, list):
            self._reply(request_id, {"error": "请求格式错误：需要数组"})
            return
        if not items:
            self._reply(request_id, {"translations": []})
            return

        self._busy = True
        try:
            texts = [str(item.get("text", "")) for item in items if isinstance(item, dict)]
            if len(texts) != len(items):
                self._reply(request_id, {"error": "请求格式错误：条目必须是对象"})
                return
            translated = self.translator.translate_many(texts)
            self._reply(request_id, {
                "translations": [
                    {"id": str(item.get("id", index)), "text": value}
                    for index, (item, value) in enumerate(zip(items, translated))
                ],
                "stats": self.translator.last_stats.summary(),
            })
        except TranslationError as exc:
            self._reply(request_id, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 兜底上报给页面，避免页面永久等待
            self._reply(request_id, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            self._busy = False

    def _reply(self, request_id: str, payload: dict) -> None:
        self.translations_ready.emit(request_id, json.dumps(payload, ensure_ascii=False))
