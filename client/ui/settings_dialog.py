"""设置对话框：DeepSeek API 密钥、翻译参数、界面与请求选项。

密钥只写入用户本机配置目录；对话框本身不回显完整密钥，只显示掩码。
"""
from __future__ import annotations

import os
import pathlib

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..settings import Settings, default_download_dir, settings_path
from ..translate.engine import DeepSeekTranslator, TranslationError


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)
        # 模态对话框可能出现在主窗口之下或多屏之外，看起来像「闪退」；
        # 明确设置为独立窗口并抬到前台，随后由 showEvent 居中。
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setModal(True)

        layout = QVBoxLayout(self)

        # --- 翻译服务 ---
        translate_box = QGroupBox("翻译服务（当前仅支持 DeepSeek）")
        form = QFormLayout(translate_box)

        key_row = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("sk-…（留空表示不启用翻译）")
        self.key_edit.setText(settings.deepseek_api_key)
        key_row.addWidget(self.key_edit, 1)
        self.reveal_button = QPushButton("显示")
        self.reveal_button.setCheckable(True)
        self.reveal_button.toggled.connect(self._toggle_reveal)
        key_row.addWidget(self.reveal_button)
        key_wrapper = QWidget()
        key_wrapper.setLayout(key_row)
        form.addRow("API 密钥", key_wrapper)

        self.base_url_edit = QLineEdit(settings.deepseek_base_url)
        form.addRow("接口地址", self.base_url_edit)

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        for model in ("deepseek-chat", "deepseek-reasoner"):
            self.model_combo.addItem(model)
        self.model_combo.setCurrentText(settings.deepseek_model)
        form.addRow("模型", self.model_combo)

        self.target_language_edit = QLineEdit(settings.translate_target_language)
        form.addRow("目标语言", self.target_language_edit)

        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 80)
        self.batch_spin.setValue(settings.translate_batch_size)
        self.batch_spin.setToolTip("每次请求提交的文本条数；越大越省请求，但单次响应也更长")
        form.addRow("每批条数", self.batch_spin)

        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 16)
        self.concurrency_spin.setValue(settings.translate_max_concurrency)
        form.addRow("并发请求数", self.concurrency_spin)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(5, 300)
        self.timeout_spin.setValue(settings.translate_timeout_seconds)
        self.timeout_spin.setSuffix(" 秒")
        form.addRow("翻译超时", self.timeout_spin)

        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(0, 6)
        self.retry_spin.setValue(settings.translate_max_retries)
        form.addRow("失败重试次数", self.retry_spin)

        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self._test_connection)
        self.test_label = QLabel("")
        self.test_label.setWordWrap(True)
        test_row = QHBoxLayout()
        test_row.addWidget(self.test_button)
        test_row.addWidget(self.test_label, 1)
        test_wrapper = QWidget()
        test_wrapper.setLayout(test_row)
        form.addRow("连接检查", test_wrapper)

        layout.addWidget(translate_box)

        # --- 抓取与界面 ---
        general_box = QGroupBox("抓取与界面")
        general_form = QFormLayout(general_box)

        self.per_page_spin = QSpinBox()
        self.per_page_spin.setRange(6, 60)
        self.per_page_spin.setValue(settings.per_page)
        self.per_page_spin.setToolTip("每页展示的条目数（按行列铺开）")
        general_form.addRow("每页条目数", self.per_page_spin)

        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 5000)
        self.delay_spin.setSingleStep(50)
        self.delay_spin.setValue(settings.request_delay_ms)
        self.delay_spin.setSuffix(" 毫秒")
        self.delay_spin.setToolTip("接口请求间隔，避免过于频繁访问站点")
        general_form.addRow("请求间隔", self.delay_spin)

        self.request_timeout_spin = QSpinBox()
        self.request_timeout_spin.setRange(5, 120)
        self.request_timeout_spin.setValue(settings.request_timeout_seconds)
        self.request_timeout_spin.setSuffix(" 秒")
        general_form.addRow("接口超时", self.request_timeout_spin)

        self.show_original_check = QCheckBox("汉化后同时显示英文原文")
        self.show_original_check.setChecked(settings.show_original_text)
        general_form.addRow("显示", self.show_original_check)

        self.home_edit = QLineEdit(settings.web_home_url)
        general_form.addRow("内嵌浏览器首页", self.home_edit)

        layout.addWidget(general_box)

        # --- 下载 ---
        download_box = QGroupBox("下载")
        download_form = QFormLayout(download_box)

        dir_row = QHBoxLayout()
        self.download_dir_edit = QLineEdit(settings.download_dir)
        self.download_dir_edit.setPlaceholderText("留空表示使用系统「下载」文件夹")
        dir_row.addWidget(self.download_dir_edit, 1)
        self.browse_button = QPushButton("浏览…")
        self.browse_button.clicked.connect(self._choose_download_dir)
        dir_row.addWidget(self.browse_button)
        self.open_dir_button = QPushButton("打开目录")
        self.open_dir_button.clicked.connect(self._open_download_dir)
        dir_row.addWidget(self.open_dir_button)
        dir_wrapper = QWidget()
        dir_wrapper.setLayout(dir_row)
        download_form.addRow("保存位置", dir_wrapper)

        self.ask_each_time_check = QCheckBox("每次下载都询问保存位置")
        self.ask_each_time_check.setChecked(settings.download_ask_each_time)
        download_form.addRow("询问", self.ask_each_time_check)

        self.open_after_check = QCheckBox("下载完成后自动打开所在文件夹")
        self.open_after_check.setChecked(settings.download_open_folder_after)
        download_form.addRow("完成后", self.open_after_check)

        download_note = QLabel(
            "说明：站点内的模组文件下载会保存到上面的目录；重名文件自动追加 (1)、(2) 避免覆盖。"
            "当前生效路径：" )
        download_note.setWordWrap(True)
        download_note.setStyleSheet("color: #9aa0a6; font-size: 12px;")
        download_form.addRow("", download_note)
        self.download_effective_label = QLabel("")
        self.download_effective_label.setWordWrap(True)
        self.download_effective_label.setStyleSheet("color: #8ab4f8; font-size: 12px;")
        download_form.addRow("", self.download_effective_label)
        self.download_dir_edit.textChanged.connect(self._refresh_effective_dir)
        self._refresh_effective_dir()

        layout.addWidget(download_box)

        note = QLabel(
            f"密钥只保存在本机：{settings_path()}\n"
            "也可用环境变量 DEEPSEEK_API_KEY 覆盖（优先级更高）。"
            "密钥不会写入项目文件、日志或验收证据。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #9aa0a6; font-size: 12px;")
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.about_button = QPushButton("关于")
        self.about_button.clicked.connect(self.open_about)
        buttons.addButton(self.about_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _toggle_reveal(self, shown: bool) -> None:
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        self.reveal_button.setText("隐藏" if shown else "显示")

    # --- 下载目录 ---
    def _current_download_dir(self) -> pathlib.Path:
        text = self.download_dir_edit.text().strip()
        return pathlib.Path(text) if text else default_download_dir()

    def _refresh_effective_dir(self) -> None:
        """显示实际生效的目录，让不可写时的回退对用户可见。"""
        candidate = self._current_download_dir()
        probe = Settings(**{**self.settings.__dict__})
        probe.download_dir = str(candidate)
        effective = probe.resolved_download_dir()
        if effective != candidate:
            self.download_effective_label.setText(
                f"⚠ 该目录不可写，将回退到：{effective}")
        else:
            exists = "已存在" if candidate.exists() else "将在首次下载时创建"
            self.download_effective_label.setText(f"生效路径：{effective}（{exists}）")

    def open_about(self) -> None:
        from .about_dialog import AboutDialog
        AboutDialog(self).exec()

    def _choose_download_dir(self) -> None:
        start = str(self._current_download_dir())
        chosen = QFileDialog.getExistingDirectory(self, "选择下载保存位置", start)
        if chosen:
            self.download_dir_edit.setText(chosen)
            self._refresh_effective_dir()

    def _open_download_dir(self) -> None:
        target = self._current_download_dir()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "无法打开目录", f"{target}\n{exc}")
            return
        try:
            if os.name == "nt":
                os.startfile(str(target))  # noqa: S606 - Windows 打开目录
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        except OSError as exc:
            QMessageBox.warning(self, "无法打开目录", f"{target}\n{exc}\n系统未能打开文件管理器。")

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """确保对话框出现在屏幕可见区域内并置于前台。"""
        super().showEvent(event)
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            hint = self.sizeHint()
            width = max(self.minimumWidth(), min(hint.width(), available.width() - 80))
            height = min(max(hint.height(), 420), available.height() - 80)
            self.resize(width, height)
            parent = self.parentWidget()
            if parent is not None and parent.isVisible():
                centre = parent.frameGeometry().center()
            else:
                centre = available.center()
            x = min(max(centre.x() - width // 2, available.left() + 20),
                    available.right() - width - 20)
            y = min(max(centre.y() - height // 2, available.top() + 20),
                    available.bottom() - height - 20)
            self.move(x, y)
        self.raise_()
        self.activateWindow()

    def _collect(self) -> Settings:
        settings = Settings(**{**self.settings.__dict__})
        settings.deepseek_api_key = self.key_edit.text().strip()
        settings.deepseek_base_url = self.base_url_edit.text().strip() or "https://api.deepseek.com"
        settings.deepseek_model = self.model_combo.currentText().strip() or "deepseek-chat"
        settings.translate_target_language = self.target_language_edit.text().strip() or "简体中文"
        settings.translate_batch_size = self.batch_spin.value()
        settings.translate_max_concurrency = self.concurrency_spin.value()
        settings.translate_timeout_seconds = self.timeout_spin.value()
        settings.translate_max_retries = self.retry_spin.value()
        settings.per_page = self.per_page_spin.value()
        settings.request_delay_ms = self.delay_spin.value()
        settings.request_timeout_seconds = self.request_timeout_spin.value()
        settings.show_original_text = self.show_original_check.isChecked()
        settings.web_home_url = self.home_edit.text().strip() or "https://gamebanana.com/"
        settings.download_dir = self.download_dir_edit.text().strip()
        settings.download_ask_each_time = self.ask_each_time_check.isChecked()
        settings.download_open_folder_after = self.open_after_check.isChecked()
        return settings.normalized()

    def apply_to(self, target: Settings) -> None:
        updated = self._collect()
        for key, value in updated.__dict__.items():
            if key not in {"config_dir", "load_error", "load_error_backup"}:
                setattr(target, key, value)

    def key_was_cleared(self) -> bool:
        """用户是否主动清空了密钥（保存时据此决定是否允许清空磁盘上的旧值）。"""
        return not self.key_edit.text().strip() and bool(self.settings.deepseek_api_key.strip())

    def _test_connection(self) -> None:
        candidate = self._collect()
        if not candidate.resolved_api_key():
            self.test_label.setText("请先填写 API 密钥")
            return
        self.test_button.setEnabled(False)
        self.test_label.setText("正在测试…")
        translator = DeepSeekTranslator(candidate)
        try:
            result = translator.translate_many(["Fast Furnaces"])
        except TranslationError as exc:
            self.test_label.setText(f"失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            self.test_label.setText(f"失败：{type(exc).__name__}: {exc}")
        else:
            self.test_label.setText(f"成功：Fast Furnaces → {result[0]}")
        finally:
            self.test_button.setEnabled(True)

    def accept(self) -> None:
        candidate = self._collect()
        if candidate.deepseek_api_key and not candidate.deepseek_api_key.startswith("sk-"):
            answer = QMessageBox.question(
                self, "密钥格式确认",
                "DeepSeek API 密钥通常以 sk- 开头。仍要保存吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        super().accept()
