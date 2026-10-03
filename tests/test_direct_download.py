"""详情内两个下载入口的行为回归，不连接外网。"""
import os
import pathlib
import sys
import tempfile
import unittest
from html.parser import HTMLParser

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("DEEPSEEK_API_KEY", None)
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QWidget
from client.api import FileInfo, SearchOutcome, Submission, SubmissionDetail
from client.common import SessionStore
from client.settings import Settings
from client.translate import DeepSeekTranslator
from client.ui.main_window import MainWindow


class OfflineClient:
    timeout = 25
    request_delay_ms = 0
    session = None

    def __init__(self, directory):
        self.session_store = SessionStore(pathlib.Path(directory) / "cookies.json")

    def apply_session_cookies(self):
        return 0

    def list_games(self, *args, **kwargs):
        return []

    def browse_model(self, *args, **kwargs):
        return SearchOutcome(submissions=[], total=0, page=1, per_page=24, is_complete=True)


class Browser(QWidget):
    download_status = Signal(str)
    download_progress_changed = Signal(object, object, str)
    download_state_changed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.downloads = []
        self.pages = []
        self.cancelled = 0
        self.retries = 0

    def download_url(self, url):
        self.downloads.append(url)
        self.download_status.emit("开始下载：fixture.zip")
        self.download_state_changed.emit("DownloadInProgress", "fixture.zip")

    def load_url(self, url):
        self.pages.append(url)

    def shutdown(self):
        pass

    def cancel_download(self):
        self.cancelled += 1
        self.download_state_changed.emit("DownloadCancelled", "fixture.zip")

    def retry_download(self):
        self.retries += 1
        self.download_state_changed.emit("DownloadInProgress", "fixture.zip")


class Anchors(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs).get("href"))


class DirectDownload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        settings = Settings(config_dir=self.directory.name)
        self.browser = Browser()
        self.window = MainWindow(settings, OfflineClient(self.directory.name),
                                 DeepSeekTranslator(settings),
                                 web_widget_factory=lambda settings: self.browser)
        self.window.detail = SubmissionDetail(
            submission=Submission(id=100, model="Mod", name="fixture", profile_url=""),
            files=[FileInfo(11, "first.zip", 123, "https://gamebanana.com/dl/11"),
                   FileInfo(22, 'second & test.zip', 456, "https://gamebanana.com/dl/22")])
        self.window.right_tabs.setCurrentWidget(self.window.detail_view)

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.directory.cleanup()

    def test_each_file_keeps_original_link_and_gets_direct_link(self):
        rendered = self.window._detail_html(self.window.detail)
        anchors = Anchors()
        anchors.feed(rendered)
        for file in self.window.detail.files:
            self.assertIn(file.download_url, anchors.links)
            self.assertIn(f"download:{file.id}", anchors.links)
        self.assertEqual(rendered.count("跳转下载页"), 2)
        self.assertEqual(rendered.count("直接下载"), 2)
        self.assertIn("second &amp; test.zip", rendered)

    def test_direct_download_selects_file_and_stays_in_detail(self):
        self.window.detail_view.link_activated.emit("download:22")
        self.assertEqual(self.browser.downloads, ["https://gamebanana.com/dl/22"])
        self.assertEqual(self.browser.pages, [])
        self.assertIs(self.window.right_tabs.currentWidget(), self.window.detail_view)
        self.assertIn("开始下载", self.window.statusBar().currentMessage())
        self.browser.download_status.emit("下载完成：fixture.zip")
        self.assertIn("下载完成", self.window.statusBar().currentMessage())

    def test_original_link_keeps_navigation_behavior(self):
        self.window.detail_view.link_activated.emit("https://gamebanana.com/dl/11")
        self.assertEqual(self.browser.pages, ["https://gamebanana.com/dl/11"])
        self.assertIs(self.window.right_tabs.currentWidget(), self.browser)

    def test_stale_or_malformed_link_never_downloads_another_file(self):
        for link in ("download:999", "download:not-an-id"):
            self.window.detail_view.link_activated.emit(link)
        self.assertEqual(self.browser.downloads, [])
        self.assertEqual(self.browser.pages, [])

    def test_missing_url_has_no_direct_link(self):
        self.window.detail.files[0].download_url = ""
        rendered = self.window._detail_html(self.window.detail)
        self.assertNotIn('href="download:11"', rendered)
        self.window.detail_view.link_activated.emit("download:11")
        self.assertEqual(self.browser.downloads, [])


if __name__ == "__main__":
    unittest.main()
