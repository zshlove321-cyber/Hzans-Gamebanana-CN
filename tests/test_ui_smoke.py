"""UI 冒烟自检：无显示器环境下装配窗口、卡片与设置对话框。

覆盖：
  1. 卡片网格能按行列铺开指定条目，并正确显示中文标题与原题回退；
  2. 设置对话框可构造、可回填、密钥以掩码显示且不落盘到仓库；
  3. 主窗口可构造（有 QtWebEngine 时为双标签页，缺依赖时降级为纯索引模式）；
  4. 未配置密钥时点「一键汉化」给出提示而不是崩溃；
  5. 后台任务线程能正常结束并被回收（不出现自等待或线程泄漏）。

说明：本脚本全程在 Qt 事件循环内运行，并带硬超时，任何一步挂起都会以失败退出。
运行：python tests/test_ui_smoke.py
"""
from __future__ import annotations

import os
import pathlib
import re
import sys
import tempfile

# 离线自检不继承宿主真实密钥，避免“无密钥”用例误发付费翻译请求。
os.environ.pop("DEEPSEEK_API_KEY", None)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

# QtWebEngine 要求在与 QApplication 同一个进程里共享 OpenGL 上下文
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

from client.api import SearchOutcome, Submission  # noqa: E402
from client.common import SessionStore  # noqa: E402
from client.settings import Settings  # noqa: E402
from client.translate import DeepSeekTranslator  # noqa: E402
from client.ui.cards import CardGrid  # noqa: E402
from client.ui.settings_dialog import SettingsDialog  # noqa: E402
from client.ui.workers import TaskRunner  # noqa: E402

FAILURES: list[str] = []
TIMEOUT_MS = 90000


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def process_events(milliseconds: int = 50) -> None:
    """在不阻塞的前提下让 Qt 处理挂起事件。"""
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def sample_submissions(count: int = 5) -> list[Submission]:
    return [
        Submission(id=1000 + index, model="Mod", name=f"Sample Mod {index}",
                   profile_url=f"https://gamebanana.com/mods/{1000 + index}",
                   author="Author", game="Minecraft", root_category="Gameplay",
                   tags=["Software Used: Blender"], views=index * 10, has_files=True,
                   date_added="2026-01-01 10:00")
        for index in range(count)
    ]


class OfflineClient:
    """离线替身：只提供窗口构造所需的最小接口，不发起任何网络请求。"""

    def __init__(self, config_dir: str) -> None:
        self.timeout = 25
        self.request_delay_ms = 0
        self.session = None
        self.session_store = SessionStore(pathlib.Path(config_dir) / "cookies.json")

    def apply_session_cookies(self) -> int:
        return 0

    def list_games(self, *args, **kwargs):
        return []

    def browse_model(self, *args, **kwargs):
        return SearchOutcome(submissions=[], total=0, page=1, per_page=24, is_complete=True)

    def search(self, *args, **kwargs):
        return SearchOutcome(submissions=[], total=0, page=1, per_page=24, is_complete=True)


# ------------------------------------------------------------------ 各项检查
def test_card_grid() -> None:
    grid = CardGrid()
    submissions = sample_submissions(7)
    grid.set_submissions(submissions, show_original=True)
    check(len(grid.cards) == 7, f"卡片数量应为 7，实际 {len(grid.cards)}")
    layout = grid.layout()
    check(layout is not None and layout.count() == 7, "网格布局未包含全部卡片")
    item = layout.itemAtPosition(1, 0)
    check(item is not None and item.widget() is grid.cards[4], "卡片未按行列顺序排列")

    card = grid.cards[0]
    check(card.title_label.text() == "Sample Mod 0", "未汉化时应显示原文标题")
    card.set_translated({"name": "示例模组 0"})
    check(card.title_label.text() == "示例模组 0", "汉化后标题未切换为中文")
    check(card.original_label.text() == "Sample Mod 0", "汉化后未保留英文原文")

    grid.set_columns(3)
    item = grid.layout().itemAtPosition(1, 0)
    check(item is not None and item.widget() is grid.cards[3], "改列数后未重新排布")
    grid.clear()
    check(not grid.cards, "clear 后仍有卡片残留")


def test_settings_dialog() -> None:
    with tempfile.TemporaryDirectory() as raw:
        settings = Settings(config_dir=raw, deepseek_api_key="sk-abcdef1234567890")
        dialog = SettingsDialog(settings)
        check(dialog.key_edit.echoMode() == dialog.key_edit.EchoMode.Password, "密钥未以密码方式显示")
        check(dialog.key_edit.text() == "sk-abcdef1234567890", "密钥未正确回填")
        dialog.reveal_button.setChecked(True)
        check(dialog.key_edit.echoMode() == dialog.key_edit.EchoMode.Normal, "显示按钮未切换为明文")

        collected = dialog._collect()
        check(collected.deepseek_api_key == "sk-abcdef1234567890", "收集结果丢失密钥")
        check(bool(collected.deepseek_model), "模型字段为空")
        masked = collected.masked_api_key()
        check("abcdef1234567890" not in masked, f"掩码泄露了完整密钥：{masked}")

        dialog.batch_spin.setValue(500)
        check(dialog._collect().translate_batch_size <= 80, "批大小未做上界约束")
        dialog.close()


def test_translator_wiring() -> None:
    with tempfile.TemporaryDirectory() as raw:
        settings = Settings(config_dir=raw)
        translator = DeepSeekTranslator(settings)
        check(translator.cache.path.parent.name == pathlib.Path(raw).name,
              "翻译缓存未落在用户配置目录")
        try:
            translator.translate_many(["hello"])
        except Exception as exc:  # noqa: BLE001
            check("密钥" in str(exc), f"未配置密钥的报错不够明确：{exc}")
        else:
            FAILURES.append("未配置密钥时不应成功翻译")


