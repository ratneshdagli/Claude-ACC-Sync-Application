"""Local GUI: a tiny loopback-only HTTP server + a single-page UI shown in an Edge/Chrome *app window*.

Security model: binds to 127.0.0.1 only, random port, random per-launch token required on every API call,
Host/Origin checks (defeats DNS-rebinding and cross-site requests), no CORS headers, static files limited to
the bundled index.html.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import analysis, autosync, desktop, executor, planner, service
from .common import TOOL_NAME, TOOL_VERSION, ToolError, norm_path
from .planner import Options

HERE = os.path.dirname(os.path.abspath(__file__))


def _index_html() -> bytes:
    from importlib import resources
    return resources.files("claude_session_sync").joinpath("ui", "index.html").read_bytes()


class App:
    def __init__(self, ctx):
        self.ctx = ctx
        self.lock = threading.Lock()
        self.plans: dict[str, tuple] = {}     # plan_id -> (plan, request)
        self.jobs: dict[str, dict] = {}
        self.token = secrets.token_urlsafe(24)
        self.port = 0

    # --- jobs (long operations) ---
    def start_job(self, title: str, fn):
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "title": title, "log": [], "done": False, "ok": None, "result": None, "error": None}
        self.jobs[jid] = job

        def logf(msg):
            job["log"].append(str(msg))

        def run():
            try:
                with self.lock:
                    job["result"] = fn(logf)
                job["ok"] = True
            except ToolError as e:
                job["ok"], job["error"] = False, str(e)
            except Exception as e:  # unexpected
                job["ok"], job["error"] = False, f"{type(e).__name__}: {e}"
                job["log"].append(traceback.format_exc())
            finally:
                job["done"] = True

        threading.Thread(target=run, daemon=True).start()
        return jid

    def fresh(self):
        return service.load_store(self.ctx)

    def opts(self, d: dict) -> Options:
        allowed = set(Options.__dataclass_fields__)
        return Options(**{k: v for k, v in (d or {}).items() if k in allowed})


def make_handler(app: App):

    class H(BaseHTTPRequestHandler):
        server_version = "cssync"

        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype == "application/json" else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

        def _guard(self, need_token=True) -> bool:
            port = app.port
            if self.headers.get("Host", "") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                self._json({"error": "bad host"}, 403)
                return False
            org = self.headers.get("Origin")
            if org and org not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
                self._json({"error": "bad origin"}, 403)
                return False
            if need_token and not secrets.compare_digest(self.headers.get("X-Token", ""), app.token):
                self._json({"error": "bad token"}, 403)
                return False
            return True

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                if not self._guard(need_token=False):
                    return
                self._send(200, _index_html(), "text/html")
                return
            if not self._guard():
                return
            q = parse_qs(u.query)
            try:
                if u.path == "/api/overview":
                    st = app.fresh()
                    return self._json(service.overview(app.ctx, st))
                if u.path == "/api/sessions":
                    st = app.fresh()
                    deep = q.get("deep", ["0"])[0] == "1"
                    return self._json(service.session_rows(st, analysis.assess_all(st, deep=deep)))
                if u.path == "/api/session":
                    return self._json(self._session_detail(q["path"][0]))
                if u.path == "/api/job":
                    j = app.jobs.get(q["id"][0])
                    return self._json(j or {"error": "unknown job"}, 200 if j else 404)
                if u.path == "/api/auto":
                    return self._json(autosync.auto_overview(app.ctx, app.fresh()))
                if u.path == "/api/history":
                    ops = app.ctx.state.list_ops()
                    keep = ("op_id", "kind", "status", "created", "note", "source_account", "target_account", "_dir")
                    return self._json({"ops": [{k: m.get(k) for k in keep} | {"changes": len(m.get("changes", []))} for m in ops],
                                       "log": app.ctx.state.read_log(60)})
                self._json({"error": "not found"}, 404)
            except ToolError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def do_POST(self):
            if not self._guard():
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                self._json(self._post(urlparse(self.path).path, body))
            except ToolError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()}, 500)

        # ---- endpoints ----
        def _session_detail(self, path):
            st = app.fresh()
            rec = next((r for r in st.all_records() if norm_path(r.path) == norm_path(path)), None)
            if not rec:
                raise ToolError("record not found")
            a = analysis.assess(st, rec, deep=True)
            from . import schema
            cls = schema.classify_keys((rec.raw or {}).keys())
            ref = st.transcripts.get(rec.cli_id or "")
            side = []
            if ref:
                for d in ref.sidecar_dirs:
                    for r_, _ds, fs in os.walk(d):
                        for f in fs:
                            side.append(os.path.relpath(os.path.join(r_, f), d))
            return {"local_id": rec.local_id, "cli_id": rec.cli_id, "account_id": rec.account_id, "org_id": rec.org_id,
                    "path": rec.path, "title": rec.title, "cwd": rec.cwd, "status": a.status,
                    "flags": [f.to_dict() for f in a.flags], "transcript": a.transcript, "transcript_path": a.transcript_path,
                    "fields": cls, "size": rec.size, "sha256": rec.sha256, "prior_cli_ids": rec.prior_ids,
                    "sidecar_files": side[:200], "released_markers": ref.released_markers if ref else [],
                    "tombstones": [{"what": w, "org": o, "path": p} for w, o, p in analysis.tombstone_hits(st, rec)],
                    "project_exists": bool(rec.cwd and os.path.isdir(rec.cwd))}

        def _post(self, path, b):
            if path == "/api/plan":
                st = app.fresh()
                plan = service.make_plan(app.ctx, st, b["source"], b["target"], b.get("sessions"), b.get("projects"),
                                         bool(b.get("all")), app.opts(b.get("options")))
                app.plans[plan.id] = (plan, b)
                d = plan.to_dict()
                d["runnable"] = len(plan.runnable)
                d["desktop_running"] = desktop.is_running(app.ctx.inst) if app.ctx.is_live() else False
                d["inside_desktop"] = desktop.running_inside_desktop(app.ctx.inst)
                return d
            if path == "/api/apply":
                plan, req = app.plans.get(b.get("plan_id"), (None, None))
                if not plan:
                    raise ToolError("plan expired; preview again")
                if not b.get("confirm"):
                    raise ToolError("confirmation required")
                close, restart, force = bool(b.get("close_desktop")), bool(b.get("restart_desktop")), bool(b.get("force_close"))

                def job(log):
                    ctx = executor.Ctx(app.ctx.inst, app.ctx.state, app.ctx.real, log)
                    store = service.load_store(ctx)
                    p = plan
                    was = ctx.is_live() and desktop.is_running(ctx.inst)
                    if was:
                        if not close:
                            raise ToolError("Claude Desktop is running; tick 'Close Claude Desktop first' or quit it.")
                        res = desktop.close_desktop(ctx.inst, store, force=force, log=log)
                        if not res.get("closed"):
                            raise ToolError(res.get("note") or "could not close Claude Desktop")
                        log("Claude Desktop closed; re-reading the store.")
                        store = service.load_store(ctx)
                        p = service.make_plan(ctx, store, req["source"], req["target"], req.get("sessions"),
                                              req.get("projects"), bool(req.get("all")), app.opts(req.get("options")))
                        if service.plan_fingerprint(p) != service.plan_fingerprint(plan):
                            if restart:
                                desktop.start_desktop(ctx.inst)
                            raise ToolError("The plan changed while Claude Desktop was closing (it flushed data). "
                                            "Nothing was written. Preview again.")
                    try:
                        r = executor.apply_plan(ctx, p, store, include_transcript_backup=bool(b.get("backup_transcripts")))
                    finally:
                        if was and restart:
                            log("Restarting Claude Desktop ...")
                            desktop.start_desktop(ctx.inst)
                    ctx.state.learn_identity(store)
                    return r

                return {"job": app.start_job("Sync", job)}
            if path == "/api/auto/config":
                st = app.fresh()
                upd = {}
                for k in ("max_age_days", "keep_backups"):
                    if k in b:
                        upd[k] = b[k]
                if "include_archived" in b:
                    upd["include_archived"] = bool(b["include_archived"])
                if b.get("mode") == "both":
                    ids = [st.find_account(x).id for x in (b.get("accounts") or [])]
                    if len(ids) == 1:
                        raise ToolError("Pick at least two accounts to mirror between (or none = all).")
                    upd["accounts"], upd["routes"] = ids, []
                elif b.get("mode") == "one-way":
                    f, t = st.find_account(b.get("from") or ""), st.find_account(b.get("to") or "")
                    if f.id == t.id:
                        raise ToolError("From and To must be different accounts.")
                    upd["routes"], upd["accounts"] = [{"from": f.id, "to": t.id}], []
                app.ctx.state.set_auto_config(upd)
                return autosync.auto_overview(app.ctx, st)
            if path == "/api/auto/preview":
                return autosync.summarise(autosync.run_once(app.ctx, origin="manual", dry_run=True))
            if path == "/api/auto/watcher":
                act = b.get("action")
                if act == "start":
                    return autosync.start_watcher(app.ctx)
                if act == "stop":
                    return autosync.stop_watcher(app.ctx, force=bool(b.get("force")))
                raise ToolError("unknown watcher action")
            if path == "/api/auto/enable":
                on = bool(b.get("enable"))
                if on:
                    r = autosync.autostart_set(app.ctx, True)
                    w = autosync.start_watcher(app.ctx)
                    return {"autostart": r, "watcher": w}
                autosync.autostart_set(app.ctx, False)
                return {"autostart": {"enabled": False}, "watcher": autosync.stop_watcher(app.ctx, force=bool(b.get("force")))}
            if path in ("/api/auto/run", "/api/launch"):
                launch = path == "/api/launch"
                close, restart, force = bool(b.get("close_desktop")), bool(b.get("restart_desktop")), bool(b.get("force_close"))

                def job(log):
                    ctx = executor.Ctx(app.ctx.inst, app.ctx.state, app.ctx.real, log)
                    store = service.load_store(ctx)
                    was = ctx.is_live() and desktop.is_running(ctx.inst)
                    if was and launch:
                        raise ToolError("Claude Desktop is already running.")
                    if was:
                        if not close:
                            raise ToolError("Claude Desktop is running; tick 'Close Claude Desktop first' or quit it.")
                        res = desktop.close_desktop(ctx.inst, store, force=force, log=log)
                        if not res.get("closed"):
                            raise ToolError(res.get("note") or "could not close Claude Desktop")
                        log("Claude Desktop closed.")
                    try:
                        r = autosync.run_once(ctx, log=log, origin="manual")
                    finally:
                        if (was and restart) or launch:
                            log("Starting Claude Desktop ...")
                            desktop.start_desktop(ctx.inst)
                    return autosync.summarise(r)
                return {"job": app.start_job("Auto-sync run", job)}
            if path == "/api/backup":
                def job(log):
                    ctx = executor.Ctx(app.ctx.inst, app.ctx.state, app.ctx.real, log)
                    return executor.standalone_backup(ctx, service.load_store(ctx), bool(b.get("include_transcripts")))
                return {"job": app.start_job("Backup", job)}
            if path == "/api/verify":
                def job(log):
                    ctx = executor.Ctx(app.ctx.inst, app.ctx.state, app.ctx.real, log)
                    return executor.verify_store(ctx, service.load_store(ctx), deep=bool(b.get("deep")))
                return {"job": app.start_job("Verify", job)}
            if path == "/api/rollback":
                def job(log):
                    ctx = executor.Ctx(app.ctx.inst, app.ctx.state, app.ctx.real, log)
                    store = service.load_store(ctx)
                    was = ctx.is_live() and desktop.is_running(ctx.inst)
                    if was:
                        if not b.get("close_desktop"):
                            raise ToolError("Claude Desktop is running; tick 'Close Claude Desktop first' or quit it.")
                        res = desktop.close_desktop(ctx.inst, store, force=bool(b.get("force_close")), log=log)
                        if not res.get("closed"):
                            raise ToolError(res.get("note") or "could not close Claude Desktop")
                        store = service.load_store(ctx)
                    try:
                        return executor.rollback(ctx, b.get("op"), store, force=bool(b.get("force")))
                    finally:
                        if was and b.get("restart_desktop"):
                            desktop.start_desktop(ctx.inst)
                return {"job": app.start_job("Rollback", job)}
            if path == "/api/report":
                st = app.fresh()
                rep = service.diagnostic_report(app.ctx, st, bool(b.get("redact")))
                p = service.write_report(app.ctx, rep)
                return {"path": p, "report": rep}
            if path == "/api/label":
                st = app.fresh()
                acct = st.find_account(b["account"])
                app.ctx.state.set_label(acct.id, b.get("label") or None, b.get("email") or None)
                return {"ok": True}
            if path == "/api/open-project":
                st = app.fresh()
                p = b.get("path") or ""
                known = {norm_path(r.cwd) for r in st.all_records() if r.cwd}
                if norm_path(p) not in known:
                    raise ToolError("Not a project folder of any known session.")
                if not os.path.isdir(p):
                    raise ToolError("Folder no longer exists: " + p)
                os.startfile(p)  # noqa: S606 (opens in Explorer; path validated against known sessions)
                return {"ok": True}
            if path == "/api/desktop":
                st = app.fresh()
                act = b.get("action")
                if act == "status":
                    return service.desktop_status(app.ctx, st)
                if act == "start":
                    return {"started": desktop.start_desktop(app.ctx.inst)}
                if act == "close":
                    return desktop.close_desktop(app.ctx.inst, st, force=bool(b.get("force")))
            raise ToolError("unknown endpoint")

    return H


def _find_browser() -> str | None:
    cands = []
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                      os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    for c in cands:
        if os.path.exists(c):
            return c
    return shutil.which("msedge") or shutil.which("chrome")


def serve(ctx, open_browser: bool = True, port: int = 0):
    app = App(ctx)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    app.port = srv.server_address[1]
    port = app.port
    url = f"http://127.0.0.1:{port}/?t={app.token}"
    print(f"{TOOL_NAME} {TOOL_VERSION} GUI: {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if open_browser:
        br = _find_browser()
        try:
            if br:
                subprocess.Popen([br, f"--app={url}", "--window-size=1320,860",
                                  f"--user-data-dir={os.path.join(ctx.state.root, 'gui-profile')}",
                                  "--no-first-run", "--no-default-browser-check"])
            else:
                webbrowser.open(url)
        except OSError:
            webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
