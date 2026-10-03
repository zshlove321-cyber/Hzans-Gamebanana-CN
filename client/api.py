"""GameBanana 公开 apiv11 客户端与数据归一化。

依据 `docs/research/api-notes.md` 的实测结论实现：
  * 无效路由会返回 HTTP 200 + `_sErrorCode`，必须检查响应体；
  * 部分响应带 UTF-8 BOM，统一按 utf-8-sig 解码；
  * 列表记录字段形态不完全一致（`_aTags` 为字符串数组或对象数组），归一化时都要兼容。
"""
from __future__ import annotations

import json
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import quote, urlparse

import requests

from .common import (DEFAULT_USER_AGENT, ApiError, DiskCache, SessionStore, retry_call,
                     LoginRequiredError, SiteVerificationRequiredError)

API_BASE = "https://gamebanana.com/apiv11"
SITE_BASE = "https://gamebanana.com"

MODEL_LABELS = {
    "Mod": "模组",
    "Game": "游戏",
    "Sound": "音效",
    "Model": "模型",
    "Skin": "皮肤",
    "Map": "地图",
    "Spray": "喷漆",
    "Tool": "工具",
    "ModCategory": "模组分类",
    "Project": "项目",
    "Concept": "概念",
    "Idea": "点子",
    "Blog": "博客",
    "News": "新闻",
    "Question": "提问",
    "Poll": "投票",
    "Review": "评测",
    "Member": "成员",
    "Contest": "比赛",
    "Bug": "站点缺陷",
    "Wip": "半成品",
    "Wiki": "百科",
    "Thread": "论坛帖",
    "Request": "求助",
    "Studio": "工作室",
    "Club": "社团",
    "Jam": "创作活动",
    "Tutorial": "教程",
    "Article": "文章",
    "App": "站点应用",
    "Medal": "勋章",
    "Award": "奖项",
    "Podcast": "播客",
    "Playlist": "播放列表",
    "Event": "活动",
}

SORT_OPTIONS = {
    "default": "默认",
    "new": "最新",
    "updated": "最近更新",
    "popular": "最热",
    "liked": "最多点赞",
    "downloaded": "最多下载",
}


# ---------------------------------------------------------------- 分区登记表
# 站点把不同类型的内容分成若干「分区」。实测（docs/research/partition-routes.json、
# partition-fields.json）：所有分区都支持
#   <Model>/Index        浏览该分区内容
#   <Model>/<id>/ProfilePage  读取该条目的详情（含正文）
# 因此客户端用同一套流程处理，不再为单个类型写特例。
#
#   kind=content  有文件/正文的投稿类内容 → 详情页展示正文、文件、致谢
#   kind=profile  人/组织 → 详情页展示简介与统计，无文件
#   kind=category 分类/容器 → 详情页展示归属与简介，主要价值是浏览其内容
PARTITIONS: dict[str, dict[str, str]] = {
    "Mod": {"label": "模组", "kind": "content", "noun": "模组"},
    "Sound": {"label": "音效", "kind": "content", "noun": "音效"},
    "Model": {"label": "模型", "kind": "content", "noun": "模型"},
    "Spray": {"label": "喷漆", "kind": "content", "noun": "喷漆"},
    "Tool": {"label": "工具", "kind": "content", "noun": "工具"},
    "Wip": {"label": "半成品", "kind": "content", "noun": "半成品"},
    "Project": {"label": "项目", "kind": "content", "noun": "项目"},
    "Concept": {"label": "概念", "kind": "content", "noun": "概念"},
    "Skin": {"label": "皮肤", "kind": "content", "noun": "皮肤"},
    "Map": {"label": "地图", "kind": "content", "noun": "地图"},
    "Game": {"label": "游戏", "kind": "category", "noun": "游戏专区"},
    "ModCategory": {"label": "模组分类", "kind": "category", "noun": "分类"},
    "Member": {"label": "成员", "kind": "profile", "noun": "成员"},
    "Studio": {"label": "工作室", "kind": "profile", "noun": "工作室"},
    "Club": {"label": "社团", "kind": "profile", "noun": "社团"},
    "Thread": {"label": "论坛帖", "kind": "content", "noun": "帖子"},
    "Request": {"label": "求助", "kind": "content", "noun": "求助"},
    "Question": {"label": "提问", "kind": "content", "noun": "提问"},
    "News": {"label": "新闻", "kind": "content", "noun": "新闻"},
    "Blog": {"label": "博客", "kind": "content", "noun": "博客"},
    "Article": {"label": "文章", "kind": "content", "noun": "文章"},
    "Tutorial": {"label": "教程", "kind": "content", "noun": "教程"},
    "Review": {"label": "评测", "kind": "content", "noun": "评测"},
    "Poll": {"label": "投票", "kind": "content", "noun": "投票"},
    "Contest": {"label": "比赛", "kind": "content", "noun": "比赛"},
    "Jam": {"label": "创作活动", "kind": "content", "noun": "创作活动"},
    "Event": {"label": "活动", "kind": "content", "noun": "活动"},
    "Podcast": {"label": "播客", "kind": "content", "noun": "播客"},
    "Idea": {"label": "点子", "kind": "content", "noun": "点子"},
    "Bug": {"label": "站点缺陷", "kind": "content", "noun": "缺陷"},
    "App": {"label": "站点应用", "kind": "content", "noun": "应用"},
    "Wiki": {"label": "百科", "kind": "content", "noun": "百科"},
    "Medal": {"label": "勋章", "kind": "profile", "noun": "勋章"},
    "Award": {"label": "奖项", "kind": "profile", "noun": "奖项"},
    "Playlist": {"label": "播放列表", "kind": "category", "noun": "播放列表"},
}