def test_download_settings() -> None:
    """下载目录设置：可收集、可回填、重名不覆盖、不可写时回退。"""
    from client.settings import default_download_dir, unique_path

    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        target = tmp / "my downloads"

        settings = Settings(config_dir=str(tmp))
        settings.download_dir = str(target)
        settings.download_ask_each_time = True
        settings.download_open_folder_after = True
        settings.normalized()
        resolved = settings.resolved_download_dir()
        check(resolved == target, f"下载目录未按设置生效：{resolved}")
        check(target.is_dir(), "下载目录未被自动创建")

        # 重名文件自动追加序号，绝不覆盖已有文件
        first = unique_path(target, "mod.zip")
        first.write_bytes(b"x")
        second = unique_path(target, "mod.zip")
        check(second != first, "重名文件被覆盖")
        check(second.name == "mod (1).zip", f"重名处理结果异常：{second.name}")
        second.write_bytes(b"y")
        third = unique_path(target, "mod.zip")
        check(third.name == "mod (2).zip", f"第二次重名处理异常：{third.name}")

        # 目录名里的路径分隔符必须被剥离，避免写出目录
        nested = unique_path(target, "../../evil.zip")
        check(nested.parent == target, f"文件名未做净化：{nested}")

        # 留空时回退系统下载目录
        blank = Settings(config_dir=str(tmp), download_dir="")
        blank.normalized()
        check(blank.download_dir == str(default_download_dir()),
              f"留空未回退默认下载目录：{blank.download_dir}")

        # 不可写目录应回退而不是抛异常
        blocked = tmp / "blocked"
        blocked.write_text("占位文件，阻止同名目录创建", encoding="utf-8")
        bad = Settings(config_dir=str(tmp), download_dir=str(blocked / "sub"))
        fallback = bad.resolved_download_dir()
        check(fallback != blocked / "sub", "不可写目录未回退")
        check(fallback.is_dir(), f"回退目录不存在：{fallback}")

        # 设置对话框应携带并回填下载字段
        dialog = SettingsDialog(settings)
        check(dialog.download_dir_edit.text() == str(target), "设置对话框未回填下载目录")
        check(dialog.ask_each_time_check.isChecked(), "未回填「每次询问」选项")
        dialog.download_dir_edit.setText(str(tmp / "another"))
        dialog.ask_each_time_check.setChecked(False)
        collected = dialog._collect()
        check(collected.download_dir == str(tmp / "another"), "下载目录未被收集")
        check(not collected.download_ask_each_time, "「每次询问」开关未被收集")
        dialog.close()


def test_task_runner() -> None:
    """后台任务应成功回调并回收线程，且不出现线程自等待。"""
    runner = TaskRunner()
    state: dict[str, object] = {"done": False, "result": None, "error": None}

    def work():
        return 6 * 7

    runner.run(work, on_success=lambda value: state.update(result=value, done=True),
               on_error=lambda message: state.update(error=message, done=True))
    for _ in range(200):
        process_events(20)
        if state["done"]:
            break
    check(state["done"], "后台任务未在预期时间内回调")
    check(state["result"] == 42, f"后台任务返回值错误：{state['result']}")
    check(state["error"] is None, f"后台任务意外报错：{state['error']}")
    check(runner.active_count == 0, f"任务结束后线程未回收：{runner.active_count}")
    runner.stop_all()


def test_download_logic() -> None:
    """下载处理逻辑（与 Qt 解耦，因此任何环境都能验证）：

    状态文案映射 + 不覆盖已有文件的保存路径。
    """
    from client.settings import describe_download_state, resolve_download_target

    finished, text = describe_download_state("DownloadCompleted", "mod.zip", r"C:\dl")
    check(finished and "下载完成" in text and "mod.zip" in text,
          f"完成状态文案异常：{text}")

    finished, text = describe_download_state("DownloadCancelled", "mod.zip", r"C:\dl")
    check(finished and "已取消" in text, f"取消状态文案异常：{text}")

    finished, text = describe_download_state("DownloadInterrupted", "mod.zip", r"C:\dl", "磁盘已满")
    check(finished and "中断" in text and "磁盘已满" in text, f"中断状态文案异常：{text}")

    finished, text = describe_download_state("DownloadInProgress", "mod.zip", r"C:\dl")
    check(not finished and "下载中" in text, f"进行中文案异常：{text}")

    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        target, name = resolve_download_target(tmp, "cool_mod.zip")
        check(name == "cool_mod.zip" and target.parent == tmp, f"目标路径异常：{target}")
        target.write_bytes(b"x")
        second, name2 = resolve_download_target(tmp, "cool_mod.zip")
        check(name2 == "cool_mod (1).zip", f"重名未避让：{name2}")
        check(second != target and not second.exists(), "避让后的路径不应指向已有文件")


def test_download_widget() -> None:
    """真实 WebEngine 下载接线（仅在装了 QtWebEngine 时执行）。

    用假 download request 驱动 QWebEngineProfile 的下载处理，不产生真实下载。
    """
    class FakeSignal:
        def __init__(self) -> None:
            self.slots: list = []

        def connect(self, slot) -> None:
            self.slots.append(slot)

        def emit(self, *args) -> None:
            for slot in self.slots:
                slot(*args)

    class FakeDownloadState:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakeRequest:
        """模拟 QWebEngineDownloadRequest 被用到的接口。"""

        def __init__(self, filename: str) -> None:
            self._dir = ""
            self._name = filename
            self._received = 0
            self._total = 0
            self._state = FakeDownloadState("DownloadRequested")
            self._interrupt = ""
            self.accepted = False
            self.cancelled = False
            self.receivedBytesChanged = FakeSignal()
            self.totalBytesChanged = FakeSignal()
            self.stateChanged = FakeSignal()

        # --- 模拟 Qt 接口 ---
        def downloadFileName(self):
            return self._name

        def downloadDirectory(self):
            return self._dir

        def setDownloadDirectory(self, value):
            self._dir = value

        def setDownloadFileName(self, value):
            self._name = value

        def accept(self):
            self.accepted = True

        def cancel(self):
            self.cancelled = True

        def receivedBytes(self):
            return self._received

        def totalBytes(self):
            return self._total

        def state(self):
            return self._state

        def interruptReasonString(self):
            return self._interrupt

        # --- 测试驱动 ---
        def progress(self, received, total):
            self._received, self._total = received, total
            self.receivedBytesChanged.emit()

        def finish(self, state_name, interrupt=""):
            self._state = FakeDownloadState(state_name)
            self._interrupt = interrupt
            self.stateChanged.emit(self._state)

    from client.ui import web_view as web_view_module
    if not web_view_module.is_webengine_available_here():
        print("  （未安装 QtWebEngine：跳过下载处理验证）")
        return

    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        target = tmp / "dl"
        settings = Settings(config_dir=str(tmp), download_dir=str(target))
        widget = web_view_module.WebViewWidget(settings)
        try:
            check(widget.profile.downloadPath() == str(target),
                  f"默认下载路径未指向设置：{widget.profile.downloadPath()}")

            request = FakeRequest("cool_mod.zip")
            widget._on_download_requested(request)
            check(request.accepted, "下载请求未被接受")
            check(request.downloadDirectory() == str(target),
                  f"下载目录未落到设置：{request.downloadDirectory()}")

            request.progress(512, 2048)
            check(widget.progress.value() == 25, f"进度计算异常：{widget.progress.value()}")
            check("下载中" in widget.status_label.text(), "进度状态未显示")

            request.finish("DownloadCompleted")
            check("下载完成" in widget.status_label.text(),
                  f"完成状态未显示：{widget.status_label.text()}")

            request2 = FakeRequest("broken.zip")
            widget._on_download_requested(request2)
            request2.finish("DownloadInterrupted", "网络错误")
            check("中断" in widget.status_label.text(),
                  f"中断状态未显示：{widget.status_label.text()}")

            # 每次询问模式：没有选择文件时应取消下载
            settings.download_ask_each_time = True
            widget.settings = settings
            request3 = FakeRequest("ask.zip")
            original_get_save = web_view_module.QFileDialog.getSaveFileName
            web_view_module.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))
            try:
                widget._on_download_requested(request3)
            finally:
                web_view_module.QFileDialog.getSaveFileName = original_get_save
            check(request3.cancelled, "询问模式下取消选择应取消下载")
        finally:
            widget.shutdown()


