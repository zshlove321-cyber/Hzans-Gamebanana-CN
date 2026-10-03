"""开源发布说明、首次确认和作者信息。"""
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QMessageBox, QPushButton, QVBoxLayout

from .. import __version__
from ..settings import save_settings

PROJECT_URL = "https://github.com/zshlove321-cyber/Hzans-Gamebanana-CN"
SPONSOR_URL = "https://afdian.com/a/Hzans777"


class AboutDialog(QDialog):
    def __init__(self, parent=None, *, first_launch=False):
        super().__init__(parent)
        self.setWindowTitle("首次使用须知" if first_launch else "关于 GameBanana 中文索引")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        title = QLabel(f"<h2>GameBanana 中文索引 v{__version__}</h2>")
        layout.addWidget(title)
        self.notice = QLabel(
            f"本软件由 <b>Hzans</b> 于 GitHub 开源发布。<br><br>"
            "<b>没有任何付费购买渠道，谨防诈骗。</b><br>"
            "软件免费使用，赞助完全自愿，不影响任何软件功能。<br><br>"
            f'<a style="color: #64b5ff" href="{PROJECT_URL}">GitHub 项目主页 · 源码与正式版本</a><br><br>'
            "创作和维护不易，如果这个项目对你有帮助，欢迎请作者喝杯咖啡 ☕。<br>"
            f'<a style="color: #64b5ff" href="{SPONSOR_URL}">爱发电 · 赞助 Hzans</a><br><br>'
            "本项目与 GameBanana、DeepSeek 无官方关联。<br>"
            "可选的翻译服务使用你自己的 API 密钥，服务费用由相应平台收取。"
        )
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.RichText)
        self.notice.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.notice.setOpenExternalLinks(True)
        layout.addWidget(self.notice)
        buttons = QDialogButtonBox()
        self.sponsor_button = QPushButton("☕ 请作者喝杯咖啡")
        self.sponsor_button.clicked.connect(self.open_sponsor)
        buttons.addButton(self.sponsor_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.confirm_button = QPushButton("我已知晓" if first_launch else "关闭")
        self.confirm_button.setDefault(True)
        buttons.addButton(self.confirm_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def open_sponsor(self):
        if not QDesktopServices.openUrl(QUrl(SPONSOR_URL)):
            QMessageBox.information(self, "无法打开浏览器", f"请在浏览器中打开：\n{SPONSOR_URL}")


def show_first_launch_notice(window):
    """确认、× 和 Esc 均记住关闭状态；之后通过设置查看。"""
    if window._closing_window or window.settings.open_source_notice_acknowledged is True:
        return
    dialog = AboutDialog(window, first_launch=True)
    dialog.exec()
    window.settings.open_source_notice_acknowledged = True
    try:
        save_settings(window.settings)
    except OSError:
        window.settings.open_source_notice_acknowledged = False
        QMessageBox.warning(window, "无法保存确认状态", "本次已确认，但设置未能保存，下次启动仍会提示。")
