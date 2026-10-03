"""设置存储：保存在用户配置目录，绝不写入仓库、日志或验收证据。

真实 API 密钥只落在用户本机 `%APPDATA%\\banana-index\\settings.json`（或环境变量 `DEEPSEEK_API_KEY`）。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "banana-index"
ENV_API_KEY = "DEEPSEEK_API_KEY"


def default_config_dir() -> Path:
    """优先用户配置目录；不可写时退回工作区目录，避免启动即崩溃。

    受限环境（只读漫游配置、沙箱等）下 `%APPDATA%\\banana-index` 可能无法创建，
    此时退回 `<项目目录>/.banana-index`；两者都不可用则用临时目录。

    可用环境变量 `BANANA_INDEX_CONFIG_DIR` 显式指定（看门狗用它保证父子进程一致）。
    """
    override = os.environ.get("BANANA_INDEX_CONFIG_DIR", "").strip()
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        candidates.append(Path(base) / APP_NAME)
    candidates.append(Path.home() / f".{APP_NAME}")
    candidates.append(Path.cwd() / f".{APP_NAME}")
    candidates.append(Path(tempfile.gettempdir()) / APP_NAME)

    # 恢复普通权限后继续使用原有配置，避免从沙箱回退目录切换到空目录。
    # 显式指定的目录仍优先；不复制或覆盖用户密钥与浏览器会话。
    if not override:
        candidates.sort(key=lambda path: not (path / "settings.json").is_file())

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError:
            continue
        return candidate
    return candidates[-1]


def ensure_config_dir(path: Path) -> Path:
    """确保配置目录可用；失败时抛出带中文说明的 OSError。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        fallback = default_config_dir()
        raise OSError(
            f"配置目录不可写：{path}（{exc}）。可改用自动回退目录：{fallback}，"
            f"或设置环境变量 APPDATA 指向可写位置。") from exc
    return path


@dataclass
class Settings:
    """客户端设置。密钥字段默认空，只在用户本机配置里出现。"""

    # 翻译服务（当前仅 DeepSeek）
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    translate_max_concurrency: int = 4
    translate_batch_size: int = 20
    translate_timeout_seconds: int = 60
    translate_max_retries: int = 2
    translate_target_language: str = "简体中文"
    # 请求行为
    request_timeout_seconds: int = 25
    request_delay_ms: int = 250
    per_page: int = 24
    # 下载
    download_dir: str = ""
    download_ask_each_time: bool = False
    download_open_folder_after: bool = False
    # 界面
    ui_language: str = "zh_CN"
    show_original_text: bool = True
    web_home_url: str = "https://gamebanana.com/"
    # 开启后每次换页都自动汉化（「全程一键汉化」）
    auto_translate_all_pages: bool = False
    open_source_notice_acknowledged: bool = False
    # 会话
    session_profile_dir: str = ""
    config_dir: str = field(default="", compare=False)
    # 读取失败时的诊断信息（非持久化字段，写盘时会被排除）
    load_error: str = field(default="", compare=False)
    load_error_backup: str = field(default="", compare=False)

    def resolved_api_key(self) -> str:
        """优先环境变量，其次配置文件；两者都空表示未配置。"""
        return os.environ.get(ENV_API_KEY, "").strip() or self.deepseek_api_key.strip()

    def masked_api_key(self) -> str:
        key = self.resolved_api_key()
        if not key:
            return "（未配置）"
        if len(key) <= 8:
            return "*" * len(key)
        return f"{key[:4]}{'*' * 8}{key[-4:]}"

    def is_translation_ready(self) -> bool:
        return bool(self.resolved_api_key())

    def normalized(self) -> "Settings":
        self.translate_max_concurrency = max(1, min(16, int(self.translate_max_concurrency)))
        self.translate_batch_size = max(1, min(80, int(self.translate_batch_size)))
        self.translate_timeout_seconds = max(5, min(300, int(self.translate_timeout_seconds)))
        self.translate_max_retries = max(0, min(6, int(self.translate_max_retries)))
        self.request_timeout_seconds = max(5, min(120, int(self.request_timeout_seconds)))
        self.request_delay_ms = max(0, min(5000, int(self.request_delay_ms)))
        self.per_page = max(6, min(60, int(self.per_page)))
        if not self.download_dir.strip():
            self.download_dir = str(default_download_dir())
        return self

    def download_dir_candidates(self) -> list[Path]:
        """下载目录候选链：设置值 → 系统下载目录 → 用户主目录 → 临时目录。

        `download_dir` 为空时直接从系统下载目录开始，避免重复同一路径。
        """
        chain: list[Path] = []
        configured = self.download_dir.strip()
        if configured:
            chain.append(Path(configured))
        for item in (default_download_dir(), Path.home(), Path(tempfile.gettempdir())):
            if item not in chain:
                chain.append(item)
        return chain

    def resolved_download_dir(self, probe: bool = True) -> Path:
        """返回**确实可写**的下载目录。

        逐项探测候选链（真实写一个探针文件再删除），全部失败才退到临时目录。
        原实现只探测首选目录、不探测回退目录，当回退目录同样不可写时仍会把它
        当作可用目录返回——受限环境（低完整性令牌、受限沙箱）下实测会一路失败。
        """
        chain = self.download_dir_candidates()
        if not probe:
            return chain[0]
        for candidate in chain:
            if directory_writable(candidate):
                return candidate
        return Path(tempfile.gettempdir())

    def describe_download_dir(self) -> str:
        """说明当前下载目录，以及是否发生了回退。"""
        chosen = self.resolved_download_dir()
        configured = self.download_dir.strip()
        if configured and chosen != Path(configured):
            return f"下载目录：{chosen}（设置的 {configured} 不可写，已自动回退）"
        return f"下载目录：{chosen}"

    def to_json(self) -> str:
        """序列化设置；诊断字段与 config_dir 不写盘，避免污染配置文件。"""
        payload = asdict(self)
        for key in ("load_error", "load_error_backup"):
            payload.pop(key, None)
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def directory_writable(directory: Path) -> bool:
    """真实探测目录是否可写：创建目录并写入探针文件，随后删除。

    只看是否存在或能否 mkdir 都不够——受限令牌下目录可能可读可列举但不可写。
    """
    probe_path: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe_path = directory / f".banana-write-probe-{uuid.uuid4().hex[:8]}"
        probe_path.write_text("ok", encoding="utf-8")
        return True
    except OSError:
        return False
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink()
            except OSError:
                pass


