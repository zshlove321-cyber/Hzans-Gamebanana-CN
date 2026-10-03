"""检测进程完整性。低完整性可来自启动文件标签，并不意味着用户提权。"""
from __future__ import annotations

import os
import subprocess
import sys


def integrity_level() -> int | None:
    """读取本进程 Windows 完整性 RID：Low=4096，Medium=8192，High=12288。"""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                          ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
    advapi.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        return None
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 25, None, 0, ctypes.byref(needed))
        data = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 25, data, needed, ctypes.byref(needed)):
            return None
        sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
        count = advapi.GetSidSubAuthorityCount(sid)[0]
        return advapi.GetSidSubAuthority(sid, count - 1)[0]
    finally:
        kernel.CloseHandle(token)


def is_elevated() -> bool:
    """当前进程是否以管理员（提升）权限运行。非 Windows 一律返回 False。"""
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - 检测失败按「未提升」处理，不影响使用
        try:
            result = subprocess.run(
                ["net", "session"], capture_output=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return result.returncode == 0
        except Exception:  # noqa: BLE001
            return False


def elevation_notice() -> str:
    """需要提醒用户时返回说明文字；否则返回空串。"""
    if not is_elevated():
        return ""
    return "当前以管理员权限运行；建议使用普通权限启动，以保持浏览器和文件管理器的权限一致。"
