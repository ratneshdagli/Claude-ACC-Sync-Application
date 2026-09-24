"""Command-line interface."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import textwrap

from . import analysis, autosync, desktop, executor, planner, service
from .common import TOOL_NAME, TOOL_VERSION, ToolError, fmt_ms, human_size, short
from .planner import BLOCKED, CONFLICT_, CREATE, NOOP, UPDATE, Options

# ---- tiny output helpers ----------------------------------------------------------------------------------------

_COLOR = sys.stdout is not None and sys.stdout.isatty() and not os.environ.get("NO_COLOR")   # no stdout in the windowless exe
if _COLOR and os.name == "nt":
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)
    except Exception:
        _COLOR = False

STATUS_COLOR = {analysis.HEALTHY: "32", analysis.SYNCED: "36", analysis.STALE: "33", analysis.REVIEW: "33",
                analysis.MISSING: "31", analysis.CONFLICT: "31", analysis.DELETED: "35",
                "New": "32", "Update": "34"}


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def cs(status: str) -> str:
    return c(status, STATUS_COLOR.get(status, "0"))


def table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(h) for h in headers]
    for r in rows:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(_strip(v)))
    def line(cells):
        return "  ".join(_pad(v, widths[i]) for i, v in enumerate(cells))
    out = [line(headers), line(["-" * w for w in widths])]
    out += [line(r) for r in rows]
    return "\n".join(out)


def _strip(s: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


def _pad(s: str, w: int) -> str:
    return s + " " * (w - len(_strip(s)))


def trunc(s: str | None, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def emit_json(obj):
    print(json.dumps(obj, indent=1, ensure_ascii=False, default=str))


# ---- argument parsing ---------------------------------------------------------------------------------------------

def add_selectors(p):
    g = p.add_argument_group("what to synchronise")
    g.add_argument("--source", "-S", required=True, help="source account (id/prefix, label or e-mail)")
    g.add_argument("--target", "-T", required=True, help="target account (id/prefix, label or e-mail)")
    g.add_argument("--session", "-s", action="append", default=[], metavar="REF",
                   help="session: local id, CLI id, id prefix or title (repeatable)")
    g.add_argument("--project", "-p", action="append", default=[], metavar="PATH", help="every session of this project folder")
    g.add_argument("--all", action="store_true", help="every compatible (non-archived) session of the source account")
    g.add_argument("--include-archived", action="store_true")
    g.add_argument("--source-org", help="restrict to this source organisation (id prefix)")
    g.add_argument("--target-org", help="target organisation (id prefix) if it cannot be inferred")
    o = p.add_argument_group("policy options (all explicit, all off by default)")
    o.add_argument("--resurrect", action="store_true", help="re-create a session the TARGET account deleted (removes its tombstone)")
    o.add_argument("--clear-released-marker", action="store_true",
                   help="delete the '<cli>.desktop-released.json' marker beside a transcript")
    o.add_argument("--force-update", action="store_true", help="overwrite progress fields even if the target record is ahead")
    o.add_argument("--new-id-on-collision", action="store_true",
                   help="if the wanted target file name belongs to another conversation, use a fresh id")
    o.add_argument("--carry-unknown", action="store_true", help="also copy fields this tool does not recognise")
    o.add_argument("--preserve-local-id", action="store_true",
                   help="reuse the source's local id (default: local_<cliSessionId>)")
    o.add_argument("--drop-permission-grants", action="store_true",
                   help="do not carry always-allowed rules / granted folders (permission mode is kept)")
    o.add_argument("--no-deep-verify", action="store_true", help="skip the full transcript scan in the preview")


def opts_from(a) -> Options:
    return Options(include_archived=a.include_archived, carry_unknown=a.carry_unknown,
                   preserve_local_id=a.preserve_local_id, resurrect=a.resurrect,
                   clear_released_marker=a.clear_released_marker, force_update=a.force_update,
                   new_id_on_collision=a.new_id_on_collision, target_org=a.target_org, source_org=a.source_org,
                   deep_verify=not a.no_deep_verify, drop_permission_grants=a.drop_permission_grants)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=TOOL_NAME, description="Safely synchronise Claude Desktop / Claude Code sessions "
                                "between Claude accounts on this Windows machine.",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=textwrap.dedent("""\
        typical use:
          claude-session-sync scan
          claude-session-sync sessions
          claude-session-sync preview --source A --target B --session "AI security"
          claude-session-sync sync    --source A --target B --session "AI security" --close-desktop --restart-desktop
          claude-session-sync verify
          claude-session-sync rollback
        Nothing is written by scan/accounts/sessions/preview/verify/report."""))
    p.add_argument("--version", action="version", version=f"{TOOL_NAME} {TOOL_VERSION}")
    p.add_argument("--data-root", help="Claude Desktop data folder (contains claude-code-sessions). Default: auto-detected")
    p.add_argument("--claude-home", help="Claude Code folder (contains projects/). Default: ~/.claude")
    p.add_argument("--state-dir", help="where backups/ledger/logs go. Default: ~/.claude-session-sync")
    p.add_argument("--json", action="store_true", help="machine-readable output where supported")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    s = sub.add_parser("scan", help="detect installation, schema, accounts, sessions (read-only)")
    s.add_argument("--no-probe", action="store_true", help="skip scanning the installed app bundle")

    sub.add_parser("accounts", help="list accounts / organisations (read-only)")

    s = sub.add_parser("sessions", help="list sessions with health status (read-only)")
    s.add_argument("--account", "-a", help="only this account")
    s.add_argument("--project", "-p", help="only this project folder")
    s.add_argument("--status", help="only this status (e.g. healthy, stale, conflict, missing, deleted, review)")
    s.add_argument("--search", "-q", help="text in title/path/id")
    s.add_argument("--deep", action="store_true", help="full transcript scan for every session (slower)")
    s.add_argument("--details", action="store_true", help="show reasons and full ids")

    s = sub.add_parser("preview", help="dry run: show exactly what sync would do (read-only)")
    add_selectors(s)
    s.add_argument("--verbose", "-v", action="store_true", help="show every field value")

    s = sub.add_parser("sync", help="create/update target records (backup, atomic write, verify, auto-rollback)")
    add_selectors(s)
    s.add_argument("--verbose", "-v", action="store_true")
    s.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    s.add_argument("--close-desktop", action="store_true", help="gracefully close Claude Desktop first")
    s.add_argument("--restart-desktop", action="store_true", help="start Claude Desktop again afterwards")
    s.add_argument("--force-close", action="store_true", help="terminate Desktop if it will not close / sessions are busy")
    s.add_argument("--unsafe-allow-running", action="store_true",
                   help="allow writing while Desktop runs, only if the target account is not the signed-in one")
    s.add_argument("--backup-transcripts", action="store_true", help="also copy the transcripts into the backup")

    s = sub.add_parser("verify", help="health check of the whole store, ledger and past operations (read-only)")
    s.add_argument("--deep", action="store_true", help="parse every transcript line")

    s = sub.add_parser("backup", help="full backup of the session store (metadata) [+ transcripts]")
    s.add_argument("--include-transcripts", action="store_true")
    s.add_argument("--note", default="")

    s = sub.add_parser("rollback", help="undo a sync operation from its backup")
    s.add_argument("--op", help="operation id/prefix (default: the last sync)")
    s.add_argument("--force", action="store_true", help="also overwrite files modified after the sync")
    s.add_argument("--unsafe-allow-running", action="store_true")

    s = sub.add_parser("restore", help="restore files from a standalone backup (never deletes)")
    s.add_argument("--backup", required=True)

    sub.add_parser("history", help="list operations and backups")

    s = sub.add_parser("report", help="export a diagnostic report (no conversation content, no credentials)")
    s.add_argument("--out")
    s.add_argument("--redact", action="store_true", help="hash titles, paths and e-mails")

    s = sub.add_parser("label", help="give an account a friendly name")
    s.add_argument("account")
    s.add_argument("name")
    s.add_argument("--email")

    s = sub.add_parser("desktop", help="Claude Desktop process control")
    s.add_argument("action", choices=["status", "close", "start"])
    s.add_argument("--force-close", action="store_true")

    s = sub.add_parser("auto", help="automatic mirroring of Code-tab sessions between accounts",
                       description="Keeps your accounts' Code tabs in step automatically. Files are only written while "
                                   "Claude Desktop is closed, so the watcher syncs right after you quit Desktop.")
    s.add_argument("action", choices=["status", "config", "run", "enable", "disable", "start", "stop"],
                   help="status | config (change settings) | run (one pass now) | enable/disable (start with Windows + "
                        "background watcher) | start/stop (watcher process only)")
    s.add_argument("--accounts", nargs="+", metavar="ACCOUNT", help="config: mirror only among these accounts (both ways)")
    s.add_argument("--all-accounts", action="store_true", help="config: mirror among every account that has an organisation")
    s.add_argument("--one-way", nargs=2, metavar=("FROM", "TO"), help="config: only copy FROM -> TO")
    s.add_argument("--max-age-days", type=int, help="config: only sessions active in the last N days (0 = all)")
    s.add_argument("--include-archived", action=argparse.BooleanOptionalAction, default=None)
    s.add_argument("--keep-backups", type=int, help="config: keep the newest N automatic backups (0 = keep all)")
    s.add_argument("--dry-run", action="store_true", help="run: show what would happen, change nothing")
    s.add_argument("--force", action="store_true", help="stop: terminate the watcher if it does not stop by itself")
    s.add_argument("--yes", "-y", action="store_true", help="enable: do not ask for confirmation")

    s = sub.add_parser("watch", help="foreground watcher: mirror sessions whenever Claude Desktop has just been closed")
    s.add_argument("--quiet", action="store_true", help="log to the log file only (used by the start-with-Windows entry)")

    sub.add_parser("launch", help="sync (if Desktop is closed), then start Claude Desktop")

    s = sub.add_parser("gui", help="open the graphical interface")
    s.add_argument("--no-browser", action="store_true", help="only print the URL")
    s.add_argument("--port", type=int, default=0)
    return p


# ---- rendering ---------------------------------------------------------------------------------------------------------

def render_overview(ov: dict):
    i = ov["installation"]
    print(c("Claude Desktop installation", "1"))
    print(f"  type           : {i['kind'].upper()}" + (f"  (package {i['package_family_name']})" if i.get("package_family_name") else ""))
    print(f"  version        : {i.get('version') or 'unknown'}")
    print(f"  data root      : {i.get('data_root') or '-'}   [{i.get('data_root_source')}]")
    print(f"  session store  : {i.get('sessions_root') or '-'}")
    print(f"  transcripts    : {i.get('projects_root')}  ({ov['transcripts']} conversations found)")
    for n in i.get("notes", []):
        print(f"  note           : {n}")
    pr = ov.get("app_probe")
    if pr:
        print(f"  schema probe   : {pr['status'].upper()} - {pr['detail']}")
    d = ov["desktop"]
    print(f"  Claude Desktop : {'RUNNING (%d processes)' % d['process_count'] if d['running'] else 'not running'}"
          + ("  [this tool runs inside it]" if d["inside_desktop"] else ""))
    if d["live_sessions"]:
        print("  live sessions  : " + ", ".join(f"{short(l['cli_id'])}({l['status']})" for l in d["live_sessions"]))
    print()
    print(c("Accounts", "1"))
    for a in ov["accounts"]:
        act = c(" [signed in]", "32") if a["is_active"] else ""
        print(f"  {a['id']}  {a['name']}{act}")
        for o in a["orgs"]:
            print(f"      org {o['id']}  {o['records']} sessions, {o['tombstones']} tombstones"
                  f"  ({o['evidence'] or 'weak evidence: no records / no pairing'})")
    for w in ov["warnings"]:
        print("  warning:", w)


def render_plan(plan: planner.Plan, store, verbose=False):
    src, tgt = store.accounts[plan.source_account], store.accounts[plan.target_account]
    print(c(f"PLAN {plan.id}: {src.name()} ({src.id[:8]})  ->  {tgt.name()} ({tgt.id[:8]}), org {(plan.target_org or '?')[:8]}", "1"))
    for b in plan.blockers:
        print(c("  BLOCKED: " + b, "31"))
    for w in plan.warnings:
        print(c("  warning: " + w, "33"))
    for it in plan.items:
        s = it.source
        tag = {"create": "NEW", "update": "UPDATE", "noop": "SYNCED", "blocked": "SKIP", "conflict": "CONFLICT"}[it.action]
        print()
        print(f"  {cs(it.label or tag):<{20 if not _COLOR else 30}} {s.title!r}   {s.cwd}")
        print(f"      source  {s.local_id}   cli {s.cli_id}")
        tp = it.assessment.transcript_path
        if tp:
            t = it.assessment.transcript or {}
            print(f"      shared transcript (NOT copied, NOT modified): {tp}  [{human_size(t.get('size'))}]")
        if it.target_path:
            print(f"      target  {it.target_local_id}   {it.target_path}")
        for r in it.reasons:
            print(c(f"      ! {r}", "31" if it.action == CONFLICT_ else "33"))
        for w in it.warnings:
            print(c(f"      ~ {w}", "33"))
        for i_ in it.infos:
            print(f"      i {i_}")
        if it.action in (CREATE, UPDATE):
            if it.action == CREATE:
                print(f"      copies {len(it.changes)} conversation-identity fields "
                      f"(cliSessionId, cwd, title, timestamps, model, permissions, turn counters, lineage ...)")
            else:
                for ch in it.changes:
                    print(f"      field {ch.field}: {planner._brief(ch.old)} -> {planner._brief(ch.new)}   ({ch.note})")
            for k, v in it.dropped.items():
                print(f"      NOT copied ({k.replace('_', ' ')}): {', '.join(v)}")
            if verbose and it.action == CREATE:
                for ch in it.changes:
                    print(f"        {ch.field} = {planner._brief(ch.new, 140)}")
            for op in it.ops:
                print(f"      FILE {op.kind.upper():7s} {op.path}" + (f"  ({len(op.new_bytes)} bytes)" if op.new_bytes else ""))
    print()
    cnt = plan.counts()
    print("  summary: " + ", ".join(f"{v} {k}" for k, v in cnt.items()))
    print("  never touched: conversation transcripts (~/.claude/projects), the source records, your project folders")


def confirm(msg: str) -> bool:
    try:
        return input(msg + " Type 'yes' to continue: ").strip().lower() == "yes"
    except (EOFError, KeyboardInterrupt):
        return False


# ---- commands ------------------------------------------------------------------------------------------------------------

def cmd_scan(a, ctx):
    store = service.load_store(ctx)
    ov = service.overview(ctx, store, with_probe=not a.no_probe)
    rows = service.session_rows(store)
    if a.json:
        return emit_json({"overview": ov, "sessions": rows})
    render_overview(ov)
    counts: dict = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print()
    print(c("Sessions", "1") + f"  {len(rows)} records: " + ", ".join(f"{v} {cs(k)}" for k, v in counts.items()))
    orph = analysis.find_orphans(store)
    for k, v in orph.items():
        if v:
            print(f"  {k.replace('_', ' ')}: {len(v)}")
    sh = analysis.shared_conversations(store)
    if sh:
        print(f"  conversations already visible in more than one account: {len(sh)}")
    print("\n(read-only: nothing was changed)")


def cmd_accounts(a, ctx):
    store = service.load_store(ctx)
    ov = service.overview(ctx, store, with_probe=False)
    if a.json:
        return emit_json(ov["accounts"])
    rows = []
    for x in ov["accounts"]:
        for o in x["orgs"] or [{"id": "-", "records": 0, "tombstones": 0, "evidence": None}]:
            rows.append([x["id"], x["name"], "yes" if x["is_active"] else "", o["id"], str(o["records"]),
                         str(o["tombstones"]), o["evidence"] or "weak"])
    print(table(rows, ["ACCOUNT", "NAME", "SIGNED IN", "ORGANISATION", "SESSIONS", "TOMBSTONES", "PAIRING EVIDENCE"]))
    print("\nNames come from --label / the CLI login (~/.claude.json). Set one: claude-session-sync label <account> <name>")


def cmd_sessions(a, ctx):
    store = service.load_store(ctx)
    asses = analysis.assess_all(store, deep=a.deep)
    rows = service.session_rows(store, asses)
    if a.account:
        acct = store.find_account(a.account)
        rows = [r for r in rows if r["account_id"] == acct.id]
    if a.project:
        rows = [r for r in rows if planner.norm_path(r["cwd"]) == planner.norm_path(a.project)]
    if a.status:
        rows = [r for r in rows if a.status.lower() in r["status"].lower()]
    if a.search:
        q = a.search.lower()
        rows = [r for r in rows if q in (r["title"] or "").lower() or q in (r["cwd"] or "").lower()
                or q in r["local_id"].lower() or q in (r["cli_id"] or "").lower()]
    if a.json:
        return emit_json(rows)
    out = []
    for r in rows:
        acct = store.accounts[r["account_id"]]
        also = ",".join(store.accounts[x].name()[:8] for x in r["also_in_accounts"] if x in store.accounts)
        out.append([acct.name()[:14], short(r["local_id"], 15), short(r["cli_id"] or "-", 8), trunc(r["title"], 34),
                    trunc(os.path.basename((r["cwd"] or "").rstrip("\\/")) or r["cwd"], 20), r["last_activity_text"],
                    cs(r["status"]) + (c(" +" + also, "36") if also else "") + (c(" LIVE", "32") if r["live"] else "")])
    print(table(out, ["ACCOUNT", "LOCAL ID", "CLI ID", "TITLE", "PROJECT", "LAST ACTIVITY", "STATUS"]))
    if a.details:
        for r in rows:
            if r["reasons"]:
                print(f"\n{r['local_id']}  {r['title']}")
                for x in r["reasons"]:
                    print("   -", x)
    print(f"\n{len(rows)} session(s). '+xxxx' = the same conversation is already visible in that account.")


def _plan(a, ctx, store):
    return service.make_plan(ctx, store, a.source, a.target, a.session, a.project, a.all, opts_from(a))


def cmd_preview(a, ctx):
    store = service.load_store(ctx)
    plan = _plan(a, ctx, store)
    if a.json:
        return emit_json(plan.to_dict())
    render_plan(plan, store, a.verbose)
    print("\n(dry run: nothing was written)")
    return 2 if plan.blockers else 0


def cmd_sync(a, ctx):
    store = service.load_store(ctx)
    plan = _plan(a, ctx, store)
    render_plan(plan, store, a.verbose)
    if plan.blockers:
        return 2
    runnable = plan.runnable
    if not runnable:
        print("\nNothing to do.")
        executor.apply_plan(ctx, plan, store)          # refreshes ledger baselines for already-synced items
        return 0
    # explain the Desktop requirement up-front
    running = desktop.is_running(ctx.inst) if ctx.is_live() else False
    if running:
        print(c("\nClaude Desktop is running. It must be closed while its session store is modified.", "33"))
        if not a.close_desktop:
            print("Re-run with --close-desktop (and --restart-desktop), or quit Desktop yourself first.")
            return 2
    if not a.yes and not confirm(f"\nApply {len(runnable)} change(s) to the Claude session store"
                                 + (" (Claude Desktop will be closed first)?" if running else "?")):
        print("Aborted; nothing changed.")
        return 1
    was_running = running
    if running:
        res = desktop.close_desktop(ctx.inst, store, force=a.force_close, log=ctx.log)
        if not res.get("closed"):
            raise ToolError(res.get("note") or "could not close Claude Desktop")
        ctx.log("Claude Desktop closed. Re-reading the store ...")
        store = service.load_store(ctx)
        plan2 = _plan(a, ctx, store)
        if service.plan_fingerprint(plan2) != service.plan_fingerprint(plan):
            print(c("The plan changed while Claude Desktop was shutting down (it flushed session data). "
                    "New plan:", "33"))
            render_plan(plan2, store, a.verbose)
            if not a.yes and not confirm("Apply the UPDATED plan?"):
                if a.restart_desktop:
                    desktop.start_desktop(ctx.inst)
                print("Aborted; nothing changed.")
                return 1
        plan = plan2
    try:
        result = executor.apply_plan(ctx, plan, store, allow_running=a.unsafe_allow_running,
                                     include_transcript_backup=a.backup_transcripts)
    finally:
        if was_running and a.restart_desktop:
            ctx.log("Restarting Claude Desktop ...")
            desktop.start_desktop(ctx.inst)
    ctx.state.learn_identity(store)
    if a.json:
        return emit_json(result)
    ok = [x for x in result.get("verification", []) if x["ok"]]
    print(c(f"\nDONE  operation {result['op_id']}: {len(ok)}/{len(result['verification'])} verification checks passed.", "32"))
    print(f"  backup / rollback data: {result['backup_dir']}")
    print(f"  undo with: {TOOL_NAME} rollback --op {result['op_id']}")
    if not (was_running and a.restart_desktop):
        print("  Start Claude Desktop (signed in as the target account) - the session appears in its Code tab.")
    return 0


def cmd_verify(a, ctx):
    store = service.load_store(ctx)
    rep = executor.verify_store(ctx, store, deep=a.deep)
    if a.json:
        return emit_json(rep)
    print(c("Session records: ", "1") + ", ".join(f"{v} {cs(k)}" for k, v in rep["counts"].items()))
    for path, r in rep["assessments"].items():
        if r["status"] != analysis.HEALTHY:
            print(f"  {cs(r['status'])}  {os.path.basename(path)}")
            for x in r["reasons"]:
                print(f"       - {x}")
    for k, v in rep["orphans"].items():
        if v:
            print(f"{k.replace('_', ' ')}: {len(v)}")
            for x in v[:10]:
                print("   ", x)
    for x in rep["ledger_issues"] + rep["problems"]:
        print("  !", x)
    if rep["shared"]:
        print(f"\nConversations visible in several accounts: {len(rep['shared'])}")
        for cid, locs in rep["shared"].items():
            print(f"   {cid[:8]}: {', '.join(locs)}")
    if rep["op_checks"]:
        print("\nFiles written by earlier syncs:")
        for x in rep["op_checks"]:
            print(f"   {x['op_id']}  {x['file']}: {x['state']}")
    bad = rep["counts"].get(analysis.MISSING, 0) + rep["counts"].get(analysis.CONFLICT, 0)
    return 1 if bad else 0


def cmd_backup(a, ctx):
    store = service.load_store(ctx)
    r = executor.standalone_backup(ctx, store, a.include_transcripts, a.note)
    if a.json:
        return emit_json(r)
    print(f"Backup {r['op_id']}: {r['files']} session-store file(s)"
          + (f", {r['transcript_files']} transcript file(s)" if r["transcript_files"] else "")
          + f", {human_size(r['bytes'])}\n  -> {r['dir']}")
    print("Restore with: " + f"{TOOL_NAME} restore --backup {r['op_id']}")


def cmd_rollback(a, ctx):
    store = service.load_store(ctx)
    r = executor.rollback(ctx, a.op, store, force=a.force, allow_running=a.unsafe_allow_running)
    if a.json:
        return emit_json(r)
    for x in r["restored"]:
        print("  ", x)
    for x in r["problems"]:
        print(c("  ! " + x, "31"))
    print(("Rolled back " if r["ok"] else "INCOMPLETE rollback of ") + r["op_id"] +
          f"  (safety backup of the pre-rollback state: {r['rollback_backup']})")
    return 0 if r["ok"] else 1


def cmd_restore(a, ctx):
    store = service.load_store(ctx)
    r = executor.restore_backup(ctx, a.backup, store)
    print(f"restored {len(r['restored'])} file(s), {r['unchanged']} already identical. Safety backup: {r['safety_backup']}")
    print(r["note"])


def cmd_history(a, ctx):
    ops = ctx.state.list_ops()
    if a.json:
        return emit_json([{k: v for k, v in m.items() if k not in ("pre_tree", "post_tree", "items", "verification", "pre_transcripts")} for m in ops])
    rows = [[m["op_id"], m.get("kind", "?"), m.get("status", "?"), m.get("created", ""), str(len(m.get("changes", []))),
             m.get("note", "")] for m in ops]
    print(table(rows, ["OPERATION", "KIND", "STATUS", "CREATED", "CHANGES", "NOTE"]) if rows else "no operations yet")


def cmd_report(a, ctx):
    store = service.load_store(ctx)
    rep = service.diagnostic_report(ctx, store, a.redact)
    path = service.write_report(ctx, rep, a.out)
    print("Diagnostic report written to", path)
    print("It contains ids, titles, folder paths and hashes -- no conversation content or credentials."
          + ("" if a.redact else " Use --redact before sharing it publicly."))


def cmd_label(a, ctx):
    store = service.load_store(ctx)
    acct = store.find_account(a.account)
    ctx.state.set_label(acct.id, a.name, a.email)
    print(f"Account {acct.id} is now called '{a.name}'.")


def cmd_desktop(a, ctx):
    store = service.load_store(ctx)
    if a.action == "status":
        return emit_json(service.desktop_status(ctx, store)) if a.json else print(json.dumps(service.desktop_status(ctx, store), indent=1))
    if a.action == "close":
        print(json.dumps(desktop.close_desktop(ctx.inst, store, force=a.force_close), indent=1))
    else:
        print("started" if desktop.start_desktop(ctx.inst) else "could not start Claude Desktop")


def cmd_gui(a, ctx):
    from .ui_server import serve
    serve(ctx, open_browser=not a.no_browser, port=a.port)


def render_run(res: dict):
    if res["status"] == "skipped":
        print(c("Skipped: ", "33") + (res.get("reason") or ""))
        return
    for r in res["routes"]:
        bits = []
        if r["created"]:
            bits.append(c(f"{len(r['created'])} new", "32") + " (" + ", ".join(trunc(x["title"], 34) for x in r["created"]) + ")")
        if r["updated"]:
            bits.append(c(f"{len(r['updated'])} updated", "34") + " (" + ", ".join(trunc(x["title"], 34) for x in r["updated"]) + ")")
        if r["synced"]:
            bits.append(f"{r['synced']} already in step")
        if r["skipped"]:
            bits.append(c(f"{len(r['skipped'])} skipped", "33"))
        if r.get("blocked"):
            bits.append(c("not possible: " + "; ".join(r["blocked"]), "33"))
        if r.get("error"):
            bits.append(c("ERROR: " + r["error"], "31"))
        print(f"  {r['from_name']} -> {r['to_name']}: " + (", ".join(bits) or "nothing to do"))
        for x in r["skipped"]:
            print(f"      skipped {trunc(x['title'], 40)!r}: {x['why']}" + (f" - {trunc(x['detail'], 90)}" if x.get("detail") else ""))
    for n in res.get("notes", []):
        print("  note:", n)
    tail = "dry run - nothing written" if res.get("dry_run") else f"{res['created']} new, {res['updated']} updated"
    print(c(f"{res['status'].upper()}", "32" if res["status"] == "ok" else "31") + f"  ({tail}, {res.get('seconds', 0)}s)")


def cmd_auto(a, ctx):
    act = a.action
    if act == "config":
        upd: dict = {}
        store = service.load_store(ctx)
        if a.one_way:
            upd["routes"] = [{"from": store.find_account(a.one_way[0]).id, "to": store.find_account(a.one_way[1]).id}]
            upd["accounts"] = []
        if a.accounts:
            upd["accounts"] = [store.find_account(x).id for x in a.accounts]
            upd["routes"] = []
        if a.all_accounts:
            upd["accounts"], upd["routes"] = [], []
        if a.max_age_days is not None:
            upd["max_age_days"] = a.max_age_days
        if a.include_archived is not None:
            upd["include_archived"] = a.include_archived
        if a.keep_backups is not None:
            upd["keep_backups"] = a.keep_backups
        if upd:
            ctx.state.set_auto_config(upd)
        act = "status"
    if act == "status":
        ov = autosync.auto_overview(ctx)
        if a.json:
            return emit_json(ov)
        w = ov["watcher"]
        print(c("Automatic sync", "1"))
        if w["running"]:
            watcher_text = c("RUNNING", "32") + f" (pid {w['pid']}, {w['state']})"
        else:
            watcher_text = c("not running", "33")
        print(f"  background watcher : {watcher_text}")
        print(f"  starts with Windows: {c('yes', '32') if ov['autostart']['enabled'] else 'no'}")
        print(f"  Claude Desktop     : {'running (sync waits until you close it)' if ov['desktop_running'] else 'closed'}")
        print("  routes             : " + (", ".join(f"{r['from_name']} -> {r['to_name']}" for r in ov["routes"]) or "none"))
        cfg = ov["config"]
        print(f"  settings           : last {cfg['max_age_days'] or 'all'} days, archived {'included' if cfg['include_archived'] else 'excluded'}, "
              f"keep {cfg['keep_backups'] or 'all'} automatic backups")
        for n in ov["notes"]:
            print("  note:", n)
        lr = w.get("last_run")
        if lr:
            print(f"  last run           : {lr.get('finished') or lr.get('started')}  {lr.get('status')}  "
                  f"{lr.get('created', 0)} new, {lr.get('updated', 0)} updated" + (f"  ({lr['reason']})" if lr.get("reason") else ""))
        for e in ov["events"][-5:]:
            print(f"  event {e['ts']}  {e['event']}  " + ", ".join(f"{k}={v}" for k, v in e.items() if k in ("created", "updated", "errors", "removed") and v))
        if ov["log"]:
            print("  watcher log (last lines):")
            for x in ov["log"][-6:]:
                print("     " + x)
        return 0
    if act == "run":
        res = autosync.run_once(ctx, log=ctx.log, origin="manual", dry_run=a.dry_run)
        if a.json:
            return emit_json(res)
        render_run(res)
        return 0 if res["status"] in ("ok", "skipped") else 1
    if act == "enable":
        if not a.yes:
            print("First pass would do this (dry run, nothing written):")
            render_run(autosync.run_once(ctx, log=ctx.log, origin="manual", dry_run=True))
            if not confirm("\nTurn on automatic sync (starts with Windows, runs after every Claude Desktop close)?"):
                print("Nothing changed.")
                return 1
        r = autosync.autostart_set(ctx, True)
        print("Will start with Windows:", r["command"])
        w = autosync.start_watcher(ctx)
        print("Watcher already running." if w.get("already") else f"Watcher started (pid {w['pid']}).")
        print("From now on, sessions are mirrored between your accounts every time you quit Claude Desktop.")
        return 0
    if act == "disable":
        autosync.autostart_set(ctx, False)
        r = autosync.stop_watcher(ctx, force=a.force)
        print("Start-with-Windows removed. " + ("Watcher stopped." if r["stopped"] else r.get("note", "")))
        return 0 if r["stopped"] else 1
    if act == "start":
        w = autosync.start_watcher(ctx)
        print("Watcher already running." if w.get("already") else f"Watcher started (pid {w['pid']}).")
        return 0
    if act == "stop":
        r = autosync.stop_watcher(ctx, force=a.force)
        print("Watcher stopped." if r["stopped"] else r["note"])
        return 0 if r["stopped"] else 1


def cmd_watch(a, ctx):
    log = autosync.make_file_log(ctx.state, echo=not a.quiet)
    ctx.log = log
    autosync.watch(ctx, log=log)


def cmd_launch(a, ctx):
    if desktop.is_running(ctx.inst):
        print("Claude Desktop is already running - nothing to do. (With the watcher on, sessions are mirrored when you quit it.)")
        return 0
    res = autosync.run_once(ctx, log=ctx.log, origin="manual")
    render_run(res)
    if desktop.start_desktop(ctx.inst):
        print("Claude Desktop started.")
        return 0
    print(c("Could not start Claude Desktop automatically; open it from the Start menu.", "31"))
    return 1


COMMANDS = {"scan": cmd_scan, "accounts": cmd_accounts, "sessions": cmd_sessions, "preview": cmd_preview,
            "sync": cmd_sync, "verify": cmd_verify, "backup": cmd_backup, "rollback": cmd_rollback,
            "restore": cmd_restore, "history": cmd_history, "report": cmd_report, "label": cmd_label,
            "desktop": cmd_desktop, "gui": cmd_gui, "auto": cmd_auto, "watch": cmd_watch, "launch": cmd_launch}


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    p = build_parser()
    a = p.parse_args(argv)
    if not a.cmd:
        a = p.parse_args(["gui"] if len(sys.argv) == 1 and getattr(sys, "frozen", False) else ["--help"])
    try:
        ctx = service.make_ctx(a.data_root, a.claude_home, a.state_dir)
        rc = COMMANDS[a.cmd](a, ctx)
        return int(rc or 0)
    except ToolError as e:
        print(c("error: ", "31") + str(e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