def test_translation_bridge() -> None:
    """QWebChannel 翻译通道：协议正确、错误可读、统计回传（不需要浏览器页面）。"""
    import json as jsonlib

    from client.ui.translate_bridge import TranslationBridge

    with tempfile.TemporaryDirectory() as raw:
        settings = Settings(config_dir=raw)
        translator = DeepSeekTranslator(settings)
        # 用假传输层替代真实网络，验证通道协议而无需密钥
        from test_translate_engine import FakeTransport
        translator._transport = FakeTransport([])
        settings.deepseek_api_key = "sk-test-not-real"

        bridge = TranslationBridge(translator)
        received: list[tuple[str, dict]] = []
        bridge.translations_ready.connect(
            lambda request_id, payload: received.append((request_id, jsonlib.loads(payload))))

        items = jsonlib.dumps([{"id": "u0", "text": "Fast Furnaces"},
                               {"id": "u1", "text": "Skins"}], ensure_ascii=False)
        bridge.requestTranslations("r1", items)
        check(len(received) == 1, f"应回传 1 次结果，实际 {len(received)}")
        if received:
            request_id, payload = received[0]
            check(request_id == "r1", f"请求 ID 未回传：{request_id}")
            translations = {item["id"]: item["text"] for item in payload.get("translations", [])}
            check(translations.get("u0") == "译:Fast Furnaces",
                  f"id 未按约定对应译文：{translations}")
            check("stats" in payload, "回传结果缺少统计信息")

        # 非法请求应给出可读错误而不是抛异常
        received.clear()
        bridge.requestTranslations("r2", "这不是 JSON")
        check(len(received) == 1 and "error" in received[0][1],
              f"非法请求应回传 error：{received}")


def test_partition_registry() -> None:
    """分区登记表：每个分区都有语义、译名与可下钻行为。"""
    from client.api import (
        PARTITIONS,
        SEARCHABLE_MODELS,
        is_partition_content,
        model_label,
        partition_kind,
        partition_of,
    )

    check(len(PARTITIONS) >= 30, f"登记分区过少：{len(PARTITIONS)}")
    for model, info in PARTITIONS.items():
        check(bool(info.get("label")), f"{model} 缺少中文标签")
        check(info.get("kind") in {"content", "profile", "category"},
              f"{model} 的 kind 非法：{info.get('kind')}")

    check(partition_kind("Mod") == "content", "模组应为内容类")
    check(partition_kind("Member") == "profile", "成员应为个人类")
    check(partition_kind("Game") == "category", "游戏应为分类类")
    check(partition_kind("ModCategory") == "category", "模组分类应为分类类")
    check(not is_partition_content("Member"), "个人类分区不应标记为可下载")
    check(is_partition_content("Mod"), "模组应标记为可下载")
    check(model_label("Thread") == "论坛帖", f"论坛帖译名异常：{model_label('Thread')}")
    check(model_label("Studio") == "工作室", f"工作室译名异常：{model_label('Studio')}")
    # 未登记类型必须给出兜底而不是崩溃
    fallback = partition_of("TotallyUnknownModel")
    check(fallback["kind"] == "content" and fallback["label"], f"未登记类型兜底异常：{fallback}")
    check("Mod" in SEARCHABLE_MODELS and "Game" in SEARCHABLE_MODELS,
          "可搜索类型集合缺少基础分区")


def test_all_partition_card_semantics() -> None:
    """四类分区的卡片语义各自正确（内容/个人/分类）。"""
    from client.api import Submission
    from client.ui.cards import SubmissionCard

    # 内容类：显示作者与时间
    mod = Submission(id=1, model="Mod", name="Cool Mod", profile_url="",
                     author="Alice", date_added="2026-01-01 10:00")
    card = SubmissionCard(mod)
    try:
        check("作者 Alice" in card.stats_label.text(),
              f"内容类卡片应显示作者：{card.stats_label.text()!r}")
    finally:
        card.deleteLater()

    # 个人类：不显示文件/作者，显示加入时间与发布量
    member = Submission(id=2, model="Member", name="SomeUser", profile_url="",
                        date_added="2020-05-01 08:00", posts=42)
    card = SubmissionCard(member)
    try:
        check("加入 2020-05-01" in card.meta_label.text(),
              f"个人类卡片应显示加入时间：{card.meta_label.text()!r}")
        check("发布 42" in card.stats_label.text(),
              f"个人类卡片应显示发布量：{card.stats_label.text()!r}")
        check("含文件" not in card.stats_label.text(), "个人类卡片不应显示文件标记")
    finally:
        card.deleteLater()

    # 分类类：提示可下钻，不显示作者/发布日期
    category = Submission(id=3, model="ModCategory", name="Gameplay", profile_url="",
                          date_added="2021-01-01 00:00", author="Someone")
    card = SubmissionCard(category)
    try:
        check("点击进入" in card.stats_label.text(),
              f"分类类卡片应提示可下钻：{card.stats_label.text()!r}")
        check("作者" not in card.stats_label.text() and "2021" not in card.meta_label.text(),
              f"分类类卡片不应显示作者/发布日期："
              f"{card.meta_label.text()!r} / {card.stats_label.text()!r}")
    finally:
        card.deleteLater()


