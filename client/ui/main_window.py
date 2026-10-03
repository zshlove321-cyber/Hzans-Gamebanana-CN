"""主窗口：中文索引界面 + 详情 + 可汉化的内嵌网页浏览。

布局（对应 UI.txt 第2条「单独做一个中文索引UI界面，将搜索到的内容按行列展示」）：
  ┌ 工具栏：搜索框 / 站点 / 类型 / 排序 / 一键汉化 / 还原 / 设置 ┐
  ├ 左：卡片网格（按行列展示）+ 翻页                          ┤
  └ 右：条目详情（可跳转站点页）；若 QtWebEngine 可用则含网页浏览与一键汉化整页 ┘
"""
from __future__ import annotations

import html as html_module
import os
import pathlib
import re
import threading
import traceback
from typing import Any

import requests

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTabBar,
    QTabWidget,
    QTextBrowser,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..api import (
    SEARCHABLE_MODELS,
    SITE_BASE,
    SORT_OPTIONS,
    GameBananaClient,
    GameRef,
    Submission,
    SubmissionDetail,
    collect_translatable,
    is_chinese,
    partition_of,
)
from ..common import ApiError
from ..settings import Settings, save_settings
from ..translate import DeepSeekTranslator, TranslationError
from .cards import CardGrid
from .detail_view import (
    PartitionDetailView,
    sanitize_html,
    scaled_data_uri,
    video_links,
)
from .environment import elevation_notice
from .settings_dialog import SettingsDialog
from .workers import TaskRunner

DETAIL_HTML = """<style>
  body {{ font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; font-size: 13px; color: #e8eaed; }}
  h2 {{ font-size: 17px; margin: 0 0 4px 0; }}
  .orig {{ color: #9aa0a6; margin-bottom: 8px; }}
  .meta {{ color: #bdc1c6; margin: 2px 0; }}
  .tag {{ color: #8ab4f8; }}
  .body {{ color: #e8eaed; }}
  .body img {{ max-width: 100%; }}
  .body a {{ color: #8ab4f8; }}
  .gallery td {{ padding: 4px; border: 0; }}
  .gallery img {{ border: 1px solid #3c4043; border-radius: 3px; }}
  .hero {{ margin: 4px 0; text-align: center; }}
  .hero img {{ border: 1px solid #3c4043; border-radius: 4px; }}
  .thumbs td {{ padding: 3px; border: 0; text-align: center; }}
  .thumbs img {{ border: 1px solid #3c4043; border-radius: 3px; }}
  .thumbs td.current img {{ border: 2px solid #8ab4f8; }}
  .translated {{ color: #d7dadd; border-left: 3px solid #4a6d99; padding-left: 8px; }}
  pre {{ background: #1f2023; padding: 6px; border-radius: 3px; color: #d7dadd; white-space: pre-wrap; }}
  code {{ background: #1f2023; color: #d7dadd; }}
  blockquote {{ border-left: 3px solid #5f6368; margin-left: 0; padding-left: 8px; color: #bdc1c6; }}
  table {{ border-collapse: collapse; margin-top: 8px; width: 100%; }}
  td, th {{ border-bottom: 1px solid #3c4043; padding: 4px 6px; text-align: left; font-size: 12px; }}
  a {{ color: #8ab4f8; }}
  .section {{ margin-top: 12px; font-weight: bold; color: #fdd663; }}
</style>
"""


