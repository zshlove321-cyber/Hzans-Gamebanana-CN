"""详情面板：作者正文（HTML）+ 预览相册 + 下载与致谢。

设计要点（稳定性优先）：
  * **渲染期不做任何网络访问**。早期实现用自定义 QTextDocument 在渲染时
    异步取图并回调 addResource，实测会在渲染线程与工作线程之间产生竞态，
    导致原生访问违例（进程直接消失）。现在改为：
    图片先由主窗口在工作线程取回字节，再以 data: URI 内联进 HTML，
    QTextDocument 只做纯文本布局，不触发任何 IO；
  * 站点返回的作者正文是 HTML，可能含脚本或不安全属性，也可能残留浏览器
    翻译插件插入的标记（`<font _mstmutation="1">`），必须先净化；
  * 相册按官方网页排版：一张主图 + 缩略图条 + 计数与左右切换。
"""
from __future__ import annotations

import base64
import html as html_module
import re
from typing import Any

from PySide6.QtCore import QBuffer, QIODevice, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QProgressBar, QTextBrowser, QVBoxLayout, QWidget

# 允许保留的标签（其余标签会被去掉，只留文字）
ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "s", "strike", "del", "ins", "sub", "sup",
    "p", "br", "hr", "div", "span", "font", "small", "big", "center",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tr", "td", "th", "caption",
    "a", "img", "blockquote", "pre", "code", "figure", "figcaption",
}
# 允许保留的属性
ALLOWED_ATTRS = {"href", "src", "alt", "title", "width", "height", "colspan", "rowspan"}
# 需要整段丢弃的标签（含内容）
DROP_TAGS = {"script", "style", "iframe", "object", "embed", "form",
             "input", "button", "select", "textarea", "noscript", "svg", "math"}
# 自闭合标签：不需要配对的结束标签
_VOID_TAGS = {"br", "hr", "img"}