def test_html_sanitizer() -> None:
    """作者正文净化：保留排版、去掉脚本与危险属性、标签成对。"""
    from client.ui.detail_view import sanitize_html

    check(sanitize_html("<b>粗体</b> 与 <i>斜体</i>") == "<b>粗体</b> 与 <i>斜体</i>",
          "应保留基本排版标签")
    check("alert" not in sanitize_html("<script>alert(1)</script>安全"),
          "脚本内容必须被移除")
    check(sanitize_html("<script>x</script>安全") == "安全", "脚本移除后应只剩文本")
    # 浏览器翻译插件会插入 _mstmutation 属性，必须清掉但不能破坏文字
    cleaned = sanitize_html('<font _mstmutation="1">插件残留</font>正常')
    check("_mstmutation" not in cleaned and "插件残留正常" in cleaned,
          f"插件残留属性未清理干净：{cleaned}")
    # 事件属性必须去掉，src 保留
    img = sanitize_html('<img src="https://x.com/a.jpg" onerror="alert(1)">')
    check("onerror" not in img and "a.jpg" in img, f"img 属性净化异常：{img}")
    # javascript: 链接应被丢弃且不留下悬挂闭合标签
    bad_link = sanitize_html('<a href="javascript:alert(1)">恶意</a>')
    check("javascript" not in bad_link, "javascript: 链接必须丢弃")
    check("</a>" not in bad_link, f"丢弃起始标签后不应留下悬挂 </a>：{bad_link}")
    check("恶意" in bad_link, "丢弃链接后正文文字应保留")
    # 合法链接保留
    good = sanitize_html('<a href="https://gamebanana.com/mods/1">合法</a>')
    check('href="https://gamebanana.com/mods/1"' in good, f"合法链接被误删：{good}")
    # 未闭合标签要补齐，避免后续排版错乱
    check(sanitize_html("未闭合 <b>粗体") == "未闭合 <b>粗体</b>",
          "未闭合标签应被补齐")
    # 纯文本里的 & 不应被双重转义
    check(sanitize_html("plain & more") == "plain &amp; more", "文本应做一次转义")
    # HTML 实体要解码成真实字符（站点正文含 &nbsp;），否则会显示字面量
    check(sanitize_html("a&nbsp;b") == "a\u00a0b", f"&nbsp; 应解码为空格：{sanitize_html('a&nbsp;b')!r}")
    check(sanitize_html("&yen;100") == "¥100", "货币实体应解码")
    # 但转义后的标签必须作为文字保留，不能被当成真标签
    escaped = sanitize_html("&lt;script&gt; 文字")
    check("<script>" not in escaped and "文字" in escaped,
          f"转义后的标签不应变成真标签（会丢内容）：{escaped}")
    check(sanitize_html("") == "", "空输入应返回空串")


def test_media_extraction() -> None:
    """预览媒体与嵌入视频的解析（详情面板画廊的数据来源）。"""
    from client.api import MediaItem
    from client.ui.detail_view import video_links

    item = MediaItem(type="screenshot", caption="说明",
                     original="https://i/x.jpg", medium="https://i/530.jpg",
                     large="https://i/800.jpg", thumb="https://i/100.jpg")
    check(item.best("large") == "https://i/800.jpg", "large 优先取 800 尺寸")
    check(item.best("medium") == "https://i/530.jpg", "medium 优先取 530 尺寸")
    check(item.best("thumb") == "https://i/100.jpg", "thumb 优先取 100 尺寸")
    # 缺尺寸时应回退而不是返回空
    sparse = MediaItem(original="https://i/x.jpg")
    check(sparse.best("large") == "https://i/x.jpg", "缺尺寸时应回退到原图")

    links = video_links(["https://www.youtube.com/watch?v=abc",
                         "https://vimeo.com/123",
                         "https://unknown.example/x"])
    check(links[0][0] == "YouTube", f"YouTube 未识别：{links}")
    check(links[1][0] == "Vimeo", f"Vimeo 未识别：{links}")
    check(links[2][0] == "unknown.example", f"未知站点应显示主机名：{links}")
    check(video_links([]) == [], "空输入应返回空列表")


def test_image_scaling() -> None:
    """图片必须按面板宽度预缩放（Qt 不支持百分比宽度，实测过会撑满面板）。"""
    from PySide6.QtGui import QImage

    from client.ui.detail_view import sanitize_html, scaled_data_uri

    # 造一张 1920x1080 的图，验证缩放后宽度受限
    source = QImage(1920, 1080, QImage.Format.Format_RGB32)
    source.fill(0x334455)
    from PySide6.QtCore import QBuffer, QIODevice
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    source.save(buffer, "PNG")
    payload = bytes(buffer.data())

    uri = scaled_data_uri(payload, "https://x/big.png", 460)
    check(uri.startswith("data:image/png;base64,"), "应生成 data: URI")
    import base64
    decoded = base64.b64decode(uri.split(",", 1)[1])
    result = QImage()
    check(result.loadFromData(decoded), "缩放后的图片应可解码")
    check(result.width() <= 460, f"缩放后宽度应 ≤ 460，实际 {result.width()}")
    check(abs(result.width() / result.height() - 1920 / 1080) < 0.05,
          f"缩放应保持宽高比：{result.width()}x{result.height()}")
    # 小图不应被放大
    small = QImage(100, 50, QImage.Format.Format_RGB32)
    small.fill(0x112233)
    small_buffer = QBuffer()
    small_buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    small.save(small_buffer, "PNG")
    small_uri = scaled_data_uri(bytes(small_buffer.data()), "https://x/small.png", 460)
    small_decoded = base64.b64decode(small_uri.split(",", 1)[1])
    small_result = QImage()
    small_result.loadFromData(small_decoded)
    check(small_result.width() == 100, f"小图不应被放大，实际 {small_result.width()}")
    # 空数据与非法图片要安全返回
    check(scaled_data_uri(b"", "https://x/a.png", 460) == "", "空数据应返回空串")
    fallback = scaled_data_uri(b"not-an-image", "https://x/a.png", 460)
    check(fallback.startswith("data:"), "非法图片应退化为原样内联而不是报错")

    # 正文里的 img 必须写入像素宽度（不能依赖 CSS max-width）
    body = '<p>文字</p><img src="https://x/huge.jpg"><img src="https://y/s.jpg" width="2000">'
    cleaned = sanitize_html(body)
    widths = re.findall(r'<img[^>]*width="(\d+)"', cleaned)
    check(len(widths) == 2, f"每个 img 都应带像素宽度：{cleaned}")
    check(all(int(w) <= 460 for w in widths), f"图片宽度必须被夹到上限：{widths}")
    check('width="100%"' not in cleaned, "不得使用百分比宽度")
    check(sanitize_html(body, max_image_width=None).count("width=") == 1,
          "关闭限制时应保持原样")


