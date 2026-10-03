"""内嵌网页浏览 + 整页一键汉化。

登录态实现（UI.txt「套用网页登录凭据状态」）：
  QWebEngineProfile 使用固定持久化目录，站点 Cookie 由 Chromium 自身落盘，
  用户在软件内登录一次即可长期复用；同时支持把外部导出的站点 Cookie 导入。

一键汉化实现（参考 kiss-translator 的机制，见 docs/research/kiss-translator-notes.md）：
  注入脚本在页面内按跳过规则提取文本单元 → 通过 QWebChannel 交给 Python →
  Python 侧复用 DeepSeek 引擎（并发/批处理/缓存）→ 结果回填 DOM，可一键还原。
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
from pathlib import Path
from typing import Any

import requests

from PySide6.QtCore import QObject, Qt, QUrl, QTimer, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..settings import Settings, describe_download_state, unique_path
from ..translate import DeepSeekTranslator
from .capabilities import is_webengine_available
from .translate_bridge import TranslationBridge

WEBPAGE_DIR = pathlib.Path(__file__).resolve().parents[1] / "webpage"
TRANSLATE_SCRIPT = WEBPAGE_DIR / "translate.js"
SKIP_RULES = pathlib.Path(__file__).resolve().parents[1] / "translate" / "skip_rules.json"


def is_webengine_available_here() -> bool:
    """本模块可用即说明 QtWebEngine 与 QWebChannel 都已安装。"""
    return is_webengine_available()


def should_open_internally(url: str) -> bool:
    """新窗口请求是否应留在内嵌浏览器内。

    站点上的下载按钮等链接带 `target="_blank"`。若交给外部处理，QtWebEngine 会
    把请求转给系统默认浏览器（表现为「一点下载就跳出 Edge」），在管理员权限下
    还会触发「现有实例正在以提升的权限运行」的系统弹窗。

    规则：http(s) 一律留在内嵌浏览器；其余协议（mailto:、外部客户端等）
    交给系统处理，避免内嵌浏览器无法打开而卡住。
    """
    lowered = (url or "").strip().lower()
    if not lowered:
        return False
    return lowered.startswith(("http://", "https://", "about:"))


_DOWNLOAD_SUFFIXES = (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
                      ".exe", ".msi", ".apk", ".jar", ".pak", ".vpk", ".bsp",
                      ".pk3", ".wad", ".dll", ".iso", ".nsp", ".xci")


def is_download_url(url: str) -> bool:
    """判断链接是否指向文件下载。

    站点下载按钮形如 `https://gamebanana.com/dl/1834746`，会 302 到
    `filecacheNN.gamebanana.com/mods/xxx.rar`；该响应只有 Content-Type，
    **没有 Content-Disposition: attachment**，交给 Chromium 判定并不可靠
    （实测表现为下载没有开始、还可能把请求甩给系统浏览器）。因此这里自行识别，
    由客户端直接下载到设置目录。
    """
    lowered = (url or "").strip().lower().split("?")[0].split("#")[0]
    if not lowered.startswith(("http://", "https://")):
        return False
    path = lowered.split("://", 1)[-1]
    if path.endswith(_DOWNLOAD_SUFFIXES):
        return True
    # 站点下载入口：/dl/<id>
    marker = lowered.split("://", 1)[-1]
    return "/dl/" in marker


class _InternalPage(QWebEnginePage):
    """让新窗口/下载留在软件内，不外泄到系统浏览器。

    `download_handler` 由 WebViewWidget 注入：命中下载链接时由它接管，
    不再依赖 Chromium 的下载判定。
    """

    def __init__(self, profile, parent=None) -> None:
        super().__init__(profile, parent)
        self.download_handler = None

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):  # noqa: N802
        """主导航命中下载链接时交给客户端自己的下载通道。

        只拦主框架导航：子资源（图片/脚本）不应触发下载逻辑。
        """
        if is_main_frame and self.download_handler is not None:
            target = url.toString()
            if is_download_url(target):
                self.download_handler(target)
                return False  # 不导航，避免把请求甩给系统浏览器
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    def createWindow(self, window_type):  # noqa: N802 - Qt 命名
        """新窗口请求：留在内嵌浏览器内，避免外泄到系统浏览器。

        QtWebEngine 默认会把 `target="_blank"` 交给系统默认浏览器，导致
        「点下载就跳出 Edge」。这里统一返回当前 page，让请求在原视图内完成。
        """
        return self

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        # 保持安静：站点脚本报错不应污染客户端日志
        return


class WebViewWidget(QWidget):
    """网页浏览标签页：地址栏 + 一键汉化 / 还原 + 进度条 + 内嵌视图。"""

    download_status = Signal(str)
    download_progress_changed = Signal(object, object, str)
    download_state_changed = Signal(str, str)

    def __init__(self, settings: Settings, parent: QWidget | None = None, session_store=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.session_store = session_store
        self.translator = DeepSeekTranslator(settings)
        self._active_download = None
        self._last_download_url = ""
        self._download_state = ""
        self._cancel_pending_download = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        bar = QHBoxLayout()
        bar.setContentsMargins(6, 6, 6, 0)
        self.back_button = QPushButton("←")
        self.back_button.setMaximumWidth(38)
        self.forward_button = QPushButton("→")
        self.forward_button.setMaximumWidth(38)
        self.reload_button = QPushButton("刷新")
        self.url_edit = QLineEdit(settings.web_home_url)
        self.url_edit.returnPressed.connect(self._navigate_from_bar)
        self.translate_button = QPushButton("一键汉化本页")
        self.translate_button.clicked.connect(self.translate_page)
        self.restore_button = QPushButton("还原原文")
        self.restore_button.clicked.connect(self.restore_original)
        self.sync_button = QPushButton("同步登录态")
        self.sync_button.setToolTip("把已保存的站点 Cookie 注入内嵌浏览器（用于复用外部登录）")
        self.sync_button.clicked.connect(self.sync_login_cookies)

        bar.addWidget(self.back_button)
        bar.addWidget(self.forward_button)
        bar.addWidget(self.reload_button)
        bar.addWidget(self.url_edit, 1)
        bar.addWidget(self.translate_button)
        bar.addWidget(self.restore_button)
        bar.addWidget(self.sync_button)
        layout.addLayout(bar)

        self.status_label = QLabel("提示：先在此登录 GameBanana，登录状态会被保存并复用。")
        self.status_label.setContentsMargins(8, 0, 8, 0)
        self.status_label.setStyleSheet("color: #9aa0a6; font-size: 12px;")
        layout.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setMaximumHeight(4)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # 持久化 Profile：Cookie / 缓存写到用户配置目录，等价于浏览器「记住登录」。
        # 路径必须在任何页面创建之前设置：QtWebEngine 会在首次使用时按 Profile 名
        # 在默认目录下建存储目录，若默认目录不可写会直接 FATAL 崩溃。
        storage = pathlib.Path(settings.session_profile_dir or
                              (pathlib.Path(settings.config_dir or ".") / "webprofile"))
        storage.mkdir(parents=True, exist_ok=True)
        self.profile = QWebEngineProfile("banana-index", self)
        self.profile.setPersistentStoragePath(str(storage))
        self.profile.setCachePath(str(storage / "cache"))
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        self.profile.setHttpUserAgent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
        cookie_store = self.profile.cookieStore()
        cookie_store.cookieAdded.connect(self._site_cookie_added)
        cookie_store.cookieRemoved.connect(self._site_cookie_removed)
        # Restore Chromium's persisted cookies into the API's local session store.
        QTimer.singleShot(0, cookie_store.loadAllCookies)

        self.view = QWebEngineView(self)
        self.page = _InternalPage(self.profile, self.view)
        self.page.download_handler = self._download_by_url
        self.view.setPage(self.page)
        settings_obj = self.view.settings()
        settings_obj.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings_obj.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        settings_obj.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True)
        layout.addWidget(self.view, 1)

        # 翻译通道
        self.channel = QWebChannel(self.page)
        self.bridge = TranslationBridge(self.translator, self)
        self.channel.registerObject("bananaBridge", self.bridge)
        self.page.setWebChannel(self.channel)
        self.bridge.translations_ready.connect(self._deliver_translations)

        self._install_scripts()
        self._wire_signals()
        self._prepare_download_dir()
        self.view.load(QUrl(settings.web_home_url))

    # ---------------------------------------------------------------- 下载
    def _prepare_download_dir(self) -> None:
        """把内嵌浏览器的默认下载位置指向用户设置。"""
        directory = self.settings.resolved_download_dir()
        self.profile.setDownloadPath(str(directory))

    def download_url(self, url: str) -> None:
        """供索引详情直接下载，无需加载站点页或切换浏览标签。"""
        self._download_by_url(url)

    def cancel_download(self) -> None:
        request = self._active_download
        if request is not None and self._download_state == "DownloadInProgress":
            request.cancel()
        elif self._download_state == "DownloadRequested":
            self._cancel_pending_download = True
            self._download_state = "DownloadCancelling"
            self.status_label.setText("正在取消下载请求…")
            self.download_status.emit("正在取消下载请求…")
            self.download_state_changed.emit("DownloadCancelling", "")

    def retry_download(self) -> None:
        if self._last_download_url and self._download_state in {"DownloadCancelled", "DownloadInterrupted"}:
            previous = self._active_download
            self._active_download = None
            if previous is not None:
                previous.cancel()
            self.download_url(self._last_download_url)

    def _download_by_url(self, url: str) -> None:
        """显式发起浏览器下载，复用登录 Cookie、保存位置与进度通道。

        page.download 不依赖 attachment 响应头，也不导航离开当前页面。
        原来的 requests 通道丢失浏览器 Cookie，还引用了不存在的 tasks，
        并从工作线程更新界面；统一由 downloadRequested 接管。
        """
        if getattr(self, "_download_state", "") in {"DownloadRequested", "DownloadInProgress", "DownloadCancelling"}:
            message = "已有下载正在进行，请等待完成或先取消当前下载。"
            self.status_label.setText(message)
            if hasattr(self, "download_status"):
                self.download_status.emit(message)
            return
        message = f"正在请求下载：{url}"
        self._last_download_url = url
        self._cancel_pending_download = False
        self._download_state = "DownloadRequested"
        if hasattr(self, "download_state_changed"):
            self.download_state_changed.emit("DownloadRequested", url)
        self.status_label.setText(message)
        if hasattr(self, "download_status"):
            self.download_status.emit(message)
        self.page.download(QUrl(url))

    def _download_stream(self, url: str, directory, report) -> str:
        """流式下载到临时文件后改名，避免半成品留在目录里。

        校验要点（实测教训）：无效的下载入口可能返回 200 + 一整个 HTML 页面
        （如 `/dl/0`），若直接落盘会得到名为 `0` 的垃圾文件。因此这里拒绝
        HTML 响应，并要求最终文件名带扩展名。
        """
        import tempfile
        from pathlib import Path as _Path

        import requests

        session = self._session()
        with session.get(url, stream=True, timeout=60, allow_redirects=True) as response:
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/html" in content_type:
                raise ValueError(
                    f"该地址返回的是网页而不是文件（{response.url}），可能不是有效的下载链接")

            # 文件名优先取响应头，其次取重定向后的地址，最后退回原地址
            name = ""
            disposition = response.headers.get("Content-Disposition") or ""
            if "filename=" in disposition:
                name = disposition.split("filename=", 1)[1].strip().strip('"; ')
            for candidate in (name, _Path(response.url.split("?")[0]).name,
                              _Path(url.split("?")[0]).name):
                if candidate and "." in candidate:
                    name = candidate
                    break
            else:
                raise ValueError(
                    f"无法确定文件名（{response.url}），可能不是有效的下载链接")

            target = unique_path(directory, name)
            total = int(response.headers.get("Content-Length") or 0)
            done_bytes = 0
            handle = tempfile.NamedTemporaryFile(
                delete=False, dir=str(directory), suffix=".part")
            temp_path = _Path(handle.name)
            try:
                with handle:
                    for chunk in response.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        done_bytes += len(chunk)
                        report(done_bytes, total, target.name)
                if done_bytes == 0:
                    raise ValueError("下载内容为空")
                os.replace(temp_path, target)
            except BaseException:
                try:
                    temp_path.unlink()
                except OSError:
                    pass
                raise
        return str(target)

    def _session(self):
        """每线程独立的 requests 会话（requests.Session 非线程安全）。"""
        local = getattr(self, "_download_local", None)
        if local is None:
            local = threading.local()
            self._download_local = local
        session = getattr(local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers["User-Agent"] = self.profile.httpUserAgent()
            local.session = session
        return session

    def _on_download_requested(self, request) -> None:
        """处理下载请求：按设置决定直接保存还是询问位置，并显示进度。"""
        def status(text: str) -> None:
            self.status_label.setText(text)
            if hasattr(self, "download_status"):
                self.download_status.emit(text)

        def state_changed(state: str, name: str = "") -> None:
            self._download_state = state
            if hasattr(self, "download_state_changed"):
                self.download_state_changed.emit(state, name)

        if getattr(self, "_download_state", "") == "DownloadInProgress":
            request.cancel()
            status("已有下载正在进行，请等待完成或先取消当前下载。")
            return

        if getattr(self, "_cancel_pending_download", False):
            request.cancel()
            self._cancel_pending_download = False
            state_changed("DownloadCancelled")
            return
        if hasattr(request, "url"):
            self._last_download_url = request.url().toString()

        # 无效 /dl/ 入口可能返回 200 + HTML 错误页，不能当成模组文件落盘。
        if hasattr(request, "mimeType") and "text/html" in request.mimeType().lower():
            request.cancel()
            status("下载失败：该地址返回网页而不是文件，请检查链接或登录状态。")
            state_changed("DownloadInterrupted")
            return
        directory = self.settings.resolved_download_dir()
        try:
            request.setDownloadDirectory(str(directory))
        except Exception as exc:  # noqa: BLE001 - 某些构建缺少该接口
            status(f"下载目录设置失败：{exc}")

        chosen = None
        if self.settings.download_ask_each_time:
            chosen = QFileDialog.getSaveFileName(
                self, "选择保存位置", str(directory / Path(request.downloadFileName()).name))[0]
            if not chosen:
                request.cancel()
                status("已取消下载")
                state_changed("DownloadCancelled")
                return
            target = Path(chosen)
            try:
                request.setDownloadDirectory(str(target.parent))
                request.setDownloadFileName(target.name)
            except Exception as exc:  # noqa: BLE001
                status(f"保存位置设置失败：{exc}")

        target = unique_path(Path(request.downloadDirectory()), request.downloadFileName())
        request.setDownloadFileName(target.name)
        try:
            request.accept()
        except Exception as exc:  # noqa: BLE001 - 例如目标不可写
            status(f"下载被拒绝：{exc}")
            state_changed("DownloadInterrupted")
            return

        name = Path(request.downloadFileName()).name
        if chosen:
            status(f"开始下载：{Path(chosen).name}")
        else:
            status(f"开始下载：{name} → {directory}")

        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._active_download = request
        state_changed("DownloadInProgress", name)

        def on_bytes_changed() -> None:
            if self._active_download is not request or self._download_state != "DownloadInProgress":
                return
            # 信号无参数，必须重新读取当前值
            received = request.receivedBytes()
            total = request.totalBytes()
            if hasattr(self, "download_progress_changed"):
                self.download_progress_changed.emit(received, total, name)
            if total > 0:
                self.progress.setValue(int(received * 100 / total))
                status(
                    f"下载中：{name} {_human_size(received)} / {_human_size(total)}")
            else:
                status(f"下载中：{name}（{_human_size(received)}）")

        def on_state_changed(state) -> None:
            if self._active_download is not request:
                return
            state_name = getattr(state, "name", str(state))
            state_changed(state_name, name)
            finished, text = describe_download_state(
                state_name, name, request.downloadDirectory(), request.interruptReasonString())
            if not finished:
                status(text)
                return
            self.progress.setVisible(False)
            self.progress.setRange(0, 100)
            status(text)
            if state_name == "DownloadCompleted" and self.settings.download_open_folder_after:
                self.open_download_folder()

        request.receivedBytesChanged.connect(on_bytes_changed)
        request.totalBytesChanged.connect(on_bytes_changed)
        request.stateChanged.connect(on_state_changed)

    def open_download_folder(self) -> None:
        """在系统文件管理器中打开下载目录。失败只提示，不抛异常。"""
        directory = self.settings.resolved_download_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            if os.name == "nt":
                os.startfile(str(directory))  # noqa: S606 - Windows 打开目录
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))
        except OSError as exc:
            # 受限环境（例如进程被沙箱或策略限制）下这是可预期的失败，
            # 用状态栏与提示框说明，而不是让异常冒泡成崩溃对话框。
            self.status_label.setText(f"无法打开目录：{directory}（{exc.strerror or exc}）")
            QMessageBox.information(
                self, "无法打开下载目录",
                f"{directory}\n\n{type(exc).__name__}: {exc.strerror or exc}\n\n"
                "可手动打开该路径，或在「设置」里改到有写入权限的目录。")

    # ---------------------------------------------------------------- 初始化
    def _install_scripts(self) -> None:
        rules_json = SKIP_RULES.read_text(encoding="utf-8")
        script_source = TRANSLATE_SCRIPT.read_text(encoding="utf-8")
        bootstrap = (
            "(function () {\n"
            "  const ready = function () {\n"
            "    if (!window.qt || !window.qt.webChannelTransport) { return setTimeout(ready, 60); }\n"
            "    new QWebChannel(qt.webChannelTransport, function (channel) {\n"
            "      window.__bananaBridge = channel.objects.bananaBridge;\n"
            f"      window.__bananaIndexSkipRules = {json.dumps(rules_json, ensure_ascii=False)};\n"
            "      if (window.__bananaIndexTranslate) {\n"
            "        window.__bananaIndexTranslate.setRules(window.__bananaIndexSkipRules);\n"
            "      }\n"
            "    });\n"
            "  };\n"
            "  ready();\n"
            f"  {script_source}\n"
            "  const apply = function () {\n"
            "    if (window.__bananaIndexTranslate && window.__bananaIndexSkipRules) {\n"
            "      window.__bananaIndexTranslate.setRules(window.__bananaIndexSkipRules);\n"
            "    }\n"
            "  };\n"
            "  document.addEventListener('DOMContentLoaded', apply);\n"
            "  apply();\n"
            "})();\n"
        )
        script = QWebEngineScript()
        script.setName("banana-index-translate")
        script.setSourceCode(bootstrap)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        self.profile.scripts().insert(script)

    def _wire_signals(self) -> None:
        self.back_button.clicked.connect(self.view.back)
        self.forward_button.clicked.connect(self.view.forward)
        self.reload_button.clicked.connect(self.view.reload)
        self.view.urlChanged.connect(lambda url: self.url_edit.setText(url.toString()))
        self.view.loadStarted.connect(lambda: self.progress.setVisible(True))
        self.view.loadFinished.connect(self._on_load_finished)
        self.view.loadProgress.connect(self.progress.setValue)
        # 站点内的下载链接（游戏模组文件）走这里，保存到用户设置的目录
        self.profile.downloadRequested.connect(self._on_download_requested)

    def _deliver_translations(self, request_id: str, payload_json: str) -> None:
        """把 Python 侧译文回传给页面脚本（QWebChannel 只支持返回值，故用全局回调）。"""
        script = (f"window.__bananaIndexOnTranslations && "
                  f"window.__bananaIndexOnTranslations({json.dumps(request_id)}, "
                  f"{json.dumps(payload_json)})")
        self.view.page().runJavaScript(script)
        try:
            payload = json.loads(payload_json)
        except ValueError:
            payload = {}
        if payload.get("error"):
            self.status_label.setText(f"汉化失败：{payload['error']}")
        elif payload.get("stats"):
            self.status_label.setText(f"本页翻译：{payload['stats']}")

    def _on_load_finished(self, ok: bool) -> None:
        self.progress.setVisible(False)
        if ok:
            self.status_label.setText(f"已加载：{self.view.url().toString()}")
        else:
            self.status_label.setText("页面加载失败（可能是网络或站点限制）")

    # ---------------------------------------------------------------- 交互
    def _navigate_from_bar(self) -> None:
        text = self.url_edit.text().strip()
        if not text:
            return
        if not text.startswith(("http://", "https://")):
            text = "https://" + text
        self.load_url(text)

    def load_url(self, url: str) -> None:
        self._mark("内嵌网页跳转", url=url)
        self.view.load(QUrl(url))

    def _mark(self, action: str, **extra: Any) -> None:
        """记录活动，供崩溃后的异常退出检测器判断死在哪个环节。"""
        try:
            from ..watchdog import write_marker
            write_marker(pathlib.Path(self.settings.config_dir or "."), action=action, **extra)
        except Exception:  # noqa: BLE001 - 埋点不能影响功能
            pass

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.translator.settings.translate_batch_size = settings.translate_batch_size
        self.translator.settings.translate_max_concurrency = settings.translate_max_concurrency
        self.translator.settings.translate_timeout_seconds = settings.translate_timeout_seconds
        self.translator.settings.translate_max_retries = settings.translate_max_retries
        self.translator.settings.deepseek_api_key = settings.deepseek_api_key
        self.translator.settings.deepseek_base_url = settings.deepseek_base_url
        self.translator.settings.deepseek_model = settings.deepseek_model
        self.translator.settings.translate_target_language = settings.translate_target_language
        # 下载目录改为新设置后立即生效，无需重启
        self._prepare_download_dir()
        self.status_label.setText(
            f"设置已更新：下载保存到 {settings.resolved_download_dir()}")

    def translate_page(self) -> None:
        if not self.settings.is_translation_ready():
            QMessageBox.information(self, "需要配置密钥",
                                    "请先在「设置」里填写 DeepSeek API 密钥，然后再使用一键汉化。")
            return
        self._mark("汉化整页", url=self.view.url().toString())
        self.status_label.setText("正在汉化本页…")
        self.view.page().runJavaScript(
            "window.__bananaIndexTranslate ? window.__bananaIndexTranslate.translate() : "
            "{status:'error',message:'注入脚本未就绪'}",
            self._handle_translate_result)

    def _handle_translate_result(self, result: Any) -> None:
        if not isinstance(result, dict):
            self.status_label.setText("汉化脚本未返回结果")
            return
        status = result.get("status")
        if status == "ok":
            self.status_label.setText(
                f"汉化完成：本页 {result.get('units')} 个文本单元，"
                f"新译 {result.get('translated')} 个，复用缓存 {result.get('cached')} 个。"
                f"（{self.translator.last_stats.summary()}）")
        elif status == "error":
            self.status_label.setText(f"汉化失败：{result.get('message')}")
        elif status == "busy":
            self.status_label.setText("已有汉化任务进行中…")
        else:
            self.status_label.setText(f"汉化状态：{status}")

    def restore_original(self) -> None:
        self.view.page().runJavaScript(
            "window.__bananaIndexTranslate ? window.__bananaIndexTranslate.restore() : "
            "{status:'error',message:'注入脚本未就绪'}",
            lambda result: self.status_label.setText(
                "已还原本页原文" if isinstance(result, dict) and result.get("status") == "restored"
                else "还原失败"))

    def set_translations(self, _translations: dict[str, str]) -> None:
        """索引界面翻译结果与网页共享同一 DeepSeek 缓存，无需重复推送。"""

    def sync_login_cookies(self) -> None:
        """把已保存的站点 Cookie 注入内嵌浏览器，便于复用外部登录态。"""
        store = getattr(self, "session_store", None)
        cookies = store.load() if store is not None else {}
        if not cookies:
            QMessageBox.information(self, "暂无已保存的登录态",
                                    "请先在本窗口登录 GameBanana；登录后 Cookie 会自动保存。\n"
                                    "也可以在设置里导入外部导出的站点 Cookie。")
            return
        from PySide6.QtNetwork import QNetworkCookie
        store_target = self.profile.cookieStore()
        count = 0
        for name, value in cookies.items():
            cookie = QNetworkCookie(name.encode("utf-8"), value.encode("utf-8"))
            cookie.setDomain(".gamebanana.com")
            cookie.setPath("/")
            store_target.setCookie(cookie)
            count += 1
        self.status_label.setText(f"已注入 {count} 条站点 Cookie，请刷新页面")

    def _persist_site_cookie(self, cookie, removed=False) -> None:
        domain = cookie.domain().lower().lstrip('.')
        if domain != 'gamebanana.com' and not domain.endswith('.gamebanana.com'):
            return
        store = self.session_store
        if store is None:
            return
        name = bytes(cookie.name()).decode('utf-8', errors='replace')
        if not name:
            return
        cookies = store.load()
        if removed:
            cookies.pop(name, None)
        else:
            value = bytes(cookie.value()).decode('utf-8', errors='replace')
            if value:
                cookies[name] = value
            else:
                cookies.pop(name, None)
        try:
            store.save(cookies)
        except OSError:
            self.status_label.setText('网页会话无法保存，请检查配置目录是否可写。')

    def _site_cookie_added(self, cookie) -> None:
        self._persist_site_cookie(cookie)

    def _site_cookie_removed(self, cookie) -> None:
        self._persist_site_cookie(cookie, removed=True)

    def shutdown(self) -> None:
        try:
            self.view.stop()
            self.page.deleteLater()
        except RuntimeError:
            pass


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def create_web_widget(settings: Settings, session_store=None) -> WebViewWidget:
    """工厂函数：供主窗口在 QtWebEngine 可用时创建浏览标签页。"""
    return WebViewWidget(settings, session_store=session_store)
