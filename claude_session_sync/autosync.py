"""Automatic mirroring of Claude Desktop *Code-tab* sessions between accounts, and the background watcher.

Scope (by construction): only the per-account Desktop records
``claude-code-sessions/<account>/<org>/local_*.json`` are ever read as sources.  A conversation that was only ever
run from the ``claude`` terminal has no such record, so it is never selected; Cowork / local-agent-mode data is not
looked at either.  Transcripts are never copied or modified (see planner.py).

When it runs: the Desktop app rewrites its records from memory while it is open, so the store may only be changed
while Desktop is closed.  The watcher therefore waits for Desktop to exit (that is also the moment its records are
freshly flushed), waits a few seconds, and then mirrors every account into every other one through the very same
planner/executor that the manual ``sync`` uses (backup, atomic write, verification, automatic rollback).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from . import desktop, executor, planner, schema, service, winproc
from .common import ToolError, iso_now, read_json
from .executor import Ctx
from .planner import CREATE, UPDATE, Options
from .state import State

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClaudeSessionSync"

# messages of apply errors that are worth another attempt a few seconds later
_RETRYABLE = ("appeared since", "disappeared since", "changed since", "Another claude-session-sync", "is running")


# ---- routes --------------------------------------------------------------------------------------------------

def resolve_routes(store, cfg: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """Ordered (source_account_id, target_account_id) pairs to keep in sync, plus human notes."""
    notes: list[str] = []
    routes: list[tuple[str, str]] = []

    def acct(ref):
        try:
            return store.find_account(ref).id
        except ToolError as e:
            notes.append(f"setting ignored: {e}")
            return None

    if cfg.get("routes"):
        for r in cfg["routes"]:
            f, t = acct(r.get("from")), acct(r.get("to"))
            if f and t and f != t and (f, t) not in routes:
                routes.append((f, t))
        return routes, notes
    if cfg.get("accounts"):
        ids = [x for x in (acct(a) for a in cfg["accounts"]) if x]
    else:
        ids = [a.id for a in store.accounts.values() if a.orgs or a.paired_orgs]
    for s in ids:
        for t in ids:
            if s != t:
                routes.append((s, t))
    if len(ids) < 2:
        notes.append("fewer than two accounts to mirror between; sign in to Claude Desktop with each account once")
    return routes, notes


# ---- one run -------------------------------------------------------------------------------------------------

def run_once(ctx: Ctx, *, log=print, origin: str = "auto", dry_run: bool = False, cfg: dict | None = None) -> dict:
    """Mirror sessions along every configured route.  Safe to call any time: it refuses while Desktop runs.

    Returns a summary dict; ``status`` is one of ok | skipped | partial | error.
    """
    cfg = cfg or ctx.state.auto_config()
    t0 = time.time()
    res: dict = {"started": iso_now(), "origin": origin, "dry_run": dry_run, "status": "ok", "created": 0,
                 "updated": 0, "routes": [], "notes": [], "errors": []}

    def done(status=None, reason=None):
        if status:
            res["status"] = status
        if reason:
            res["reason"] = reason
        res["finished"] = iso_now()
        res["seconds"] = round(time.time() - t0, 1)
        return res

    live = ctx.is_live()
    if live and not dry_run:
        if desktop.desktop_processes(ctx.inst):
            return done("skipped", "Claude Desktop is running")
        if desktop.running_inside_desktop(ctx.inst):
            return done("skipped", "this process was started from inside Claude Desktop (its view of the store is "
                                   "virtualised); run it from a normal Windows session instead")
        if not cfg.get("ignore_probe"):
            pr = schema.probe_app(ctx.inst.install_location, ctx.inst.version, ctx.state.cache)
            if pr.missing:
                return done("skipped", f"the installed Claude Desktop ({pr.app_version}) no longer contains the storage "
                                       f"conventions this tool relies on ({', '.join(pr.missing)}). Nothing was changed. "
                                       "Run a manual preview, or set ignore_probe once you have checked it.")
    store = service.load_store(ctx)
    routes, notes = resolve_routes(store, cfg)
    res["notes"] += notes
    cutoff = 0
    if cfg.get("max_age_days"):
        cutoff = int((time.time() - cfg["max_age_days"] * 86400) * 1000)

    for s_id, t_id in routes:
        rr: dict = {"from": s_id, "to": t_id, "from_name": store.accounts[s_id].name(),
                    "to_name": store.accounts[t_id].name(), "created": [], "updated": [], "skipped": [], "synced": 0}
        res["routes"].append(rr)
        try:
            recs = planner.select_sessions(store, s_id, None, None, True, bool(cfg.get("include_archived")), None)
            recs = [r for r in recs if r.last_activity >= cutoff]
            if not recs:
                continue
            plan = planner.build_plan(store, ctx.state, s_id, t_id, recs,
                                      Options(include_archived=bool(cfg.get("include_archived")), deep_verify=False))
            if plan.blockers:
                rr["blocked"] = plan.blockers
                continue
            for it in plan.items:
                brief = {"title": it.source.title, "cli_id": it.source.cli_id}
                if it.action == CREATE:
                    rr["created"].append(brief)
                elif it.action == UPDATE:
                    rr["updated"].append(brief | {"fields": [c.field for c in it.changes]})
                elif it.action == planner.NOOP:
                    rr["synced"] += 1
                else:
                    rr["skipped"].append(brief | {"why": it.label, "detail": (it.reasons or [""])[0]})
            if dry_run:
                continue
            if plan.runnable:
                out = executor.apply_plan(ctx, plan, store, origin=origin)
                rr["op_id"] = out.get("op_id")
                res["created"] += len(rr["created"])
                res["updated"] += len(rr["updated"])
                store = service.load_store(ctx)          # fresh hashes for the next route
            elif rr["synced"]:
                executor.apply_plan(ctx, plan, store)    # only refreshes the ledger baselines
        except ToolError as e:
            rr["error"] = str(e)
            res["errors"].append(f"{rr['from_name']} -> {rr['to_name']}: {e}")
            try:
                store = service.load_store(ctx)
            except ToolError:
                pass
    if res["errors"]:
        done("partial" if (res["created"] or res["updated"]) else "error")
    else:
        done()
    if not dry_run:
        try:
            executor.prune_auto_backups(ctx, int(cfg.get("keep_backups") or 0))
        except OSError as e:
            res["notes"].append(f"could not prune old automatic backups: {e}")
    if not dry_run and (res["created"] or res["updated"] or res["errors"]):
        ctx.state.log({"event": "auto-run", "origin": origin, "status": res["status"], "created": res["created"],
                       "updated": res["updated"], "errors": res["errors"]})
    return res


def summarise(res: dict) -> dict:
    """Small dict for the status file / GUI."""
    return {k: res.get(k) for k in ("started", "finished", "origin", "status", "reason", "created", "updated",
                                    "errors", "notes", "seconds", "dry_run")} | {
        "routes": [{"from": r["from_name"], "to": r["to_name"], "created": [c["title"] for c in r["created"]],
                    "updated": [c["title"] for c in r["updated"]], "synced": r["synced"],
                    "skipped": [f"{c['title']} - {c['why']}" + (f": {c['detail'][:110]}" if c.get("detail") else "")
                                for c in r["skipped"]],
                    "blocked": r.get("blocked"), "error": r.get("error")} for r in res.get("routes", [])]}


def run_with_retry(ctx: Ctx, *, log=print, origin="auto", attempts: int = 3, wait: float = 15,
                   stop: threading.Event | None = None) -> dict:
    res: dict = {}
    for i in range(attempts):
        res = run_once(ctx, log=log, origin=origin)
        transient = res.get("status") in ("error", "partial") and any(
            any(t in e for t in _RETRYABLE) for e in res.get("errors", []))
        if not transient or i == attempts - 1:
            break
        log(f"transient problem ({res['errors'][0]}); retrying in {wait:.0f}s")
        if stop and stop.wait(wait):
            break
        elif not stop:
            time.sleep(wait)
    return res


# ---- watcher --------------------------------------------------------------------------------------------------

def make_file_log(state: State, echo: bool = False):
    """Append-only log with a size cap, for the hidden watcher (there is no console)."""
    path = state.watch_log_path

    def log(msg: str):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
        if echo:
            print(line, flush=True)
        try:
            state.ensure()
            if os.path.exists(path) and os.path.getsize(path) > 400_000:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-800:]
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(tail)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    return log


def read_watch_log(state: State, n: int = 60) -> list[str]:
    try:
        with open(state.watch_log_path, "r", encoding="utf-8", errors="replace") as f:
            return [x.rstrip("\n") for x in f.readlines()[-n:]]
    except OSError:
        return []


def watcher_running(state: State) -> dict | None:
    """Lock info of a live watcher, or None (a stale lock from a crash is not 'running')."""
    try:
        info = read_json(state.watch_lock_path)
        if winproc.pid_alive(int(info["pid"]), info.get("start_ft")):
            return info
    except Exception:
        pass
    return None


def _acquire_watch_lock(state: State) -> None:
    state.ensure()
    for _ in range(2):
        try:
            fd = os.open(state.watch_lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = watcher_running(state)
            if info:
                raise ToolError(f"the background watcher is already running (pid {info['pid']}).")
            try:
                os.remove(state.watch_lock_path)
            except OSError:
                pass
            continue
        import json
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "start_ft": winproc.start_time(os.getpid()), "since": iso_now()}, f)
        return
    raise ToolError("could not acquire the watcher lock")


def watch(ctx: Ctx, *, log=print, stop: threading.Event | None = None, max_seconds: float | None = None,
          desktop_running=None) -> None:
    """Foreground loop: mirror sessions every time Claude Desktop has just been closed.

    ``desktop_running`` (callable -> bool) exists for tests; by default the real Desktop process list is used.
    """
    st = ctx.state
    is_running = desktop_running or (lambda: bool(desktop.desktop_processes(ctx.inst)) if ctx.is_live() else False)
    _acquire_watch_lock(st)
    started = iso_now()
    stop = stop or threading.Event()
    status: dict = {"pid": os.getpid(), "started": started, "state": "starting", "last_run": None}
    try:
        os.remove(st.watch_stop_path)
    except OSError:
        pass

    def publish(state_name: str, **kw):
        status.update({"state": state_name, "heartbeat": iso_now(), **kw})
        st.write_status(status)

    cfg = st.auto_config()
    poll = max(1, int(cfg["poll_seconds"]))
    prev: bool | None = None
    due: float | None = None
    last_beat = 0.0
    t_end = time.time() + max_seconds if max_seconds else None
    log(f"watcher started (pid {os.getpid()}); mirroring when Claude Desktop is closed")
    try:
        while not stop.is_set():
            if os.path.exists(st.watch_stop_path):
                try:
                    os.remove(st.watch_stop_path)
                except OSError:
                    pass
                log("stop requested")
                break
            if t_end and time.time() > t_end:
                break
            running = is_running()
            cfg = st.auto_config()
            settle = max(1, int(cfg["settle_seconds"]))
            if prev is None:
                if not running and cfg.get("on_start"):
                    due = time.time() + settle
                    log("Claude Desktop is closed; catching up shortly")
            elif prev and not running:
                due = time.time() + settle
                log(f"Claude Desktop closed; syncing in {settle}s")
            elif running and due:
                due = None
                log("Claude Desktop started again; sync cancelled")
            if prev is None or prev != running:
                publish("desktop-running" if running else "idle", desktop_running=running)
            prev = running
            if due and not running and time.time() >= due:
                due = None
                publish("syncing", desktop_running=False)
                try:
                    res = run_with_retry(ctx, log=log, origin="auto", stop=stop)
                except Exception as e:                        # never let the watcher die
                    res = {"status": "error", "errors": [f"{type(e).__name__}: {e}"], "created": 0, "updated": 0,
                           "started": iso_now(), "finished": iso_now(), "routes": [], "notes": []}
                brief = summarise(res)
                status["last_run"] = brief
                if res.get("status") == "skipped":
                    log(f"skipped: {res.get('reason')}")
                else:
                    log(f"sync {res['status']}: {res['created']} new, {res['updated']} updated"
                        + (f"; errors: {'; '.join(res['errors'])}" if res.get("errors") else ""))
                publish("desktop-running" if is_running() else "idle")
            elif time.time() - last_beat > 20:
                publish(status.get("state", "idle"))
                last_beat = time.time()
            stop.wait(poll)
    finally:
        try:
            st.write_status({**status, "state": "stopped", "heartbeat": iso_now(), "pid": None})
            os.remove(st.watch_lock_path)
        except OSError:
            pass
        log("watcher stopped")


def watcher_argv(*args: str) -> list[str]:
    """Command line that starts this tool windowless (frozen exe, zipapp or plain source tree)."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        gui = os.path.join(os.path.dirname(exe), "claude-session-sync-gui.exe")
        return [gui if os.path.exists(gui) else exe, *args]
    py = sys.executable
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    py = pyw if os.path.exists(pyw) else py
    here = os.path.abspath(__file__)
    if ".pyz" in here.lower():
        pyz = here[: here.lower().index(".pyz") + 4]
        return [py, pyz, *args]
    root = os.path.dirname(os.path.dirname(here))
    boot = f"import sys; sys.path.insert(0, {root!r}); from claude_session_sync.cli import main; sys.exit(main({list(args)!r}))"
    return [py, "-c", boot]


