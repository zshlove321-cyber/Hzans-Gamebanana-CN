"""权限恢复后的设置选择、下载询问和错误边界回归。"""
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from client.settings import default_config_dir, Settings
from client.ui.web_view import WebViewWidget
from tools.verify_download_wiring import FakeRequest


class Label:
    def setText(self, text):
        self.text = text


class Progress:
    def setVisible(self, value):
        pass

    def setRange(self, *values):
        pass

    def setValue(self, value):
        pass


class Regression(unittest.TestCase):
    def test_preserves_existing_fallback_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            project = base / "project"
            existing = project / ".banana-index"
            existing.mkdir(parents=True)
            (existing / "settings.json").write_text("{}")
            with patch.dict("os.environ", {"APPDATA": str(base / "roaming"),
                                           "BANANA_INDEX_CONFIG_DIR": ""}), \
                    patch("client.settings.Path.cwd", return_value=project), \
                    patch("client.settings.Path.home", return_value=base / "home"):
                self.assertEqual(default_config_dir(), existing)

    def test_explicit_config_override_stays_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            override = pathlib.Path(tmp) / "explicit"
            with patch.dict("os.environ", {"BANANA_INDEX_CONFIG_DIR": str(override)}):
                self.assertEqual(default_config_dir(), override)

    def test_download_uses_browser_profile(self):
        downloads = []
        holder = types.SimpleNamespace(status_label=Label(),
                                       page=types.SimpleNamespace(download=downloads.append))
        WebViewWidget._download_by_url(holder, "https://gamebanana.com/dl/123")
        self.assertEqual(downloads[0].toString(), "https://gamebanana.com/dl/123")

    def test_native_request_accepts_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            (directory / "example.zip").write_bytes(b"existing")
            holder = types.SimpleNamespace(settings=Settings(download_dir=tmp),
                                           status_label=Label(), progress=Progress())
            request = FakeRequest("example.zip")
            WebViewWidget._on_download_requested(holder, request)
            self.assertTrue(request.accepted)
            self.assertEqual(request.downloadFileName(), "example (1).zip")
            request.finish("DownloadCompleted")
            self.assertIn("下载完成", holder.status_label.text)
            self.assertEqual((directory / "example.zip").read_bytes(), b"existing")

    def test_cancel_save_does_not_accept(self):
        with tempfile.TemporaryDirectory() as tmp:
            holder = types.SimpleNamespace(settings=Settings(download_dir=tmp,
                                                              download_ask_each_time=True),
                                           status_label=Label(), progress=Progress())
            request = FakeRequest("example.zip")
            with patch("client.ui.web_view.QFileDialog.getSaveFileName", return_value=("", "")):
                WebViewWidget._on_download_requested(holder, request)
            self.assertTrue(request.cancelled)
            self.assertFalse(request.accepted)

    def test_html_error_response_does_not_become_a_file(self):
        holder = types.SimpleNamespace(status_label=Label())
        request = FakeRequest("error.html")
        request.mimeType = lambda: "text/html; charset=utf-8"
        WebViewWidget._on_download_requested(holder, request)
        self.assertTrue(request.cancelled)
        self.assertFalse(request.accepted)
        self.assertIn("返回网页", holder.status_label.text)


if __name__ == "__main__":
    unittest.main()
