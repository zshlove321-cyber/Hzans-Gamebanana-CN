"""异常退出检测器（看门狗）。

用途：客户端偶尔会「突然消失」——这类崩溃往往发生在 Qt/C++ 内部，进程内的
Python 钩子与 faulthandler 不一定来得及落盘，控制台也可能一起消失。
本模块以**独立进程**方式启动客户端，因此客户端无论怎么死，看门狗都活着，
可以记录：

  * 退出码与 Windows 异常码（0xC0000005 访问违例等）的中文含义；
  * 退出前最后写入的「活动标记」（当前动作、页面、时间），据此判断死在哪个环节；
  * 崩溃日志文件（`<配置目录>/logs/crash-*.log`）的路径与尾部内容；
  * 一份汇总报告 `<配置目录>/logs/last-crash.txt`，方便直接发给维护者。

用法：
    python -m client.watchdog                    # 以看门狗方式启动客户端
    python -m client.watchdog --no-watchdog      # 直接启动，不经看门狗
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

from .settings import default_config_dir, load_settings

# Windows 异常退出码（NTSTATUS）→ 中文说明
EXIT_CODE_MEANINGS = {
    0xC0000005: "访问违例（读写非法内存），常见于渲染/驱动或对象生命周期问题",
    0xC0000006: "页面文件/内存读取失败",
    0xC000001D: "非法指令",
    0xC0000094: "整数除零",
    0xC0000096: "特权指令",
    0xC00000FD: "栈溢出（可能是无限递归）",
    0xC0000374: "堆损坏（内存越界写）",
    0xC0000409: "栈缓冲区溢出/快速失败（安全 cookie 被破坏，常见于底层库崩溃）",
    0xC0000602: "快速失败异常（fail-fast，通常是 Qt/C++ 主动终止）",
    0xC0000135: "缺少 DLL 依赖",
    0xC0000139: "DLL 入口点缺失",
    0xC0000142: "DLL 初始化失败",
    0x80000003: "断点异常（调试断点或断言失败）",
}

MARKER_NAME = "session-activity.json"
REPORT_NAME = "last-crash.txt"
HISTORY_NAME = "crash-history.jsonl"


def history_path(config_dir: pathlib.Path) -> pathlib.Path:
    return config_dir / "logs" / HISTORY_NAME


def append_history(config_dir: pathlib.Path, exit_code: int, elapsed: float, marker: dict) -> None:
    """把每次异常退出追加为一行 JSONL，便于统计崩溃频率与规律。"""
    kind, meaning = describe_exit_code(exit_code)
    record = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "exit_code": exit_code,
        "exit_code_hex": f"0x{exit_code & 0xFFFFFFFF:08X}",
        "kind": kind,
        "meaning": meaning,
        "elapsed_seconds": round(elapsed, 1),
        "last_action": marker.get("action", ""),
        "last_detail": {k: v for k, v in marker.items()
                        if k not in {"action", "updated_at", "pid"}},
    }
    path = history_path(config_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_history(config_dir: pathlib.Path, limit: int = 50) -> list[dict]:
    path = history_path(config_dir)
    if not path.is_file():
        return []
    records: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return records[-limit:]


def summarize_history(config_dir: pathlib.Path) -> str:
    """给出崩溃频率与集中环节的简要统计，用于判断是否有规律。"""
    records = read_history(config_dir)
    if not records:
        return "（无崩溃历史）"
    codes: dict[str, int] = {}
    actions: dict[str, int] = {}
    for item in records:
        codes[item.get("exit_code_hex", "?")] = codes.get(item.get("exit_code_hex", "?"), 0) + 1
        action = item.get("last_action") or "（未记录）"
        actions[action] = actions.get(action, 0) + 1
    first, last = records[0].get("at", "?"), records[-1].get("at", "?")
    lines = [f"崩溃次数：{len(records)}（首次 {first}，最近 {last}）",
             "按异常码：" + "、".join(f"{k}×{v}" for k, v in sorted(codes.items(), key=lambda x: -x[1])),
             "按最后动作：" + "、".join(f"{k}×{v}" for k, v in sorted(actions.items(), key=lambda x: -x[1]))]
    return "\n".join(lines)


def marker_path(config_dir: pathlib.Path) -> pathlib.Path:
    return config_dir / "logs" / MARKER_NAME


def report_path(config_dir: pathlib.Path) -> pathlib.Path:
    return config_dir / "logs" / REPORT_NAME


def describe_exit_code(code: int) -> tuple[str, str]:
    """返回 (分类, 中文说明)。分类取值：normal / crash / error。"""
    if code == 0:
        return "normal", "正常退出"
    unsigned = code & 0xFFFFFFFF
    if unsigned in EXIT_CODE_MEANINGS:
        return "crash", f"0x{unsigned:08X} {EXIT_CODE_MEANINGS[unsigned]}"
    # 1 与 2 也常见于 Python 未捕获异常（异常钩子会另行记录）
    if unsigned in (1, 2):
        return "error", f"退出码 {code}（通常是 Python 未处理异常，见崩溃日志）"
    if unsigned == 0xFFFFFFFF or code == -1:
        return "error", "退出码 -1（进程被强制终止或主动退出）"
    return "crash", f"0x{unsigned:08X} 未知异常码（非正常退出）"


def write_marker(config_dir: pathlib.Path, **fields) -> None:
    """记录当前活动状态，供崩溃后判断死在哪个环节。"""
    path = marker_path(config_dir)
    payload = {"updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": os.getpid()}
    payload.update(fields)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")
    except OSError:
        pass


def read_marker(config_dir: pathlib.Path) -> dict:
    path = marker_path(config_dir)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def clear_marker(config_dir: pathlib.Path) -> None:
    try:
        marker_path(config_dir).unlink(missing_ok=True)
    except OSError:
        pass


def latest_crash_log(config_dir: pathlib.Path) -> pathlib.Path | None:
    directory = config_dir / "logs"
    if not directory.is_dir():
        return None
    files = sorted(directory.glob("crash-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def tail(path: pathlib.Path, lines: int = 40) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def build_report(config_dir: pathlib.Path, exit_code: int, started_at: float,
                 elapsed: float, marker: dict, log_file: pathlib.Path | None) -> str:
    kind, meaning = describe_exit_code(exit_code)
    lines = [
        "======== GameBanana 中文索引 · 异常退出报告 ========",
        f"时间        : {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"运行时长    : {elapsed:.1f} 秒（自 {time.strftime('%H:%M:%S', time.localtime(started_at))}）",
        f"退出码      : {exit_code}（{meaning}）",
        f"判定        : {'崩溃' if kind == 'crash' else ('异常' if kind == 'error' else '正常')}",
        "",
        "--- 崩溃前最后的活动标记 ---",
        json.dumps(marker, ensure_ascii=False, indent=2) if marker else "（无记录）",
        "",
        f"--- 崩溃日志 ---\n{log_file if log_file else '（未找到 logs/crash-*.log）'}",
        "",
    ]
    if log_file is not None:
        excerpt = tail(log_file)
        lines.append("--- 崩溃日志尾部 ---")
        lines.append(excerpt if excerpt else "（日志为空）")
        lines.append("")
    lines.extend([
        "--- 环境 ---",
        f"python      : {sys.version.split()[0]}",
        f"平台        : {sys.platform}",
        f"配置目录    : {config_dir}",
        "",
        "把本文件内容发给维护者即可定位问题。",
        "==================================================",
    ])
    return "\n".join(lines) + "\n"


def save_report(config_dir: pathlib.Path, text: str) -> pathlib.Path:
    """同时写「最新报告」与带时间戳的历史报告，避免多次崩溃互相覆盖。"""
    directory = config_dir / "logs"
    latest = directory / REPORT_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return latest
    stamp = time.strftime("%Y%m%d-%H%M%S")
    history = directory / f"crash-report-{stamp}.txt"
    for path in (latest, history):
        try:
            path.write_text(text, encoding="utf-8", newline="\n")
        except OSError:
            pass
    return latest

def run_with_watchdog(argv: list[str], config_dir: pathlib.Path, no_pause: bool = False,
                      auto_restart: int = 0) -> int:
    """以子进程方式启动客户端并监控其退出。

    auto_restart > 0 时，遇到崩溃会记录并自动重启（最多这么多次），
    使偶发的原生崩溃不再打断使用；退出码仍如实返回，便于外部判断。
    """
    child_argv = [sys.executable, "-m", "client.app", *argv]
    env = dict(os.environ)
    env["BANANA_INDEX_CONFIG_DIR"] = str(config_dir)
    env["BANANA_INDEX_WATCHED"] = "1"

    print(f"[看门狗] 配置目录：{config_dir}")
    if auto_restart:
        print(f"[看门狗] 崩溃自动重启：最多 {auto_restart} 次")

    attempt = 0
    last_code = 0
    while True:
        attempt += 1
        started_at = time.time()
        label = f"第 {attempt} 次启动" if attempt > 1 else "启动"
        print(f"[看门狗] {label}：{' '.join(child_argv)}")
        process = subprocess.Popen(child_argv, env=env)
        try:
            exit_code = process.wait()
        except KeyboardInterrupt:
            process.terminate()
            process.wait()
            print("[看门狗] 收到中断，已终止客户端")
            return 0

        elapsed = time.time() - started_at
        last_code = exit_code
        marker = read_marker(config_dir)
        log_file = latest_crash_log(config_dir)
        kind, meaning = describe_exit_code(exit_code)

        if kind == "normal":
            clear_marker(config_dir)
            print(f"[看门狗] 客户端正常退出（用时 {elapsed:.0f} 秒）")
            return 0

        append_history(config_dir, exit_code, elapsed, marker)
        report = build_report(config_dir, exit_code, started_at, elapsed, marker, log_file)
        saved = save_report(config_dir, report)
        print()
        print(report)
        print(f"[看门狗] 报告已保存：{saved}")

        if kind == "crash" and attempt <= auto_restart:
            print(f"[看门狗] 检测到崩溃，3 秒后自动重启（第 {attempt + 1} 次，"
                  f"上限 {auto_restart + 1} 次）…")
            time.sleep(3)
            continue
        break

    print()
    print("[看门狗] 崩溃历史统计：")
    print(summarize_history(config_dir))
    if not no_pause:
        try:
            input("[看门狗] 按回车键关闭…")
        except (EOFError, KeyboardInterrupt):
            pass
    return 1 if last_code != 0 else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GameBanana 中文索引 · 异常退出检测器")
    parser.add_argument("--no-watchdog", action="store_true",
                        help="不启用看门狗，直接在当前进程启动客户端")
    parser.add_argument("--no-pause", action="store_true",
                        help="崩溃后不等待按键（供自动化使用）")
    parser.add_argument("--print-report", action="store_true",
                        help="只打印最近的异常退出报告后退出")
    parser.add_argument("--history", action="store_true",
                        help="打印崩溃历史统计后退出")
    parser.add_argument("--restart", type=int, default=None, metavar="N",
                        help="崩溃后自动重启，最多 N 次（默认读环境变量，缺省 2）")
    known, passthrough = parser.parse_known_args(argv)

    try:
        settings = load_settings()
        config_dir = pathlib.Path(settings.config_dir or default_config_dir())
    except Exception:  # noqa: BLE001 - 配置读取失败也要能用
        config_dir = default_config_dir()

    if known.history:
        print(f"崩溃历史（{history_path(config_dir)}）：")
        print(summarize_history(config_dir))
        records = read_history(config_dir, limit=10)
        if records:
            print("\n最近 10 次：")
            for item in reversed(records):
                print(f"  {item.get('at')}  {item.get('exit_code_hex')}  "
                      f"{item.get('elapsed_seconds')}s  最后动作={item.get('last_action')}")
        return 0

    if known.print_report:
        path = report_path(config_dir)
        if path.is_file():
            print(path.read_text(encoding="utf-8", errors="replace"))
            return 0
        print(f"未找到异常退出报告：{path}")
        return 1

    if known.no_watchdog:
        from .app import main as app_main
        return app_main(passthrough) or 0

    restart = known.restart
    if restart is None:
        # 默认自动重启 2 次：偶发原生崩溃不应打断使用；可用环境变量关闭
        raw = os.environ.get("BANANA_INDEX_AUTO_RESTART", "2")
        try:
            restart = max(0, int(raw))
        except ValueError:
            restart = 2
    return run_with_watchdog(passthrough, config_dir, no_pause=known.no_pause,
                             auto_restart=restart)


if __name__ == "__main__":
    sys.exit(main())
