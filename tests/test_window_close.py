"""一次关闭立即隐藏，后台不再执行排队任务或触发界面回调。"""
import pathlib
import sys
import threading
import time
import unittest
import subprocess

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_direct_download import DirectDownload
from client.ui.workers import TaskRunner


class WindowClose(DirectDownload):
    def test_one_close_exits_qt_event_loop(self):
        child = subprocess.run([sys.executable, __file__, "--single-close-child"],
                               capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr.decode(errors="replace"))
        self.assertIn(b"single close exited", child.stdout)

    def test_first_close_hides_window_before_cleanup_and_does_not_reenter(self):
        self.window.show()
        self.app.processEvents()
        visibility = []
        original = self.window.tasks.stop_all

        def cleanup():
            visibility.append(self.window.isVisible())
            self.window.close()  # 收尾中再次收到关闭，不得重复清理。
            original()

        self.window.tasks.stop_all = cleanup
        self.assertTrue(self.window.close())
        self.assertEqual(visibility, [False])
        self.assertFalse(self.window.isVisible())

    def test_shutdown_drops_queued_tasks_and_suppresses_callbacks(self):
        runner = TaskRunner(pool_size=1)
        started = threading.Event()
        release = threading.Event()
        executed = []
        callbacks = []

        def active():
            started.set()
            release.wait(1)
            return 1

        runner.submit(active, lambda value: callbacks.append(value))
        deadline = time.monotonic() + 2
        while not started.is_set() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(started.is_set())
        for value in range(20):
            runner.submit(lambda value=value: executed.append(value), callbacks.append)
        timer = threading.Timer(0.05, release.set)
        timer.start()
        try:
            runner.stop_all()
            self.app.processEvents()
        finally:
            release.set()
            timer.join()
        self.assertEqual(executed, [])
        self.assertEqual(callbacks, [])


if __name__ == "__main__":
    if "--single-close-child" in sys.argv:
        from PySide6.QtCore import QTimer
        DirectDownload.setUpClass()
        fixture = DirectDownload()
        fixture.setUp()
        timed_out = []
        QTimer.singleShot(100, fixture.window.close)
        QTimer.singleShot(5000, lambda: (timed_out.append(True), fixture.app.quit()))
        fixture.window.show()
        code = fixture.app.exec()
        fixture.tearDown()
        if timed_out:
            raise SystemExit("one close did not exit event loop")
        print("single close exited")
        raise SystemExit(code)
    else:
        unittest.main()
