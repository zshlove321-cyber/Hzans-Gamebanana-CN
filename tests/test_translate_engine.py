"""DeepSeek 翻译引擎自检：用假传输层验证全链路，不需要密钥、不联网、不产生费用。

覆盖：
  1. 批处理切分与顺序还原。
  2. 磁盘缓存命中（第二次调用不再发请求）。
  3. 未配置密钥时给出中文可读错误。
  4. 响应条数不符、HTTP 错误、非 JSON content 的容错与重试。
  5. 跳过规则命中的内容在生产链路上不会被送去翻译（与 extractor 联动）。

运行：python tests/test_translate_engine.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from client.settings import Settings  # noqa: E402
from client.translate.engine import DeepSeekTranslator, TranslationError  # noqa: E402
from client.translate.extractor import extract_units  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def extract_sources(user_content: str) -> list[str]:
    """从用户消息里取出待翻译文本数组。

    真实实现会在文本数组**之前**注入术语表与强化重试说明，
    因此不能简单地把整段当 JSON 解析——必须从最后一个 JSON 数组开始截取。
    """
    start = user_content.rfind("[")
    if start == -1:
        return []
    try:
        value = json.loads(user_content[start:])
    except ValueError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _sources_of(call: dict) -> list[str]:
    return extract_sources(call["payload"]["messages"][1]["content"])


class FakeTransport:
    """记录调用并按预设脚本返回响应；脚本用尽后按输入逐条生成 '译:<原文>'。"""

    def __init__(self, script: list[object] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[dict] = []

    def __call__(self, base_url: str, payload: dict, headers: dict, timeout: int) -> dict:
        self.calls.append({"base_url": base_url, "payload": payload, "headers": headers, "timeout": timeout})
        item = self.script.pop(0) if self.script else None
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict) and "choices" in item:
            return item
        sources = extract_sources(payload["messages"][1]["content"])
        translations = item if isinstance(item, list) else [f"译:{s}" for s in sources]
        return {"choices": [{"message": {"content": json.dumps(translations, ensure_ascii=False)}}]}


def make_settings(tmp: pathlib.Path, **overrides) -> Settings:
    settings = Settings(config_dir=str(tmp), deepseek_api_key="sk-test-not-real", translate_batch_size=2,
                        translate_max_concurrency=2, translate_max_retries=1)
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings.normalized()


def test_batching_and_order() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        transport = FakeTransport([])
        translator = DeepSeekTranslator(make_settings(tmp), transport=transport)
        texts = ["one", "two", "three", "four", "five"]
        result = translator.translate_many(texts)
        check(result == [f"译:{t}" for t in texts], f"批处理结果或顺序错误：{result}")
        check(len(transport.calls) == 3, f"批次数量应为 3（batch=2），实际 {len(transport.calls)}")
        check(translator.last_stats.translated == 5, f"统计错误：{translator.last_stats.summary()}")


def test_cache_hit() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        transport = FakeTransport([])
        settings = make_settings(tmp)
        translator = DeepSeekTranslator(settings, transport=transport)
        first = translator.translate_many(["hello", "world"])
        calls_after_first = len(transport.calls)
        # 新实例、同一缓存目录：应全部命中缓存，不再发起请求
        second_translator = DeepSeekTranslator(settings, transport=transport)
        second = second_translator.translate_many(["hello", "world"])
        check(first == second, "缓存命中后结果不一致")
        check(len(transport.calls) == calls_after_first, "缓存未生效，重复发起了请求")
        check(second_translator.last_stats.cached == 2, f"缓存统计错误：{second_translator.last_stats.summary()}")


def test_missing_key() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        settings = make_settings(tmp, deepseek_api_key="")
        translator = DeepSeekTranslator(settings, transport=FakeTransport([]))
        try:
            translator.translate_many(["hello"])
        except TranslationError as exc:
            check("密钥" in str(exc), f"未配置密钥的错误信息不可读：{exc}")
        else:
            FAILURES.append("未配置密钥时应当抛出 TranslationError")


def test_length_mismatch_retries_then_fails() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        transport = FakeTransport([["只有一条"], ["还是只有一条"]])
        translator = DeepSeekTranslator(make_settings(tmp), transport=transport)
        result = translator.translate_many(["a", "b"])
        check(result == ["a", "b"], f"失败时应保留原文，实际 {result}")
        check(len(transport.calls) == 2, f"应重试 1 次共 2 次调用，实际 {len(transport.calls)}")
        check(translator.last_stats.failed > 0, "失败计数未记录")


def test_retry_then_success() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        transport = FakeTransport([TranslationError("模拟网络错误")])
        translator = DeepSeekTranslator(make_settings(tmp), transport=transport)
        result = translator.translate_many(["retry me"])
        check(result == ["译:retry me"], f"重试后应成功，实际 {result}")


def test_fenced_json_content() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        fenced = "```json\n[\"你好\"]\n```"
        transport = FakeTransport([{"choices": [{"message": {"content": fenced}}]}])
        translator = DeepSeekTranslator(make_settings(tmp), transport=transport)
        check(translator.translate_many(["hi"]) == ["你好"], "Markdown 代码块包裹的响应未解析")


def test_wrapped_object_content() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        wrapped = json.dumps({"translations": ["包裹结果"]}, ensure_ascii=False)
        transport = FakeTransport([{"choices": [{"message": {"content": wrapped}}]}])
        translator = DeepSeekTranslator(make_settings(tmp), transport=transport)
        check(translator.translate_many(["wrapped"]) == ["包裹结果"], "对象包裹的响应未解析")


def test_deduplication() -> None:
    """同一页重复文本只发一次请求。

    注意：新实现会对「模型原样返回」的条目做一次强化重试，因此总调用次数
    可能多于批次数；这里校验的是**首次请求已去重**，而不是总次数。
    """
    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        transport = FakeTransport([])
        translator = DeepSeekTranslator(make_settings(tmp), transport=transport)
        texts = ["Video Games", "Skins", "Video Games", "Skins", "Video Games"]
        result = translator.translate_many(texts)
        check(result == ["译:Video Games", "译:Skins", "译:Video Games", "译:Skins", "译:Video Games"],
              f"去重后结果错位：{result}")
        check(translator.last_stats.deduplicated == 3,
              f"去重计数错误：{translator.last_stats.summary()}")

        # 首次请求应只提交去重后的 2 条文本
        first_sources = _sources_of(transport.calls[0])
        check(sorted(first_sources) == ["Skins", "Video Games"],
              f"首次请求未去重：{first_sources}")
        # 全部条目都应拿到译文（重复项共享同一译文）
        check(all("译:" in item for item in result), f"存在未翻译项：{result}")


def _sources_of(call: dict) -> list[str]:
    """首次请求应提交的文本数组（术语块会被前置，故用稳健提取）。"""
    return extract_sources(call["payload"]["messages"][1]["content"])


def test_pipeline_skips_code() -> None:
    """提取阶段丢弃的内容不应出现在送翻译的文本里。"""
    html = ("<div><p>Install by running <code>furnace_patch()</code> now</p>"
            "<pre>SKIP_ME_PRE</pre><p>translatePage()</p><p>{{name}}</p></div>")
    units = [unit.text for unit in extract_units(html)]
    check("SKIP_ME_PRE" not in " ".join(units), "pre 内容进入了翻译单元")
    check("furnace_patch()" not in " ".join(units), "code 内容进入了翻译单元")
    check(any("Install by running" in text for text in units), "正文未被提取")


def test_prompt_and_glossary() -> None:
    """提示词与术语表：覆盖多语种、默认翻译、术语优先级与缓存版本化。"""
    from client.translate.engine import (
        PROMPT_VERSION,
        _needs_translation,
        build_system_prompt,
        cache_signature,
        set_glossary_version,
    )
    from client.translate.glossary import load_glossary

    prompt = build_system_prompt("简体中文")
    # 提示词模板含占位符示例（{{name}}、${var}），必须原样保留且能正确替换 target
    dollar_var = chr(36) + "{var}"
    check("{target}" not in prompt, "提示词未完成 target 替换（format/替换有误）")
    check("{{name}}" in prompt, "提示词丢失了占位符示例 {{name}}")
    check(dollar_var in prompt, "提示词丢失了占位符示例 ${var}")
    check("简体中文" in prompt, "提示词未写明目标语言")
    check("源语言可能是任何语言" in prompt, "提示词未声明源语言可为任意语言（这是漏译根因）")
    check("默认要翻译" in prompt, "提示词缺少「默认要翻译」的明确指令")
    check("术语表" in prompt, "提示词未声明术语表优先级")
    check("缩写" in prompt and "俚语" in prompt, "提示词未覆盖缩写与俚语处理")

    glossary = load_glossary()
    check(glossary.total_terms() >= 150, f"术语表条目过少：{glossary.total_terms()}")
    for section in ("abbreviations", "slang", "game_terms", "keep_original"):
        check(section in glossary.sections, f"术语表缺少分组：{section}")

    # 只注入命中的术语，且大小写不敏感
    rendered = glossary.render(["OP build needs nerf ASAP"])
    check("OP" in rendered, f"术语表未命中 OP：{rendered[:120]}")
    check("NERF" in rendered or "nerf" in rendered, "术语表未命中 nerf")
    check("Fast Furnaces" not in rendered, "术语表注入了未命中的术语")
    # 词边界匹配：op 不应命中 option
    boundary = glossary.render(["just an option"])
    check("过于强力" not in boundary, f"词边界匹配失效（op 命中了 option）：{boundary[:120]}")
    # 空文本不产生术语块
    check(glossary.render([]) == "", "空输入不应产生术语块")

    # 需要翻译的判定：非中文含字母为 True，中文/纯符号为 False
    check(_needs_translation("ШКОЛА ПРОПАДАЕТ!"), "俄语文本应判定为需要翻译")
    check(_needs_translation("¡Modo historia!"), "西语文本应判定为需要翻译")
    check(_needs_translation("P.N.C.R."), "缩写应判定为需要翻译")
    check(not _needs_translation("高速道路MOD"), "含中文的文本不应重试")
    check(not _needs_translation("1.20.1"), "纯数字版本号不应重试")
    check(not _needs_translation("+ - /"), "纯符号不应重试")
    check(not _needs_translation("A"), "单个字母不应重试")

    # 缓存签名包含提示词与术语表版本
    base = cache_signature("简体中文", "deepseek-chat")
    check(PROMPT_VERSION in base, f"缓存签名未包含提示词版本：{base}")
    set_glossary_version(12345)
    check(cache_signature("简体中文", "deepseek-chat") != base,
          "术语表版本变化后缓存签名应改变（否则改术语表不生效）")
    set_glossary_version(glossary.version)


def test_retry_recovers_untranslated() -> None:
    """模型原样返回时，强化重试应把条目救回来，并计入统计。"""
    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)

        class EchoThenTranslate(FakeTransport):
            def __call__(self, base_url, payload, headers, timeout):
                self.calls.append({"payload": payload})
                sources = json.loads(payload["messages"][1]["content"].rsplit("\n", 1)[-1]) \
                    if "\n" in payload["messages"][1]["content"] else \
                    json.loads(payload["messages"][1]["content"])
                # 第一次调用原样返回，第二次（强化重试）给出译文
                if len(self.calls) == 1:
                    body = sources
                else:
                    body = [f"译:{s}" for s in sources]
                return {"choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}]}

        transport = EchoThenTranslate([])
        translator = DeepSeekTranslator(make_settings(tmp), transport=transport)
        result = translator.translate_many(["GG EZ no re"])
        check(result == ["译:GG EZ no re"], f"强化重试未救回条目：{result}")
        check(len(transport.calls) == 2, f"应发生 2 次调用（初次+重试），实际 {len(transport.calls)}")
        check(translator.last_stats.retried == 1, f"重试统计错误：{translator.last_stats.summary()}")
        check(translator.last_stats.recovered == 1,
              f"救回统计错误：{translator.last_stats.summary()}")

        # 已含中文的条目不应触发重试（避免无意义计费）
        transport2 = EchoThenTranslate([])
        translator2 = DeepSeekTranslator(make_settings(tmp), transport=transport2)
        result2 = translator2.translate_many(["高速道路MOD"])
        check(result2 == ["高速道路MOD"], f"中文条目不应被改写：{result2}")
        check(len(transport2.calls) == 1,
              f"中文条目不应触发重试，实际调用 {len(transport2.calls)} 次")


def main() -> int:
    for test in (test_batching_and_order, test_cache_hit, test_missing_key,
                 test_length_mismatch_retries_then_fails, test_retry_then_success,
                 test_fenced_json_content, test_wrapped_object_content,
                 test_deduplication, test_prompt_and_glossary,
                 test_retry_recovers_untranslated, test_pipeline_skips_code):
        name = test.__name__
        before = len(FAILURES)
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - 自检需要报告任何异常
            FAILURES.append(f"{name} 抛出异常：{type(exc).__name__}: {exc}")
        status = "通过" if len(FAILURES) == before else "未通过"
        print(f"[{status}] {name}")

    if FAILURES:
        print("\n自检未通过：", file=sys.stderr)
        for item in FAILURES:
            print(f"  * {item}", file=sys.stderr)
        return 1
    print("\n自检通过：翻译链路（批处理/缓存/重试/容错/跳过规则联动）全部符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())
