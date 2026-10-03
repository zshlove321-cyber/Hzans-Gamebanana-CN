"""真实会话下验证下载接线：QWebEngineProfile 的默认下载路径是否跟随设置。

不依赖网络下载，只验证「设置 → Profile → 请求落地目录」这条链路。
运行：python tools/verify_download_wiring.py [目标目录]
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", newline="\n")

from PySide6.QtWidgets import QApplication  # noqa: E402

from client import app as app_module  # noqa: E402
from client.settings import Settings  # noqa: E402


class FakeSignal:
    def __init__(self) -> None:
        self.slots = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def emit(self, *args) -> None:
        for slot in self.slots:
            slot(*args)


class FakeState:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeRequest:
    def __init__(self, filename: str) -> None:
        self._dir = ""
        self._name = filename
        self._state = FakeState("DownloadRequested")
        self._received = 0
        self._total = 0
        self.accepted = False
        self.cancelled = False
        self.receivedBytesChanged = FakeSignal()
        self.totalBytesChanged = FakeSignal()
        self.stateChanged = FakeSignal()

    def downloadFileName(self):
        return self._name

    def downloadDirectory(self):
        return self._dir

    def setDownloadDirectory(self, value):
        self._dir = value

    def setDownloadFileName(self, value):
        self._name = value

    def accept(self):
        self.accepted = True

    def cancel(self):
        self.cancelled = True

    def receivedBytes(self):
        return self._received

    def totalBytes(self):
        return self._total

    def state(self):
        return self._state

    def interruptReasonString(self):
        return ""

    def progress(self, received, total):
        self._received, self._total = received, total
        self.receivedBytesChanged.emit()

    def finish(self, name):
        self._state = FakeState(name)
        self.stateChanged.emit(self._state)


def main() -> int:
    if not app_module.WEBENGINE_AVAILABLE:
        # 需要区分「没装模块」与「模块齐全但数据目录不可写」：
        # 后者在正常桌面会话（%APPDATA% 可写）下不会发生
        from client.ui.capabilities import is_webengine_available, missing_webengine_modules
        if is_webengine_available():
            print("SKIP：QtWebEngine 模块齐全，但当前环境无法启用（Chromium 数据目录不可写，"
                  "通常因为 %APPDATA% 受限）。请在正常桌面会话下重跑本脚本。")
        else:
            print(f"SKIP：未安装 QtWebEngine 模块（缺 {', '.join(missing_webengine_modules())}）")
        return 2

    target = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else \
        pathlib.Path(tempfile.mkdtemp()) / "banana-downloads"
    QApplication.instance() or QApplication([])

    from client.ui.web_view import WebViewWidget

    settings = Settings(config_dir=str(target.parent / "cfg"), download_dir=str(target))
    widget = WebViewWidget(settings)
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        print(f"[{'OK  ' if condition else 'FAIL'}] {message}", flush=True)
        if not condition:
            failures.append(message)

    check(widget.profile.downloadPath() == str(target),
          f"Profile 默认下载路径 == 设置目录（实际 {widget.profile.downloadPath()}）")

    request = FakeRequest("example_mod.zip")
    widget._on_download_requested(request)
    check(request.accepted, "下载请求被接受")
    check(pathlib.Path(request.downloadDirectory()).resolve() == target.resolve(),
          f"请求落地目录 == 设置目录（实际 {request.downloadDirectory()}）")

    request.progress(300, 1000)
    check(widget.progress.value() == 30, f"进度条更新为 30%（实际 {widget.progress.value()}）")

    request.finish("DownloadCompleted")
    check("下载完成" in widget.status_label.text(),
          f"完成文案正确（实际 {widget.status_label.text()!r}）")

    # 切目录后应立刻生效，无需重启
    new_dir = target.parent / "another-dir"
    settings.download_dir = str(new_dir)
    widget.apply_settings(settings)
    check(widget.profile.downloadPath() == str(new_dir),
          f"改设置后 Profile 路径立即更新（实际 {widget.profile.downloadPath()}）")

    widget.shutdown()
    print("\n结论：" + ("下载接线全部正确" if not failures else f"存在 {len(failures)} 项问题"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