def _refuse_inside_desktop(ctx: Ctx, what: str) -> None:
    if desktop.running_inside_desktop(ctx.inst):
        raise ToolError(f"{what} from inside Claude Desktop is not possible: the process would be a child of Desktop "
                        "(it would end when Desktop closes, exactly when it is needed) and would see Windows' "
                        "virtualised view of the app's folders. Do this from a normal terminal or by opening the GUI "
                        "outside Claude Desktop.")


def start_watcher(ctx: Ctx) -> dict:
    if watcher_running(ctx.state):
        return {"started": False, "already": True}
    _refuse_inside_desktop(ctx, "Starting the background watcher")
    args = ["--state-dir", ctx.state.root] if ctx.state.root != State().root else []
    if ctx.inst.data_root and not ctx.is_live():
        args += ["--data-root", ctx.inst.data_root]
    flags = 0x00000008 | 0x00000200 | 0x08000000      # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    subprocess.Popen(watcher_argv(*args, "watch", "--quiet"), creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):                                   # up to ~8 s
        time.sleep(0.2)
        info = watcher_running(ctx.state)
        if info:
            return {"started": True, "pid": info["pid"]}
    raise ToolError("the watcher process did not start; see " + ctx.state.watch_log_path)


def stop_watcher(ctx: Ctx, wait: float = 20, force: bool = False) -> dict:
    info = watcher_running(ctx.state)
    if not info:
        return {"stopped": True, "was_running": False}
    ctx.state.ensure()
    with open(ctx.state.watch_stop_path, "w") as f:
        f.write(iso_now())
    end = time.time() + wait
    while time.time() < end:
        if not watcher_running(ctx.state):
            return {"stopped": True, "was_running": True}
        time.sleep(0.4)
    if force:
        winproc.terminate(int(info["pid"]))
        time.sleep(0.5)
        try:
            os.remove(ctx.state.watch_lock_path)
        except OSError:
            pass
        return {"stopped": True, "was_running": True, "forced": True}
    return {"stopped": False, "was_running": True,
            "note": "the watcher is busy (probably in the middle of a sync); try again in a moment"}