def test_settings_never_loses_api_key() -> None:
    """设置持久化必须保护密钥：空值不得覆盖磁盘上已有的非空密钥。

    这是实测踩到的真实数据丢失：调用方构造了默认 Settings 后保存，
    把用户密钥抹成空字符串。
    """
    import json as jsonlib

    from client.settings import Settings as S
    from client.settings import load_settings, save_settings

    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        path = tmp / "settings.json"
        path.write_text(jsonlib.dumps({"deepseek_api_key": "sk-existing-key-abcdef"}),
                        encoding="utf-8")

        # 场景一：默认 Settings 保存 → 密钥必须保留
        save_settings(S(config_dir=str(tmp)))
        stored = jsonlib.loads(path.read_text(encoding="utf-8-sig"))
        check(stored.get("deepseek_api_key") == "sk-existing-key-abcdef",
              f"空密钥覆盖了磁盘上的密钥：{stored.get('deepseek_api_key')!r}")

        # 场景二：显式允许清空 → 生效
        save_settings(S(config_dir=str(tmp)), allow_key_clear=True)
        stored = jsonlib.loads(path.read_text(encoding="utf-8-sig"))
        check(stored.get("deepseek_api_key") == "", "显式清空应生效")

        # 场景三：设置文件损坏 → 备份 + 记录错误，不静默当空配置
        path.write_text("{ 这不是合法 JSON", encoding="utf-8")
        broken = load_settings(tmp)
        check(bool(broken.load_error), "损坏的设置文件应记录读取错误")
        check(bool(broken.load_error_backup)
              and pathlib.Path(broken.load_error_backup).is_file(),
              f"损坏文件应被备份：{broken.load_error_backup!r}")
        check(path.is_file(), "损坏文件本身不应被删除")

        # 场景四：诊断字段不得写回配置文件
        save_settings(broken, allow_key_clear=True)
        written = jsonlib.loads(path.read_text(encoding="utf-8-sig"))
        check("load_error" not in written and "load_error_backup" not in written,
              f"诊断字段不应写盘：{sorted(written)}")

        # 场景五：正常往返（含新选项）
        source = S(config_dir=str(tmp), deepseek_api_key="sk-round-trip-123456",
                   auto_translate_all_pages=True)
        save_settings(source, allow_key_clear=True)
        reloaded = load_settings(tmp)
        check(reloaded.deepseek_api_key == "sk-round-trip-123456", "密钥往返丢失")
        check(reloaded.auto_translate_all_pages is True, "全程汉化开关往返丢失")


def test_continuous_translation_toggle() -> None:
    """全程汉化开关：按钮状态、样式、设置持久化与自动触发条件。"""
    from client.api import SearchOutcome
    from client.settings import Settings as S
    from client.ui.main_window import MainWindow, _green_dot_icon

    with tempfile.TemporaryDirectory() as raw:
        settings = S(config_dir=raw)
        client = OfflineClient(raw)
        window = MainWindow(settings, client, DeepSeekTranslator(settings),
                            web_widget_factory=None)
        try:
            action = window.translate_all_action
            check(action.isCheckable(), "全程汉化按钮必须是可勾选的")
            check("全程" in action.text(), f"按钮文案应含「全程」：{action.text()}")
            check(not action.isChecked(), "默认应为关闭状态")
            check(window.translate_all_button.styleSheet() == "", "默认不应有高亮样式")

            # 未配置密钥时开启：应提示而不是崩溃，且不进入自动模式
            action.setChecked(True)
            check(window.continuous_translation is True, "开启后标志应为 True")
            check("1e8e3e" in window.translate_all_button.styleSheet(),
                  "开启后应显示绿色高亮")
            check("汉化中" in action.text(), f"开启后文案应标明状态：{action.text()}")
            check(settings.auto_translate_all_pages is True, "开启状态应写入设置")

            # 无密钥时不应真的发起翻译
            window.submissions = []
            window._auto_translate_if_enabled()
            check(not window.translated_pages, "无密钥/无条目时不应产生翻译记录")

            action.setChecked(False)
            check(window.continuous_translation is False, "关闭后标志应为 False")
            check(window.translate_all_button.styleSheet() == "",
                  "关闭后应清除高亮样式")
            check(settings.auto_translate_all_pages is False, "关闭状态应写入设置")

            # 图标生成不应抛异常（无外部资源依赖）
            check(not _green_dot_icon().isNull(), "绿色圆点图标应可生成")
        finally:
            window.close()
            process_events(200)

    # 已保存的开启状态应在下次启动时恢复
    with tempfile.TemporaryDirectory() as raw:
        settings = S(config_dir=raw, auto_translate_all_pages=True)
        window = MainWindow(settings, OfflineClient(raw), DeepSeekTranslator(settings),
                            web_widget_factory=None)
        try:
            check(window.continuous_translation is True, "启动时应恢复开启状态")
            check(window.translate_all_action.isChecked(), "按钮应恢复勾选")
            check("1e8e3e" in window.translate_all_button.styleSheet(),
                  "恢复后应显示绿色高亮")
        finally:
            window.close()
            process_events(200)


def test_detail_body_translation_timing() -> None:
    """作者说明的补译时序：先点汉化、后点开条目也必须能翻译。

    这是实测漏译的场景：整页汉化只在本页字段就绪时执行一次，
    而作者说明要等点开具体条目才知道内容，因此详情就绪后需要补译一趟。
    """
    from client.api import Submission, SubmissionDetail
    from client.settings import Settings as S
    from client.ui.main_window import MainWindow

    def make_window(root: str, key: str = "sk-test-fake-key-123456") -> MainWindow:
        settings = S(config_dir=root, deepseek_api_key=key)
        return MainWindow(settings, OfflineClient(root), DeepSeekTranslator(settings),
                          web_widget_factory=None)

    def make_detail(item_id: int, body: str) -> SubmissionDetail:
        return SubmissionDetail(
            submission=Submission(id=item_id, model="Mod", name="T", profile_url=""),
            body=body)

    with tempfile.TemporaryDirectory() as raw:
        window = make_window(raw)
        try:
            window.detail_view.resize(600, 800)
            english = "<p>Here, Dave is replacing Baldi; there is no audio of Dave.</p>"

            # 场景一：先登记本页已汉化（用户点过汉化），之后才点开条目 → 应补译
            window.translated_pages.add(window._page_key())
            detail = make_detail(1, english)
            window.detail = detail
            calls: list[str] = []
            original_submit = window.tasks.submit
            window.tasks.submit = lambda *a, **k: calls.append("submit")
            window._maybe_translate_detail_body(detail)
            check(len(calls) == 1, f"先汉化后点开条目应补译作者说明，实际请求 {len(calls)} 次")
            window.tasks.submit = original_submit

            # 场景二：未表达汉化意愿 → 不得偷偷计费
            window.translated_pages.clear()
            window.continuous_translation = False
            quiet = make_detail(2, english)
            window.detail = quiet
            calls.clear()
            window.tasks.submit = lambda *a, **k: calls.append("submit")
            window._maybe_translate_detail_body(quiet)
            check(len(calls) == 0, "未开启汉化时不应翻译作者说明")
            window.tasks.submit = original_submit

            # 场景三：正文已是中文 → 不翻译
            chinese = make_detail(3, "<p>这是一个中文说明，不需要翻译。</p>")
            window.detail = chinese
            window.translated_pages.add(window._page_key())
            calls.clear()
            window.tasks.submit = lambda *a, **k: calls.append("submit")
            window._maybe_translate_detail_body(chinese)
            check(len(calls) == 0, "已是中文的作者说明不应翻译")
            window.tasks.submit = original_submit

            # 场景四：已有译文 → 不重复请求
            translated_detail = make_detail(4, english)
            translated_detail.body_translation = "已有译文"
            window.detail = translated_detail
            calls.clear()
            window.tasks.submit = lambda *a, **k: calls.append("submit")
            window._maybe_translate_detail_body(translated_detail)
            check(len(calls) == 0, "已有译文时不应重复请求")
            window.tasks.submit = original_submit

            # 场景五：无正文 → 不请求
            empty = make_detail(5, "")
            window.detail = empty
            calls.clear()
            window.tasks.submit = lambda *a, **k: calls.append("submit")
            window._maybe_translate_detail_body(empty)
            check(len(calls) == 0, "无作者说明时不应请求")
            window.tasks.submit = original_submit

            # 场景六：并发保护 —— 同一批未完成前重复触发只请求一次
            window.translated_pages.add(window._page_key())
            pending = make_detail(6, english)
            window.detail = pending
            window.tasks.submit = lambda *a, **k: None  # 回调永不返回，模拟进行中
            window._maybe_translate_detail_body(pending)
            window._maybe_translate_detail_body(pending)
            check((pending.submission.model, pending.submission.id) in window._body_translating,
                  "进行中的条目应被登记，避免重复请求")
            window.tasks.submit = original_submit

            # 正文取源要剥掉 HTML 标签，避免把标签送进模型
            window.detail = make_detail(7, "<p>Hello <b>bold</b> world</p>")
            source = window._detail_body_source()
            check("<" not in source and "Hello" in source,
                  f"正文取源应剥离 HTML 标签：{source!r}")
        finally:
            window.close()
            process_events(200)


