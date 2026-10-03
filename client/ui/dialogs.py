"""通用对话框辅助：错误提示不应依赖主窗口是否可用。"""
from __future__ import annotations

from PySide6.QtWidgets import QApplication, QMessageBox


def show_error(title: str, message: str) -> None:
    """弹出一个错误提示框；已存在父窗口时挂到当前活动窗口上。"""
    if QApplication.instance() is None:
        return
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(title)
    box.setText(message)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    active = QApplication.activeWindow()
    if active is not None:
        box.setParent(active, box.windowFlags())
    box.show()
    box.raise_()
