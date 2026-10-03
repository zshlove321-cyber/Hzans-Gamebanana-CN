"""共享工具：会话 Cookie 持久化、磁盘缓存、带重试的 HTTP 请求。

会话说明（对应 UI.txt「套用网页登录凭据状态」）：
  公开 apiv11 接口无需登录即可读取；登录态的作用是让站点识别访问者身份
  （订阅、可见性与下载权限）。因此客户端把浏览器会话 Cookie 保存到用户目录，
  并可选地附加到 API 请求上，使索引请求也带上同一身份。
"""
from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
RETRY_STATUS = {429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    """面向用户可见的接口错误（中文消息）。retryable 标记是否值得重试。"""

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class LoginRequiredError(ApiError):
    """The site explicitly requested authentication; not inferred from missing cookies."""


class SiteVerificationRequiredError(ApiError):
    """A browser verification challenge, distinct from account login."""


def retry_call(attempts: int = 2, base_delay: float = 0.6,
               on_error: Callable[[Exception], None] | None = None):
    """同步重试装饰器：只重试网络类异常与显式标记可重试的错误，参数错误立即失败。"""

    def decorator(func):
        def wrapper(*args, **kwargs):
            last: Exception | None = None
            for attempt in range(attempts + 1):
                try:
                    return func(*args, **kwargs)
                except ApiError as exc:
                    last = exc
                    if not exc.retryable:
                        raise
                except requests.RequestException as exc:
                    last = exc
                if attempt < attempts:
                    time.sleep(base_delay * (2 ** attempt) + random.uniform(0, 0.2))
            raise ApiError(f"请求失败，已重试 {attempts} 次：{last}", retryable=True) from last

        wrapper.__name__ = func.__name__
        return wrapper

    return decorator


class DiskCache:
    """带 TTL 的 JSON 磁盘缓存，避免重复抓取同一 URL。

    写盘策略（性能关键）：**合并写盘**。
    早期实现每次 put 都序列化整个文件并落盘；实测缓存 550 KB 时单次 4 ms，
    一页 24 张缩略图就要在同一把锁内串行写 25 次（约 100 ms），
    既拖慢界面又抢占工作线程。现在改为：
      * put 只更新内存并把缓存标记为脏；
      * 距上次落盘超过 flush_interval 秒才真正写盘（默认 2 秒）；
      * 退出、切换页面等关键点调用 flush(force=True) 兜底。

    另外 **不缓存二进制响应**（缩略图等）：这类内容体积大、命中率低，
    且 HTTP 层与 Qt 自带图片缓存已能覆盖，放进 JSON 缓存会让写放大急剧上升。
    """

    def __init__(self, path: Path, ttl_seconds: int = 900,
                 flush_interval: float = 2.0) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self.flush_interval = flush_interval
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._dirty = False
        # 初始化为当前时间：否则首次 put 会因为「距上次落盘很久」而立刻写盘，
        # 破坏合并写盘的效果。
        self._last_flush = time.time()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self._data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            if time.time() - float(entry.get("at", 0)) > self.ttl_seconds:
                return None
            return entry.get("value")

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = {"at": time.time(), "value": value}
            self._dirty = True
        self.flush()

    def flush(self, force: bool = False) -> None:
        """合并写盘：整个「取快照 → 写临时文件 → 替换」都在锁内完成。

        距上次落盘不足 flush_interval 且非强制时直接返回，把多次 put 合并成一次写。
        """
        with self._lock:
            if not self._dirty:
                return
            now = time.time()
            if not force and (now - self._last_flush) < self.flush_interval:
                return
            payload = json.dumps(self._data, ensure_ascii=False)
            self._dirty = False
            self._last_flush = now
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(payload, encoding="utf-8", newline="\n")
                tmp.replace(self.path)
            except OSError:
                # 缓存写失败不应影响主流程；下次 put 会再次尝试
                self._dirty = True

    def close(self) -> None:
        """退出前落盘，保证合并写盘不丢数据。"""
        self.flush(force=True)

    def _mark_dirty(self) -> None:
        """仅供基准测试：把缓存标记为待落盘。"""
        with self._lock:
            self._dirty = True


@dataclass
class SessionStore:
    """站点 Cookie 持久化（登录一次，长期复用）。"""

    path: Path

    def load(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.get("cookies", {}).items()}

    def save(self, cookies: dict[str, str]) -> None:
        # 只存站点自身的 Cookie，避免把站点无关凭据写到磁盘
        domain_cookies = {k: v for k, v in cookies.items() if k and v}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"cookies": domain_cookies, "saved_at": time.time()},
                                  ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        tmp.replace(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def has_session(self) -> bool:
        return bool(self.load())