# 分类类分区如何下钻到「该分类的内容」：
#   game     → 用该游戏的子流（Game/{id}/Subfeed），可指定类型
#   category → 用 <> Index 的 Generic_Category 过滤器
CATEGORY_DRILLDOWN: dict[str, str] = {
    "Game": "game",
    "ModCategory": "category",
}

# 有文件下载的分区（其余分区详情页不展示下载表）
DOWNLOADABLE_KINDS = {"content"}

# 关键词搜索（Util/Search/Results）支持按这些类型过滤；
# 其余分区（成员/工作室/社团/分类…）只能通过各自的 <Model>/Index 浏览。
SEARCHABLE_MODELS = frozenset({
    "Mod", "Game", "Sound", "Model", "Spray", "Tool", "Wip", "Project", "Concept",
    "Skin", "Map", "News", "Blog", "Article", "Tutorial", "Review", "Poll",
    "Contest", "Jam", "Event", "Podcast", "Idea", "Bug", "App", "Wiki",
    "Question", "Request", "Thread", "Member", "Studio", "Club",
})


def partition_of(model: str) -> dict[str, str]:
    """返回分区的展示信息；未登记的类型给出保守默认值。"""
    info = PARTITIONS.get(model)
    if info:
        return info
    return {"label": model or "未知", "kind": "content", "noun": model or "条目"}


def partition_label(model: str) -> str:
    return partition_of(model)["label"]


def partition_kind(model: str) -> str:
    return partition_of(model)["kind"]


def is_partition_content(model: str) -> bool:
    """该分区是否为「可下载的投稿内容」（决定详情页是否展示文件与正文）。"""
    return partition_kind(model) in DOWNLOADABLE_KINDS


def model_label(model: str) -> str:
    """类型的中文名；优先取分区登记表，其次取模型译名表。"""
    if model in PARTITIONS:
        return PARTITIONS[model]["label"]
    return MODEL_LABELS.get(model, model or "未知")


def normalize_tags(raw: Any) -> list[str]:
    """`_aTags` 在列表接口是字符串数组，在详情页是 {_sTitle,_sValue} 数组。"""
    if not isinstance(raw, list):
        return []
    tags: list[str] = []
    for item in raw:
        if isinstance(item, str):
            tags.append(item)
        elif isinstance(item, dict):
            title = str(item.get("_sTitle") or "").strip()
            value = str(item.get("_sValue") or "").strip()
            if title and value:
                tags.append(f"{title}: {value}")
            elif value:
                tags.append(value)
    return tags


def format_timestamp(value: Any) -> str:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone()
    return moment.strftime("%Y-%m-%d %H:%M")


