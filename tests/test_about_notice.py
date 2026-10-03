"""首次确认跨启动保存，关于不改变设置，不访问网络。"""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_direct_download import DirectDownload
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog
from client.settings import load_settings, save_settings
from client.ui.about_dialog import AboutDialog, PROJECT_URL, SPONSOR_URL, show_first_launch_notice
from client.ui.settings_dialog import SettingsDialog


class AboutNotice(DirectDownload):
    def test_confirm_is_persisted_and_not_shown_on_restart(self):
        self.window.settings.deepseek_api_key = 'sk-offline-notice-test'
        self.window.settings.download_dir = self.directory.name
        save_settings(self.window.settings)
        def confirm():
            dialog = self.app.activeModalWidget()
            self.assertIsInstance(dialog, AboutDialog)
            self.assertEqual(dialog.confirm_button.text(), '我已知晓')
            dialog.confirm_button.click()
        QTimer.singleShot(0, confirm)
        show_first_launch_notice(self.window)
        restored = load_settings(pathlib.Path(self.directory.name))
        self.assertTrue(restored.open_source_notice_acknowledged)
        self.assertEqual(restored.deepseek_api_key, 'sk-offline-notice-test')
        self.assertEqual(restored.download_dir, self.directory.name)
        self.window.settings = restored
        with patch('client.ui.about_dialog.AboutDialog') as dialog:
            show_first_launch_notice(self.window)
            dialog.assert_not_called()

    def test_close_remembers_notice_on_restart(self):
        QTimer.singleShot(0, lambda: self.app.activeModalWidget().close())
        show_first_launch_notice(self.window)
        self.assertTrue(load_settings(pathlib.Path(self.directory.name)).open_source_notice_acknowledged)

    def test_escape_remembers_notice_on_restart(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        QTimer.singleShot(0, lambda: QTest.keyClick(self.app.activeModalWidget(), Qt.Key.Key_Escape))
        show_first_launch_notice(self.window)
        self.assertTrue(load_settings(pathlib.Path(self.directory.name)).open_source_notice_acknowledged)

    def test_save_failure_does_not_pretend_acknowledged(self):
        with patch.object(AboutDialog, 'exec', return_value=QDialog.DialogCode.Accepted), \
             patch('client.ui.about_dialog.save_settings', side_effect=OSError('offline fixture')), \
             patch('client.ui.about_dialog.QMessageBox.warning') as warning:
            show_first_launch_notice(self.window)
            self.assertFalse(self.window.settings.open_source_notice_acknowledged)
            warning.assert_called_once()

    def test_about_shows_author_free_notice_and_verified_links(self):
        dialog = AboutDialog(self.window)
        self.assertIn('Hzans', dialog.notice.text())
        self.assertIn('没有任何付费购买渠道', dialog.notice.text())
        self.assertIn('谨防诈骗', dialog.notice.text())
        self.assertIn(PROJECT_URL, dialog.notice.text())
        self.assertIn(SPONSOR_URL, dialog.notice.text())
        self.assertEqual(dialog.confirm_button.text(), '关闭')
        with patch('client.ui.about_dialog.QDesktopServices.openUrl', return_value=True) as opened:
            dialog.sponsor_button.click()
            self.assertEqual(opened.call_args.args[0].toString(), SPONSOR_URL)
        self.assertFalse(self.window.settings.open_source_notice_acknowledged)

    def test_settings_about_does_not_save_other_edits(self):
        dialog = SettingsDialog(self.window.settings, self.window)
        dialog.key_edit.setText('sk-unsaved-fixture')
        with patch.object(AboutDialog, 'exec', return_value=QDialog.DialogCode.Accepted) as opened:
            dialog.about_button.click()
            opened.assert_called_once()
        self.assertEqual(self.window.settings.deepseek_api_key, '')
        self.assertFalse(self.window.settings.open_source_notice_acknowledged)
        self.assertFalse((pathlib.Path(self.directory.name) / 'settings.json').exists())

    def test_closed_main_window_never_opens_notice(self):
        self.window.close()
        with patch('client.ui.about_dialog.AboutDialog') as dialog:
            show_first_launch_notice(self.window)
            dialog.assert_not_called()


if __name__ == '__main__':
    unittest.main()