def test_restore_clears_detail_body() -> None:
    """「还原原文」必须同时还原作者说明，并且不被全程汉化立刻补译回来。"""
    from client.api import Submission, SubmissionDetail
    from client.settings import Settings as S
    from client.ui.main_window import MainWindow

    with tempfile.TemporaryDirectory() as raw:
        settings = S(config_dir=raw, deepseek_api_key="sk-test-fake-key-123456")
        window = MainWindow(settings, OfflineClient(raw), DeepSeekTranslator(settings),
                            web_widget_factory=None)
        try:
            window.detail_view.resize(600, 800)
            detail = SubmissionDetail(
                submission=Submission(id=11, model="Mod", name="T", profile_url=""),
                body="<p>English body text here.</p>")
            detail.body_translation = "这是译文"
            window.detail = detail
            window.translated_pages.add(window._page_key())
            window.continuous_translation = True
            token = (detail.submission.model, detail.submission.id)
            window._body_cache[token] = "这是译文"

            window.restore_originals()
            check(detail.body_translation == "",
                  f"还原原文应清空作者说明译文：{detail.body_translation!r}")
            check(token not in window._body_cache, "还原原文应清空作者说明缓存")
            check("中文对照" not in window.detail_view.plain_text(),
                  "还原后面板不应再显示中文对照区块")

            # 全程汉化开着也不应立刻把正文补译回来
            window.tasks.submit = lambda *a, **k: check(False, "还原后不应自动补译")
            window._maybe_translate_detail_body(detail)

            # 用户再次主动点汉化 → 解除还原状态，允许重新翻译
            window.start_page_translation()
            check(window._restored is False, "主动汉化后应解除「已还原」状态")

            # 无效翻页（第 1 页往前）不应取消还原状态
            window._restored = True
            window.turn_page(-1)
            check(window._restored is True, "无效翻页不应取消「已还原」状态")
        finally:
            window.close()
            process_events(200)


def test_body_translation_cache_reuse() -> None:
    """再次打开同一条目应复用作者说明译文，不重复请求。"""
    from client.api import Submission, SubmissionDetail
    from client.settings import Settings as S
    from client.ui.main_window import MainWindow

    with tempfile.TemporaryDirectory() as raw:
        settings = S(config_dir=raw, deepseek_api_key="sk-test-fake-key-123456")
        window = MainWindow(settings, OfflineClient(raw), DeepSeekTranslator(settings),
                            web_widget_factory=None)
        try:
            window.detail_view.resize(600, 800)
            window.continuous_translation = True
            detail = SubmissionDetail(
                submission=Submission(id=12, model="Mod", name="T", profile_url=""),
                body="<p>Some body text.</p>")
            window.detail = detail
            token = (detail.submission.model, detail.submission.id)
            window._body_cache[token] = "缓存的译文"

            window.tasks.submit = lambda *a, **k: check(False, "命中缓存时不应重复请求")
            window._maybe_translate_detail_body(detail)
            check(detail.body_translation == "缓存的译文",
                  f"应回填缓存译文：{detail.body_translation!r}")
            check("中文对照" in window.detail_view.plain_text(), "缓存译文应显示在面板上")
        finally:
            window.close()
            process_events(200)


def _load_should_open_internally():
    """从 web_view.py 源码中取出 should_open_internally，不导入 QtWebEngine。

    该模块顶层依赖 QtWebEngine，未安装时会 ImportError，因此这里只编译
    目标函数所在的少量源码，保证纯逻辑（新窗口路由）在无 WebEngine 环境也能验证。
    """
    import ast as ast_module

    source = (pathlib.Path(__file__).resolve().parents[1]
              / "client" / "ui" / "web_view.py").read_text(encoding="utf-8")
    tree = ast_module.parse(source)
    target = next((node for node in tree.body
                   if isinstance(node, ast_module.FunctionDef)
                   and node.name == "should_open_internally"), None)
    if target is None:
        raise AssertionError("web_view.py 中找不到 should_open_internally")
    module = ast_module.Module(body=[target], type_ignores=[])
    namespace: dict = {}
    exec(compile(module, "web_view.py", "exec"), namespace)  # noqa: S102 - 受控源码
    return namespace["should_open_internally"]