def format_filesize(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


@dataclass
class Submission:
    """归一化后的条目：索引界面直接消费该结构。"""

    id: int
    model: str
    name: str
    profile_url: str
    thumbnail: str = ""
    author: str = ""
    author_url: str = ""
    game: str = ""
    game_url: str = ""
    root_category: str = ""
    sub_category: str = ""
    tags: list[str] = field(default_factory=list)
    version: str = ""
    views: int = 0
    likes: int = 0
    posts: int = 0
    date_added: str = ""
    date_updated: str = ""
    has_files: bool = False
    is_obsolete: bool = False
    was_featured: bool = False
    # 游戏专区的条目总数提示（列表卡片用；详情走 PartitionDetail）
    game_hint: str = ""
    # 翻译结果由界面层填充，保持 API 层与翻译层解耦
    translations: dict[str, str] = field(default_factory=dict)

    @property
    def model_label(self) -> str:
        return model_label(self.model)

    def translatable_fields(self) -> dict[str, str]:
        """需要送翻译的字段；不含 ID、URL、版本号、校验值。"""
        fields = {"name": self.name}
        if self.root_category and not _is_chinese(self.root_category):
            fields["root_category"] = self.root_category
        if self.sub_category and not _is_chinese(self.sub_category):
            fields["sub_category"] = self.sub_category
        for index, tag in enumerate(self.tags):
            if not _is_chinese(tag):
                fields[f"tag{index}"] = tag
        return {key: value for key, value in fields.items() if value.strip()}

    def display(self, field_name: str) -> str:
        """优先返回译文，缺失或未启用时回退原文。"""
        return self.translations.get(field_name) or getattr(self, field_name, "")

    def display_tag(self, index: int) -> str:
        return self.translations.get(f"tag{index}") or self.tags[index]


def is_chinese(text: str) -> bool:
    """判断文本是否已含中文（用于避免重复翻译已是中文的内容）。"""
    return any("\u4e00" <= char <= "\u9fff" for char in text)


# 兼容内部旧引用
_is_chinese = is_chinese


@dataclass
class GameRef:
    id: int
    name: str
    profile_url: str = ""
    icon_url: str = ""
    submissions: int = 0
    translation: str = ""

    def display(self) -> str:
        return self.translation or self.name


@dataclass
class CategoryRef:
    id: int
    name: str
    model: str = "ModCategory"
    profile_url: str = ""
    icon_url: str = ""

    def display(self) -> str:
        return self.name


@dataclass
class SearchOutcome:
    submissions: list[Submission]
    total: int
    page: int
    per_page: int
    is_complete: bool
    sections: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FileInfo:
    id: int
    filename: str
    filesize: int
    download_url: str
    md5: str = ""
    added: str = ""
    downloads: int = 0
    av_result: str = ""


@dataclass
class MediaItem:
    """预览媒体：保留多种尺寸，供详情页与画廊按需取用。"""

    type: str = "screenshot"
    caption: str = ""
    original: str = ""
    medium: str = ""   # 530-90_
    large: str = ""    # 800-90_
    thumb: str = ""    # 100-90_

    def best(self, prefer: str = "medium") -> str:
        order = {"large": (self.large, self.original, self.medium, self.thumb),
                 "medium": (self.medium, self.large, self.original, self.thumb),
                 "thumb": (self.thumb, self.medium, self.original)}.get(
                     prefer, (self.medium, self.large, self.original, self.thumb))
        for candidate in order:
            if candidate:
                return candidate
        return ""


@dataclass
class SubmissionDetail:
    submission: Submission
    description: str = ""
    files: list[FileInfo] = field(default_factory=list)
    download_page_url: str = ""
    credits: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    # 详情页媒体：预览图（多尺寸）与嵌入视频/媒体链接
    media: list[MediaItem] = field(default_factory=list)
    embedded_media: list[str] = field(default_factory=list)
    # 作者说明的中文对照（由界面层翻译后回填，与站点双语排版一致）
    body_translation: str = ""
    # 通用分区详情：正文、统计、归属与可下钻动作
    body: str = ""
    kind: str = "content"
    stats: list[tuple[str, str]] = field(default_factory=list)
    origin: list[tuple[str, str]] = field(default_factory=list)
    # 该分区自身的内容总数（用于「继续浏览」提示）
    partition_total: int = 0
    # 分类类分区：可下钻查看其内容（game=游戏子流，category=索引过滤器）
    drilldown: str = ""
    drilldown_label: str = ""


def _pick_thumbnail(media: Any) -> str:
    if not isinstance(media, dict):
        return ""
    images = media.get("_aImages")
    if not isinstance(images, list) or not images:
        return ""
    first = images[0]
    if not isinstance(first, dict):
        return ""
    if first.get("_sUrl"):
        return str(first["_sUrl"])
    base = str(first.get("_sBaseUrl") or "").rstrip("/")
    for key in ("_sFile100", "_sFile", "_sFile220", "_sFile530"):
        if first.get(key):
            return f"{base}/{first[key]}"
    return ""


def submission_from_record(record: dict) -> Submission:
    game = record.get("_aGame") if isinstance(record.get("_aGame"), dict) else {}
    submitter = record.get("_aSubmitter") if isinstance(record.get("_aSubmitter"), dict) else {}
    root = record.get("_aRootCategory") if isinstance(record.get("_aRootCategory"), dict) else {}
    sub = record.get("_aSubCategory") if isinstance(record.get("_aSubCategory"), dict) else {}
    return Submission(
        id=int(record.get("_idRow") or 0),
        model=str(record.get("_sModelName") or ""),
        name=str(record.get("_sName") or ""),
        profile_url=str(record.get("_sProfileUrl") or ""),
        thumbnail=_pick_thumbnail(record.get("_aPreviewMedia")),
        author=str(submitter.get("_sName") or ""),
        author_url=str(submitter.get("_sProfileUrl") or ""),
        game=str(game.get("_sName") or ""),
        game_url=str(game.get("_sProfileUrl") or ""),
        root_category=str(root.get("_sName") or ""),
        sub_category=str(sub.get("_sName") or ""),
        tags=normalize_tags(record.get("_aTags")),
        version=str(record.get("_sVersion") or ""),
        views=int(record.get("_nViewCount") or 0),
        likes=int(record.get("_nLikeCount") or 0),
        posts=int(record.get("_nPostCount") or 0),
        date_added=format_timestamp(record.get("_tsDateAdded")),
        date_updated=format_timestamp(record.get("_tsDateModified") or record.get("_tsDateUpdated")),
        has_files=bool(record.get("_bHasFiles")),
        is_obsolete=bool(record.get("_bIsObsolete")),
        was_featured=bool(record.get("_bWasFeatured")),
    )


class GameBananaClient:
    """只读客户端：搜索、分类浏览、条目详情。不做整站爬取。"""

    def __init__(self, timeout: int = 25, request_delay_ms: int = 250,
                 cache: DiskCache | None = None, session_store: SessionStore | None = None) -> None:
        self.timeout = timeout
        self.request_delay_ms = max(0, request_delay_ms)
        self.cache = cache
        self.session_store = session_store
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self._last_request_at = 0.0
        self._injected_cookie_names: set[str] = set()

    # --- 底层请求 ---
    def _throttle(self) -> None:
        if self.request_delay_ms <= 0:
            return
        elapsed = (time.time() - self._last_request_at) * 1000
        if elapsed < self.request_delay_ms:
            time.sleep((self.request_delay_ms - elapsed) / 1000)
        self._last_request_at = time.time()

    def apply_session_cookies(self) -> int:
        """把持久化的站点登录 Cookie 附加到会话上。返回注入条数。"""
        if not self.session_store:
            return 0
        cookies = self.session_store.load()
        for cookie in list(self.session.cookies):
            domain = cookie.domain.lstrip('.').lower()
            if ((cookie.name in self._injected_cookie_names or cookie.name in cookies)
                    and (domain == 'gamebanana.com' or domain.endswith('.gamebanana.com'))):
                # Replace a canonical cookie atomically; remove differently scoped
                # API cookies with the same name so an old anonymous session cannot
                # precede the browser's authenticated cookie in the request header.
                if cookie.name in cookies and cookie.domain == '.gamebanana.com' and cookie.path == '/':
                    continue
                try:
                    self.session.cookies.clear(cookie.domain, cookie.path, cookie.name)
                except KeyError:
                    pass  # Another request may already have expired this cookie.
        for name, value in cookies.items():
            self.session.cookies.set(name, value, domain=".gamebanana.com")
        self._injected_cookie_names = set(cookies)
        return len(cookies)

    def has_login_session(self) -> bool:
        return bool(self.session_store and self.session_store.has_session())

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = ""
        if params:
            query = "?" + "&".join(f"{quote(str(k), safe='[]')}={quote(str(v), safe='')}"
                                   for k, v in params.items() if v not in (None, ""))
        url = f"{API_BASE}/{path}{query}"
        # Pick up browser login/logout without requiring an application restart.
        self.apply_session_cookies()
        identity = {cookie.name: cookie.value for cookie in self.session.cookies
                    if cookie.domain.lstrip('.').lower() == 'gamebanana.com'
                    or cookie.domain.lstrip('.').lower().endswith('.gamebanana.com')}
        cache_key = url
        if identity:
            cache_key += '|session:' + hashlib.sha256(
                json.dumps(identity, sort_keys=True).encode('utf-8')).hexdigest()
        if self.cache is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached
        recovering = False

        @retry_call(attempts=2)
        def fetch():
            nonlocal recovering
            self._throttle()
            request_url = url
            if recovering:
                request_url += ('&' if '?' in url else '?') + f'_banana_retry={time.time_ns()}'
            try:
                response = self.session.get(request_url, timeout=self.timeout)
            except requests.RequestException:
                recovering = True
                raise
            text = response.content.decode('utf-8-sig', errors='replace').lstrip()
            final_url = urlparse(getattr(response, 'url', '') or request_url)
            final_path = final_url.path.rstrip('/').lower()
            host = (final_url.hostname or '').lower()
            site_response = host == 'gamebanana.com' or host.endswith('.gamebanana.com')
            login_redirect = site_response and final_path in {'/members/account/login', '/account/login', '/login'}
            # A login link in an ordinary HTML page is NOT evidence of required login.
            login_form = ('<form' in text.lower() and 'type="password"' in text.lower()
                          and ('action="/members/account/login' in text.lower() or 'action="/account/login' in text.lower()))
            lowered = text.lower()
            if (str(response.headers.get('cf-mitigated', '')).lower() == 'challenge'
                    or ('<html' in lowered and any(marker in lowered for marker in
                        ['cf-chl-', '/cdn-cgi/challenge-platform/', '<title>just a moment']))):
                raise SiteVerificationRequiredError('站点要求网页验证。请在内嵌网页完成验证后重试；这不等同于未登录。')
            if (site_response and (response.status_code == 401 or login_form)) or login_redirect:
                raise LoginRequiredError('站点明确要求登录，或当前登录状态已失效。请在内嵌网页登录后重试。')
            if text.startswith('{'):
                try:
                    error_body = json.loads(text)
                except ValueError:
                    error_body = {}
                if (isinstance(error_body, dict) and str(error_body.get('_sErrorCode', '')).upper()
                        in {'LOGIN_REQUIRED', 'NOT_LOGGED_IN', 'AUTH_REQUIRED', 'UNAUTHENTICATED'}):
                    raise LoginRequiredError('站点明确要求登录，或当前登录状态已失效。请在内嵌网页登录后重试。')
            if response.status_code >= 500 or response.status_code == 429:
                recovering = True
                raise ApiError(f'站点暂时不可用（HTTP {response.status_code}）', retryable=True)
            if response.status_code >= 400:
                raise ApiError(f'站点拒绝或无法找到该条目（HTTP {response.status_code}），不能据此判断需要登录。')
            if not text.startswith(('{', '[')):
                recovering = True
                raise ApiError(f'站点未返回可读取的详情数据（HTTP {response.status_code}），请稍后重试或查看原网页。', retryable=True)
            try:
                data = json.loads(text)
            except ValueError as exc:
                recovering = True
                raise ApiError('站点返回的数据不完整，请稍后重试。', retryable=True) from exc
            if isinstance(data, dict) and data.get('_sErrorCode'):
                code = str(data['_sErrorCode']).upper()
                if code in {'LOGIN_REQUIRED', 'NOT_LOGGED_IN', 'AUTH_REQUIRED', 'UNAUTHENTICATED'}:
                    raise LoginRequiredError('站点明确要求登录，或当前登录状态已失效。请在内嵌网页登录后重试。')
                if code == 'NO_SUCH_ROUTE':
                    raise ApiError(f'该接口路由不存在（{path}）')
                raise ApiError(f'接口返回错误：{code}')
            return data

        data = fetch()
        if self.cache is not None:
            self.cache.put(cache_key, data)
        return data

    # --- 搜索与浏览 ---
    def search(self, keyword: str, page: int = 1, per_page: int = 15,
               model: str | None = None) -> SearchOutcome:
        params: dict[str, Any] = {"_sSearchString": keyword, "_nPage": page, "_nPerpage": per_page}
        if model:
            params["_sModelName"] = model
        data = self._get_json("Util/Search/Results", params)
        metadata = data.get("_aMetadata") or {}
        records = data.get("_aRecords") or []
        total = int(metadata.get("_nRecordCount") or 0)
        sections = []
        for item in metadata.get("_aSectionMatchCounts") or []:
            if not isinstance(item, dict):
                continue
            count = int(item.get("_nMatchCount") or 0)
            # 站点对部分类型会返回「全站总数」而不是本次搜索命中数
            # （实测关键词仅 998 条命中时 Mod 段却报 297824），
            # 这类明显不可信的数字不呈现，避免误导分类筛选。
            if count > max(total, 1):
                continue
            sections.append({
                "model": item.get("_sModelName") or "",
                "label": model_label(str(item.get("_sModelName") or "")),
                "title": item.get("_sPluralTitle") or "",
                "count": count,
            })
        return SearchOutcome(
            submissions=[submission_from_record(r) for r in records if isinstance(r, dict)],
            total=total,
            page=page,
            per_page=int(metadata.get("_nPerpage") or per_page),
            is_complete=bool(metadata.get("_bIsComplete")),
            sections=sections,
        )

    def browse_mods(self, page: int = 1, per_page: int = 24, game_id: int | None = None,
                    category_id: int | None = None, sort: str | None = None) -> SearchOutcome:
        return self.browse_model("Mod", page=page, per_page=per_page, game_id=game_id,
                                 category_id=category_id, sort=sort)

    def browse_model(self, model: str, page: int = 1, per_page: int = 24,
                     game_id: int | None = None, category_id: int | None = None,
                     sort: str | None = None) -> SearchOutcome:
        """浏览某个模型类型的最新条目。

        `Model/Index` 是各类目共用的索引路由（实测 Mod/Sound/Model/Spray/Tool/Wip/Project/
        Concept/Game 均可用，Skin 与 Map 因站点缺类文件返回 PHP 警告）。
        """
        params: dict[str, Any] = {"_nPage": page, "_nPerpage": per_page,
                                  "_csvModelInGameCategory": "true"}
        if game_id:
            params["_aFilters[Generic_Game]"] = game_id
        if category_id:
            params["_aFilters[Generic_Category]"] = category_id
        if sort and sort != "default":
            params["_sSort"] = sort
        data = self._get_json(f"{model}/Index", params)
        metadata = data.get("_aMetadata") or {}
        records = data.get("_aRecords") or []
        return SearchOutcome(
            submissions=[submission_from_record(r) for r in records if isinstance(r, dict)],
            total=int(metadata.get("_nRecordCount") or 0),
            page=page,
            per_page=int(metadata.get("_nPerpage") or per_page),
            is_complete=bool(metadata.get("_bIsComplete")),
        )

    def game_feed(self, game_id: int, page: int = 1, per_page: int = 24) -> SearchOutcome:
        data = self._get_json(f"Game/{game_id}/Subfeed", {"page": page, "per_page": per_page})
        metadata = data.get("_aMetadata") or {}
        records = data.get("_aRecords") or []
        return SearchOutcome(
            submissions=[submission_from_record(r) for r in records if isinstance(r, dict)],
            total=int(metadata.get("_nRecordCount") or 0),
            page=page,
            per_page=int(metadata.get("_nPerpage") or per_page),
            is_complete=bool(metadata.get("_bIsComplete")),
        )

    # Game/Index 的实测硬限制：_nPerpage 超过 50 会被服务端忽略并返回 0 条记录
    # （HTTP 200 但 _aRecords 为空），因此这里强制按 50 分页抓取。
    GAME_PAGE_SIZE = 50

    def list_games(self, page: int = 1, per_page: int = 50, limit: int | None = None) -> list[GameRef]:
        """抓取游戏列表。

        `limit` 为希望获取的总数；内部按 50 条一页分页累积，
        避免一次请求过大触发服务端静默返回空列表。
        """
        target = limit or max(per_page, self.GAME_PAGE_SIZE)
        games: list[GameRef] = []
        current = page
        while len(games) < target:
            data = self._get_json("Game/Index", {"_nPage": current,
                                                 "_nPerpage": self.GAME_PAGE_SIZE})
            records = data.get("_aRecords") or []
            if not records:
                break
            for record in records:
                if not isinstance(record, dict):
                    continue
                games.append(GameRef(
                    id=int(record.get("_idRow") or 0),
                    name=str(record.get("_sName") or ""),
                    profile_url=str(record.get("_sProfileUrl") or ""),
                    icon_url=_pick_thumbnail(record.get("_aPreviewMedia")),
                ))
            if len(records) < self.GAME_PAGE_SIZE:
                break
            current += 1
        return games[:target]

    def list_mod_categories(self, per_page: int = 60, game_id: int | None = None) -> list[CategoryRef]:
        params: dict[str, Any] = {"_nPerpage": per_page}
        if game_id:
            params["_aFilters[Generic_Game]"] = game_id
        data = self._get_json("ModCategory/Index", params)
        categories: list[CategoryRef] = []
        for record in data.get("_aRecords") or []:
            if not isinstance(record, dict):
                continue
            categories.append(CategoryRef(
                id=int(record.get("_idRow") or 0),
                name=str(record.get("_sName") or ""),
                model=str(record.get("_sModelName") or "ModCategory"),
                profile_url=str(record.get("_sProfileUrl") or ""),
                icon_url=_pick_thumbnail(record.get("_aPreviewMedia")),
            ))
        return categories

    def mod_profile_page(self, mod_id: int) -> dict:
        data = self._get_json(f"Mod/{mod_id}/ProfilePage")
        return data if isinstance(data, dict) else {}

    def partition_detail(self, submission: Submission) -> SubmissionDetail:
        """读取任意分区的条目详情。

        实测所有分区都用 `<Model>/<id>/ProfilePage`，且共有字段一致
        （`_sName`/`_sText`/`_sProfileUrl`/`_aPreviewMedia`/`_aSubmitter`/
        `_nViewCount`/`_nLikeCount`/`_nPostCount`/`_aGame`/`_aCategory`/`_aTags`），
        因此这里统一解析，不再按类型写分支。
        """
        model = submission.model or "Mod"
        data = self._get_json(f"{model}/{submission.id}/ProfilePage")
        kind = partition_kind(model)

        files: list[FileInfo] = []
        for item in data.get("_aFiles") or []:
            if not isinstance(item, dict):
                continue
            files.append(FileInfo(
                id=int(item.get("_idRow") or 0),
                filename=str(item.get("_sFile") or ""),
                filesize=int(item.get("_nFilesize") or 0),
                download_url=str(item.get("_sDownloadUrl") or ""),
                md5=str(item.get("_sMd5Checksum") or ""),
                added=format_timestamp(item.get("_tsDateAdded")),
                downloads=int(item.get("_nDownloadCount") or 0),
                av_result=str(item.get("_sAvResult") or ""),
            ))

        credits: list[str] = []
        for item in data.get("_aCredits") or []:
            if isinstance(item, dict):
                role = str(item.get("_sRole") or "").strip()
                names = [str(a.get("_sName")) for a in item.get("_aAuthors") or []
                         if isinstance(a, dict) and a.get("_sName")]
                if names:
                    credits.append(f"{role}：{'、'.join(names)}" if role else "、".join(names))
        for item in data.get("_aContributingStudios") or []:
            if isinstance(item, dict) and item.get("_sName"):
                credits.append(f"参与工作室：{item['_sName']}")

        requirements: list[str] = []
        for item in data.get("_aRequirements") or []:
            if isinstance(item, dict) and item.get("_sName"):
                requirements.append(str(item["_sName"]))

        images: list[str] = []
        media: list[MediaItem] = []
        preview = data.get("_aPreviewMedia") or {}
        for item in preview.get("_aImages") or []:
            if not isinstance(item, dict):
                continue
            base = str(item.get("_sBaseUrl") or "").rstrip("/")
            filename = str(item.get("_sFile") or "")

            def _url(candidate: Any) -> str:
                if not candidate:
                    return ""
                text = str(candidate)
                if text.startswith("http"):
                    return text
                return f"{base}/{text}" if base else ""

            entry = MediaItem(
                type=str(item.get("_sType") or "screenshot"),
                caption=str(item.get("_sCaption") or ""),
                original=_url(filename),
                medium=_url(item.get("_sFile530") or filename),
                large=_url(item.get("_sFile800") or filename),
                thumb=_url(item.get("_sFile220") or item.get("_sFile100")),
            )
            media.append(entry)
            chosen = entry.best("medium")
            if chosen:
                images.append(chosen)

        # _aEmbeddedMedia 实测是 URL 字符串数组（如 YouTube 链接）
        embedded: list[str] = []
        raw_embedded = data.get("_aEmbeddedMedia")
        if isinstance(raw_embedded, list):
            for item in raw_embedded:
                if isinstance(item, str) and item.strip():
                    embedded.append(item.strip())
                elif isinstance(item, dict):
                    for key in ("_sUrl", "_sEmbedUrl", "_sFile"):
                        if item.get(key):
                            embedded.append(str(item[key]))
                            break

        stats: list[tuple[str, str]] = []
        for key, label in (("_nViewCount", "浏览"), ("_nLikeCount", "点赞"),
                           ("_nPostCount", "评论"), ("_nSubscriberCount", "关注"),
                           ("_nDownloadCount", "下载")):
            value = data.get(key)
            if isinstance(value, (int, float)) and value:
                stats.append((label, f"{int(value):,}"))

        origin: list[tuple[str, str]] = []
        for key, label in (("_aGame", "所属游戏"), ("_aCategory", "分类"),
                           ("_aSubCategory", "子分类"), ("_aSuperCategory", "上级分类")):
            item = data.get(key)
            if isinstance(item, dict) and item.get("_sName"):
                origin.append((label, str(item["_sName"])))
        submitter = data.get("_aSubmitter")
        if isinstance(submitter, dict) and submitter.get("_sName"):
            origin.append(("作者", str(submitter["_sName"])))

        body = str(data.get("_sText") or data.get("_sDescription") or "")

        # 该分区内容总数：让用户知道点「浏览」后会有多少内容
        partition_total = 0
        try:
            index = self._get_json(f"{model}/Index", {"_nPerpage": 1})
            partition_total = int((index.get("_aMetadata") or {}).get("_nRecordCount") or 0)
        except ApiError:
            partition_total = 0

        # 分类类分区给出下钻方式
        drilldown = CATEGORY_DRILLDOWN.get(model, "")
        drilldown_label = ""
        if drilldown == "game":
            # 游戏的「内容数」是它专区里的条目数（子流），不是游戏分区总数
            try:
                feed = self._get_json(f"Game/{submission.id}/Subfeed", {"page": 1, "per_page": 1})
                zone_total = int((feed.get("_aMetadata") or {}).get("_nRecordCount") or 0)
            except ApiError:
                zone_total = 0
            if zone_total:
                partition_total = zone_total
                drilldown_label = f"进入该游戏专区（{zone_total} 条内容）"

        detail_submission = submission
        if data.get("_sName"):
            detail_submission.name = str(data["_sName"])
        if data.get("_sProfileUrl"):
            detail_submission.profile_url = str(data["_sProfileUrl"])

        return SubmissionDetail(
            submission=detail_submission,
            description=body,
            files=files,
            download_page_url=f"{SITE_BASE}/{model.lower()}/{submission.id}",
            credits=credits,
            requirements=requirements,
            images=images,
            media=media,
            embedded_media=embedded,
            body=body,
            kind=kind,
            stats=stats,
            origin=origin,
            partition_total=partition_total,
            drilldown=drilldown,
            drilldown_label=drilldown_label,
        )

    def submission_detail(self, submission: Submission) -> SubmissionDetail:
        """兼容入口：等价于 partition_detail。"""
        return self.partition_detail(submission)


def collect_translatable(submissions: Iterable[Submission]) -> tuple[list[str], list[tuple[int, str]]]:
    """把一批条目的可翻译字段展平为 (文本列表, 回填位置)，供批量翻译使用。"""
    texts: list[str] = []
    slots: list[tuple[int, str]] = []
    for index, submission in enumerate(submissions):
        for field_name, value in submission.translatable_fields().items():
            texts.append(value)
            slots.append((index, field_name))
    return texts, slots
