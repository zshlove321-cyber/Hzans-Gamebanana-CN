"""GameBanana API 客户端自检：真实只读请求 + 归一化校验。

覆盖：
  1. 搜索接口可解析，条目字段归一化后非空（标题、URL、作者、游戏、时间）；
  2. 分类浏览接口可解析，支持按游戏过滤；
  3. 条目详情接口可解析，能取到文件与下载地址；
  4. 错误体约定：无效路由抛出中文 ApiError，而不是把错误当数据；
  5. 缓存生效：同一请求第二次不再访问网络。

网络不可用时以退出码 2 报告 SKIP（不算通过、也不算失败）。
运行：python tests/test_api_client.py
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from client.api import GameBananaClient, collect_translatable, model_label  # noqa: E402
from client.common import ApiError, DiskCache  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def make_client(tmp: pathlib.Path) -> GameBananaClient:
    return GameBananaClient(timeout=30, request_delay_ms=200,
                            cache=DiskCache(tmp / "api-cache.json", ttl_seconds=600))


def test_search(client: GameBananaClient) -> None:
    outcome = client.search("skyrim", page=1, per_page=12)
    check(len(outcome.submissions) > 0, "搜索未返回任何条目")
    check(outcome.total > 0, "搜索总数缺失")
    check(any(section["count"] > 0 for section in outcome.sections), "缺少分类命中数(_aSectionMatchCounts)")
    first = outcome.submissions[0]
    check(bool(first.name), "条目标题为空")
    check(bool(first.profile_url), "条目链接为空")
    check(bool(first.model), "条目类型为空")
    check(bool(first.date_added), "条目时间未归一化")
    print(f"  搜索 skyrim：{len(outcome.submissions)} 条 / 共 {outcome.total}，"
          f"分类维度 {len(outcome.sections)} 个")
    print(f"  首条：[{model_label(first.model)}] {first.name} | {first.author} | {first.date_added}")
    print(f"  可翻译字段：{list(first.translatable_fields())}")


def test_search_by_model(client: GameBananaClient) -> None:
    outcome = client.search("furnace", page=1, per_page=8, model="Mod")
    check(all(item.model == "Mod" for item in outcome.submissions), "按模型搜索返回了其他类型")
    check(len(outcome.submissions) > 0, "按模型搜索无结果")
    print(f"  按模型搜索 Mod/furnace：{len(outcome.submissions)} 条")


def test_browse(client: GameBananaClient) -> None:
    outcome = client.browse_mods(page=1, per_page=8, game_id=4507)
    check(len(outcome.submissions) > 0, "按游戏浏览无结果")
    check(outcome.total > 0, "浏览总数缺失")
    first = outcome.submissions[0]
    check(bool(first.game), "浏览结果缺少所属游戏")
    print(f"  浏览游戏 4507：{len(outcome.submissions)} 条 / 共 {outcome.total}")
    print(f"  首条：{first.name} | 游戏={first.game} | 分类={first.root_category or first.sub_category or '—'}")


def test_games_and_categories(client: GameBananaClient) -> None:
    games = client.list_games(page=1, limit=120)
    check(len(games) >= 100, f"游戏列表过少（{len(games)} 个），可能是分页参数问题")
    check(all(game.id and game.name for game in games), "游戏列表存在缺字段项")
    # 站点行为：Game/Index 的 _nPerpage 超过 50 会静默返回空列表，
    # 因此 list_games 必须自行按 50 分页；这里直接验证上限行为。
    capped = client.list_games(page=1, limit=300)
    check(len(capped) >= 120, f"分页累积失效：limit=300 只拿到 {len(capped)} 个")
    check(len({game.id for game in capped}) == len(capped), "游戏列表出现重复项")

    categories = client.list_mod_categories(per_page=20)
    check(len(categories) > 0, "模组分类列表为空")
    print(f"  游戏 {len(capped)} 个（示例：{capped[0].name}）；分类 {len(categories)} 个"
          f"（示例：{categories[0].name}）")


def test_detail(client: GameBananaClient) -> None:
    outcome = client.browse_mods(page=1, per_page=3, game_id=4507)
    target = next((item for item in outcome.submissions if item.has_files), None)
    check(target is not None, "未找到含文件的条目用于详情测试")
    if target is None:
        return
    detail = client.submission_detail(target)
    check(bool(detail.files), "详情未返回文件列表")
    if detail.files:
        check("gamebanana.com" in detail.files[0].download_url, "下载地址异常")
        print(f"  详情 {target.name}：文件 {len(detail.files)} 个，"
              f"首个 {detail.files[0].filename} ({detail.files[0].download_url})")


def test_error_convention(client: GameBananaClient) -> None:
    """无效路由必须抛出 ApiError，不能把错误体当数据。

    站点对无效路由的表现有两种（实测都出现过）：
      * HTTP 200 + body 里的 `_sErrorCode: NO_SUCH_ROUTE`；
      * HTTP 404 + 同样的错误体。
    两种都算识别成功，但都必须走 ApiError 且不重试。
    """
    try:
        client._get_json("Mod/241905/Profile")
    except ApiError as exc:
        message = str(exc)
        check("NO_SUCH_ROUTE" in message or "路由" in message,
              f"错误信息不可读：{message}")
        check(not exc.retryable, f"参数类错误不应标记可重试：{message}")
        print(f"  无效路由错误信息：{message[:70]}")
    else:
        FAILURES.append("无效路由未抛出 ApiError（会把错误体当数据）")


def test_cache(client: GameBananaClient) -> None:
    calls = {"n": 0}
    original = client.session.get

    def counting_get(url, **kwargs):
        calls["n"] += 1
        return original(url, **kwargs)

    client.session.get = counting_get  # type: ignore[assignment]
    client.search("zelda", page=2, per_page=5)
    first_round = calls["n"]
    client.search("zelda", page=2, per_page=5)
    check(calls["n"] == first_round, f"缓存未生效：第二轮新增 {calls['n'] - first_round} 次请求")
    print(f"  缓存命中检查：第一轮 {first_round} 次请求，第二轮 0 次新增")


def test_translatable_slots(client: GameBananaClient) -> None:
    outcome = client.search("skyrim", page=1, per_page=5)
    texts, slots = collect_translatable(outcome.submissions)
    check(len(texts) == len(slots), "可翻译文本与回填位置数量不一致")
    check(all(text.strip() for text in texts), "可翻译文本中存在空白项")
    print(f"  待翻译字段：{len(texts)} 项（示例：{texts[:2]}）")


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        client = make_client(pathlib.Path(raw))
        try:
            client.search("test", page=1, per_page=1)
        except (ApiError, OSError) as exc:
            print(f"SKIP：无法访问 GameBanana API（{exc}）")
            return 2

        for test in (test_search, test_search_by_model, test_browse, test_games_and_categories,
                     test_detail, test_error_convention, test_cache, test_translatable_slots):
            print(f"[{test.__name__}]")
            before = len(FAILURES)
            try:
                test(client)
            except Exception as exc:  # noqa: BLE001 - 自检需要报告任何异常
                FAILURES.append(f"{test.__name__} 抛出异常：{type(exc).__name__}: {exc}")
            if len(FAILURES) > before:
                print(f"  -> 未通过")

    if FAILURES:
        print("\n自检未通过：", file=sys.stderr)
        for item in FAILURES:
            print(f"  * {item}", file=sys.stderr)
        return 1
    print("\n自检通过：搜索、浏览、分类、详情、错误体约定与缓存全部符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())