# ---- start with Windows (HKCU Run key: no admin rights, visible in Task Manager > Startup) ------------------------

def _cmdline(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv)


def autostart_get() -> str | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_VALUE)[0]
    except Exception:
        return None


def autostart_set(ctx: Ctx, enable: bool) -> dict:
    import winreg
    _refuse_inside_desktop(ctx, "Changing the start-with-Windows setting")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if enable:
            cmd = _cmdline(watcher_argv("watch", "--quiet"))
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, cmd)
            return {"enabled": True, "command": cmd}
        try:
            winreg.DeleteValue(k, RUN_VALUE)
        except FileNotFoundError:
            pass
    return {"enabled": False}


# ---- overview for CLI / GUI ----------------------------------------------------------------------------------------------

def auto_overview(ctx: Ctx, store=None) -> dict:
    store = store or service.load_store(ctx)
    cfg = ctx.state.auto_config()
    routes, notes = resolve_routes(store, cfg)
    info = watcher_running(ctx.state)
    status = ctx.state.read_status()
    ev = [e for e in ctx.state.read_log(400) if e.get("event") in ("auto-run", "prune-auto-backups")]
    from . import analysis
    attention = []
    for r in store.all_records():
        a = analysis.assess(store, r)
        if a.status in (analysis.MISSING, analysis.CONFLICT, analysis.REVIEW, analysis.DELETED):
            attention.append({"title": r.title, "account": store.accounts[r.account_id].name(), "status": a.status,
                              "why": (a.reasons(("error", "warn")) or [""])[0]})
    return {
        "attention": attention,
        "config": cfg,
        "routes": [{"from": s, "to": t, "from_name": store.accounts[s].name(), "to_name": store.accounts[t].name()}
                   for s, t in routes],
        "notes": notes,
        "watcher": {"running": bool(info), "pid": (info or {}).get("pid"), "since": (info or {}).get("since"),
                    "state": status.get("state") if info else "stopped", "heartbeat": status.get("heartbeat"),
                    "last_run": status.get("last_run")},
        "autostart": {"enabled": bool(autostart_get()), "command": autostart_get()},
        "desktop_running": bool(desktop.desktop_processes(ctx.inst)) if ctx.is_live() else False,
        "inside_desktop": desktop.running_inside_desktop(ctx.inst),
        "events": ev[-25:],
        "log": read_watch_log(ctx.state, 40),
        "accounts": [{"id": a.id, "name": a.name(), "sessions": sum(len(o.records) for o in a.orgs.values()),
                      "usable": bool(a.orgs or a.paired_orgs)} for a in store.accounts.values()],
    }