def test_external_launch_avoidance() -> None:
    """尽量不调起外部程序：管理员权限下 Windows 会拦住提升进程的子进程。

    同时验证：图片走内置查看器、站内链接走内嵌浏览器、新窗口不外泄、权限提示。
    """
    from client.api import MediaItem, Submission, SubmissionDetail
    from client.settings import Settings as S
    from client.ui import main_window as mw
    from client.ui.environment import elevation_notice, is_elevated
    from client.ui.main_window import MainWindow

    # 环境检测：不应抛异常；非管理员时不产生提示
    elevated = is_elevated()
    check(isinstance(elevated, bool), "权限检测应返回布尔值")
    if not elevated:
        check(elevation_notice() == "", "非管理员时不应给出权限提示")
    else:
        check("管理员" in elevation_notice(), "管理员时应给出说明")

    # 新窗口请求应留在内嵌浏览器内（否则点下载会跳出系统浏览器）。
    # 注意：不能直接 import client.ui.web_view —— 它依赖 QtWebEngine，
    # 在未安装/不可用的环境会 ImportError；此处按源码模块单独加载该纯函数。
    internal = _load_should_open_internally()
    for url in ("https://gamebanana.com/dl/123", "http://x/y.zip", "about:blank"):
        check(internal(url), f"http 链接应留在内嵌浏览器：{url}")
    for url in ("mailto:a@b.com", "steam://run/1", "", "   "):
        check(not internal(url), f"非 http 协议应交给系统：{url!r}")

    # 图片 URL 判定
    for url in ("https://images.gamebanana.com/img/ss/mods/800-90_a.jpg",
                "https://x/a.PNG", "https://x/a.webp?v=2"):
        check(MainWindow._is_image_url(url.lower()), f"应识别为图片：{url}")
    for url in ("https://gamebanana.com/mods/123", "https://x/a.zip", "https://x/page"):
        check(not MainWindow._is_image_url(url.lower()), f"不应识别为图片：{url}")

    with tempfile.TemporaryDirectory() as raw:
        settings = S(config_dir=raw)
        window = MainWindow(settings, OfflineClient(raw), DeepSeekTranslator(settings),
                            web_widget_factory=None)
        try:
            window.detail_view.resize(600, 800)
            media = [
                MediaItem(type="screenshot",
                          original=f"https://images.gamebanana.com/img/ss/mods/{i}.jpg",
                          medium=f"https://images.gamebanana.com/img/ss/mods/530-90_{i}.jpg",
                          large=f"https://images.gamebanana.com/img/ss/mods/800-90_{i}.jpg",
                          thumb=f"https://images.gamebanana.com/img/ss/mods/100-90_{i}.jpg")
                for i in range(3)]
            window.detail = SubmissionDetail(
                submission=Submission(id=1, model="Mod", name="X", profile_url=""),
                media=media)

            # 记录所有外泄到系统程序与内嵌浏览器的调用
            launched: list[str] = []
            in_app: list[str] = []
            original_open = mw.QDesktopServices.openUrl
            mw.QDesktopServices.openUrl = staticmethod(
                lambda target: launched.append(str(target)))
            window.open_url_in_web = lambda url: in_app.append(url)
            try:
                # 图片 → 内置查看器，绝不调起外部程序
                window._open_detail_link(media[0].original)
                check(hasattr(window, "_image_viewer"), "图片应交给内置查看器")
                check(not launched, f"打开图片不应调起外部程序：{launched}")

                # 游戏条目背景图也要留在内嵌浏览器
                window._open_detail_link("https://images.gamebanana.com/img/ss/mods/100-90_0.jpg")
                check(not launched, "预览图不应调起外部程序")

                # 站内条目 → 内嵌浏览器
                in_app.clear()
                window._open_detail_link("https://gamebanana.com/mods/123")
                check(in_app == ["https://gamebanana.com/mods/123"],
                      f"站内链接应走内嵌浏览器：{in_app}")

                # 相册切换仍留在软件内，不算任何跳转
                in_app.clear()
                launched.clear()
                window._open_detail_link("img:1")
                check(not in_app and not launched, "相册切换不应触发任何跳转")

                # 外部非图片链接 → 交给系统（无法内嵌时的兜底）
                launched.clear()
                window._open_detail_link("https://example.com/file.zip")
                check(len(launched) == 1 and "file.zip" in launched[0],
                      f"外部非图片链接应交给系统：{launched}")
            finally:
                mw.QDesktopServices.openUrl = original_open
        finally:
            window.close()
            process_events(300)


def test_download_dir_fallback_chain() -> None:
    """下载目录必须是**确实可写**的：逐项探测候选链，全部失败才退到临时目录。

    原实现只探测首选目录、不探测回退目录——受限令牌下回退目录同样不可写时
    仍会被当作可用目录返回（实测踩到）。
    """
    import tempfile as tempfile_module

    from client.settings import (
        Settings as S,
        default_download_dir,
        directory_writable,
    )

    with tempfile.TemporaryDirectory() as raw:
        root = pathlib.Path(raw)
        writable = root / "writable"
        writable.mkdir()

        # 可写目录探测为真；不存在的目录会被创建并探测
        check(directory_writable(writable), "可写目录应探测为可写")
        fresh = root / "fresh" / "deeper"
        check(directory_writable(fresh), "不存在的目录应创建后探测为可写")
        check(fresh.is_dir(), "探测过程应创建该目录")
        check(not list(writable.glob(".banana-write-probe-*")),
              "探测文件应被清理，不残留")

        # 候选链：设置值在首位，且包含系统下载目录、主目录、临时目录
        settings = S(config_dir=raw, download_dir=str(writable))
        chain = settings.download_dir_candidates()
        check(chain[0] == writable, f"候选链首位应为设置值：{chain[0]}")
        check(default_download_dir() in chain, "候选链应包含系统下载目录")
        check(pathlib.Path(tempfile_module.gettempdir()) in chain, "候选链应包含临时目录")
        check(len(chain) == len(set(chain)), f"候选链不应重复：{chain}")

        # 可写时直接采用设置值，且不提示回退
        check(settings.resolved_download_dir() == writable, "首选可写时应采用首选")
        check("回退" not in settings.describe_download_dir(), "未回退时不应提示回退")

        # 指向一个必然失败的路径（把文件当目录用）时应回退到可写目录
        blocker = root / "a-file"
        blocker.write_text("x", encoding="utf-8")
        broken = S(config_dir=raw, download_dir=str(blocker))
        resolved = broken.resolved_download_dir()
        check(resolved != blocker, f"不可用的设置目录不应被采用：{resolved}")
        check(directory_writable(resolved), f"回退结果必须确实可写：{resolved}")
        check("回退" in broken.describe_download_dir(),
              f"发生回退时应说明：{broken.describe_download_dir()}")

        # probe=False 只取首选，不做探测（供提示文案使用）
        check(broken.resolved_download_dir(probe=False) == blocker,
              "probe=False 应原样返回首选路径")


