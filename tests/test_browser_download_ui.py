"""网页标签生命周期与可见下载操作回归。"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_direct_download import DirectDownload
from PySide6.QtWidgets import QTabBar


class BrowserDownloadUI(DirectDownload):
    def test_browser_can_close_and_reopen_without_losing_provider(self):
        tab = self.window.right_tabs.indexOf(self.browser)
        bar = self.window.right_tabs.tabBar()
        self.assertTrue(any(bar.tabButton(tab, side) is not None for side in
                            (QTabBar.ButtonPosition.LeftSide, QTabBar.ButtonPosition.RightSide)))
        self.window.right_tabs.tabCloseRequested.emit(tab)
        self.assertEqual(self.window.right_tabs.count(), 1)
        self.assertIs(self.window.right_tabs.currentWidget(), self.window.detail_view)
        self.window.detail_view.link_activated.emit("download:22")
        self.assertEqual(self.browser.downloads, ["https://gamebanana.com/dl/22"])
        self.assertEqual(self.window.right_tabs.count(), 1)
        self.window.open_url_in_web("https://gamebanana.com/mods/100")
        self.assertEqual(self.window.right_tabs.count(), 2)
        self.assertIs(self.window.right_tabs.currentWidget(), self.browser)

    def test_detail_tab_has_no_close_button(self):
        for side in (QTabBar.ButtonPosition.LeftSide, QTabBar.ButtonPosition.RightSide):
            self.assertIsNone(self.window.right_tabs.tabBar().tabButton(0, side))
        self.window.right_tabs.tabCloseRequested.emit(0)
        self.assertEqual(self.window.right_tabs.count(), 2)

    def test_blue_progress_handles_bytes_unknown_length_and_completion(self):
        self.browser.download_state_changed.emit("DownloadRequested", "fixture.zip")
        progress = self.window.download_progress
        self.assertFalse(progress.isHidden())
        self.assertEqual(progress.maximum(), 0)
        self.browser.download_progress_changed.emit(3_000_000_000, 6_000_000_000, "fixture.zip")
        self.assertEqual(progress.value(), 50)
        self.assertIn("#2196f3", progress.styleSheet())
        self.browser.download_progress_changed.emit(100, 0, "fixture.zip")
        self.assertEqual(progress.maximum(), 0)
        self.browser.download_state_changed.emit("DownloadCompleted", "fixture.zip")
        self.assertEqual(progress.value(), 100)
        self.assertIn("完成", progress.format())
        self.assertTrue(self.window.cancel_download_button.isHidden())
        self.assertTrue(self.window.retry_download_button.isHidden())

    def test_cancel_and_retry_buttons_control_download(self):
        self.window.detail_view.link_activated.emit("download:22")
        self.assertFalse(self.window.cancel_download_button.isHidden())
        self.window.cancel_download_button.click()
        self.assertEqual(self.browser.cancelled, 1)
        self.assertIn("已取消", self.window.download_progress.format())
        self.assertFalse(self.window.retry_download_button.isHidden())
        self.window.retry_download_button.click()
        self.assertEqual(self.browser.retries, 1)
        self.assertFalse(self.window.cancel_download_button.isHidden())

    def test_interrupted_download_offers_retry(self):
        self.browser.download_state_changed.emit("DownloadInterrupted", "fixture.zip")
        self.assertFalse(self.window.retry_download_button.isHidden())
        self.assertIn("可重试", self.window.download_progress.format())

    def test_terminal_notice_can_close_with_its_status_text(self):
        for state in ("DownloadCancelled", "DownloadInterrupted", "DownloadCompleted"):
            with self.subTest(state=state):
                self.browser.download_state_changed.emit("DownloadRequested", "fixture.zip")
                self.browser.download_state_changed.emit(state, "fixture.zip")
                self.browser.download_status.emit("下载结果：fixture.zip")
                self.assertFalse(self.window.close_download_notice_button.isHidden())
                self.assertEqual(self.window.retry_download_button.isHidden(), state == "DownloadCompleted")
                self.window.close_download_notice_button.click()
                self.assertTrue(self.window.download_progress.isHidden())
                self.assertTrue(self.window.retry_download_button.isHidden())
                self.assertTrue(self.window.close_download_notice_button.isHidden())
                self.assertEqual(self.window.statusBar().currentMessage(), "")

    def test_dismissed_notice_stays_closed_until_next_download(self):
        self.browser.download_state_changed.emit("DownloadCancelled", "fixture.zip")
        self.window.close_download_notice_button.click()
        self.browser.download_status.emit("已取消下载：fixture.zip")
        self.browser.download_progress_changed.emit(100, 100, "fixture.zip")
        self.browser.download_state_changed.emit("DownloadCancelled", "fixture.zip")
        self.assertTrue(self.window.download_progress.isHidden())
        self.assertEqual(self.window.statusBar().currentMessage(), "")
        self.browser.download_state_changed.emit("DownloadRequested", "next.zip")
        self.browser.download_status.emit("正在请求下载：next.zip")
        self.assertFalse(self.window.download_progress.isHidden())
        self.assertFalse(self.window.cancel_download_button.isHidden())
        self.assertTrue(self.window.close_download_notice_button.isHidden())
        self.assertIn("next.zip", self.window.statusBar().currentMessage())

    def test_close_download_notice_preserves_unrelated_status(self):
        self.browser.download_state_changed.emit("DownloadCancelled", "fixture.zip")
        self.browser.download_status.emit("已取消下载：fixture.zip")
        self.window.statusBar().showMessage("搜索完成：找到 10 项")
        self.window.close_download_notice_button.click()
        self.assertEqual(self.window.statusBar().currentMessage(), "搜索完成：找到 10 项")


if __name__ == "__main__":
    unittest.main()