def _green_dot_icon(size: int = 12) -> QIcon:
    """生成一个绿色圆点图标，用于标记「全程汉化」已开启。

    不依赖外部图片资源：直接画一个抗锯齿圆点。
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor("#34a853"))          # 绿色
    painter.setPen(QPen(QColor("#1e8e3e"), 1))
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return QIcon(pixmap)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, client: GameBananaClient,
                 translator: DeepSeekTranslator | None = None,
                 web_widget_factory=None) -> None:
        super().__init__()
        self.settings = settings
        self.client = client
        self.translator = translator or DeepSeekTranslator(settings)
        self.tasks = TaskRunner(self)
        self._web_widget_factory = web_widget_factory
        # 缩略图线程各自的 Session（requests.Session 非线程安全）
        self._thumb_local = threading.local()

        self.submissions: list[Submission] = []
        self.detail: SubmissionDetail | None = None
        self.page = 1
        self.total = 0
        self.current_game: GameRef | None = None
        self.translated_pages: set[tuple[str, int, int, int | None]] = set()
        self._game_list_loaded = False
        self._section_model = ""
        # 批量切换筛选时置位，避免每个下拉框变化都触发一次搜索
        self._suspend_search = False
        # 请求代次：用于丢弃过期响应
        self._load_generation = 0
        # 当前正在浏览的模组分类（点击「模组分类」条目后进入）
        self._active_category: Submission | None = None
        # 「全程一键汉化」：开启后每次换页自动汉化（状态从设置恢复）
        self.continuous_translation = bool(getattr(settings, "auto_translate_all_pages", False))
        # 正在补译作者说明的条目，避免重复请求
        self._body_translating: set[tuple[str, int]] = set()
        # 作者说明译文缓存（模型, ID）→ 译文：再次打开同一条目时直接复用，
        # 也便于「还原原文」把缓存一并清掉
        self._body_cache: dict[tuple[str, int], str] = {}
        # 用户点过「还原原文」后置位：全程汉化不再自动补译回来
        self._restored = False
        self._download_notice_dismissed = False
        self._download_ui_state = ""
        self._last_download_message = ""
        self._closing_window = False

        self.setWindowTitle(f"GameBanana 中文索引 v{__version__}")
        self.resize(1500, 950)
        self.setStatusBar(QStatusBar())
        self._build_toolbar()
        self._build_body()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(200)
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)
        self.download_progress = QProgressBar()
        self.download_progress.setObjectName("downloadProgress")
        self.download_progress.setMinimumWidth(300)
        self.download_progress.setMaximumWidth(400)
        self.download_progress.setFixedHeight(26)
        self.download_progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.download_progress.setFormat("下载 %p%")
        self.download_progress.setStyleSheet("""
            QProgressBar { background: #172f4d; border: 1px solid #52b5ff;
                border-radius: 5px; color: #ffffff; font-weight: bold; }
            QProgressBar::chunk { background: #2196f3; border-radius: 4px; }
        """)
        self.download_progress.hide()
        self.statusBar().addPermanentWidget(self.download_progress)
        self.cancel_download_button = QPushButton("取消下载")
        self.cancel_download_button.setToolTip("取消当前下载")
        self.cancel_download_button.clicked.connect(self._cancel_download)
        self.cancel_download_button.hide()
        self.statusBar().addPermanentWidget(self.cancel_download_button)
        self.retry_download_button = QPushButton("重试")
        self.retry_download_button.clicked.connect(self._retry_download)
        self.retry_download_button.hide()
        self.statusBar().addPermanentWidget(self.retry_download_button)
        self.close_download_notice_button = QToolButton()
        self.close_download_notice_button.setText("×")
        self.close_download_notice_button.setToolTip("关闭下载提示")
        self.close_download_notice_button.setAccessibleName("关闭下载提示")
        self.close_download_notice_button.setFixedSize(28, 26)
        self.close_download_notice_button.clicked.connect(self._dismiss_download_notice)
        self.close_download_notice_button.hide()
        self.statusBar().addPermanentWidget(self.close_download_notice_button)
        self._apply_style()

        QTimer.singleShot(200, self.load_games)
        QTimer.singleShot(400, self.do_search)

        # 恢复「全程汉化」开关状态（保存过则自动生效，绿色高亮）
        if self.continuous_translation:
            self.translate_all_action.setChecked(True)
            self._style_continuous_action()

        # 以管理员权限运行时给出一次说明：避免用户对着系统弹窗不明所以
        notice = elevation_notice()
        if notice:
            self._mark("检测到提升权限运行")
            QTimer.singleShot(900, lambda: self.statusBar().showMessage(notice, 20000))

    # ---------------------------------------------------------------- 界面搭建
    def _build_toolbar(self) -> None:
        bar = QToolBar("主工具栏")
        bar.setMovable(False)
        self.addToolBar(bar)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索模组、皮肤、地图、音效…（回车开始搜索）")
        self.search_edit.setMinimumWidth(360)
        self.search_edit.returnPressed.connect(self.do_search)
        bar.addWidget(self.search_edit)

        self.search_button = QPushButton("搜索")
        self.search_button.clicked.connect(self.do_search)
        bar.addWidget(self.search_button)

        bar.addSeparator()
        bar.addWidget(QLabel(" 游戏 "))
        self.game_combo = QComboBox()
        self.game_combo.setMinimumWidth(220)
        self.game_combo.addItem("全部游戏", None)
        self.game_combo.currentIndexChanged.connect(self._on_game_changed)
        bar.addWidget(self.game_combo)

        bar.addWidget(QLabel(" 类型 "))
        self.model_combo = QComboBox()
        self.model_combo.addItem("全部类型", "")
        for value, label in (("Mod", "模组"), ("Sound", "音效"), ("Model", "模型"), ("Skin", "皮肤"),
                             ("Map", "地图"), ("Spray", "喷漆"), ("Tool", "工具"), ("Game", "游戏"),
                             ("Project", "项目"), ("Concept", "概念"), ("News", "新闻")):
            self.model_combo.addItem(label, value)
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        bar.addWidget(self.model_combo)

        bar.addWidget(QLabel(" 排序 "))
        self.sort_combo = QComboBox()
        for value, label in SORT_OPTIONS.items():
            self.sort_combo.addItem(label, value)
        self.sort_combo.currentIndexChanged.connect(lambda _=0: self.do_search())
        bar.addWidget(self.sort_combo)

        bar.addSeparator()
        self.translate_action = QAction("本页一键汉化", self)
        self.translate_action.setShortcut(QKeySequence("Ctrl+T"))
        self.translate_action.setToolTip("只汉化当前页条目（Ctrl+T）")
        self.translate_action.triggered.connect(self.translate_current_page)
        bar.addAction(self.translate_action)

        # 全程汉化：开启后每次翻页/切换筛选都会自动汉化，免去逐页点击。
        # 用显式 QToolButton 承载（而不是 addAction 自动生成的按钮），
        # 这样可以直接用样式表把「已开启」画成绿色高亮。
        self.translate_all_action = QAction("全程一键汉化", self)
        self.translate_all_action.setCheckable(True)
        self.translate_all_action.setShortcut(QKeySequence("Ctrl+Shift+T"))
        self.translate_all_action.toggled.connect(self.set_continuous_translation)
        self.translate_all_button = QToolButton()
        self.translate_all_button.setDefaultAction(self.translate_all_action)
        self.translate_all_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        bar.addWidget(self.translate_all_button)

        self.restore_action = QAction("还原原文", self)
        self.restore_action.setShortcut(QKeySequence("Ctrl+R"))
        self.restore_action.triggered.connect(self.restore_originals)
        bar.addAction(self.restore_action)

        self.open_action = QAction("打开站点页", self)
        self.open_action.triggered.connect(self.open_selected_in_web)
        bar.addAction(self.open_action)

        self.download_dir_action = QAction("下载目录", self)
        self.download_dir_action.setToolTip("打开当前下载保存位置（可在设置里修改）")
        self.download_dir_action.triggered.connect(self.open_download_dir)
        bar.addAction(self.download_dir_action)

        bar.addSeparator()
        self.settings_action = QAction("设置", self)
        self.settings_action.triggered.connect(self.open_settings)
        bar.addAction(self.settings_action)

    def _build_body(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左：结果网格
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        self.result_label = QLabel("准备就绪")
        self.result_label.setContentsMargins(10, 6, 10, 0)
        left_layout.addWidget(self.result_label)

        # 按内容分类筛选（搜索结果自带各类型命中数），让「游戏 / 模组 / 音效…」分开看
        self.section_row = QWidget()
        self.section_layout = QHBoxLayout(self.section_row)
        self.section_layout.setContentsMargins(10, 2, 10, 2)
        self.section_layout.setSpacing(6)
        self.section_layout.addStretch(1)
        self.section_row.setVisible(False)
        left_layout.addWidget(self.section_row)
        self._section_buttons: list[QPushButton] = []

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.grid = CardGrid()
        self.grid.card_activated.connect(self.show_detail)
        self.scroll.setWidget(self.grid)
        left_layout.addWidget(self.scroll, 1)

        pager = QHBoxLayout()
        pager.setContentsMargins(10, 0, 10, 8)
        self.prev_button = QPushButton("← 上一页")
        self.prev_button.clicked.connect(lambda: self.turn_page(-1))
        self.next_button = QPushButton("下一页 →")
        self.next_button.clicked.connect(lambda: self.turn_page(1))
        self.page_label = QLabel("第 1 页")
        pager.addWidget(self.prev_button)
        pager.addWidget(self.page_label)
        pager.addWidget(self.next_button)
        pager.addStretch(1)
        left_layout.addLayout(pager)

        splitter.addWidget(left)

        # 右：详情 + 网页浏览
        self.right_tabs = QTabWidget()
        self.right_tabs.setTabsClosable(True)
        self.right_tabs.tabCloseRequested.connect(self._close_right_tab)
        self.detail_view = PartitionDetailView(self)
        self.detail_view.link_activated.connect(self._open_detail_link)
        self.detail_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.right_tabs.addTab(self.detail_view, "条目详情")
        for side in (QTabBar.ButtonPosition.LeftSide, QTabBar.ButtonPosition.RightSide):
            self.right_tabs.tabBar().setTabButton(0, side, None)

        self.web_view = None
        if self._web_widget_factory is not None:
            try:
                self.web_view = self._web_widget_factory(self.settings)
            except Exception as exc:  # noqa: BLE001 - 缺少 WebEngine 时降级为纯索引模式
                self.statusBar().showMessage(f"内嵌网页不可用，已降级为纯索引模式：{exc}")
                self.web_view = None
        if self.web_view is not None:
            self.right_tabs.addTab(self.web_view, "网页浏览 / 整页汉化")
            if hasattr(self.web_view, "download_status"):
                self.web_view.download_status.connect(self._on_download_status)
            if hasattr(self.web_view, "download_progress_changed"):
                self.web_view.download_progress_changed.connect(self._on_download_progress)
                self.web_view.download_state_changed.connect(self._on_download_state)

        splitter.addWidget(self.right_tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.setCentralWidget(splitter)

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #202124; color: #e8eaed; }
            QToolBar { background: #292a2d; border: 0; padding: 6px; spacing: 6px; }
            QLineEdit, QComboBox { background: #303134; border: 1px solid #5f6368; border-radius: 4px;
                                   padding: 4px 8px; color: #e8eaed; }
            QPushButton { background: #3c4043; border: 1px solid #5f6368; border-radius: 4px;
                          padding: 5px 12px; color: #e8eaed; }
            QPushButton:hover { background: #4a4d51; }
            QPushButton:disabled { color: #80868b; }
            QFrame#submissionCard { background: #292a2d; border: 1px solid #3c4043; border-radius: 6px; }
            QFrame#submissionCard:hover { border: 1px solid #8ab4f8; }
            QLabel#cardThumb { background: #1f2023; border-radius: 4px; color: #80868b; }
            QLabel#cardTitle { font-size: 13px; font-weight: bold; color: #ffffff; }
            QLabel#cardOriginal { font-size: 11px; color: #9aa0a6; }
            QLabel#cardMeta { font-size: 11px; color: #bdc1c6; }
            QLabel#cardTags { font-size: 11px; color: #8ab4f8; }
            QLabel#cardStats { font-size: 11px; color: #9aa0a6; }
            QTabWidget::pane { border: 1px solid #3c4043; }
            QTabBar::tab { background: #292a2d; padding: 6px 14px; color: #bdc1c6; }
            QTabBar::tab:selected { background: #3c4043; color: #ffffff; }
            QScrollArea { border: 0; }
            QStatusBar { background: #292a2d; color: #bdc1c6; }
        """)

    # ---------------------------------------------------------------- 数据加载
    def load_games(self) -> None:
        if self._game_list_loaded:
            return

        def work():
            # 分页抓取：单次 per_page 过大时站点会静默返回空列表
            return self.client.list_games(page=1, limit=300)

        def done(games: list[GameRef]) -> None:
            self._game_list_loaded = True
            for game in games:
                self.game_combo.addItem(f"{game.name}（{game.id}）", game)
            self.statusBar().showMessage(f"已载入 {len(games)} 个游戏分类")

        def failed(message: str) -> None:
            self.statusBar().showMessage(f"游戏列表载入失败：{message}（不影响搜索）")

        self.tasks.run(work, done, failed)

    def _search_params(self) -> tuple[str, str, GameRef | None, str, int]:
        keyword = self.search_edit.text().strip()
        model = self.model_combo.currentData() or ""
        game = self.game_combo.currentData()
        sort = self.sort_combo.currentData() or "default"
        if game is not None and model == "Game":
            # 进入游戏专区后类型仍停在「游戏」是自相矛盾的（专区里没有游戏本身），
            # 自动落到「模组」——游戏专区里最常看的内容。
            model = "Mod"
        return keyword, model, game, sort, self.settings.per_page

    def _clear_active_category(self) -> None:
        """用户手动改筛选时退出「分类视图」，避免分类过滤悄悄生效。"""
        if self._active_category is not None:
            self._active_category = None

    def _on_game_changed(self, _index: int = 0) -> None:
        self._clear_active_category()
        self.do_search()

    def _on_model_changed(self, _index: int = 0) -> None:
        self._clear_active_category()
        self.do_search()

    def do_search(self) -> None:
        if self._suspend_search:
            return  # 正在批量切换筛选，等切换完成后统一查询
        keyword, model, game, sort, per_page = self._search_params()
        if game is not None:
            self._active_category = None  # 选了游戏就以游戏专区为准
        self.page = 1
        self._load_page(keyword, model, game, sort, per_page, self.page)

    def turn_page(self, delta: int) -> None:
        """翻页。换页视为重新表达汉化意愿（还原过也继续翻页汉化）。"""
        keyword, model, game, sort, per_page = self._search_params()
        target = self.page + delta
        if target < 1:
            return
        if delta > 0 and self.total and (self.page * per_page) >= self.total:
            self.statusBar().showMessage("已经是最后一页")
            return
        # 确实换页了才算重新表达意愿（无效翻页不应取消「已还原」状态）
        self._restored = False
        self.page = target
        self._load_page(keyword, model, game, sort, per_page, self.page)

    def _load_page(self, keyword: str, model: str, game: GameRef | None, sort: str,
                   per_page: int, page: int) -> None:
        # 请求代次：快速切换筛选时会有多个请求在飞，只有最新一次的结果才能覆盖界面，
        # 否则先发的慢请求返回后会把后发的结果冲掉（表现为分区与内容不匹配）。
        self._load_generation += 1
        generation = self._load_generation
        self._mark("加载索引页", keyword=keyword, model=model or "Mod",
                   game=(game.name if game else ""), page=page)
        self._set_busy(True, "正在抓取…")

        def work():
            if game is not None:
                # 选定游戏后以「游戏专区」为主：返回该专区全部条目（含模组/音效/求助等）。
                # 不再用关键词二次过滤——游戏专区的条目名通常不含游戏名本身，
                # 那样过滤会把整个专区清空。
                return self.client.game_feed(game.id, page=page, per_page=per_page)
            if self._active_category is not None:
                # 模组分类视图：按分类 ID 过滤模组索引
                return self.client.browse_model("Mod", page=page, per_page=per_page,
                                                category_id=self._active_category.id, sort=sort)
            if keyword:
                return self.client.search(keyword, page=page, per_page=per_page, model=model or None)
            return self.client.browse_model(model or "Mod", page=page, per_page=per_page, sort=sort)

        def done(outcome) -> None:
            if generation != self._load_generation:
                return  # 已有更新的请求发出，丢弃这次过期结果
            self.submissions = outcome.submissions
            self.total = outcome.total
            self._section_model = model
            self._update_sections(getattr(outcome, "sections", None) or [])
            self.grid.set_submissions(self.submissions, self.settings.show_original_text,
                                      frame_fetcher=self._request_thumbnail)
            scope = []
            if game is not None:
                scope.append(f"游戏专区「{game.name}」")
            elif keyword:
                scope.append(f"关键词「{keyword}」")
            if model:
                scope.append(f"类型「{model}」")
            note = ""
            if game is not None and keyword:
                note = f"（已忽略关键词「{keyword}」，可清空搜索框回到全站搜索）"
            elif self._active_category is not None:
                note = f"（分类：{self._active_category.name}）"
            self.result_label.setText(
                f"{' · '.join(scope) if scope else '最新条目'}：共 {self.total} 条，"
                f"本页 {len(self.submissions)} 条（第 {self.page} 页）{note}")
            self.page_label.setText(f"第 {self.page} 页")
            self.prev_button.setEnabled(self.page > 1)
            self.next_button.setEnabled(bool(self.total and self.page * per_page < self.total))
            self._set_busy(False, f"已加载 {len(self.submissions)} 条")
            current = self._page_key()
            if current in self.translated_pages:
                self._apply_cached_translations(current)
            # 全程汉化：页面数据就绪后自动翻译本页
            self._auto_translate_if_enabled()

        def failed(message: str) -> None:
            if generation != self._load_generation:
                return  # 过期请求的失败不应覆盖当前界面状态
            self._set_busy(False, "加载失败")
            QMessageBox.warning(self, "抓取失败", f"{message}\n\n请检查网络连接后重试。")

        self.tasks.run(work, done, failed)

    def _page_key(self) -> tuple[str, int, int, int | None]:
        keyword, model, game, sort, _ = self._search_params()
        return keyword, self.page, self.settings.per_page, (game.id if game else None)

    def _request_thumbnail(self, card) -> None:
        """异步拉取缩略图。

        * 提交到固定大小线程池（4 个常驻线程），不再为每张图新建 QThread；
        * 每个工作线程使用自己的 requests.Session：requests 明确说明
          Session 非线程安全，共用连接池曾在崩溃报告的线程栈中出现；
        * 结果**不写入磁盘缓存**（体积大、命中率低），仅经 Qt 图片对象展示；
        * 工作线程只取字节，QPixmap 解码留在主线程，避免 Qt 图形对象跨线程。
        """
        url = card.submission.thumbnail
        if not url:
            card.set_thumbnail_failed()
            return
        timeout = self.settings.request_timeout_seconds

        def work():
            session = self._thumbnail_session()
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return response.content

        def done(data: bytes) -> None:
            card.set_thumbnail(data)

        def failed(_message: str) -> None:
            card.set_thumbnail_failed()

        self.tasks.submit(work, done, failed)

    def _thumbnail_session(self):
        """返回当前线程专属的 Session（首次使用时创建并复用连接池）。"""
        session = getattr(self._thumb_local, "session", None)
        if session is None:
            session = requests.Session()
            # 继承主会话的请求头与站点 Cookie，保证登录态一致
            session.headers.update(dict(self.client.session.headers))
            try:
                session.cookies.update(self.client.session.cookies)
            except Exception:  # noqa: BLE001 - Cookie 复制失败不应中断下载
                pass
            self._thumb_local.session = session
        return session

    def _update_sections(self, sections: list[dict[str, Any]]) -> None:
        """刷新「按分类查看」按钮行。

        搜索结果自带各类型的命中数（例如：游戏 39、模组 170、音效 12），
        点击即可只保留该类型，实现分类区别展示。
        """
        for button in self._section_buttons:
            button.setParent(None)
            button.deleteLater()
        self._section_buttons.clear()

        useful = [section for section in sections if section.get("count")]
        useful.sort(key=lambda item: item["count"], reverse=True)
        if not useful:
            self.section_row.setVisible(False)
            return

        label = QLabel("按分类查看：")
        label.setStyleSheet("color: #9aa0a6; font-size: 12px;")
        self.section_layout.insertWidget(0, label)
        self._section_buttons.append(label)  # 复用同一批清理逻辑

        def add_button(text: str, action, checked: bool, highlight: bool = False) -> None:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setChecked(checked)
            style = "QPushButton { font-size: 12px; padding: 3px 10px; }"
            if highlight:
                style += ("QPushButton { background: #3d5a80; border: 1px solid #8ab4f8; }"
                          "QPushButton:hover { background: #4a6d99; }")
            button.setStyleSheet(style)
            button.clicked.connect(lambda _=False, fn=action: fn())
            self.section_layout.insertWidget(len(self._section_buttons), button)
            self._section_buttons.append(button)

        # 游戏专区单独置顶：搜索「游戏名」时最容易混进成员/百科/工作室等无关类型，
        # 这里给一个显眼入口直接看游戏。
        games = next((item for item in useful if item["model"] == "Game"), None)
        if games:
            add_button(f"🎮 游戏专区（{games['count']}）",
                       lambda: self._apply_section("Game"),
                       self._section_model == "Game", highlight=True)

        total = sum(section["count"] for section in useful)
        add_button(f"全部类型（{total}）", lambda: self._apply_section(""), not self._section_model)

        # 其余分区按钮：点击即浏览该分区（成员/工作室/社团这类没有关键词搜索的分区
        # 也能通过自己的索引查看，而不是像以前那样强行拿去搜关键词得到空结果）。
        for section in useful[:12]:
            if section["model"] == "Game":
                continue
            model = section["model"]
            label = section["label"]
            searchable = model in SEARCHABLE_MODELS
            if searchable:
                action = (lambda m=model: self._apply_section(m))
            else:
                action = (lambda m=model, n=label: self.browse_partition(m, n))
            add_button(f"{label}（{section['count']}）", action,
                       self._section_model == model)
        self.section_row.setVisible(True)

    def _apply_section(self, model: str) -> None:
        """把顶部「类型」下拉框切到指定分类并重新搜索。"""
        position = 0
        for index in range(self.model_combo.count()):
            if (self.model_combo.itemData(index) or "") == model:
                position = index
                break
        if position == self.model_combo.currentIndex():
            self.do_search()
        else:
            self.model_combo.setCurrentIndex(position)

    # ---------------------------------------------------------------- 详情
    def show_detail(self, submission: Submission) -> None:
        """读取任意分区条目的详情；分类类分区在详情之后继续下钻到其内容。"""
        noun = partition_of(submission.model)["noun"]
        self._mark("加载条目详情", name=submission.name, model=submission.model,
                   id=submission.id)
        self._set_busy(True, f"正在读取{noun}详情…")

        def work():
            return self.client.partition_detail(submission)

        def done(detail: SubmissionDetail) -> None:
            self.detail = detail
            self._detail_hero = 0          # 主图索引：切换缩略图时重渲染
            self.detail_view.media_bytes.clear()
            self._render_detail()
            self.right_tabs.setCurrentIndex(0)
            noun_text = partition_of(detail.submission.model)["noun"]
            # 分类类分区（游戏 / 模组分类…）：继续下钻到该分类下的真实内容。
            # 注意：下钻只切换左侧列表，详情面板保持本次渲染的内容，
            # 这样用户仍能看到该分类的归属与简介（此前会被专区说明覆盖）。
            if detail.kind == "category" and detail.drilldown:
                self._drill_into_category(detail)
            self._set_busy(False, f"已载入{noun_text}「{detail.submission.name}」详情")
            # 相册图片随后台加载完成再补进面板（渲染期不做网络访问）
            self._load_detail_media(detail)
            # 已在汉化模式下：补译本条目作者说明（先点汉化、后点开条目也能汉化）
            self._maybe_translate_detail_body(detail)

        def failed(message: str) -> None:
            self._set_busy(False, "详情加载失败")
            noun_text = partition_of(submission.model)["noun"]
            self.statusBar().showMessage(f"{noun_text}详情加载失败：{message}")
            if message.startswith('LoginRequiredError:'):
                self._prompt_site_access(submission, login=True)
            elif message.startswith('SiteVerificationRequiredError:'):
                self._prompt_site_access(submission, login=False)

        self.tasks.run(work, done, failed)

    def _prompt_site_access(self, submission: Submission, *, login: bool) -> None:
        if self._closing_window or getattr(self, '_site_access_prompt_open', False):
            return
        self._site_access_prompt_open = True
        try:
            title = '需要登录 GameBanana' if login else '需要完成网页验证'
            text = ('站点明确要求登录，或当前登录状态已失效。\n'
                    '请在软件的内嵌网页中登录 GameBanana，随后返回条目重新查看详情。'
                    if login else
                    '站点要求浏览器验证，并不代表你没有登录。\n'
                    '请在内嵌网页完成验证，随后返回条目重新查看详情。')
            if self.web_view is None:
                QMessageBox.information(self, title, text + '\n\n当前未启用内嵌浏览器，请退出安全模式后重新启动软件。')
                return
            dialog = QMessageBox(QMessageBox.Icon.Information, title, text, parent=self)
            open_button = dialog.addButton('网页登录' if login else '打开验证页', QMessageBox.ButtonRole.AcceptRole)
            dialog.addButton('稍后', QMessageBox.ButtonRole.RejectRole)
            dialog.exec()
            if dialog.clickedButton() is open_button:
                self.open_url_in_web('https://gamebanana.com/members/account/login' if login
                                     else submission.profile_url or f'https://gamebanana.com/mods/{submission.id}')
        finally:
            self._site_access_prompt_open = False

    def _drill_into_category(self, detail: SubmissionDetail) -> None:
        """按分类详情给出的方式下钻：游戏用子流，模组分类用索引过滤器。"""
        item = detail.submission
        # 模组分类：切到模组分区并按该分类过滤
        if detail.drilldown == "category":
            self._active_category = item
            self._mark("进入分类", category=item.name, id=item.id)
            self._suspend_search = True
            try:
                self.game_combo.setCurrentIndex(0)
                self._select_model("Mod")
            finally:
                self._suspend_search = False
            self.statusBar().showMessage(f"正在浏览分类「{item.name}」下的模组…")
            self.do_search()
            return

        # 游戏：切到该游戏专区
        self.open_game(item)

    def _select_model(self, model: str) -> bool:
        for index in range(self.model_combo.count()):
            if (self.model_combo.itemData(index) or "") == model:
                self.model_combo.setCurrentIndex(index)
                return True
        return False

    def browse_partition(self, model: str, name: str = "") -> None:
        """浏览某个分区的内容（把「类型」切到该分区并重新查询）。

        适用于所有分区：模组/音效/模型… 以及成员/工作室/社团这类
        「本身不是内容、但有自己索引」的分区。
        """
        label = partition_of(model)["label"]
        self._mark("浏览分区", model=model, name=name)

        position = None
        for index in range(self.model_combo.count()):
            if (self.model_combo.itemData(index) or "") == model:
                position = index
                break
        if position is None:
            # 下拉框没登记该分区时临时补一项，保证任何分区都能浏览
            self.model_combo.addItem(label, model)
            position = self.model_combo.count() - 1

        # 分区浏览不再叠加游戏筛选（否则会把范围锁死在某款游戏里）
        self._suspend_search = True
        try:
            self.game_combo.setCurrentIndex(0)
            if self.model_combo.currentIndex() == position:
                pass
            else:
                self.model_combo.setCurrentIndex(position)
        finally:
            self._suspend_search = False
        self.statusBar().showMessage(f"正在浏览「{label}」分区…")
        self.do_search()

    def open_game(self, game: Submission) -> None:
        """把列表切到某个游戏专区（详情展示由通用分区详情负责）。"""
        self._mark("进入游戏专区", game=game.name, id=game.id)

        target = None
        for position in range(self.game_combo.count()):
            data = self.game_combo.itemData(position)
            if data is not None and getattr(data, "id", None) == game.id:
                target = position
                break

        self._suspend_search = True
        try:
            if target is None:
                ref = GameRef(id=game.id, name=game.name, profile_url=game.profile_url)
                self.game_combo.addItem(f"{game.name}（{game.id}）", ref)
                target = self.game_combo.count() - 1
                self._game_list_loaded = True
            self.model_combo.setCurrentIndex(0)
            self.game_combo.setCurrentIndex(target)
        finally:
            self._suspend_search = False

        self.statusBar().showMessage(f"进入游戏专区「{game.name}」")
        self.do_search()

    def open_game_by_id(self, game_id: int, name: str = "") -> None:
        """按 ID 进入游戏专区（用于详情面板里的按钮）。"""
        self.open_game(Submission(id=game_id, model="Game", name=name or f"游戏 {game_id}",
                                  profile_url=f"{SITE_BASE}/games/{game_id}"))

    def _render_detail(self) -> None:
        """按当前主图索引重新渲染详情面板。"""
        if self.detail is None:
            return
        self.detail_view.set_html(self._detail_html(self.detail))

    def _load_detail_media(self, detail: SubmissionDetail) -> None:
        """后台取回相册图片字节；取完再补进面板。

        渲染期不发起网络请求，避免在 QTextDocument 布局过程中回调引发的竞态
        （实测会导致原生访问违例）。这里只下载字节并以 data: URI 内联。
        """
        media = [item for item in (detail.media or []) if item.best("medium")]
        if not media:
            return

        # 按面板宽度预缩放：主图铺满可用宽度、缩略图固定小尺寸。
        # QTextDocument 不支持百分比宽度，必须在取图时就缩放好。
        panel_width = max(240, self.detail_view.browser.viewport().width() - 24)
        hero_width = min(panel_width, 720)
        thumb_width, thumb_height = 72, 42
        hero_height = int(hero_width * 0.75)

        def work() -> list[tuple[str, bytes]]:
            session = self._thumbnail_session()
            collected: list[tuple[str, bytes]] = []
            for item in media:
                for url in (item.best("large"), item.best("medium"), item.best("thumb")):
                    if not url or url in self.detail_view.media_bytes:
                        continue
                    try:
                        response = session.get(url, timeout=self.settings.request_timeout_seconds)
                        response.raise_for_status()
                        collected.append((url, response.content))
                        break  # 该图取到一个尺寸即可
                    except Exception:  # noqa: BLE001 - 单张失败不影响其余
                        continue
            return collected

        def done(pairs: list[tuple[str, bytes]]) -> None:
            if self.detail is not detail:
                return  # 期间已切换到别的条目
            for url, payload in pairs:
                self.detail_view.media_bytes[url] = payload
            if pairs:
                self._render_detail()

        self.tasks.submit(work, done, lambda _message: None)

    def _select_hero_image(self, index: int) -> None:
        """切换预览主图（点击缩略图或左右切换）。"""
        media = getattr(self.detail, "media", None) or []
        if not media:
            return
        self._detail_hero = max(0, min(index, len(media) - 1))
        self._render_detail()

    def _gallery_html(self, detail: SubmissionDetail) -> str:
        """官方网页式相册排版：上方一张主图 + 下方缩略图条 + 计数与切换。

        图片以 data: URI 内联并**预先缩放到面板宽度**：QTextDocument 不支持
        `width="100%"`、CSS `max-width` 也不可靠，若不预缩放，一张 1920px 宽的
        预览图会把整个详情面板撑满并盖住其它区块（已实测）。
        """
        media = [item for item in (detail.media or []) if item.best("medium")]
        if not media:
            return ""
        hero_index = max(0, min(getattr(self, "_detail_hero", 0), len(media) - 1))
        hero = media[hero_index]
        cached = self.detail_view.media_bytes

        panel_width = max(240, self.detail_view.browser.viewport().width() - 24)
        hero_width = min(panel_width, 720)
        thumb_width, thumb_height = 72, 42

        def inline(item, width: int, height: int = 0) -> str:
            for candidate in (item.best("large"), item.best("medium"), item.best("thumb")):
                if candidate and candidate in cached:
                    return scaled_data_uri(cached[candidate], candidate, width, height)
            return ""

        # 主图：按可用宽度等比缩放，限制高度避免竖图占满整屏
        hero_src = ""
        for candidate in (hero.best("large"), hero.best("medium"), hero.best("thumb")):
            if candidate and candidate in cached:
                hero_src = scaled_data_uri(cached[candidate], candidate,
                                           hero_width, int(hero_width * 0.9))
                break
        original = hero.original or hero.best("large") or hero.best("medium")

        parts = ['<div class="section">预览图</div>']
        parts.append('<div class="hero">')
        if hero_src:
            parts.append(f'<a href="{original}"><img src="{hero_src}" width="{hero_width}"></a>')
        else:
            parts.append('<div class="meta">（图片加载中…）</div>')
        parts.append("</div>")
        if hero.caption:
            parts.append(f'<div class="meta">{hero.caption}</div>')

        total = len(media)
        nav = [f'<div class="meta">第 {hero_index + 1} / {total} 张']
        if total > 1:
            nav.append(f'<a href="img:{(hero_index - 1) % total}">← 上一张</a>')
            nav.append(f'<a href="img:{(hero_index + 1) % total}">下一张 →</a>')
        nav.append(f'<a href="{original}">查看原图</a>')
        nav.append("</div>")
        parts.append("　".join(nav))

        if total > 1:
            parts.append('<table class="thumbs"><tr>')
            for index, item in enumerate(media):
                thumb_src = inline(item, thumb_width, thumb_height)
                mark = ' class="current"' if index == hero_index else ""
                if thumb_src:
                    parts.append(f'<td{mark}><a href="img:{index}">'
                                 f'<img src="{thumb_src}" width="{thumb_width}" '
                                 f'height="{thumb_height}"></a></td>')
                else:
                    parts.append(f'<td{mark}><a href="img:{index}">[图 {index + 1}]</a></td>')
            parts.append("</tr></table>")
        return "".join(parts)

    def _body_html(self, detail: SubmissionDetail) -> str:
        """作者说明：原文 + 可选译文对照（与站点上的双语排版一致）。"""
        body = detail.body or ""
        translated = detail.body_translation or ""
        if not body and not translated:
            return ""
        parts = ['<div class="section">作者说明</div>']
        if body:
            parts.append(f'<div class="body">{sanitize_html(body)}</div>')
        if translated:
            parts.append('<div class="meta">中文对照（DeepSeek 翻译）</div>')
            parts.append(f'<div class="body translated">{sanitize_html(translated)}</div>')
        return "".join(parts)

    def _detail_html(self, detail: SubmissionDetail) -> str:
        """通用详情渲染：按分区类型（内容/个人/分类）决定展示哪些区块。"""
        item = detail.submission
        info = partition_of(item.model)
        kind = detail.kind or info["kind"]

        title = item.display("name")
        html = [DETAIL_HTML, f"<h2>{title}</h2>"]
        if title != item.name:
            html.append(f'<div class="orig">原文：{item.name}</div>')

        # 分区标识 + 基本时间
        head = [f"分区：{info['label']}"]
        if item.date_added:
            head.append(f"发布：{item.date_added}")
        if item.date_updated:
            head.append(f"更新：{item.date_updated}")
        if item.version:
            head.append(f"版本：{item.version}")
        html.append(f'<div class="meta">{" · ".join(head)}</div>')

        # 统计（各分区有的字段不同，统一按存在的展示）
        stats = [f"{label} {value}" for label, value in detail.stats]
        if stats:
            html.append(f'<div class="meta">{" · ".join(stats)}</div>')
        elif item.views or item.likes or item.posts:
            fallback = []
            if item.views:
                fallback.append(f"浏览 {item.views}")
            if item.likes:
                fallback.append(f"点赞 {item.likes}")
            if item.posts:
                fallback.append(f"评论 {item.posts}")
            html.append(f'<div class="meta">{" · ".join(fallback)}</div>')

        # 归属关系（游戏/分类/作者）
        for label, value in detail.origin:
            html.append(f'<div class="meta">{label}：{value}</div>')

        tags = [item.display_tag(i) for i in range(len(item.tags))]
        tags = [tag for tag in tags if tag]
        if tags:
            html.append('<div class="tag">' + "　".join(f"#{tag}" for tag in tags) + "</div>")

        if item.profile_url:
            html.append(f'<div class="meta">站点页：<a href="{item.profile_url}">'
                        f'{item.profile_url}</a></div>')

        # 预览图（官方式主图 + 缩略图条）与作者正文（原文 + 中文对照）
        html.append(self._gallery_html(detail))
        html.append(self._body_html(detail))

        if detail.requirements:
            html.append('<div class="section">前置要求</div>')
            html.append("<ul>" + "".join(f"<li>{req}</li>" for req in detail.requirements) + "</ul>")

        # 文件仅在「内容」类分区展示（成员/工作室没有文件）
        if detail.files and kind == "content":
            html.append('<div class="section">下载文件</div>')
            html.append("<table><tr><th>文件名</th><th>大小</th><th>安全检测</th><th>下载</th></tr>")
            for file in detail.files:
                safe = {"clean": "通过", "ok": "通过"}.get(file.av_result, file.av_result or "—")
                url = html_module.escape(file.download_url, quote=True)
                direct = (f'<a href="download:{file.id}">直接下载</a>'
                          if file.download_url else '暂无直链')
                html.append(f'<tr><td>{html_module.escape(file.filename)}</td>'
                            f'<td>{_human_size(file.filesize)}</td><td>{safe}</td>'
                            f'<td><a href="{url}">跳转下载页</a>　{direct}</td></tr>')
            html.append("</table>")

        if detail.credits:
            html.append('<div class="section">致谢与参与</div>')
            html.append("<ul>" + "".join(f"<li>{credit}</li>" for credit in detail.credits) + "</ul>")

        videos = video_links(detail.embedded_media)
        if videos:
            html.append('<div class="section">作者附带的视频</div>')
            for label, url in videos[:6]:
                html.append(f'<div class="meta">▶ <a href="{url}">{label}：{url}</a></div>')

        # 下钻提示：告诉用户这个分区里还有多少内容、可以怎么继续看
        html.append('<div class="section">继续浏览</div>')
        if kind == "category" and detail.drilldown:
            hint = detail.drilldown_label or "已在左侧列出该分类下的内容"
            html.append(f'<div class="meta">{hint}</div>')
            if item.model == "ModCategory":
                html.append('<div class="meta">已按该分类过滤模组列表；'
                            '用顶部「游戏」可进一步限定到某款游戏。</div>')
            else:
                html.append('<div class="meta">「类型」可按模组/音效/模型等继续筛选。</div>')
        else:
            total = f"共 {detail.partition_total} 条" if detail.partition_total else "内容列表"
            html.append(f'<div class="meta">{info["label"]}分区{total}：'
                        f'把左侧「类型」切到「{info["label"]}」即可浏览同类条目。</div>')
        # 相册本身已在上方完整展示，这里不再重复「预览图 N 张」的提示
        return "".join(html)

    def open_selected_in_web(self) -> None:
        url = None
        if self.detail is not None:
            url = self.detail.submission.profile_url
        elif self.submissions:
            url = self.submissions[0].profile_url
        if not url:
            self.statusBar().showMessage("请先选择条目")
            return
        self._mark("打开内嵌网页", url=url)
        if self.web_view is None:
            # 没有内嵌浏览器时才退回外部程序；以管理员权限运行可能被系统拦下，
            # 因此先说明清楚，让用户自己决定
            extra = ("\n\n提示：当前以管理员权限运行，用外部浏览器打开时 Windows 可能"
                     "提示「现有实例正在以提升的权限运行」。") if elevation_notice() else ""
            box = QMessageBox(self)
            box.setWindowTitle("网页浏览不可用")
            box.setText("当前环境未启用内嵌浏览器，无法在软件内打开站点页。")
            box.setInformativeText(f"链接：{url}{extra}")
            open_button = box.addButton("用系统浏览器打开", QMessageBox.ButtonRole.AcceptRole)
            copy_button = box.addButton("复制链接", QMessageBox.ButtonRole.ActionRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is open_button:
                QDesktopServices.openUrl(QUrl(url))
            elif box.clickedButton() is copy_button:
                QGuiApplication.clipboard().setText(url)
                self.statusBar().showMessage("链接已复制到剪贴板")
            return
        self.web_view.load_url(url)
        self._show_web_tab()

    # ---------------------------------------------------------------- 翻译
    def translate_current_page(self) -> None:
        """本页一键汉化（GUI 槽）：缺少前置条件时用对话框提示用户。"""
        blocker = self.translation_blocker()
        if blocker:
            if blocker == "no_key":
                QMessageBox.information(self, "需要配置密钥",
                                        "请先在「设置」里填写 DeepSeek API 密钥，然后再使用一键汉化。")
                self.open_settings()
            else:
                self.statusBar().showMessage("当前没有可翻译的条目")
            return
        self.start_page_translation()

    def set_continuous_translation(self, enabled: bool) -> None:
        """开启/关闭「全程一键汉化」。

        开启时立即汉化当前页，之后每次换页（翻页、改筛选、进专区）都会自动汉化。
        状态写入设置，下次启动仍生效；按钮以绿色高亮表示已开启。
        """
        self.continuous_translation = bool(enabled)
        self._style_continuous_action()
        self.settings.auto_translate_all_pages = bool(enabled)
        try:
            save_settings(self.settings)
        except Exception:  # noqa: BLE001 - 设置保存失败不应影响本次会话
            pass

        if not enabled:
            self.statusBar().showMessage("已关闭全程汉化")
            return

        if not self.settings.is_translation_ready():
            # 无密钥时不弹模态框（会打断用户操作），只提示并引导去设置
            self.statusBar().showMessage(
                "已开启全程汉化，但尚未配置 DeepSeek 密钥；点「设置」填写后会自动开始")
            return
        self.statusBar().showMessage("已开启全程汉化：每次换页都会自动汉化")
        # 用户主动开启汉化：清掉「已还原」状态
        self._restored = False
        self.translate_current_page()

    def _style_continuous_action(self) -> None:
        """全程汉化开启时用绿色高亮，便于一眼看出当前状态。

        通过样式表的 :checked 伪类实现，开启时显示为实心绿底白字，
        关闭时恢复工具栏默认外观。
        """
        action = self.translate_all_action
        button = getattr(self, "translate_all_button", None)
        if self.continuous_translation:
            action.setIcon(_green_dot_icon())
            action.setText("全程汉化中")
            action.setToolTip("正在自动汉化每一页；再次点击关闭（Ctrl+Shift+T）")
            if button is not None:
                button.setStyleSheet(
                    "QToolButton { background: #1e8e3e; color: #ffffff;"
                    " border: 1px solid #34a853; border-radius: 4px;"
                    " padding: 4px 12px; font-weight: bold; }"
                    "QToolButton:hover { background: #23a047; }")
        else:
            action.setIcon(QIcon())
            action.setText("全程一键汉化")
            action.setToolTip("开启后每次换页都会自动汉化（Ctrl+Shift+T）；再次点击关闭")
            if button is not None:
                button.setStyleSheet("")

    def _auto_translate_if_enabled(self) -> None:
        """页面加载完成后：若开启了全程汉化则自动翻译本页。"""
        if not self.continuous_translation or not self.submissions:
            return
        if not self.settings.is_translation_ready():
            return  # 未配置密钥时静默等待，不反复弹窗
        key = self._page_key()
        if key in self.translated_pages:
            self._apply_cached_translations(key)
            return
        self.start_page_translation()

    def translation_blocker(self) -> str | None:
        """返回无法汉化的原因（"no_entries"/"no_key"），可汉化时返回 None。

        与 GUI 提示分离，便于在无显示器环境下自检。
        """
        if not self.submissions:
            return "no_entries"
        if not self.settings.is_translation_ready():
            return "no_key"
        return None

    def start_page_translation(self) -> None:
        """实际发起当前页字段汉化；前置条件由调用方保证。"""
        if not self.settings.is_translation_ready():
            self.statusBar().showMessage("未配置 DeepSeek API 密钥，请在「设置」里填写后再汉化")
            return
        texts, slots = collect_translatable(self.submissions)
        # 详情里的作者说明也一并汉化（站点本身是原文，用户需要中文对照）
        body_text = self._detail_body_source()
        if body_text:
            texts.append(body_text)
        if not texts:
            self.statusBar().showMessage("当前条目没有需要翻译的字段")
            return
        self._mark("汉化索引页字段", fields=len(texts))
        self._set_busy(True, f"正在汉化 {len(texts)} 项（含作者说明）…")
        # 用户主动要求汉化：清掉「已还原」状态，允许重新补译正文
        self._restored = False
        # 先登记本页已汉化：这样在批次进行中打开条目时，
        # _maybe_translate_detail_body 会认为「用户已表达汉化意愿」并补译正文
        self.translated_pages.add(self._page_key())

        def work():
            return self.translator.translate_many(texts)

        def done(translated: list[str]) -> None:
            body_value = translated[len(slots):] if body_text else []
            for (index, field_name), value in zip(slots, translated):
                self.submissions[index].translations[field_name] = value
            # 作者说明的译文回填到详情并重渲染（原文保留在上方做对照）
            if body_value and self.detail is not None:
                self.detail.body_translation = body_value[0]
                self._render_detail()
            self.translated_pages.add(self._page_key())
            for card in self.grid.cards:
                card.set_translated(card.submission.translations, self.settings.show_original_text)
            stats = self.translator.last_stats.summary()
            self._set_busy(False, f"汉化完成：{stats}")
            if self.web_view is not None:
                self.web_view.set_translations({self._slot_key(index, field): value
                                                for (index, field), value in zip(slots, translated)})

        def failed(message: str) -> None:
            self._set_busy(False, "汉化失败")
            QMessageBox.warning(self, "汉化失败",
                                f"{message}\n\n已翻译内容不会写入缓存，可稍后重试。")

        self.tasks.run(work, done, failed)

    @staticmethod
    def _slot_key(index: int, field_name: str) -> str:
        return f"{index}:{field_name}"

    def _detail_body_source(self) -> str:
        """取作者说明的纯文本用于翻译（HTML 标签不进模型，避免被译坏）。"""
        if self.detail is None or not self.detail.body:
            return ""
        if self.detail.body_translation:
            return ""  # 已有译文，不重复计费
        text = re.sub(r"<[^>]+>", " ", str(self.detail.body))
        text = html_module.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 4 or is_chinese(text):
            return ""  # 太短或已是中文，无需翻译
        return text

    def _maybe_translate_detail_body(self, detail: SubmissionDetail) -> None:
        """详情载入后补译作者说明，并优先复用缓存译文。

        为什么需要单独一趟：作者说明属于「点开哪个条目才知道内容」的数据，
        而整页汉化只在本页字段就绪时执行一次。若用户先点汉化、再点开条目，
        正文就永远不会被翻译（实测就是这样漏掉的）。因此这里在详情就绪后
        按需补译一次，且仅在用户已表达过汉化意愿时触发，避免意外计费。
        """
        token = self._detail_token(detail)
        # 已翻译过的条目：直接回填缓存，避免重复计费
        cached = self._body_cache.get(token)
        if cached and not detail.body_translation:
            detail.body_translation = cached
            self._render_detail()
            return
        if detail.body_translation:
            return
        if self._restored:
            return  # 用户刚点过「还原原文」，不再自动补译
        interested = self.continuous_translation or self._page_key() in self.translated_pages
        if not interested or not self.settings.is_translation_ready():
            return
        text = self._detail_body_source()
        if not text:
            return
        # 并发保护：同一个条目只补译一次
        if token in self._body_translating:
            return
        self._body_translating.add(token)

        def work():
            return self.translator.translate_many([text])

        def done(translated: list[str]) -> None:
            self._body_translating.discard(token)
            if self.detail is not detail or not translated:
                return
            value = translated[0]
            if not value or value.strip() == text.strip():
                return  # 模型原样返回：不写入，避免显示「译文=原文」的重复块
            # 请求期间用户点了「还原原文」：只留缓存，不写回界面
            self._body_cache[token] = value
            if self._restored:
                return
            detail.body_translation = value
            self._render_detail()
            self.statusBar().showMessage("已补译作者说明")

        def failed(message: str) -> None:
            self._body_translating.discard(token)
            self.statusBar().showMessage(f"作者说明翻译失败：{message}")

        self.tasks.submit(work, done, failed)

    def restore_originals(self) -> None:
        """还原为原文显示：卡片字段、详情正文、内嵌网页一起还原。"""
        for submission in self.submissions:
            submission.translations.clear()
        for card in self.grid.cards:
            card.set_translated({}, self.settings.show_original_text)
        # 详情里的作者说明也是译文，必须一起还原（此前漏掉，表现为「还原无效」）
        detail = self.detail
        if detail is not None:
            detail.body_translation = ""
            self._body_cache.pop(self._detail_token(detail), None)
            self._render_detail()
        # 记住用户已还原：全程汉化模式下不再自动补译回来，
        # 直到用户下次主动点汉化或换页才算重新表达意愿
        self._restored = True
        if self.web_view is not None:
            self.web_view.restore_original()
        has_body = detail is not None and bool(detail.body)
        self.statusBar().showMessage(
            "已还原为原文显示（含作者说明）" if has_body else "已还原为原文显示")

    def _detail_token(self, detail: SubmissionDetail) -> tuple[str, int]:
        return (detail.submission.model, detail.submission.id)

    def _apply_cached_translations(self, _key) -> None:
        for card in self.grid.cards:
            card.set_translated(card.submission.translations, self.settings.show_original_text)

    def _open_detail_link(self, url: str) -> None:
        """相册留在软件内；网页和下载优先使用内嵌浏览器。"""
        if not url:
            return
        if url.startswith("download:"):
            self._download_detail_file(url[9:])
            return
        # 相册切换：img:<索引>
        if url.startswith("img:"):
            try:
                self._select_hero_image(int(url[4:]))
            except ValueError:
                pass
            return
        lowered = url.lower()
        # 图片链接：用内置查看器打开，不调起外部程序。
        # 以管理员权限运行时，调起普通权限的浏览器/看图程序会被 Windows 拦住
        # （「现有实例正在以提升的权限运行」），内置查看可完全避免该问题。
        if self._is_image_url(lowered) or url.startswith("data:image/"):
            self.view_image(url)
            return
        host = QUrl(url).host().lower()
        if lowered.startswith(("http://", "https://")) and (
                self.web_view is not None or host == "gamebanana.com"
                or host.endswith(".gamebanana.com")):
            self.open_url_in_web(url)
            return
        QDesktopServices.openUrl(QUrl(url))

    def _download_detail_file(self, file_id: str) -> None:
        """按当前详情的文件 ID 下载，保持条目详情与网页地址不变。"""
        try:
            selected_id = int(file_id)
        except ValueError:
            self.statusBar().showMessage("下载链接已失效，请重新打开条目详情。")
            return
        file = next((item for item in (self.detail.files if self.detail else [])
                     if item.id == selected_id), None)
        if file is None or not file.download_url:
            self.statusBar().showMessage("未找到该文件的下载地址，请使用「跳转下载页」。")
            return
        if self.web_view is None:
            self.statusBar().showMessage("直接下载需要正常模式启动软件，请关闭安全模式后重试。")
            return
        self._mark("详情直接下载", file_id=file.id, filename=file.filename)
        self.web_view.download_url(file.download_url)

    @staticmethod
    def _is_image_url(url: str) -> bool:
        path = url.split("?")[0]
        return path.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"))

    def view_image(self, url: str) -> None:
        """在内置查看器中打开预览原图（支持相册翻页）。"""
        from .image_viewer import ImageViewer

        viewer = getattr(self, "_image_viewer", None)
        if viewer is None:
            viewer = ImageViewer(self)
            self._image_viewer = viewer
        gallery = [item.original or item.best("large") or item.best("medium")
                   for item in (self.detail.media if self.detail else [])
                   if item.best("medium")]
        viewer.load_url(url, self._thumbnail_session, title="", gallery=gallery)
        viewer.show()
        viewer.raise_()
        viewer.activateWindow()

    def open_url_in_web(self, url: str) -> None:
        if self.web_view is None:
            # 没有内嵌浏览器时才退回外部程序（可能因权限提升而受限）
            QDesktopServices.openUrl(QUrl(url))
            return
        self.web_view.load_url(url)
        self._show_web_tab()

    def _close_right_tab(self, index: int) -> None:
        if self.web_view is not None and self.right_tabs.widget(index) is self.web_view:
            # 关闭浏览标签，保留下载与 Cookie 所属的 Profile，避免中断文件保存。
            if hasattr(self.web_view, "view"):
                self.web_view.view.stop()
            self.right_tabs.removeTab(index)
            self.right_tabs.setCurrentWidget(self.detail_view)

    def _show_web_tab(self) -> None:
        if self.web_view is None:
            return
        if self.right_tabs.indexOf(self.web_view) < 0:
            self.right_tabs.addTab(self.web_view, "网页浏览 / 整页汉化")
        self.right_tabs.setCurrentWidget(self.web_view)

    def _on_download_progress(self, received: int, total: int, name: str) -> None:
        if self._download_notice_dismissed:
            return
        self.download_progress.show()
        self.download_progress.setToolTip(f"{name}：{_human_size(received)} / {_human_size(total)}")
        if total > 0:
            self.download_progress.setRange(0, 100)
            self.download_progress.setValue(min(100, int(received * 100 / total)))
            self.download_progress.setFormat("下载 %p%")
        else:
            self.download_progress.setRange(0, 0)
            self.download_progress.setFormat("下载中…")

    def _on_download_state(self, state: str, name: str) -> None:
        active = state in {"DownloadRequested", "DownloadInProgress"}
        if active:
            self._download_notice_dismissed = False
        elif self._download_notice_dismissed:
            return
        self._download_ui_state = state
        self.download_progress.show()
        self.download_progress.setToolTip(name)
        self.cancel_download_button.setVisible(active)
        self.retry_download_button.setVisible(state in {"DownloadCancelled", "DownloadInterrupted"})
        self.close_download_notice_button.setVisible(
            state in {"DownloadCompleted", "DownloadCancelled", "DownloadInterrupted"})
        if active:
            self.download_progress.setRange(0, 0)
            self.download_progress.setFormat("准备下载…" if state == "DownloadRequested" else "下载中…")
        else:
            self.download_progress.setRange(0, 100)
            self.download_progress.setValue(100 if state == "DownloadCompleted" else 0)
            self.download_progress.setFormat({"DownloadCompleted": "下载完成 100%",
                                              "DownloadCancelled": "已取消下载",
                                              "DownloadCancelling": "正在取消…",
                                              "DownloadInterrupted": "下载失败，可重试"}.get(state, "下载"))

    def _on_download_status(self, message: str) -> None:
        self._last_download_message = message
        if not self._download_notice_dismissed:
            self.statusBar().showMessage(message)

    def _dismiss_download_notice(self) -> None:
        if self._download_ui_state not in {"DownloadCompleted", "DownloadCancelled", "DownloadInterrupted"}:
            return
        self._download_notice_dismissed = True
        self.download_progress.hide()
        self.cancel_download_button.hide()
        self.retry_download_button.hide()
        self.close_download_notice_button.hide()
        if self.statusBar().currentMessage() == self._last_download_message:
            self.statusBar().clearMessage()

    def _cancel_download(self) -> None:
        if self.web_view is not None:
            self.web_view.cancel_download()

    def _retry_download(self) -> None:
        if self.web_view is not None:
            self.web_view.retry_download()

    # ---------------------------------------------------------------- 设置
    def open_download_dir(self) -> None:
        """打开当前下载保存位置；优先走内嵌浏览器（它会做同样的探测与回退）。"""
        if self.web_view is not None:
            self.web_view.open_download_folder()
            return
        # 用实际可写目录，并在发生回退时说明，避免用户以为文件丢在设置的目录里
        target = self.settings.resolved_download_dir()
        description = self.settings.describe_download_dir()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            if os.name == "nt":
                os.startfile(str(target))  # noqa: S606 - Windows 打开目录
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
            self.statusBar().showMessage(description)
        except OSError as exc:
            # 受限环境（沙箱、低完整性令牌）下这是可预期的失败，只提示不崩溃
            self._mark("打开下载目录失败", path=str(target), error=str(exc))
            self.statusBar().showMessage(f"无法打开目录：{target}（{exc.strerror or exc}）")
            QMessageBox.information(
                self, "无法打开下载目录",
                f"{target}\n\n{type(exc).__name__}: {exc.strerror or exc}\n\n"
                "系统未能打开文件管理器，这不代表下载保存失败。\n"
                f"实际下载目录：{target}")

    def open_settings(self) -> None:
        """打开设置对话框。任何异常都记录下来并提示用户，而不是静默退出。"""
        from .. import crashlog

        self._mark("打开设置", page=self.page, results=len(self.submissions))
        try:
            dialog = SettingsDialog(self.settings, self)
            self._mark("设置对话框已创建，进入模态循环")
            accepted = dialog.exec()
            self._mark("设置对话框已关闭", accepted=bool(accepted))
        except Exception as exc:  # noqa: BLE001 - 设置面板不应导致程序退出
            detail = traceback.format_exc()
            saved = crashlog.write_entry("打开设置失败", detail, self.settings.config_dir)
            QMessageBox.critical(self, "设置无法打开",
                                 f"{type(exc).__name__}: {exc}\n\n详情已写入：{saved}")
            return
        if not accepted:
            return
        self._mark("应用设置")
        try:
            # 用户在对话框里主动清空密钥时才允许清空磁盘上的旧值
            allow_clear = dialog.key_was_cleared()
            dialog.apply_to(self.settings)
            self.apply_settings(allow_key_clear=allow_clear)
            self._mark("设置已生效")
        except Exception as exc:  # noqa: BLE001 - 保存失败要给出可读提示
            detail = traceback.format_exc()
            saved = crashlog.write_entry("保存设置失败", detail, self.settings.config_dir)
            QMessageBox.critical(self, "设置保存失败",
                                 f"{type(exc).__name__}: {exc}\n\n详情已写入：{saved}")

    def apply_settings(self, allow_key_clear: bool = False) -> None:
        """保存设置后的生效动作（与对话框分离，便于自检）。"""
        save_settings(self.settings, allow_key_clear=allow_key_clear)
        self.client.timeout = self.settings.request_timeout_seconds
        self.client.request_delay_ms = self.settings.request_delay_ms
        self.translator = DeepSeekTranslator(self.settings)
        if self.web_view is not None:
            self.web_view.apply_settings(self.settings)
        self.grid.set_submissions(self.submissions, self.settings.show_original_text,
                                  frame_fetcher=self._request_thumbnail)
        self.statusBar().showMessage(f"设置已保存（密钥：{self.settings.masked_api_key()}）")

    # ---------------------------------------------------------------- 杂项
    def _mark(self, action: str, **extra: Any) -> None:
        """记录当前活动，供崩溃后的异常退出检测器判断死在哪个环节。"""
        try:
            from ..watchdog import write_marker
            write_marker(pathlib.Path(self.settings.config_dir or "."),
                         action=action, **extra)
        except Exception:  # noqa: BLE001 - 埋点绝不能影响正常功能
            pass

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.progress.setVisible(busy)
        self.progress.setRange(0, 0 if busy else 1)
        self.search_button.setEnabled(not busy)
        self.translate_action.setEnabled(not busy)
        if message:
            self.statusBar().showMessage(message)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        event.accept()
        if self._closing_window:
            return
        self._closing_window = True
        # 第一次点击就让窗口消失；网络任务收尾可能需要时间，不能挡住关闭。
        self.hide()
        self._mark("用户关闭窗口")
        # 缓存采用合并写盘，退出前必须强制落盘，避免最后几秒的缓存丢失
        try:
            if getattr(self.client, "cache", None) is not None:
                self.client.cache.close()
            self.translator.cache.close()
        except Exception:  # noqa: BLE001 - 落盘失败不应阻止退出
            pass
        # 图片查看器有自己的线程池，必须先回收，否则会留下仍在运行的线程
        viewer = getattr(self, "_image_viewer", None)
        if viewer is not None:
            try:
                viewer.close()
                viewer.tasks.stop_all()
            except Exception:  # noqa: BLE001
                pass
            self._image_viewer = None
        self.tasks.stop_all()
        if self.web_view is not None:
            self.web_view.shutdown()
        super().closeEvent(event)


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
