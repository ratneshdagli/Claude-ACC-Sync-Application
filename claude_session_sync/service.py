"""High-level operations shared by the CLI and the GUI (so both behave identically)."""
from __future__ import annotations

import json
import os
import platform
import hashlib
import time

from . import analysis, desktop, environment, executor, planner, schema
from .common import TOOL_NAME, TOOL_VERSION, ToolError, fmt_ms, iso_now, norm_path, stamp
from .executor import Ctx
from .planner import Options, Plan
from .state import State
from .store import Store, scan

_REAL_INST: environment.Installation | None = None


def real_installation() -> environment.Installation:
    global _REAL_INST
    if _REAL_INST is None:
        _REAL_INST = environment.detect()
    return _REAL_INST


def make_ctx(data_root: str | None = None, claude_home: str | None = None, state_dir: str | None = None,
             log=print) -> Ctx:
    data_root = data_root or os.environ.get("CLAUDE_SESSION_SYNC_DATA_ROOT")
    claude_home = claude_home or os.environ.get("CLAUDE_SESSION_SYNC_CLAUDE_HOME")
    norm = lambda p: os.path.normpath(os.path.abspath(p)) if p else p  # noqa: E731
    data_root, claude_home, state_dir = norm(data_root), norm(claude_home), norm(state_dir)
    real = real_installation() if not (data_root or claude_home) else environment.detect()
    inst = real if not (data_root or claude_home) else environment.detect(data_root=data_root, claude_home=claude_home)
    # A sandbox override must never inherit the real install's exe/aliasing conclusions about ITS data root.
    if data_root:
        inst.sessions_root = os.path.join(data_root, "claude-code-sessions")
    return Ctx(inst=inst, state=State(state_dir), real=real, log=log)


def load_store(ctx: Ctx) -> Store:
    try:
        labels = ctx.state.labels()
    except ToolError:
        labels = {}
    return scan(ctx.inst, labels)


def desktop_status(ctx: Ctx, store: Store) -> dict:
    procs = desktop.desktop_processes(ctx.inst)
    return {"running": bool(procs), "process_count": len(procs),
            "busy_sessions": [{"cli_id": b.cli_id, "pid": b.pid} for b in desktop.busy_sessions(store)],
            "live_sessions": [{"cli_id": l.cli_id, "pid": l.pid, "status": l.status, "cwd": l.cwd} for l in store.live.values()],
            "inside_desktop": desktop.running_inside_desktop(ctx.inst),
            "is_live_store": ctx.is_live()}


def overview(ctx: Ctx, store: Store, with_probe: bool = True) -> dict:
    inst = ctx.inst
    probe = None
    if with_probe:
        probe = schema.probe_app(inst.install_location, inst.version, ctx.state.cache).__dict__
    accounts = []
    for a in store.accounts.values():
        accounts.append({
            "id": a.id, "label": a.label, "email": a.email, "display_name": a.display_name, "name": a.name(),
            "is_active": a.is_active,
            "orgs": [{"id": o.org_id, "records": len(o.records), "tombstones": len(o.tombstones),
                      "evidence": a.paired_orgs.get(o.org_id), "unreadable": len(o.unreadable)} for o in a.orgs.values()],
            "paired_orgs": a.paired_orgs,
            "sessions": sum(len(o.records) for o in a.orgs.values()),
        })
    return {"tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
            "installation": inst.to_dict(), "app_probe": probe, "accounts": accounts,
            "active_account": store.active_account, "desktop": desktop_status(ctx, store),
            "transcripts": len(store.transcripts), "warnings": store.warnings, "unknown_dirs": store.unknown_dirs,
            "state_dir": ctx.state.root}


def session_rows(store: Store, assessments: dict[str, analysis.Assessment] | None = None) -> list[dict]:
    assessments = assessments or analysis.assess_all(store)
    shared = analysis.shared_conversations(store)
    rows = []
    for r in store.all_records():
        a = assessments[r.path]
        other = []
        if r.cli_id in shared:
            other = sorted({x.account_id for x in shared[r.cli_id] if x.account_id != r.account_id})
        rows.append({
            "account_id": r.account_id, "org_id": r.org_id, "local_id": r.local_id, "cli_id": r.cli_id,
            "title": r.title, "cwd": r.cwd, "last_activity": r.last_activity, "last_activity_text": fmt_ms(r.last_activity),
            "created": r.created, "archived": r.archived, "status": a.status, "live": a.live,
            "reasons": a.reasons(("error", "warn")), "infos": a.reasons(("info",)),
            "transcript": a.transcript_path, "transcript_size": (a.transcript or {}).get("size"),
            "also_in_accounts": other, "record_path": r.path, "completed_turns": r.g("completedTurns"),
            "model": r.g("model"), "prior_cli_ids": r.prior_ids, "record_size": r.size,
            "error": r.g("error"),
        })
    rows.sort(key=lambda x: -x["last_activity"])
    return rows


