"""崩溃与错误日志：把未处理异常与原生崩溃写入本机文件，便于用户回报问题。

日志位置：<配置目录>/logs/crash-YYYYMMDD.log
  * `install()` 捕获 Python 未处理异常（含主线程栈）；
  * `enable_native_crash_log()` 通过 faulthandler 额外捕获 C++/原生层的
    段错误与 Qt 致命错误——这类崩溃不会抛 Python 异常，
    表现通常是「窗口变白后直接消失」，正是最需要栈信息的情形。
"""
from __future__ import annotations

import datetime as _dt
import faulthandler
import os
import pathlib
import platform
import sys
import traceback

_installed = False
_native_stream = None


def _log_dir(config_dir: str | pathlib.Path | None) -> pathlib.Path:
    base = pathlib.Path(config_dir) if config_dir else pathlib.Path.cwd()
    directory = base / "logs"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        directory = pathlib.Path.cwd()
    return directory


def log_path(config_dir: str | pathlib.Path | None = None) -> pathlib.Path:
    stamp = _dt.datetime.now().strftime("%Y%m%d")
    return _log_dir(config_dir) / f"crash-{stamp}.log"


def enable_native_crash_log(config_dir: str | pathlib.Path | None = None) -> pathlib.Path:
    """开启 faulthandler：原生崩溃时把 Python 栈写入日志文件。

    返回日志路径。文件以追加方式打开并常驻，因此不能在程序运行期间删除。
    """
    global _native_stream
    target = log_path(config_dir)
    if _native_stream is not None:
        return target
    try:
        stream = target.open("a", encoding="utf-8", newline="\n")
    except OSError:
        fallback = pathlib.Path.cwd() / "banana-index-crash.log"
        stream = fallback.open("a", encoding="utf-8", newline="\n")
        target = fallback
    stream.write(f"\n===== 会话开始 @ {_dt.datetime.now().isoformat(timespec='seconds')} =====\n"
                 f"python={sys.version.split()[0]} platform={platform.platform()}\n")
    stream.flush()
    faulthandler.enable(file=stream, all_threads=True)
    _native_stream = stream
    # 让 Qt 自身的告警/严重日志也进入 stderr，配合看门狗捕获
    os.environ.setdefault("QT_LOGGING_RULES", "qt.webenginecontext.warning=true;*.warning=true")
    return target


def write_entry(title: str, detail: str, config_dir: str | pathlib.Path | None = None) -> pathlib.Path:
    path = log_path(config_dir)
    header = (f"\n===== {title} @ {_dt.datetime.now().isoformat(timespec='seconds')} =====\n"
              f"python={sys.version.split()[0]} platform={platform.platform()}\n")
    try:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(header)
            stream.write(detail.rstrip() + "\n")
    except OSError:
        pass
    return path


def install(config_dir: str | pathlib.Path | None, on_error=None) -> pathlib.Path:
    """安装全局异常钩子；返回日志路径。重复调用只安装一次。"""
    global _installed
    target = log_path(config_dir)
    if _installed:
        return target

    def handle(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        saved = write_entry("未处理异常", detail, config_dir)
        print(f"[banana-index] 发生错误，详情已写入：{saved}", file=sys.stderr)
        print(detail, file=sys.stderr)
        if on_error is not None:
            try:
                on_error(f"{exc_value}", saved)
            except Exception:  # noqa: BLE001 - 错误处理本身不能再抛
                pass

    sys.excepthook = handle
    _installed = True
    return target
