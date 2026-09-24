"""Process enumeration and window control via ctypes (no third-party deps)."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time
from dataclasses import dataclass

IS_WIN = sys.platform == "win32"

if IS_WIN:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    HANDLE = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.restype = HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k32.OpenProcess.restype = HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.CloseHandle.argtypes = [HANDLE]
    k32.QueryFullProcessImageNameW.argtypes = [HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    k32.GetProcessTimes.argtypes = [HANDLE, ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
                                    ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME)]
    k32.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(wt.DWORD)]
    k32.TerminateProcess.argtypes = [HANDLE, wt.UINT]

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", wt.WCHAR * 260)]

    k32.Process32FirstW.argtypes = [HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [HANDLE, ctypes.POINTER(PROCESSENTRY32W)]

TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
STILL_ACTIVE = 259
WM_CLOSE = 0x0010


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str
    exe: str | None
    start_ft: int | None


def _proc_times(h) -> int | None:
    c, e, k, u = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
    if k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
        return (c.dwHighDateTime << 32) | c.dwLowDateTime
    return None


def _exe_of(h) -> str | None:
    buf = ctypes.create_unicode_buffer(1024)
    size = wt.DWORD(1024)
    if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
        return buf.value
    return None


def list_processes(name_contains: str | None = None) -> list[Proc]:
    if not IS_WIN:
        return []
    out: list[Proc] = []
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return out
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            nm = pe.szExeFile
            if not name_contains or name_contains.lower() in nm.lower():
                exe, st = None, None
                h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pe.th32ProcessID)
                if h:
                    try:
                        exe, st = _exe_of(h), _proc_times(h)
                    finally:
                        k32.CloseHandle(h)
                out.append(Proc(pe.th32ProcessID, pe.th32ParentProcessID, nm, exe, st))
            ok = k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    return out


def start_time(pid: int) -> int | None:
    """Creation time (FILETIME) of a running process, or None."""
    if not IS_WIN:
        return None
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        return _proc_times(h)
    finally:
        k32.CloseHandle(h)


def pid_alive(pid: int, start_ft: int | str | None = None) -> bool:
    """True if pid is running (and, when given, was started at ``start_ft`` -- guards against pid reuse)."""
    if not IS_WIN:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        code = wt.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return False
        if start_ft not in (None, ""):
            st = _proc_times(h)
            if st is not None and int(start_ft) != st:
                return False
        return True
    finally:
        k32.CloseHandle(h)


def post_close_to_windows(pids: set[int]) -> int:
    """Send WM_CLOSE to every top-level window owned by ``pids`` (graceful close request)."""
    if not IS_WIN:
        return 0
    count = 0
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _lp):
        nonlocal count
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            count += 1
        return True

    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return count


def terminate(pid: int) -> bool:
    if not IS_WIN:
        return False
    h = k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not h:
        return False
    try:
        return bool(k32.TerminateProcess(h, 1))
    finally:
        k32.CloseHandle(h)


def wait_gone(pids: set[int], timeout: float) -> set[int]:
    """Wait until none of ``pids`` is alive; return the survivors."""
    end = time.time() + timeout
    alive = {p for p in pids if pid_alive(p)}
    while alive and time.time() < end:
        time.sleep(0.4)
        alive = {p for p in alive if pid_alive(p)}
    return alive
