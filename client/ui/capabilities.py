"""运行能力探测：不导入任何 QtWebEngine 模块，避免在缺少依赖时触发导入错误。

主窗口只依赖本模块判断能力，真正需要 QtWebEngine 的 `web_view.py` 由有能力时再导入。
"""
from __future__ import annotations

import importlib.util

# PySide6-Addons 提供的模块；缺任意一项都无法实现内嵌浏览器 + QWebChannel 翻译通道
ADDON_MODULES = (
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebChannel",
)


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def is_webengine_available() -> bool:
    """True 表示可以创建内嵌浏览器标签页与整页汉化通道。"""
    return all(_has_module(name) for name in ADDON_MODULES)


def missing_webengine_modules() -> list[str]:
    """列出缺失的模块，便于在界面上给出可操作的提示。"""
    return [name for name in ADDON_MODULES if not _has_module(name)]