_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>")
_ATTR_RE = re.compile(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
_FONT_MUTATION_RE = re.compile(r"<font[^>]*_mstmutation[^>]*>|</font>", re.IGNORECASE)
_VIDEO_HOSTS = {
    "youtube.com": "YouTube", "youtu.be": "YouTube", "youtube-nocookie.com": "YouTube",
    "vimeo.com": "Vimeo", "streamable.com": "Streamable",
    "gfycat.com": "Gfycat", "imgur.com": "Imgur", "sketchfab.com": "Sketchfab",
}


def sanitize_html(raw: str, max_image_width: int | None = 460) -> str:
    """净化作者正文：去掉脚本/危险属性与翻译插件残留标记，并约束图片尺寸。

    不是完整 HTML 净化器，但足以安全渲染站点描述这类受限输入：
    只保留白名单标签与属性，`href`/`src` 限 http(s)/mailto/data。
    丢弃起始标签时会连同其结束标签一起处理，避免悬挂的 `</a>`。

    实体处理：站点正文里会出现 `&nbsp;`、`&amp;` 这类实体。先整体解码成真实
    字符，输出时再只对 `& < >` 转义一次——否则会渲染出字面量 `&nbsp;`
    （实测原实现把 `&nbsp;` 变成了 `&amp;nbsp;`）。

    `max_image_width`：站点正文里的图片常常没有 width 属性，QTextDocument
    会按原始像素渲染，一张 1920px 的图会把整个详情面板占满并盖住排版。
    Qt 对 CSS `max-width` 支持不可靠，因此这里直接给每个 `img` 写入像素宽度。
    """
    if not raw:
        return ""
    text = _FONT_MUTATION_RE.sub("", str(raw))

    for tag in DROP_TAGS:
        text = re.sub(rf"<\s*{tag}\b.*?<\s*/\s*{tag}\s*>", "", text,
                      flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(rf"<\s*/?\s*{tag}\b[^>]*>", "", text, flags=re.IGNORECASE)

    def render_text(segment: str) -> str:
        """文本片段：实体解码成真实字符后再转义一次。

        不能对整个文档先 unescape：那样 `&lt;script&gt;` 会变成真正的 <script>
        并被当作标签处理（既丢内容也不安全）。这里只对标签之间的文字解码。
        """
        return html_module.escape(html_module.unescape(segment), quote=False)

    pieces: list[str] = []
    open_tags: list[str] = []
    position = 0
    for match in _TAG_RE.finditer(text):
        pieces.append(render_text(text[position:match.start()]))
        position = match.end()
        closing, tag, attrs = match.group(1), match.group(2).lower(), match.group(3)

        if tag not in ALLOWED_TAGS:
            if closing and tag in open_tags:
                while open_tags and open_tags.pop() != tag:
                    continue
            continue

        if closing:
            if tag in open_tags:
                while open_tags:
                    popped = open_tags.pop()
                    pieces.append(f"</{popped}>")
                    if popped == tag:
                        break
            continue

        kept: list[str] = []
        has_width = False
        for name, value in _ATTR_RE.findall(attrs or ""):
            name_lower = name.lower()
            if name_lower not in ALLOWED_ATTRS:
                continue  # 丢弃 _mstmutation 等私有属性
            clean = value.strip("\"'")
            if name_lower in {"href", "src"}:
                if not clean.lower().startswith(("http://", "https://", "mailto:", "data:")):
                    continue
            if name_lower == "width":
                has_width = True
                # 已有的宽度也要夹到上限，避免超大图撑破面板
                digits = re.sub(r"\D", "", clean)
                if max_image_width and digits:
                    clean = str(min(int(digits), max_image_width))
            kept.append(f'{name_lower}="{html_module.escape(clean, quote=True)}"')

        if tag == "img" and max_image_width and not has_width:
            kept.append(f'width="{max_image_width}"')

        if tag == "a" and "href" not in " ".join(kept):
            continue
        if tag in _VOID_TAGS:
            pieces.append(f"<{tag}{(' ' + ' '.join(kept)) if kept else ''}>")
            continue
        attr_text = (" " + " ".join(kept)) if kept else ""
        pieces.append(f"<{tag}{attr_text}>")
        open_tags.append(tag)

    pieces.append(render_text(text[position:]))
    while open_tags:
        pieces.append(f"</{open_tags.pop()}>")
    return "".join(pieces)


def video_links(embedded: list[str]) -> list[tuple[str, str]]:
    """把嵌入媒体 URL 变成 (站点名, 链接)。"""
    result: list[tuple[str, str]] = []
    for url in embedded or []:
        host = ""
        match = re.match(r"https?://([^/]+)", url or "")
        if match:
            host = match.group(1).lower()
            if host.startswith("www."):
                host = host[4:]
        label = ""
        for key, name in _VIDEO_HOSTS.items():
            if host == key or host.endswith("." + key):
                label = name
                break
        result.append((label or host or "嵌入媒体", url))
    return result


def data_uri(payload: bytes, mime: str = "image/png") -> str:
    """把图片字节转成 data: URI，便于直接内联进 QTextDocument（无需网络）。"""
    if not payload:
        return ""
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def scaled_data_uri(payload: bytes, url: str, box_width: int, box_height: int = 0) -> str:
    """把图片缩放到指定宽度后再转 data: URI。

    为什么要预缩放：QTextDocument 对 HTML 里的图片会尽量按固有尺寸渲染，
    `width="100%"` 不被支持、CSS `max-width` 也不可靠。实测一张 1920px 宽的
    预览图会把整个详情面板撑满并盖住其它区块。这里直接按面板宽度等比缩放，
    渲染时就只拿到合适尺寸的位图。
    """
    if not payload:
        return ""
    image = QImage()
    if not image.loadFromData(payload):
        return data_uri(payload, guess_mime(url))
    if box_width <= 0:
        return data_uri(payload, guess_mime(url))
    if image.width() > box_width:
        height = max(1, int(image.height() * box_width / image.width()))
        image = image.scaled(box_width, height,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    if box_height > 0 and image.height() > box_height:
        image = image.scaled(box_width, box_height,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return data_uri(bytes(buffer.data()), "image/png")


def guess_mime(url: str) -> str:
    lowered = url.lower().split("?")[0]
    for extension, mime in ((".png", "image/png"), (".jpg", "image/jpeg"),
                            (".jpeg", "image/jpeg"), (".webp", "image/webp"),
                            (".gif", "image/gif")):
        if lowered.endswith(extension):
            return mime
    return "image/jpeg"


class PartitionDetailView(QWidget):
    """详情展示控件：标题/元信息/相册/作者正文/下载文件/致谢。"""

    link_activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.browser = QTextBrowser(self)
        self.browser.setOpenExternalLinks(False)
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(lambda url: self.link_activated.emit(url.toString()))
        layout.addWidget(self.browser, 1)

        self.progress = QProgressBar(self)
        self.progress.setMaximumHeight(3)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # 相册图片字节缓存：URL → bytes（切换主图时不再重新下载）
        self.media_bytes: dict[str, bytes] = {}

    # --- 渲染 ---
    def set_html(self, body_html: str) -> None:
        """纯布局渲染：调用方需保证 HTML 中的图片已是 data: URI 或远程地址。

        远程地址不会在此处触发取图——由主窗口预先下载并转为 data: URI。
        """
        self.browser.setHtml(body_html)

    def plain_text(self) -> str:
        return self.browser.toPlainText()

    def show_loading(self, message: str) -> None:
        self.browser.setHtml(f'<div style="color:#9aa0a6;padding:12px">{message}</div>')
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)

    def stop_loading(self) -> None:
        self.progress.setVisible(False)
        self.progress.setRange(0, 1)