def make_plan(ctx: Ctx, store: Store, source: str, target: str, refs=None, projects=None, select_all=False,
              opts: Options | None = None) -> Plan:
    opts = opts or Options()
    src = store.find_account(source)
    tgt = store.find_account(target)
    if src.id == tgt.id:
        raise ToolError('Source and target are the same account.')
    recs = planner.select_sessions(store, src.id, refs, projects, select_all, opts.include_archived, opts.source_org)
    if not recs:
        raise ToolError("Nothing selected. Use --session, --project or --all.")
    return planner.build_plan(store, ctx.state, src.id, tgt.id, recs, opts)


def plan_fingerprint(plan: Plan) -> str:
    h = hashlib.sha256()
    for it in plan.items:
        h.update(repr((it.source.path, it.source.sha256, it.action, it.target_path,
                       [(o.kind, o.path, o.post_sha256) for o in it.ops])).encode())
    return h.hexdigest()


def diagnostic_report(ctx: Ctx, store: Store, redact: bool = False) -> dict:
    """Everything needed to diagnose a broken/changed storage format -- never message content or credentials."""
    ov = overview(ctx, store)
    asses = analysis.assess_all(store, deep=False)

    def red(s):
        if not redact or s is None:
            return s
        return "h:" + hashlib.sha256(str(s).encode()).hexdigest()[:10]

    key_union: dict[str, int] = {}
    for r in store.all_records():
        for k in (r.raw or {}):
            key_union[k] = key_union.get(k, 0) + 1
    classes = schema.classify_keys(key_union.keys())
    sessions = []
    for r in store.all_records():
        a = asses[r.path]
        sessions.append({
            "account": r.account_id, "org": r.org_id, "local_id": r.local_id, "cli_id": r.cli_id,
            "title": red(r.title), "cwd": red(r.cwd), "created": r.created, "last_activity": r.last_activity,
            "archived": r.archived, "status": a.status, "flags": [f.to_dict() for f in a.flags],
            "record_bytes": r.size, "record_sha256": r.sha256, "field_names": sorted((r.raw or {}).keys()),
            "prior_cli_ids": r.prior_ids, "transcript": red(a.transcript_path),
            "transcript_bytes": (a.transcript or {}).get("size"),
        })
    tombs = [{"account": a.id, "org": o.org_id, "stem": s, "value": v}
             for a in store.accounts.values() for o in a.orgs.values() for s, v in o.tombstones.items()]
    if redact:
        ov["installation"]["claude_home"] = red(ov["installation"].get("claude_home"))
        for a in ov["accounts"]:
            a["email"], a["label"], a["display_name"], a["name"] = (red(a[k]) for k in ("email", "label", "display_name", "name"))
    return {
        "generated": iso_now(), "redacted": redact, "tool": ov["tool"],
        "platform": {"system": platform.platform(), "python": platform.python_version()},
        "overview": ov, "field_key_counts": key_union, "field_classification": classes,
        "sessions": sessions, "tombstones": tombs, "orphans": {k: [red(x) for x in v] for k, v in analysis.find_orphans(store).items()},
        "shared_conversations": {c: [f"{r.local_id}@{r.account_id[:8]}" for r in rs] for c, rs in analysis.shared_conversations(store).items()},
        "ledger_entries": len(ctx.state.ledger().get("entries", {})),
        "operations": [{k: v for k, v in m.items() if k in ("op_id", "kind", "status", "created", "note", "source_account", "target_account")}
                       for m in ctx.state.list_ops()],
        "recent_log": ctx.state.read_log(50),
        "notes": "Contains ids, titles, folder paths and file hashes only. No conversation content, tokens or credentials are read.",
    }


def write_report(ctx: Ctx, report: dict, out: str | None = None) -> str:
    ctx.state.ensure()
    out = out or os.path.join(ctx.state.reports, f"diagnostic-{stamp()}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, ensure_ascii=False)
    return out
