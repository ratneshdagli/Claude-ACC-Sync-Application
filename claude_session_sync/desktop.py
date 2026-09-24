"""Detect / close / restart Claude Desktop safely."""
from __future__ import annotations

import os
import subprocess
import time

from . import winproc
from .common import ToolError, norm_path
from .environment import Installation
from .store import Store


def _is_desktop_exe(inst: Installation, exe: str | None) -> bool:
    if not exe:
        return False
    e = norm_path(exe)
    if os.sep + "claude-code" + os.sep in e or os.sep + "claude-code-vm" + os.sep in e:
        return False                                   # the Claude Code CLI binary, not the Desktop app
    if inst.exe_path and e == norm_path(inst.exe_path):
        return True
    if inst.install_location and e.startswith(norm_path(inst.install_location) + os.sep):
        return os.path.basename(e) in ("claude.exe",)
    return False


def desktop_processes(inst: Installation) -> list[winproc.Proc]:
    return [p for p in winproc.list_processes("claude") if _is_desktop_exe(inst, p.exe)]


def is_running(inst: Installation) -> bool:
    return bool(desktop_processes(inst))


def running_inside_desktop(inst: Installation | None = None) -> bool:
    """True if this process was launched from inside Claude Desktop (e.g. from a Code-tab session).

    Three independent signals: the CLAUDE_CODE_ENTRYPOINT env var that Desktop sets for the sessions it spawns,
    MSIX file-virtualisation aliasing of the APPDATA Claude folder, and the process ancestry.
    """
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").lower() == "claude-desktop":
        return True
    if inst is not None and inst.running_inside_package:
        return True
    procs = {p.pid: p for p in winproc.list_processes()}
    pid = os.getpid()
    seen = set()
    while pid in procs and pid not in seen:
        seen.add(pid)
        p = procs[pid]
        exe = (p.exe or "").lower().replace("/", "\\")
        if "\\windowsapps\\claude_" in exe and exe.endswith("\\app\\claude.exe"):
            return True
        pid = p.ppid
    return False


def busy_sessions(store: Store) -> list:
    return [lp for lp in store.live.values() if (lp.status or "").lower() in ("busy", "running", "working")]


def close_desktop(inst: Installation, store: Store, *, timeout: float = 45, force: bool = False,
                  log=print) -> dict:
    procs = desktop_processes(inst)
    if not procs:
        return {"was_running": False, "closed": True}
    if running_inside_desktop(inst):
        raise ToolError("This tool is running INSIDE the Claude Desktop you asked it to close (it was started from a "
                        "Code-tab session). Closing Desktop would kill this process. Run claude-session-sync from a "
                        "normal terminal (Windows Terminal / PowerShell opened outside Claude) instead.")
    busy = busy_sessions(store)
    if busy and not force:
        raise ToolError("Claude Code sessions are working right now: " +
                        ", ".join(f"{b.cli_id[:8]} (pid {b.pid})" for b in busy) +
                        ". Let them finish or stop them, or pass --force-close to close anyway.")
    pids = {p.pid for p in procs}
    log(f"Asking Claude Desktop to close ({len(pids)} processes)...")
    winproc.post_close_to_windows(pids)
    left = winproc.wait_gone(pids, timeout)
    if left and not force:
        return {"was_running": True, "closed": False,
                "note": "Claude Desktop did not exit (it may have minimised to the tray). Quit it from the tray icon "
                        "(right-click -> Quit) or use --force-close."}
    if left:
        log("Force-terminating remaining Claude Desktop processes...")
        for pid in left:
            winproc.terminate(pid)
        left = winproc.wait_gone(left, 10)
    # child Claude Code CLI processes exit with Desktop; give them a moment
    time.sleep(1.0)
    return {"was_running": True, "closed": not left, "remaining": sorted(left)}


def start_desktop(inst: Installation) -> bool:
    try:
        if inst.kind == "msix" and inst.app_user_model_id:
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{inst.app_user_model_id}"],
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            return True
        if inst.exe_path and os.path.exists(inst.exe_path):
            subprocess.Popen([inst.exe_path], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                             close_fds=True)
            return True
    except OSError:
        return False
    return False
