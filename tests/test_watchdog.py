"""异常退出检测器自检：离线、不联网、不依赖 GUI。

覆盖：
  1. Windows 异常退出码能翻译成中文说明（含 0xC0000005 访问违例）；
  2. 活动标记可写入/读取/清理，崩溃后能据此判断死在哪个环节；
  3. 报告内容包含退出码判定、活动标记、崩溃日志路径；
  4. **端到端**：用一个会原生崩溃（访问违例）的子进程做被监控对象，
     验证看门狗能捕获崩溃、写出报告并给出非零退出码。

运行：python tests/test_watchdog.py
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from client.watchdog import (  # noqa: E402
    build_report,
    clear_marker,
    describe_exit_code,
    read_marker,
    save_report,
    write_marker,
)

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def test_exit_code_meaning() -> None:
    kind, text = describe_exit_code(0)
    check(kind == "normal" and "正常" in text, f"0 应判为正常：{kind} {text}")

    kind, text = describe_exit_code(0xC0000005)
    check(kind == "crash", f"0xC0000005 应判为崩溃：{kind}")
    check("访问违例" in text, f"0xC0000005 说明缺少关键信息：{text}")

    # 负值形式（Windows 上 Python 常见）
    negative = 0xC0000005 - (1 << 32)
    kind, text = describe_exit_code(negative)
    check(kind == "crash" and "访问违例" in text,
          f"负值退出码应同样识别：{negative} -> {kind} {text}")

    kind, text = describe_exit_code(0xC0000409)
    check(kind == "crash" and "快速失败" in text, f"0xC0000409 说明异常：{text}")

    kind, text = describe_exit_code(1)
    check(kind == "error" and "Python" in text, f"退出码 1 应提示看崩溃日志：{text}")

    kind, text = describe_exit_code(0xC0000FFF)
    check(kind == "crash" and "未知异常码" in text, f"未知异常码应有兜底说明：{text}")


def test_marker_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as raw:
        config_dir = pathlib.Path(raw)
        write_marker(config_dir, action="打开设置", page=2)
        marker = read_marker(config_dir)
        check(marker.get("action") == "打开设置", f"活动标记未写入：{marker}")
        check(marker.get("page") == 2, f"附加字段丢失：{marker}")
        check("updated_at" in marker, "活动标记缺少时间戳")

        # 覆盖写入应替换旧内容（而不是追加）
        write_marker(config_dir, action="加载索引页")
        marker = read_marker(config_dir)
        check(marker.get("action") == "加载索引页", f"活动标记未更新：{marker}")
        check("page" not in marker, "旧字段应被替换")

        clear_marker(config_dir)
        check(read_marker(config_dir) == {}, "活动标记未被清理")


def test_report_content() -> None:
    with tempfile.TemporaryDirectory() as raw:
        config_dir = pathlib.Path(raw)
        log_dir = config_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "crash-20260101.log"
        log_file.write_text("Windows fatal exception: access violation\n当前线程栈...\n", encoding="utf-8")

        marker = {"action": "打开设置", "page": 1}
        report = build_report(config_dir, 0xC0000005, 1000.0, 12.5, marker, log_file)
        for expected in ("异常退出报告", "0xC0000005", "访问违例", "打开设置",
                         str(log_file), "access violation", "配置目录"):
            check(expected in report, f"报告缺少内容：{expected}")

        saved = save_report(config_dir, report)
        check(saved.is_file(), f"报告未落盘：{saved}")
        check("异常退出报告" in saved.read_text(encoding="utf-8"), "落盘报告内容不完整")


def test_watchdog_catches_native_crash() -> None:
    """端到端：监控一个原生崩溃的子进程，看门狗必须报告崩溃并保留活动标记。"""
    with tempfile.TemporaryDirectory() as raw:
        config_dir = pathlib.Path(raw)
        # 用 faulthandler 的 C 级触发器制造真正的原生崩溃：
        # ctypes.string_at(0) 会被 Python 转成 OSError，不是原生崩溃，故不用它。
        crasher = config_dir / "crasher.py"
        crasher.write_text(
            "import faulthandler, pathlib, sys\n"
            f"sys.path.insert(0, r'{ROOT}')\n"
            "from client.watchdog import write_marker\n"
            f"write_marker(pathlib.Path(r'{config_dir}'), action='测试崩溃点')\n"
            "faulthandler._sigsegv()\n",
            encoding="utf-8")

        result = subprocess.run([sys.executable, str(crasher)], capture_output=True, text=True)
        check(result.returncode != 0, "崩溃子进程应返回非零退出码")

        kind, meaning = describe_exit_code(result.returncode)
        check(kind == "crash",
              f"原生崩溃应判为崩溃：code={result.returncode}（{meaning}）")

        marker = read_marker(config_dir)
        check(marker.get("action") == "测试崩溃点",
              f"崩溃前活动标记应可读取：{marker}")

        log_file = config_dir / "logs" / "crash-test.log"
        log_file.write_text("Windows fatal exception: access violation\n", encoding="utf-8")
        report = build_report(config_dir, result.returncode, 0.0, 1.0, marker, log_file)
        check("测试崩溃点" in report, "报告未包含崩溃前活动")
        check("崩溃" in report, "报告未给出崩溃判定")


def test_watchdog_full_loop() -> None:
    """端到端跑一遍看门狗主循环：子进程原生崩溃 → 写出报告 → 返回非零码。"""
    from client import watchdog

    with tempfile.TemporaryDirectory() as raw:
        config_dir = pathlib.Path(raw)
        crasher = config_dir / "crasher.py"
        crasher.write_text(
            "import faulthandler, pathlib, sys\n"
            f"sys.path.insert(0, r'{ROOT}')\n"
            "from client.watchdog import write_marker\n"
            f"write_marker(pathlib.Path(r'{config_dir}'), action='主循环测试')\n"
            "faulthandler._sigsegv()\n",
            encoding="utf-8")

        # 让看门狗去启动崩溃脚本而不是真实客户端（仅替换启动命令）
        original_popen = watchdog.subprocess.Popen
        watchdog.subprocess.Popen = lambda argv, **kwargs: original_popen(
            [sys.executable, str(crasher)], **kwargs)
        try:
            code = watchdog.run_with_watchdog([], config_dir, no_pause=True)
        finally:
            watchdog.subprocess.Popen = original_popen

        check(code == 1, f"崩溃时应返回退出码 1，实际 {code}")

        report_file = watchdog.report_path(config_dir)
        check(report_file.is_file(), f"未生成异常退出报告：{report_file}")
        if report_file.is_file():
            text = report_file.read_text(encoding="utf-8")
            check("崩溃" in text, "报告未给出崩溃判定")
            check("主循环测试" in text, f"报告未包含崩溃前活动：{text[:200]}")
            check("0x" in text, "报告未包含异常码")

        # 正常退出时不应留下报告或活动标记
        clean_dir = config_dir / "clean"
        clean_dir.mkdir()
        ok_script = clean_dir / "ok.py"
        ok_script.write_text("print('done')\n", encoding="utf-8")
        watchdog.subprocess.Popen = lambda argv, **kwargs: original_popen(
            [sys.executable, str(ok_script)], **kwargs)
        try:
            code = watchdog.run_with_watchdog([], clean_dir, no_pause=True)
        finally:
            watchdog.subprocess.Popen = original_popen
        check(code == 0, f"正常退出应返回 0，实际 {code}")
        check(not watchdog.report_path(clean_dir).is_file(), "正常退出不应生成崩溃报告")


def test_watchdog_auto_restart() -> None:
    """崩溃后应自动重启；重启成功则整体视为已恢复。"""
    from client import watchdog

    with tempfile.TemporaryDirectory() as raw:
        config_dir = pathlib.Path(raw)
        counter = config_dir / "runs.txt"
        script = config_dir / "flaky.py"
        script.write_text(
            "import pathlib, sys, faulthandler\n"
            f"counter = pathlib.Path(r'{counter}')\n"
            "n = int(counter.read_text()) + 1 if counter.is_file() else 1\n"
            "counter.write_text(str(n))\n"
            f"sys.path.insert(0, r'{ROOT}')\n"
            "from client.watchdog import write_marker\n"
            f"write_marker(pathlib.Path(r'{config_dir}'), action=f'第{{n}}次运行')\n"
            "if n == 1:\n"
            "    faulthandler._sigsegv()   # 第一次崩溃\n"
            "print('recovered')\n",
            encoding="utf-8")

        original_popen = watchdog.subprocess.Popen
        watchdog.subprocess.Popen = lambda argv, **kwargs: original_popen(
            [sys.executable, str(script)], **kwargs)
        try:
            code = watchdog.run_with_watchdog([], config_dir, no_pause=True, auto_restart=2)
        finally:
            watchdog.subprocess.Popen = original_popen

        runs = int(counter.read_text()) if counter.is_file() else 0
        check(runs == 2, f"应在崩溃后重启一次（共运行 2 次），实际 {runs} 次")
        check(code == 0, f"重启后正常退出应返回 0，实际 {code}")

        history = watchdog.read_history(config_dir)
        check(len(history) == 1, f"崩溃历史应记录 1 条，实际 {len(history)}")
        if history:
            check("0x" in history[0].get("exit_code_hex", ""), "历史缺少异常码")
            check(history[0].get("last_action") == "第1次运行",
                  f"历史未记录崩溃前活动：{history[0]}")

        summary = watchdog.summarize_history(config_dir)
        check("崩溃次数：1" in summary, f"统计摘要异常：{summary}")
        check("按最后动作" in summary, "统计摘要缺少环节维度")


def main() -> int:
    tests = (test_exit_code_meaning, test_marker_roundtrip, test_report_content,
             test_watchdog_catches_native_crash, test_watchdog_full_loop,
             test_watchdog_auto_restart)
    for test in tests:
        before = len(FAILURES)
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - 自检需要报告任何异常
            FAILURES.append(f"{test.__name__} 抛出异常：{type(exc).__name__}: {exc}")
        print(f"[{'通过' if len(FAILURES) == before else '未通过'}] {test.__name__}", flush=True)

    if FAILURES:
        print("\n自检未通过：", file=sys.stderr)
        for item in FAILURES:
            print(f"  * {item}", file=sys.stderr)
        return 1
    print("\n自检通过：退出码判定、活动标记、报告生成与崩溃捕获全部可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
