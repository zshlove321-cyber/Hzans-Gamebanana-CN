"""程序入口：装配设置、客户端、翻译引擎与主窗口。

用法：python -m client.app   或   run.bat / run.ps1
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

from . import __version__

# 由 initialize() 在创建 QApplication 之前填充；不在导入时做副作用，
# 否则 import client.app 就会初始化 Chromium，导致环境变量/safe 模式来不及生效。
WEBENGINE_AVAILABLE = False
SAFE_MODE = False


def _is_writable(path: pathlib.Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _prepare_webengine_env() -> bool:
    """准备 QtWebEngine 数据目录；返回是否可用。

    实测结论：即使设置了 `--user-data-dir`，QtWebEngine 仍会在
    `%APPDATA%\\<org>\\QtWebEngine\\<app>` 下创建目录，该路径不可写时 Chromium
    会直接 FATAL 崩溃（进程被终止，Python 侧无法捕获）。因此在受限环境里
    必须提前判断并放弃启用 WebEngine，而不是等它崩。
    """
    appdata = os.environ.get("APPDATA")
    if appdata and not _is_writable(pathlib.Path(appdata) / "banana-index" / "chromium"):
        return False

    candidates = []
    if appdata:
        candidates.append(pathlib.Path(appdata) / "banana-index" / "chromium")
    candidates.append(pathlib.Path.cwd() / ".banana-index" / "chromium")
    candidates.append(pathlib.Path(tempfile.gettempdir()) / "banana-index" / "chromium")

    for base in candidates:
        if not _is_writable(base):
            continue
        if not os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS"):
            flags = f"--user-data-dir={base} --disk-cache-dir={base / 'cache'}"
            if SAFE_MODE:
                # 安全模式：软件渲染 + 关闭 GPU 合成，规避显卡驱动导致的渲染崩溃
                flags += " --disable-gpu --disable-gpu-compositing --disable-software-rasterizer"
                os.environ.setdefault("QT_OPENGL", "software")
            os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = flags
        return True
    return False


def initialize(safe_mode: bool | None = None) -> bool:
    """在任何 Qt 导入之前完成 WebEngine 初始化决策。

    必须在创建 QApplication 之前调用，否则 Chromium 会先以默认（可能不可用的）
    数据目录初始化并直接崩溃。本模块因此不在导入时产生副作用，由入口显式调用。
    """
    global WEBENGINE_AVAILABLE, SAFE_MODE
    from .ui.capabilities import is_webengine_available

    if safe_mode is None:
        safe_mode = ("--safe-mode" in sys.argv
                     or os.environ.get("BANANA_INDEX_SAFE_MODE") == "1")
    SAFE_MODE = safe_mode

    available = False
    if not safe_mode and is_webengine_available():
        try:
            from PySide6 import QtCore
            from PySide6 import QtWebChannel  # noqa: F401
            from PySide6 import QtWebEngineWidgets  # noqa: F401
            QtCore.QCoreApplication.setAttribute(
                QtCore.Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
            available = _prepare_webengine_env()
        except Exception:  # noqa: BLE001 - 缺少 WebEngine 不是致命错误
            available = False
    elif safe_mode:
        # 安全模式仍设置软件渲染相关变量：即使之后被显式启用 WebEngine，
        # 也走软件路径，避开显卡驱动导致的渲染崩溃
        os.environ.setdefault("QT_OPENGL", "software")
        os.environ.setdefault("QT_QUICK_BACKEND", "software")
    WEBENGINE_AVAILABLE = available
    return available


# 以下 Qt 与内部模块的导入放在 initialize() 定义之后，保持「先决策、后导入」的顺序
from PySide6.QtWidgets import QApplication  # noqa: E402

from .api import GameBananaClient  # noqa: E402
from .common import DiskCache, SessionStore  # noqa: E402
from .settings import Settings, default_config_dir, ensure_config_dir, load_settings  # noqa: E402
from .translate import DeepSeekTranslator  # noqa: E402


def build_client(settings: Settings) -> GameBananaClient:
    """装配接口客户端：缓存、会话 Cookie、超时与请求间隔都来自设置。"""
    config_dir = pathlib.Path(settings.config_dir or default_config_dir())
    ensure_config_dir(config_dir)
    cache = DiskCache(config_dir / "api-cache.json", ttl_seconds=900)
    store = SessionStore(config_dir / "session-cookies.json")
    client = GameBananaClient(timeout=settings.request_timeout_seconds,
                             request_delay_ms=settings.request_delay_ms,
                             cache=cache, session_store=store)
    injected = client.apply_session_cookies()
    if injected:
        print(f"[banana-index] 已注入 {injected} 条站点 Cookie（登录态复用）")
    return client


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    import argparse

    parser = argparse.ArgumentParser(prog="banana-index", description="GameBanana 中文索引客户端")
    parser.add_argument("--safe-mode", action="store_true",
                        help="关闭内嵌浏览器与 GPU 加速（排查渲染崩溃时使用）")
    parser.add_argument("--no-webengine", action="store_true",
                        help="仅关闭内嵌浏览器，其余保持不变（用于二分定位崩溃来源）")
    args, remaining = parser.parse_known_args(argv[1:])

    from .ui.environment import integrity_level
    level = integrity_level()
    if level is not None and level < 8192:
        print("[banana-index] 当前为低完整性进程，Windows 会限制写入和外部跳转。"
              "这可能由沙箱或启动文件标签造成，并不代表用户提权。"
              "请关闭此窗口，从资源管理器双击项目 run.bat；启动脚本会修复运行文件标签。")

    initialize(safe_mode=args.safe_mode)
    if args.no_webengine:
        # 不能用 global 声明后再在 main 内赋值，否则同名变量会被当作局部变量；
        # 这里通过模块属性赋值，语义清晰也不会遮蔽全局。
        globals()["WEBENGINE_AVAILABLE"] = False
        print("[banana-index] --no-webengine：已禁用内嵌浏览器（索引界面仍可用）")

    app = QApplication([argv[0], *remaining])
    app.setApplicationName("banana-index")
    app.setApplicationDisplayName("GameBanana 中文索引")
    app.setApplicationVersion(__version__)

    settings = load_settings()
    if settings.load_error:
        print(f"[banana-index] 警告：设置文件读取失败（{settings.load_error}）"
              f"，已备份到 {settings.load_error_backup}；将使用默认值启动。")
    # 崩溃日志：Python 未处理异常 + 原生崩溃（白屏闪退类）都写入 <配置目录>/logs/
    from . import crashlog
    from .ui.dialogs import show_error

    def on_error(message: str, saved: pathlib.Path) -> None:
        show_error("程序发生错误", f"{message}\n\n详情已写入：{saved}")

    log_file = crashlog.enable_native_crash_log(settings.config_dir)
    crashlog.install(settings.config_dir, on_error=on_error)
    print(f"[banana-index] 错误日志：{log_file}")

    client = build_client(settings)
    translator = DeepSeekTranslator(settings)

    from .ui.main_window import MainWindow
    from .ui import web_view

    factory = None
    if WEBENGINE_AVAILABLE:
        factory = lambda s: web_view.create_web_widget(s, client.session_store)  # noqa: E731
    else:
        print("[banana-index] 未检测到 QtWebEngine：以纯索引模式启动（无内嵌网页与整页汉化）")

    window = MainWindow(settings, client, translator, web_widget_factory=factory)
    window.show()
    from PySide6.QtCore import QTimer
    from .ui.about_dialog import show_first_launch_notice
    QTimer.singleShot(0, window, lambda: show_first_launch_notice(window))
    if SAFE_MODE:
        window.statusBar().showMessage(
            "安全模式：已关闭内嵌浏览器与整页汉化（索引界面与字段汉化可用）。")
        print("[banana-index] 安全模式：内嵌浏览器已禁用")
    elif not settings.is_translation_ready():
        window.statusBar().showMessage(
            "提示：尚未配置 DeepSeek API 密钥，一键汉化不可用；请在「设置」里填写。")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
