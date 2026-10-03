"""后台任务执行器。

两种用法并存：
  * `TaskRunner.run(fn, ...)`：为一次任务起一个 QThread（用于低频操作，
    例如搜索、详情、翻译）；
  * `TaskRunner.submit(fn, ...)`：提交到固定大小的线程池（用于高频短任务，
    例如一页几十张缩略图）。

为什么需要线程池：早期实现为每张缩略图各建一个 QThread，一页 24 张图就会
在短时间内反复创建/销毁 24 个线程。压测中该模式出现了原生访问违例
（faulthandler 显示主线程 `<no Python frame>`），因此改为固定 4 个常驻
工作线程，既不刷线程也天然限制了并发。

Qt 约束：QPixmap 等图形对象必须在主线程使用，因此工作线程只做网络与解码
准备，结果通过信号回到主线程处理。
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QElapsedTimer, QObject, QThread, Signal, Slot

DEFAULT_POOL_SIZE = 4


class _Signals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)


@dataclass
class _Running:
    """一次任务运行所持有的 Qt 对象，便于统一回收。"""

    thread: QThread
    job: QObject
    signals: _Signals
    stopped: bool = False
    callbacks: list = field(default_factory=list)


@dataclass
class _PoolItem:
    fn: Callable[[], Any]
    signals: _Signals


class _Job(QObject):
    """在子线程中执行一个可调用对象。"""

    def __init__(self, fn: Callable[[], Any], signals: _Signals) -> None:
        super().__init__()
        self._fn = fn
        self._signals = signals

    @Slot()
    def execute(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - 统一转成用户可读错误
            self._signals.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self._signals.succeeded.emit(result)


class _PoolWorker(QObject):
    """线程池工作线程：从队列取任务执行，直到收到停止信号。"""

    def __init__(self, work_queue: "queue.Queue[_PoolItem | None]") -> None:
        super().__init__()
        self._queue = work_queue

    @Slot()
    def loop(self) -> None:
        while True:
            try:
                item = self._queue.get()
            except Exception:  # noqa: BLE001 - 队列异常时退出线程
                return
            if item is None:
                return
            try:
                result = item.fn()
            except Exception as exc:  # noqa: BLE001
                item.signals.failed.emit(f"{type(exc).__name__}: {exc}")
            else:
                item.signals.succeeded.emit(result)


class TaskRunner(QObject):
    """任务运行器：一次性任务用 run()，高频短任务用 submit()。

    stop_all() 之后不再接受新任务（`_closing` 置位），否则正在排队的回调
    可能在池线程已退出后继续提交，触发
    `QThread: Destroyed while thread is still running` 乃至原生崩溃。
    """

    def __init__(self, parent: QObject | None = None, pool_size: int = DEFAULT_POOL_SIZE) -> None:
        super().__init__(parent)
        self._runs: list[_Running] = []
        self._queue: "queue.Queue[_PoolItem | None]" = queue.Queue()
        self._pool_threads: list[QThread] = []
        self._pool_workers: list[_PoolWorker] = []
        self._pool_size = max(1, pool_size)
        self._started = False
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    # ---------------------------------------------------------------- 线程池
    def _ensure_pool(self) -> None:
        if self._started:
            return
        self._started = True
        for _ in range(self._pool_size):
            thread = QThread()
            thread.setObjectName("banana-index-pool")
            worker = _PoolWorker(self._queue)
            worker.moveToThread(thread)
            thread.started.connect(worker.loop)
            self._pool_threads.append(thread)
            self._pool_workers.append(worker)
            thread.start()

    def submit(self, fn: Callable[[], Any], on_success: Callable[[Any], None] | None = None,
               on_error: Callable[[str], None] | None = None) -> None:
        """把任务提交到固定线程池（并发受池大小限制，不需要额外信号量）。"""
        if self._closing:
            return  # 正在关闭：拒绝新任务，避免池线程退出后再被提交
        self._ensure_pool()
        signals = _Signals()

        def handle_success(result: Any) -> None:
            if on_success and not self._closing:
                on_success(result)
            signals.deleteLater()

        def handle_failure(message: str) -> None:
            if on_error and not self._closing:
                on_error(message)
            signals.deleteLater()

        signals.succeeded.connect(handle_success)
        signals.failed.connect(handle_failure)
        self._queue.put(_PoolItem(fn=fn, signals=signals))

    # ------------------------------------------------------------ 单次任务
    def run(self, fn: Callable[[], Any], on_success: Callable[[Any], None] | None = None,
            on_error: Callable[[str], None] | None = None,
            on_finished: Callable[[], None] | None = None) -> None:
        if self._closing:
            return
        thread = QThread()
        thread.setObjectName("banana-index-task")
        signals = _Signals()          # 归属主线程，槽函数在主线程执行
        job = _Job(fn, signals)       # 执行体，随后移入子线程
        job.moveToThread(thread)
        thread.started.connect(job.execute)
        entry = _Running(thread=thread, job=job, signals=signals)
        if on_finished:
            entry.callbacks.append(on_finished)
        self._runs.append(entry)

        def release() -> None:
            if entry.stopped:
                return
            entry.stopped = True
            thread.quit()
            if entry in self._runs:
                self._runs.remove(entry)
            job.deleteLater()
            signals.deleteLater()
            thread.deleteLater()
            if not self._closing:
                for callback in entry.callbacks:
                    callback()

        def handle_success(result: Any) -> None:
            try:
                if on_success and not self._closing:
                    on_success(result)
            finally:
                release()

        def handle_failure(message: str) -> None:
            try:
                if on_error and not self._closing:
                    on_error(message)
            finally:
                release()

        signals.succeeded.connect(handle_success)
        signals.failed.connect(handle_failure)
        thread.start()

    # ---------------------------------------------------------------- 收尾
    def stop_all(self, timeout_ms: int = 3000) -> None:
        """请求所有线程退出并回收；只在主线程调用。

        先置 `_closing` 拒绝新任务，再排空队列并等待线程结束——
        否则「关闭窗口」与「详情图片仍在提交」会互相竞争。
        """
        if self._closing:
            return
        self._closing = True
        # 丢弃尚未开始的任务；退出时不继续下载整页缩略图或发起翻译。
        while True:
            try:
                pending = self._queue.get_nowait()
            except queue.Empty:
                break
            if pending is not None:
                pending.signals.deleteLater()
        for entry in list(self._runs):
            entry.thread.quit()

        # 通知线程池退出：每个工作线程收到一个 None 后返回
        for _ in self._pool_threads:
            self._queue.put(None)
        for thread in self._pool_threads:
            thread.quit()

        timer = QElapsedTimer()
        timer.start()
        from PySide6.QtWidgets import QApplication
        while timer.elapsed() < timeout_ms:
            QApplication.processEvents()
            if not self._runs and all(t.isFinished() for t in self._pool_threads):
                break

        for entry in list(self._runs):
            entry.thread.terminate()
            entry.thread.wait(1000)
            self._release(entry)
        self._runs.clear()

        # 等待线程池真正结束，避免 "QThread: Destroyed while thread is still running"
        for thread in self._pool_threads:
            if not thread.isFinished():
                thread.quit()
                if not thread.wait(timeout_ms):
                    # terminate 有风险（可能留下未释放的锁），因此先尽量等；
                    # 确实超时也保留线程对象不销毁，避免
                    # "QThread: Destroyed while thread is still running"
                    thread.terminate()
                    thread.wait(2000)
            # 不调用 deleteLater：此时事件循环可能已停止，删除会导致
            # 线程对象在仍有回调排队时被销毁。线程随进程退出回收即可。
        self._pool_threads.clear()
        self._pool_workers.clear()
        self._started = False

    def _release(self, entry: _Running) -> None:
        if entry.stopped:
            return
        entry.stopped = True
        if entry in self._runs:
            self._runs.remove(entry)
        entry.job.deleteLater()
        entry.signals.deleteLater()
        entry.thread.deleteLater()
        if not self._closing:
            for callback in entry.callbacks:
                callback()

    @property
    def active_count(self) -> int:
        return len(self._runs)

    @property
    def pool_size(self) -> int:
        return self._pool_size if self._started else 0