def default_download_dir() -> Path:
    """默认下载目录：系统「下载」文件夹；不存在时退回用户主目录。"""
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        return downloads
    return Path.home()


def unique_path(directory: Path, filename: str) -> Path:
    """把文件名放进目录并避免覆盖：重名时追加 (1)、(2)…"""
    safe_name = Path(filename).name or "download"
    candidate = directory / safe_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(1, 1000):
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem} ({uuid.uuid4().hex[:6]}){suffix}"


def settings_path(config_dir: Path | None = None) -> Path:
    return (config_dir or default_config_dir()) / "settings.json"


def describe_download_state(state_name: str, filename: str, directory: str,
                            interrupt_reason: str = "") -> tuple[bool, str]:
    """把下载状态翻译成界面文案，返回 (是否结束, 文案)。

    与 Qt 解耦，因此可以在无窗口、无 QtWebEngine 的环境下自检。
    """
    if state_name == "DownloadCompleted":
        return True, f"下载完成：{Path(directory) / filename}"
    if state_name == "DownloadCancelled":
        return True, f"已取消下载：{filename}"
    if state_name == "DownloadInterrupted":
        return True, f"下载中断：{filename}（{interrupt_reason or '未知原因'}）"
    return False, f"下载中：{filename}"


def resolve_download_target(directory: Path, filename: str) -> tuple[Path, str]:
    """给出不覆盖已有文件的最终保存路径，返回 (完整路径, 最终文件名)。"""
    target = unique_path(directory, filename)
    return target, target.name


def load_settings(config_dir: Path | None = None) -> Settings:
    """读取设置。解析失败时**保留原文件**并备份，避免静默丢失密钥。"""
    directory = config_dir or default_config_dir()
    path = directory / "settings.json"
    settings = Settings(config_dir=str(directory))
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            # 解析失败不能装作空配置：否则后续 save_settings 会把用户密钥覆盖掉
            backup = path.with_name("settings.broken.json")
            try:
                shutil.copy2(path, backup)
            except OSError:
                backup = None
            settings.load_error = f"{type(exc).__name__}: {exc}"
            settings.load_error_backup = str(backup) if backup else ""
        else:
            known = {f.name for f in fields(Settings)}
            for key, value in raw.items():
                if key in known and key not in {"config_dir", "load_error", "load_error_backup"}:
                    setattr(settings, key, value)
    if not settings.session_profile_dir:
        settings.session_profile_dir = str(directory / "webprofile")
    return settings.normalized()


def save_settings(settings: Settings, config_dir: Path | None = None,
                  allow_key_clear: bool = False) -> Path:
    """写入设置。

    安全约束：**不允许用空密钥覆盖已存在的非空密钥**——加载失败或调用方
    构造了默认 Settings 时，静默覆盖会让用户丢失密钥（实测踩过）。
    确需清空请显式传 `allow_key_clear=True`。
    """
    directory = config_dir or Path(settings.config_dir or default_config_dir())
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "settings.json"

    if not allow_key_clear and not settings.deepseek_api_key.strip() and path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8-sig"))
            existing_key = str(existing.get("deepseek_api_key") or "").strip()
        except (OSError, ValueError):
            existing_key = ""
        if existing_key:
            # 继承磁盘上已有的密钥，避免把它抹掉
            settings.deepseek_api_key = existing_key

    tmp = path.with_suffix(".tmp")
    tmp.write_text(settings.normalized().to_json(), encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path
