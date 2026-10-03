"""索引卡片网格：按行列展示搜索结果（UI.txt 第2条的「按行列展示」）。

每张卡片 = 缩略图 + 中文标题 + 原文标题 + 作者/游戏/分类/版本/时间/数据 + 标签。
一键汉化后，中文显示在原文下方（设置里可关闭原文）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..api import Submission, partition_kind, partition_of

THUMB_WIDTH = 200
THUMB_HEIGHT = 112


def is_alive(widget: QWidget | None) -> bool:
    """判断 Qt 包装对象是否仍指向有效的 C++ 实例。

    翻页/重新搜索会销毁旧卡片，但在途的缩略图请求仍会回调；
    对已销毁的控件调用 setPixmap 会抛
    `RuntimeError: Internal C++ object already deleted`。
    """
    if widget is None:
        return False
    try:
        import shiboken6
        return bool(shiboken6.Shiboken.isValid(widget))
    except Exception:  # noqa: BLE001 - 缺少 shiboken 时退化为常规判断
        return True


class SubmissionCard(QFrame):
    """单条索引条目卡片。"""

    activated = Signal(object)  # Submission

    def __init__(self, submission: Submission, show_original: bool = True,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.submission = submission
        self.show_original = show_original
        self.setObjectName("submissionCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedWidth(224)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        self.thumbnail = QLabel()
        self.thumbnail.setFixedSize(THUMB_WIDTH, THUMB_HEIGHT)
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail.setObjectName("cardThumb")
        self.thumbnail.setText("载入图片…")
        layout.addWidget(self.thumbnail, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.title_label = QLabel()
        self.title_label.setWordWrap(True)
        self.title_label.setObjectName("cardTitle")
        layout.addWidget(self.title_label)

        self.original_label = QLabel()
        self.original_label.setWordWrap(True)
        self.original_label.setObjectName("cardOriginal")
        layout.addWidget(self.original_label)

        self.meta_label = QLabel()
        self.meta_label.setWordWrap(True)
        self.meta_label.setObjectName("cardMeta")
        layout.addWidget(self.meta_label)

        self.tags_label = QLabel()
        self.tags_label.setWordWrap(True)
        self.tags_label.setObjectName("cardTags")
        layout.addWidget(self.tags_label)

        self.stats_label = QLabel()
        self.stats_label.setObjectName("cardStats")
        layout.addWidget(self.stats_label)

        layout.addStretch(1)
        self.refresh()

    def set_thumbnail(self, data: bytes) -> None:
        if not is_alive(self.thumbnail):
            return  # 卡片已被翻页销毁，忽略迟到的缩略图
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            self.thumbnail.setPixmap(pixmap.scaled(
                THUMB_WIDTH, THUMB_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            self.thumbnail.setText("")

    def set_thumbnail_failed(self) -> None:
        if not is_alive(self.thumbnail):
            return
        self.thumbnail.setText("无预览图")

    def set_translated(self, translations: dict[str, str], show_original: bool | None = None) -> None:
        if not is_alive(self.title_label):
            return
        self.submission.translations.update(translations)
        if show_original is not None:
            self.show_original = show_original
        self.refresh()

    def refresh(self) -> None:
        if not is_alive(self.title_label):
            return
        item = self.submission
        kind = partition_kind(item.model)
        translated_title = item.display("name")
        self.title_label.setText(translated_title)
        has_translation = bool(item.translations.get("name")) and translated_title != item.name
        self.original_label.setText(item.name if (has_translation and self.show_original) else "")
        self.original_label.setVisible(bool(self.original_label.text()))

        if kind == "category":
            self._refresh_category_card(item)
            return
        if kind == "profile":
            self._refresh_profile_card(item)
            return

        parts = [item.model_label]
        if item.game:
            parts.append(item.game)
        category = item.display("root_category") or item.display("sub_category")
        if category:
            parts.append(category)
        if item.version:
            parts.append(f"v{item.version}")
        if item.date_added:
            parts.append(item.date_added)
        self.meta_label.setText(" · ".join(parts))

        tags = [item.display_tag(index) for index in range(len(item.tags))]
        tags = [tag for tag in tags if tag]
        self.tags_label.setText("　".join(f"#{tag}" for tag in tags[:4]))
        self.tags_label.setVisible(bool(tags))

        stats = []
        if item.author:
            stats.append(f"作者 {item.author}")
        if item.views:
            stats.append(f"浏览 {item.views}")
        if item.likes:
            stats.append(f"点赞 {item.likes}")
        if item.has_files:
            stats.append("含文件")
        if item.is_obsolete:
            stats.append("⚠ 已过时")
        self.stats_label.setText(" · ".join(stats))

        tips = [f"<b>{item.name}</b>", item.profile_url]
        self.setToolTip("<br>".join(tips))

    def _refresh_category_card(self, item: Submission) -> None:
        """分类/专区类（游戏、模组分类）：不给作者与发布日期，改为提示可下钻。"""
        info = partition_of(item.model)
        meta = [info["noun"]]
        hint = getattr(item, "game_hint", "")
        if hint:
            meta.append(hint)
        self.meta_label.setText(" · ".join(meta))

        tags = [item.display_tag(index) for index in range(len(item.tags))]
        tags = [tag for tag in tags if tag]
        self.tags_label.setText("　".join(f"#{tag}" for tag in tags[:4]))
        self.tags_label.setVisible(bool(tags))

        self.stats_label.setText("点击进入该分区查看内容")
        self.setToolTip(f"<b>{item.name}</b>（{meta[0]}）<br>{item.profile_url}"
                        "<br>单击：查看详情并进入该分区的内容列表")

    def _refresh_profile_card(self, item: Submission) -> None:
        """个人/组织类（成员、工作室、社团）：展示身份与活跃度，不展示文件信息。"""
        parts = [item.model_label]
        if item.date_added:
            parts.append(f"加入 {item.date_added}")
        self.meta_label.setText(" · ".join(parts))

        tags = [item.display_tag(index) for index in range(len(item.tags))]
        tags = [tag for tag in tags if tag]
        self.tags_label.setText("　".join(f"#{tag}" for tag in tags[:4]))
        self.tags_label.setVisible(bool(tags))

        stats = []
        if item.posts:
            stats.append(f"发布 {item.posts}")
        if item.views:
            stats.append(f"主页访问 {item.views}")
        if item.likes:
            stats.append(f"获赞 {item.likes}")
        self.stats_label.setText(" · ".join(stats) or "点击查看资料")
        self.setToolTip(f"<b>{item.name}</b>（{item.model_label}）<br>{item.profile_url}")

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.activated.emit(self.submission)
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.activated.emit(self.submission)
        super().mouseReleaseEvent(event)


class CardGrid(QWidget):
    """卡片网格容器，按列数自动换行排列。"""

    card_activated = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(10, 10, 10, 10)
        self._layout.setHorizontalSpacing(10)
        self._layout.setVerticalSpacing(10)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._cards: list[SubmissionCard] = []
        self._columns = 4
        self._show_original = True

    @property
    def cards(self) -> list[SubmissionCard]:
        return list(self._cards)

    def set_columns(self, columns: int) -> None:
        columns = max(1, columns)
        if columns == self._columns:
            return
        self._columns = columns
        self._relayout()

    def clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._cards.clear()

    def set_submissions(self, submissions: list[Submission], show_original: bool = True,
                        frame_fetcher=None) -> None:
        self.clear()
        self._show_original = show_original
        for submission in submissions:
            card = SubmissionCard(submission, show_original=show_original)
            card.activated.connect(self.card_activated.emit)
            self._cards.append(card)
        self._relayout()
        if frame_fetcher is not None:
            for card in self._cards:
                frame_fetcher(card)

    def _relayout(self) -> None:
        while self._layout.count():
            self._layout.takeAt(0)
        for index, card in enumerate(self._cards):
            self._layout.addWidget(card, index // self._columns, index % self._columns)