def test_main_window() -> None:
    from client import app as app_module
    from client.ui.capabilities import is_webengine_available
    from client.ui.main_window import MainWindow

    # 离屏平台（CI/无人值守）下 Chromium 无法创建 GL 上下文，
    # 因此不在此环境实例化真实 WebEngine 控件；真实窗口路径由
    # tools/smoke_launch.py 在桌面会话中验证。
    offscreen = os.environ.get("QT_QPA_PLATFORM", "").lower() in {"offscreen", "minimal"}
    webengine_expected = app_module.WEBENGINE_AVAILABLE and not offscreen

    with tempfile.TemporaryDirectory() as raw:
        settings = Settings(config_dir=raw, session_profile_dir=str(pathlib.Path(raw) / "web"))
        client = OfflineClient(raw)

        factory = None
        if webengine_expected:
            def factory(s):
                from client.ui import web_view
                try:
                    return web_view.create_web_widget(s, client.session_store)
                except Exception as exc:  # noqa: BLE001
                    print(f"  （网页组件不可用：{type(exc).__name__}: {exc}）")
                    return None

        window = MainWindow(settings, client, DeepSeekTranslator(settings), web_widget_factory=factory)
        process_events(300)

        check(window.grid is not None, "主窗口缺少卡片网格")
        check(window.search_edit is not None, "主窗口缺少搜索框")
        if webengine_expected:
            check(window.web_view is not None, "QtWebEngine 可用时应创建网页标签页")
            check(window.right_tabs.count() == 2, "应同时提供详情与网页两个标签页")
            check(window.web_view.profile.persistentStoragePath() != "",
                  "网页配置目录未持久化，登录态无法复用")
        else:
            check(window.web_view is None, "降级模式不应创建网页标签页")
            check(window.right_tabs.count() == 1, "降级模式应只保留详情标签页")
            reason = "离屏平台不创建真实 WebEngine 控件" if offscreen else "运行环境不满足启用条件"
            print(f"  （{reason}：以纯索引模式验证）")
            # 模块可用性由 capabilities 判断；实际是否启用还要看数据目录可写性，
            # 因此这里只校验 WebEngine 确实没被启用（降级路径生效）。
            check(app_module.WEBENGINE_AVAILABLE is False,
                  "降级路径下 WEBENGINE_AVAILABLE 应为 False")

        window.submissions = sample_submissions(3)
        window.grid.set_submissions(window.submissions, True)

        # 未配置密钥：应给出可判定的阻断原因，不发起翻译。
        # 注意：translate_current_page() 会弹出模态对话框，自检环境无人点击，
        # 因此这里只验证非模态的判定与执行入口。
        check(window.translation_blocker() == "no_key",
              f"未配置密钥时应返回 no_key，实际 {window.translation_blocker()!r}")
        window.start_page_translation()
        # 同步判定：此处不能先处理事件，否则后台搜索完成时会覆盖状态栏
        check("未配置" in window.statusBar().currentMessage(),
              f"缺密钥时应提示配置，实际提示：{window.statusBar().currentMessage()!r}")
        process_events(200)  # 让后台搜索回调落地，确认不会因缺密钥而崩溃

        empty_window = window
        empty_window.submissions = []
        check(empty_window.translation_blocker() == "no_entries", "无条目时应返回 no_entries")
        empty_window.submissions = sample_submissions(3)

        # 设置生效路径（不经过模态对话框）
        window.apply_settings()
        check("设置已保存" in window.statusBar().currentMessage(),
              f"设置生效后未更新状态栏：{window.statusBar().currentMessage()!r}")
        window.restore_originals()
        check(all(not item.translations for item in window.submissions), "还原后仍残留译文")

        print("  ...关闭窗口并回收后台线程", flush=True)
        window.close()
        process_events(300)
        check(window.tasks.active_count == 0, "关闭窗口后仍有后台线程存活")
        print("  ...主窗口检查完成", flush=True)


# ------------------------------------------------------------------ 驱动
def test_game_card_semantics() -> None:
    """游戏卡片必须按「游戏专区」语义展示，而不是伪装成模组条目。"""
    from client.api import Submission
    from client.ui.cards import SubmissionCard

    game = Submission(id=23012, model="Game", name="Neverness To Everness",
                      profile_url="https://gamebanana.com/games/23012",
                      thumbnail="", date_added="2025-07-05 18:52", author="Nyamed")
    game.game_hint = "共 575 条"
    card = SubmissionCard(game)
    try:
        check("游戏专区" in card.meta_label.text(),
              f"游戏卡片未标明专区（{card.meta_label.text()!r}）")
        check("作者" not in card.stats_label.text() and "2025" not in card.meta_label.text(),
              f"游戏卡片不应显示模组式作者/日期（{card.meta_label.text()!r} / "
              f"{card.stats_label.text()!r}）")
        check("575" in card.meta_label.text(),
              f"游戏卡片未展示内容数量提示（{card.meta_label.text()!r}）")
        check("点击进入" in card.stats_label.text(), "游戏卡片未提示可下钻")
    finally:
        card.deleteLater()


def test_card_survives_teardown() -> None:
    """卡片被销毁后，迟到的缩略图回调不应抛异常（翻页时的真实场景）。"""
    from client.api import Submission
    from client.ui.cards import SubmissionCard, is_alive

    submission = Submission(id=1, model="Mod", name="Sample", profile_url="")
    card = SubmissionCard(submission)
    # 用 shiboken 直接释放底层 C++ 对象，确定性复现「卡片已销毁」状态；
    # deleteLater 是延迟删除，并不能保证此刻已失效。
    import shiboken6
    shiboken6.Shiboken.delete(card.title_label)
    shiboken6.Shiboken.delete(card.thumbnail)
    card.deleteLater()
    QApplication.processEvents()
    check(not is_alive(card.title_label), "底层对象已释放时 is_alive 应判定为无效")

    # 模拟网络回调在卡片销毁后才返回：不得抛 RuntimeError
    try:
        card.set_thumbnail(b"not-an-image")
        card.set_thumbnail_failed()
        card.set_translated({"name": "示例"})
    except RuntimeError as exc:
        FAILURES.append(f"迟到的回调对已销毁卡片抛异常：{exc}")


def main() -> int:
    app = QApplication.instance() or QApplication([])
    timed_out = {"value": False}

    def on_timeout() -> None:
        timed_out["value"] = True
        FAILURES.append(f"自检整体超时（>{TIMEOUT_MS} ms），可能有步骤挂起")
        app.quit()

    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(on_timeout)
    guard.start(TIMEOUT_MS)

    tests = (test_card_grid, test_settings_dialog, test_translator_wiring,
             test_download_settings, test_download_logic, test_game_card_semantics,
             test_partition_registry, test_all_partition_card_semantics,
             test_html_sanitizer, test_media_extraction, test_image_scaling,
             test_settings_never_loses_api_key, test_continuous_translation_toggle,
             test_detail_body_translation_timing,
             test_restore_clears_detail_body, test_body_translation_cache_reuse,
             test_external_launch_avoidance, test_download_dir_fallback_chain,
             test_card_survives_teardown, test_translation_bridge,
             test_task_runner, test_main_window)

    def run_all() -> None:
        for test in tests:
            before = len(FAILURES)
            try:
                test()
            except Exception as exc:  # noqa: BLE001 - 自检需要报告任何异常
                FAILURES.append(f"{test.__name__} 抛出异常：{type(exc).__name__}: {exc}")
            print(f"[{'通过' if len(FAILURES) == before else '未通过'}] {test.__name__}", flush=True)
        guard.stop()
        app.quit()

    # 先让事件循环转起来，再执行检查（Qt 事件驱动，避免阻塞式等待）
    QTimer.singleShot(0, run_all)
    app.exec()

    if FAILURES:
        print("\n自检未通过：", file=sys.stderr)
        for item in FAILURES:
            print(f"  * {item}", file=sys.stderr)
        return 1
    print("\n自检通过：卡片网格、设置对话框、后台任务、主窗口与翻译接线全部可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
