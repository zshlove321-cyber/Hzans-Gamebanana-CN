"""内置图片查看器：预览图原图在软件内查看，不调起外部程序。

为什么需要它：客户端可能以管理员权限运行，而用户的浏览器（Edge 等）通常以普通
权限运行。Windows 不允许管理员进程把新窗口注入到普通权限进程（安全边界），
于是打开外部程序时会弹出「Microsoft Edge 未响应，因为现有实例正在以提升的权限
运行」这类对话框。图片浏览完全可以自给自足，因此改为内置查看。

功能：缩放（适应窗口 / 原始尺寸 / 滚轮缩放）、拖动平移、另存为、上/下一张。
"""
from __future__ import annotations

import pathlib

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .workers import TaskRunner


class _ImageCanvas(QLabel):
    """可缩放、可拖动的图片画布。"""

    zoom_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(200, 150)
        self.setStyleSheet("background: #1b1c1e;")
        self._pixmap: QPixmap | None = None
        self._zoom = 1.0
        self._fit = True
        self._drag_origin: QPoint | None = None

    def set_image(self, image: QImage) -> None:
        self._pixmap = QPixmap.fromImage(image)
        self._fit = True
        self._apply()

    def has_image(self) -> bool:
        return self._pixmap is not None and not self._pixmap.isNull()

    def zoom_in(self) -> None:
        self._set_zoom(self._zoom * 1.25)

    def zoom_out(self) -> None:
        self._set_zoom(self._zoom / 1.25)

    def zoom_reset(self) -> None:
        self._fit = False
        self._zoom = 1.0
        self._apply()

    def fit_to_window(self) -> None:
        self._fit = True
        self._apply()

    def _set_zoom(self, value: float) -> None:
        self._fit = False
        self._zoom = max(0.05, min(value, 8.0))
        self._apply()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        if self._fit:
            self._apply()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if not self.has_image():
            return
        delta = event.angleDelta().y()
        if delta > 0:
            self._set_zoom(self._zoom * 1.15)
        elif delta < 0:
            self._set_zoom(self._zoom / 1.15)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton and not self._fit:
            self._drag_origin = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._drag_origin is None:
            return
        # 拖动平移：通过滚动区域调整位置
        area = self.parentWidget()
        while area is not None and not isinstance(area, QScrollArea):
            area = area.parentWidget()
        if isinstance(area, QScrollArea):
            delta = event.position().toPoint() - self._drag_origin
            area.horizontalScrollBar().setValue(
                area.horizontalScrollBar().value() - delta.x())
            area.verticalScrollBar().setValue(
                area.verticalScrollBar().value() - delta.y())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._drag_origin = None
        self.unsetCursor()

    def _apply(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            return
        if self._fit:
            area = self.parentWidget()
            while area is not None and not isinstance(area, QScrollArea):
                area = area.parentWidget()
            box = area.viewport().size() if isinstance(area, QScrollArea) else self.size()
            scaled = self._pixmap.scaled(
                max(64, box.width() - 8), max(64, box.height() - 8),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
        else:
            scaled = self._pixmap.scaled(
                int(self._pixmap.width() * self._zoom),
                int(self._pixmap.height() * self._zoom),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
        self.setPixmap(scaled)
        self.resize(scaled.size())
        self.zoom_changed.emit(self._zoom)


class ImageViewer(QDialog):
    """预览图查看器：只读展示 + 另存为，不依赖外部程序。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("预览图")
        self.resize(960, 720)
        self.tasks = TaskRunner(self)
        self._image = QImage()
        self._payload = b""
        self._suggested = "preview.png"
        self._gallery: list[str] = []
        self._index = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.caption = QLabel("加载中…", self)
        self.caption.setStyleSheet("color: #bdc1c6;")
        layout.addWidget(self.caption)

        self.area = QScrollArea(self)
        self.area.setWidgetResizable(False)
        self.area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas = _ImageCanvas(self.area)
        self.area.setWidget(self.canvas)
        layout.addWidget(self.area, 1)

        buttons = QHBoxLayout()
        self.prev_button = QPushButton("上一张", self)
        self.next_button = QPushButton("下一张", self)
        fit_button = QPushButton("适应窗口", self)
        reset_button = QPushButton("原始尺寸", self)
        zoom_in = QPushButton("放大", self)
        zoom_out = QPushButton("缩小", self)
        self.save_button = QPushButton("另存为…", self)
        close_button = QPushButton("关闭", self)
        for widget in (self.prev_button, self.next_button, fit_button, reset_button,
                       zoom_in, zoom_out, self.save_button, close_button):
            buttons.addWidget(widget)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        fit_button.clicked.connect(self.canvas.fit_to_window)
        reset_button.clicked.connect(self.canvas.zoom_reset)
        zoom_in.clicked.connect(self.canvas.zoom_in)
        zoom_out.clicked.connect(self.canvas.zoom_out)
        self.save_button.clicked.connect(self.save_as)
        close_button.clicked.connect(self.close)
        self.prev_button.clicked.connect(lambda: self.show_at(self._index - 1))
        self.next_button.clicked.connect(lambda: self.show_at(self._index + 1))
        self.save_button.setEnabled(False)
        self._update_nav()

    # --- 加载 ---
    def load_url(self, url: str, session_factory, title: str = "",
                 gallery: list[str] | None = None) -> None:
        """下载并展示图片；session_factory 返回 requests 会话（每线程独立）。"""
        self._gallery = list(gallery or [])
        self._index = self._gallery.index(url) if url in self._gallery else 0
        if not self._gallery:
            self._gallery = [url]
        self._update_nav()
        self._fetch(url, session_factory, title)

    def show_at(self, index: int) -> None:
        if not self._gallery:
            return
        index = max(0, min(index, len(self._gallery) - 1))
        self._index = index
        self._update_nav()
        self._fetch(self._gallery[index], self._session_factory, "")

    # 记录最近一次用于取图的会话工厂，便于翻页复用
    _session_factory = None

    def _fetch(self, url: str, session_factory, title: str) -> None:
        if session_factory is not None:
            self._session_factory = session_factory
        self.caption.setText(f"加载中… {title or url}")
        self.save_button.setEnabled(False)

        def work() -> tuple[bytes, QImage]:
            session = self._session_factory()
            response = session.get(url, timeout=30)
            response.raise_for_status()
            image = QImage()
            image.loadFromData(response.content)
            return response.content, image

        def done(result: tuple[bytes, QImage]) -> None:
            payload, image = result
            if image.isNull():
                self.caption.setText(f"无法解析该图片：{url}")
                return
            self._payload = payload
            self._image = image
            self._suggested = pathlib.Path(url.split("?")[0]).name or "preview.png"
            self.canvas.set_image(image)
            self.caption.setText(
                f"{title or ''} {image.width()}×{image.height()}  ·  {url}".strip())
            self.save_button.setEnabled(True)

        def failed(message: str) -> None:
            self.caption.setText(f"图片加载失败：{message}")

        self.tasks.submit(work, done, failed)

    def _update_nav(self) -> None:
        total = len(self._gallery)
        self.prev_button.setEnabled(total > 1 and self._index > 0)
        self.next_button.setEnabled(total > 1 and self._index < total - 1)
        if total > 1:
            self.caption.setText(f"第 {self._index + 1} / {total} 张")

    # --- 保存 ---
    def save_as(self) -> None:
        if not self._payload:
            return
        target, _ = QFileDialog.getSaveFileName(
            self, "另存为", str(pathlib.Path.home() / self._suggested))
        if not target:
            return
        try:
            pathlib.Path(target).write_bytes(self._payload)
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", f"{type(exc).__name__}: {exc}")
            return
        self.caption.setText(f"已保存：{target}")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.tasks.stop_all()
        super().closeEvent(event)
