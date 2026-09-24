"""Backup, apply, verify and rollback.  Every write in the tool goes through this module."""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Callable

from . import analysis, desktop, environment
from .common import (TOOL_NAME, TOOL_VERSION, ToolError, atomic_write, iso_now, norm_path, read_json, sha256_file,
                     stamp, walk_files, dumps_record, human_size)
from .planner import CREATE, NOOP, UPDATE, FileOp, Plan, PlanItem
from .state import State
from .store import Store, inspect_transcript, scan


@dataclass
class Ctx:
    inst: environment.Installation
    state: State
    real: environment.Installation | None = None      # the genuinely installed Claude (for the live-store gate)
    log: Callable[[str], None] = print

    def is_live(self) -> bool:
        try:
            return environment.is_live_store(self.inst, self.real)
        except Exception:
            return True


# ---- snapshots ---------------------------------------------------------------------------------------

def tree_snapshot(root: str | None, hashes: bool = True) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not root or not os.path.isdir(root):
        return out
    for p in walk_files(root):
        rel = os.path.relpath(p, root).replace("\\", "/")
        try:
            st = os.stat(p)
            out[rel] = {"size": st.st_size, "mtime_ms": int(st.st_mtime * 1000),
                        "sha256": sha256_file(p) if hashes else None}
        except OSError:
            out[rel] = {"size": None, "mtime_ms": None, "sha256": None}
    return out


def _rel(root: str, p: str) -> str:
    return os.path.relpath(p, root).replace("\\", "/")


# ---- backups ---------------------------------------------------------------------------------------------

def new_op_id(kind: str) -> str:
    return f"{stamp()}-{kind}-{os.urandom(2).hex()}"


def create_backup(ctx: Ctx, op_id: str, kind: str, *, plan: Plan | None = None, store: Store | None = None,
                  include_transcripts: bool = False, extra_projects_files: list[str] | None = None,
                  note: str = "") -> dict:
    """Full copy of the claude-code-sessions tree (small) + optional transcripts + files outside it that will be removed.

    Returns the (unsaved-yet) manifest dict.  Nothing in the Claude data is modified.
    """
    inst = ctx.inst
    bdir = os.path.join(ctx.state.backups, op_id)
    os.makedirs(bdir, exist_ok=False)
    pre_sessions = os.path.join(bdir, "pre", "claude-code-sessions")
    os.makedirs(pre_sessions, exist_ok=True)
    tree = tree_snapshot(inst.sessions_root)
    for rel in tree:
        src = os.path.join(inst.sessions_root, rel)
        dst = os.path.join(pre_sessions, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        if sha256_file(dst) != tree[rel]["sha256"]:
            raise ToolError(f"backup verification failed for {rel} (source changed while copying?)")
    # empty directories matter too (account/org folders prove pairing)
    dirs = []
    if inst.sessions_root and os.path.isdir(inst.sessions_root):
        for r, ds, _fs in os.walk(inst.sessions_root):
            for d in ds:
                rel = _rel(inst.sessions_root, os.path.join(r, d))
                dirs.append(rel)
                os.makedirs(os.path.join(pre_sessions, rel), exist_ok=True)

    outside: dict[str, dict] = {}
    to_save = list(extra_projects_files or [])
    if plan:
        for it in plan.items:
            for op in it.ops:
                if op.root == "projects" and os.path.exists(op.path):
                    to_save.append(op.path)
    for p in to_save:
        rel = _rel(inst.projects_root, p)
        dst = os.path.join(bdir, "pre", "projects", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(p, dst)
        outside[p] = {"rel": rel, "sha256": sha256_file(p)}

    transcripts: dict[str, dict] = {}
    if include_transcripts and store is not None:
        cli_ids = set()
        if plan:
            cli_ids = {i.source.cli_id for i in plan.items if i.source.cli_id}
        else:
            cli_ids = {r.cli_id for r in store.all_records() if r.cli_id}
        for cid in sorted(cli_ids):
            ref = store.transcripts.get(cid)
            if not ref:
                continue
            for jp in ref.jsonl:
                rel = _rel(inst.projects_root, jp)
                dst = os.path.join(bdir, "pre", "projects", rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(jp, dst)
                transcripts[jp] = {"rel": rel, "sha256": sha256_file(dst), "size": os.path.getsize(dst)}
            for sd in ref.sidecar_dirs:
                for fp in walk_files(sd):
                    rel = _rel(inst.projects_root, fp)
                    dst = os.path.join(bdir, "pre", "projects", rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(fp, dst)
                    transcripts[fp] = {"rel": rel, "sha256": sha256_file(dst), "size": os.path.getsize(dst)}

    manifest = {
        "op_id": op_id, "kind": kind, "created": iso_now(), "tool": TOOL_NAME, "tool_version": TOOL_VERSION,
        "note": note, "status": "prepared",
        "data_root": inst.data_root, "sessions_root": inst.sessions_root, "projects_root": inst.projects_root,
        "claude_version": inst.version, "install_kind": inst.kind,
        "pre_tree": tree, "pre_dirs": dirs, "pre_outside": outside, "pre_transcripts": transcripts,
        "changes": [], "created_dirs": [],
    }
    return manifest


def save_manifest(ctx: Ctx, manifest: dict) -> None:
    p = os.path.join(ctx.state.backups, manifest["op_id"], "manifest.json")
    atomic_write(p, json.dumps(manifest, indent=1, ensure_ascii=False).encode("utf-8"), overwrite=True)


def standalone_backup(ctx: Ctx, store: Store, include_transcripts: bool, note: str = "") -> dict:
    ctx.state.ensure()
    ctx.state.acquire_lock()
    try:
        op_id = new_op_id("backup")
        m = create_backup(ctx, op_id, "backup", store=store, include_transcripts=include_transcripts, note=note)
        m["status"] = "verified"
        save_manifest(ctx, m)
        ctx.state.log({"event": "backup", "op_id": op_id, "files": len(m["pre_tree"]),
                       "transcripts": len(m["pre_transcripts"])})
        size = sum(v["size"] or 0 for v in m["pre_tree"].values()) + sum(v["size"] for v in m["pre_transcripts"].values())
        return {"op_id": op_id, "dir": os.path.join(ctx.state.backups, op_id), "files": len(m["pre_tree"]),
                "transcript_files": len(m["pre_transcripts"]), "bytes": size}
    finally:
        ctx.state.release_lock()


# ---- gate ---------------------------------------------------------------------------------------------------

def gate_writes(ctx: Ctx, store: Store, target_account: str | None, allow_running: bool) -> list[str]:
    """Refuse writes into the live store while Claude Desktop is running.  Returns informational notes."""
    notes: list[str] = []
    if not ctx.is_live():
        notes.append("data root is a sandbox/copy, not the installed Claude Desktop's store: Desktop check skipped")
        return notes
    procs = desktop.desktop_processes(ctx.inst)
    if not procs:
        return notes
    if allow_running and target_account and store.active_account and target_account != store.active_account:
        notes.append("UNSAFE MODE: Claude Desktop is running, but the target account is not the signed-in one; "
                     "its folder is not loaded until the account is switched.")
        return notes
    raise ToolError(
        "Claude Desktop is running. It keeps session records in memory and rewrites them, so changing the store now "
        "could be lost or corrupt it.\n"
        "  -> Quit Claude Desktop (tray icon -> Quit) and run again, or let this tool do it: add --close-desktop "
        "(and --restart-desktop) when running from a normal terminal.")


# ---- apply -------------------------------------------------------------------------------------------------------

def _check_preconditions(plan: Plan, live_transcript_ok: bool) -> None:
    for path, sha in plan.preconditions.items():
        exists = os.path.exists(path)
        if sha is None:
            if exists:
                raise ToolError(f"{path} appeared since the preview. Re-run the preview.")
        else:
            if not exists:
                raise ToolError(f"{path} disappeared since the preview. Re-run the preview.")
            if sha256_file(path) != sha:
                raise ToolError(f"{path} changed since the preview (Claude Desktop wrote to it?). Re-run the preview.")
    for tp, meta in plan.transcripts.items():
        if not os.path.exists(tp):
            raise ToolError(f"transcript {tp} disappeared since the preview.")
        st = os.stat(tp)
        if (st.st_size != meta["size"] or int(st.st_mtime * 1000) != meta["mtime_ms"]) and not live_transcript_ok:
            raise ToolError(f"transcript {os.path.basename(tp)} changed since the preview (a session is writing to it?). "
                            "Re-run the preview.")


def _do_ops(ctx: Ctx, manifest: dict, ops: list[FileOp]) -> None:
    inst = ctx.inst
    for op in ops:
        root = inst.sessions_root if op.root == "sessions" else inst.projects_root
        change = {"kind": op.kind, "path": op.path, "root": op.root, "rel": _rel(root, op.path),
                  "pre_sha256": op.pre_sha256, "post_sha256": op.post_sha256, "note": op.note, "done": False}
        manifest["changes"].append(change)
    save_manifest(ctx, manifest)
    for op, change in zip(ops, manifest["changes"][-len(ops):]):
        d = os.path.dirname(op.path)
        if op.kind in ("create", "replace") and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            manifest["created_dirs"].append(d)
        if op.kind == "create":
            atomic_write(op.path, op.new_bytes, overwrite=False)
        elif op.kind == "replace":
            atomic_write(op.path, op.new_bytes, overwrite=True, keep_attrs_from=op.path)
        elif op.kind == "remove":
            os.remove(op.path)
        change["done"] = True
        ctx.log(f"    {op.kind:8s} {op.path}")
    manifest["status"] = "applied"
    save_manifest(ctx, manifest)


def verify_applied(ctx: Ctx, plan: Plan, manifest: dict, pre_hashes: dict) -> list[dict]:
    """Independent post-write verification.  Returns checks: [{ok, check, detail}]."""
    inst = ctx.inst
    checks: list[dict] = []

    def chk(ok, name, detail=""):
        checks.append({"ok": bool(ok), "check": name, "detail": detail})

    store2 = scan(inst)
    # 1. exact set of files that changed in the session store
    post_tree = tree_snapshot(inst.sessions_root)
    manifest["post_tree"] = post_tree
    pre_tree = manifest["pre_tree"]
    expected = {c["rel"] for c in manifest["changes"] if c["root"] == "sessions"}
    changed = {r for r in set(pre_tree) | set(post_tree)
               if (pre_tree.get(r) or {}).get("sha256") != (post_tree.get(r) or {}).get("sha256")}
    chk(changed == expected, "only the planned files changed in claude-code-sessions",
        "unexpected: " + ", ".join(sorted(changed ^ expected)) if changed != expected else f"{len(expected)} file(s)")
    # 2. per-change hash
    for c in manifest["changes"]:
        if c["kind"] == "remove":
            chk(not os.path.exists(c["path"]), f"removed {c['rel']}")
        else:
            ok = os.path.exists(c["path"]) and sha256_file(c["path"]) == c["post_sha256"]
            chk(ok, f"{c['kind']} {c['rel']} matches the intended bytes (SHA-256)")
    # 3. source records & transcripts untouched
    for it in plan.items:
        chk(os.path.exists(it.source.path) and sha256_file(it.source.path) == it.source.sha256,
            f"source record {it.source.local_id} unchanged")
    for tp, h in pre_hashes.items():
        chk(os.path.exists(tp) and sha256_file(tp) == h, f"transcript {os.path.basename(tp)} byte-identical to before")
    # 4. semantic checks of every written target
    claims = analysis.cli_claims(store2)
    for it in plan.runnable:
        tp = it.target_path
        if not tp or not os.path.exists(tp):
            chk(False, f"target record for {it.source.local_id} exists")
            continue
        try:
            raw = read_json(tp)
        except Exception as e:
            chk(False, f"target record {os.path.basename(tp)} parses", str(e))
            continue
        chk(raw.get("sessionId") == os.path.basename(tp)[:-5], "record sessionId equals its file name")
        chk(raw.get("cliSessionId") == it.new_raw.get("cliSessionId"), "record points at the intended CLI session",
            raw.get("cliSessionId"))
        chk(raw.get("cwd") == it.source.cwd, "record cwd equals the source's")
        chk(os.path.getsize(tp) < 10 * 1024 * 1024, "record below Claude Desktop's 10 MB limit")
        ref = store2.transcripts.get(raw.get("cliSessionId") or "")
        chk(bool(ref and ref.jsonl), "referenced transcript exists")
        if ref and ref.jsonl:
            t = inspect_transcript(ref.jsonl[0], raw["cliSessionId"], deep=True,
                                   needle=raw.get("lastAssistantUuid") if isinstance(raw.get("lastAssistantUuid"), str) else None)
            chk(t["ok"], "transcript parses, has one session id, contains lastAssistantUuid", "; ".join(t["problems"]))
        rec = next((r for r in store2.all_records() if r.path == tp), None)
        if rec:
            a = analysis.assess(store2, rec, claims=claims)
            bad = [f.message for f in a.flags if f.status in (analysis.MISSING, analysis.CONFLICT, analysis.DELETED)]
            chk(not bad, "target record is healthy under this tool's own assessment", "; ".join(bad))
        leaked = [k for k in raw if k in ("remoteMcpServersConfig", "enabledMcpTools", "error", "errorAt")
                  and not it.target]
        chk(not leaked, "no source-account connector snapshots / limit errors were copied", ", ".join(leaked))
    return checks


def apply_plan(ctx: Ctx, plan: Plan, store: Store, *, allow_running: bool = False, include_transcript_backup: bool = False,
               auto_rollback: bool = True, origin: str = "") -> dict:
    if plan.blockers:
        raise ToolError("Plan cannot run: " + "; ".join(plan.blockers))
    runnable = plan.runnable
    result: dict = {"plan_id": plan.id, "counts": plan.counts(), "status": "nothing-to-do"}
    ctx.state.ensure()
    ctx.state.acquire_lock()
    try:
        # NOOP-synced items still refresh the ledger baseline
        if not runnable:
            _update_ledger(ctx, plan, None, only_noop=True)
            return result
        notes = gate_writes(ctx, store, plan.target_account, allow_running)
        result["notes"] = notes
        live_ok = bool(store.live) and allow_running
        _check_preconditions(plan, live_transcript_ok=live_ok)
        op_id = new_op_id("sync")
        ctx.log(f"Operation {op_id}: creating backup ...")
        manifest = create_backup(ctx, op_id, "sync", plan=plan, store=store,
                                 include_transcripts=include_transcript_backup,
                                 note=(f"{origin}: " if origin else "") + f"{plan.source_account[:8]} -> {plan.target_account[:8]}")
        manifest.update({"origin": origin, "source_account": plan.source_account, "target_account": plan.target_account,
                         "target_org": plan.target_org,
                         "items": [i.summary_dict() for i in plan.items]})
        pre_hashes = {}
        for it in runnable:
            tp = it.assessment.transcript_path
            if tp and tp not in pre_hashes and not store.live.get(it.source.cli_id):
                pre_hashes[tp] = sha256_file(tp)
        manifest["transcript_hashes"] = pre_hashes
        save_manifest(ctx, manifest)
        ctx.state.log({"event": "sync-start", "op_id": op_id, "source": plan.source_account,
                       "target": plan.target_account, "counts": plan.counts()})
        ctx.log("Applying changes (atomic, one file at a time):")
        ops = [op for it in runnable for op in it.ops]
        try:
            _do_ops(ctx, manifest, ops)
            checks = verify_applied(ctx, plan, manifest, pre_hashes)
        except Exception as e:  # any failure -> restore
            manifest["status"] = "failed"
            manifest["error"] = f"{type(e).__name__}: {e}"
            save_manifest(ctx, manifest)
            ctx.state.log({"event": "sync-failed", "op_id": op_id, "error": manifest["error"]})
            if auto_rollback:
                ctx.log("ERROR during apply -> restoring the pre-operation state ...")
                rb = _restore_changes(ctx, manifest, force=True)
                manifest["status"] = "failed-rolled-back" if rb["ok"] else "failed-rollback-incomplete"
                save_manifest(ctx, manifest)
            raise ToolError(f"apply failed: {e}. " + ("State restored from backup." if auto_rollback else
                                                      f"Roll back with: rollback --op {op_id}"))
        manifest["verification"] = checks
        failed = [c for c in checks if not c["ok"]]
        if failed:
            manifest["status"] = "verification-failed"
            save_manifest(ctx, manifest)
            ctx.state.log({"event": "sync-verify-failed", "op_id": op_id, "failed": failed})
            if auto_rollback:
                ctx.log("VERIFICATION FAILED -> restoring the pre-operation state ...")
                rb = _restore_changes(ctx, manifest, force=True)
                manifest["status"] = "verification-failed-rolled-back" if rb["ok"] else "rollback-incomplete"
                save_manifest(ctx, manifest)
            result.update({"op_id": op_id, "status": manifest["status"], "verification": checks})
            raise ToolError("post-write verification failed (" + "; ".join(c["check"] for c in failed) +
                            "). " + ("The previous state was restored automatically." if auto_rollback else ""))
        manifest["status"] = "verified"
        _update_ledger(ctx, plan, manifest)
        save_manifest(ctx, manifest)
        ctx.state.log({"event": "sync-verified", "op_id": op_id, "changes": len(manifest["changes"])})
        result.update({"op_id": op_id, "status": "verified", "verification": checks,
                       "backup_dir": os.path.join(ctx.state.backups, op_id),
                       "written": [c["path"] for c in manifest["changes"]]})
        return result
    finally:
        ctx.state.release_lock()


def _update_ledger(ctx: Ctx, plan: Plan, manifest: dict | None, only_noop: bool = False) -> None:
    led = ctx.state.ledger()
    entries = led.setdefault("entries", {})
    for it in plan.items:
        if it.action in (CREATE, UPDATE) and not only_noop:
            pass
        elif it.action == NOOP and it.label == analysis.SYNCED and it.target is not None and it.new_raw is not None:
            pass
        else:
            continue
        s = it.source
        key = State.ledger_key(s.account_id, s.org_id, s.cli_id, plan.target_account, plan.target_org)
        post = None
        if it.ops:
            post = it.ops[0].post_sha256
        elif it.target is not None:
            post = it.target.sha256
        entries[key] = {"source_local_id": s.local_id, "target_local_id": it.target_local_id,
                        "target_path": it.target_path, "synced_at": iso_now(),
                        "op_id": manifest["op_id"] if manifest else None,
                        "base": it.carried_snapshot, "target_sha256": post}
    ctx.state.save_ledger(led)


def prune_auto_backups(ctx: Ctx, keep: int) -> list[str]:
    """Retention for *automatic* operations only: keep the newest ``keep`` finished auto sync backups.

    Never touches manual operations, standalone backups, rollback safety backups, or an operation that is not in a
    finished state.  ``keep`` <= 0 disables pruning.
    """
    if keep <= 0:
        return []
    finished = ("verified", "rolled-back", "verification-failed-rolled-back", "failed-rolled-back")
    auto = [m for m in ctx.state.list_ops()
            if m.get("kind") == "sync" and m.get("origin") == "auto" and m.get("status") in finished]
    auto.sort(key=lambda m: m["op_id"])
    removed: list[str] = []
    root = os.path.abspath(ctx.state.backups)
    for m in auto[:-keep] if len(auto) > keep else []:
        d = os.path.abspath(m["_dir"])
        if os.path.dirname(d) != root or not os.path.exists(os.path.join(d, "manifest.json")):
            continue                                   # only ever delete a direct child that is a real backup
        shutil.rmtree(d, ignore_errors=True)
        removed.append(m["op_id"])
    if removed:
        ctx.state.log({"event": "prune-auto-backups", "removed": removed, "kept": keep})
    return removed


# ---- rollback / restore -------------------------------------------------------------------------------------------

def _restore_changes(ctx: Ctx, manifest: dict, force: bool) -> dict:
    """Undo the changes recorded in ``manifest`` using the backup's pre-images."""
    bdir = os.path.join(ctx.state.backups, manifest["op_id"])
    problems: list[str] = []
    restored: list[str] = []
    for c in reversed(manifest["changes"]):
        path = c["path"]
        cur = sha256_file(path) if os.path.exists(path) else None
        if c["kind"] == "create":
            if cur is None:
                continue
            if cur != c["post_sha256"] and not force:
                problems.append(f"{c['rel']} was modified after the sync (hash differs); not deleting it")
                continue
            if c.get("done") or force or cur == c["post_sha256"]:
                os.remove(path)
                restored.append(f"removed {c['rel']}")
        elif c["kind"] == "replace":
            pre_file = os.path.join(bdir, "pre", "claude-code-sessions" if c["root"] == "sessions" else "projects", c["rel"])
            if cur == c["pre_sha256"]:
                continue
            if cur != c["post_sha256"] and not force:
                problems.append(f"{c['rel']} was modified after the sync (hash differs); not overwriting it")
                continue
            with open(pre_file, "rb") as f:
                data = f.read()
            atomic_write(path, data, overwrite=True, keep_attrs_from=path)
            restored.append(f"restored {c['rel']}")
        elif c["kind"] == "remove":
            pre_file = os.path.join(bdir, "pre", "claude-code-sessions" if c["root"] == "sessions" else "projects", c["rel"])
            if cur is not None:
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copy2(pre_file, path)
            restored.append(f"restored {c['rel']}")
    for d in reversed(manifest.get("created_dirs", [])):
        try:
            os.rmdir(d)
        except OSError:
            pass
    # verify against pre-state
    for c in manifest["changes"]:
        if c["kind"] == "create":
            if os.path.exists(c["path"]) and not problems:
                problems.append(f"{c['rel']} still exists after rollback")
        else:
            cur = sha256_file(c["path"]) if os.path.exists(c["path"]) else None
            if cur != c["pre_sha256"] and not any(c["rel"] in p for p in problems):
                problems.append(f"{c['rel']} does not match its pre-operation hash after rollback")
    return {"ok": not problems, "restored": restored, "problems": problems}


def rollback(ctx: Ctx, op_id: str | None, store: Store, *, force: bool = False, allow_running: bool = False) -> dict:
    ops = [m for m in ctx.state.list_ops() if m.get("kind") == "sync" and m.get("status") in
           ("verified", "applied", "failed", "verification-failed", "rollback-incomplete", "failed-rollback-incomplete")]
    if not ops:
        raise ToolError("No sync operation to roll back.")
    if op_id in (None, "last"):
        manifest = ops[-1]
    else:
        m = [x for x in ops if x["op_id"].startswith(op_id)]
        if len(m) != 1:
            raise ToolError(f"operation '{op_id}' not found (or ambiguous). Known: " + ", ".join(x["op_id"] for x in ops))
        manifest = m[0]
    manifest.pop("_dir", None)
    ctx.state.acquire_lock()
    try:
        gate_writes(ctx, store, manifest.get("target_account"), allow_running)
        # safety net: back up the current state so the rollback itself is reversible
        rb_id = new_op_id("rollback")
        rb = create_backup(ctx, rb_id, "rollback", note=f"before rolling back {manifest['op_id']}",
                           extra_projects_files=[c["path"] for c in manifest["changes"] if c["root"] == "projects" and os.path.exists(c["path"])])
        save_manifest(ctx, rb)
        ctx.log(f"Rolling back {manifest['op_id']} (safety backup {rb_id}) ...")
        res = _restore_changes(ctx, manifest, force=force)
        ctx.state.log({"event": "rollback", "op_id": manifest["op_id"], "rollback_op": rb_id, **res})
        if res["ok"]:
            manifest["status"] = "rolled-back"
            manifest["rolled_back_at"] = iso_now()
            save_manifest(ctx, manifest)
            led = ctx.state.ledger()
            for k in list(led.get("entries", {})):
                if led["entries"][k].get("op_id") == manifest["op_id"]:
                    del led["entries"][k]
            ctx.state.save_ledger(led)
            rb["status"] = "verified"
            save_manifest(ctx, rb)
        res["rollback_backup"] = rb_id
        res["op_id"] = manifest["op_id"]
        return res
    finally:
        ctx.state.release_lock()


def restore_backup(ctx: Ctx, backup_id: str, store: Store, *, allow_running: bool = False) -> dict:
    """Restore files from a standalone backup: puts back missing/different files; never deletes anything."""
    m = [x for x in ctx.state.list_ops() if x["op_id"].startswith(backup_id)]
    if len(m) != 1:
        raise ToolError(f"backup '{backup_id}' not found or ambiguous")
    man = m[0]
    bdir = man["_dir"]
    ctx.state.acquire_lock()
    try:
        gate_writes(ctx, store, None, allow_running)
        rb_id = new_op_id("restore")
        pre = create_backup(ctx, rb_id, "rollback", note=f"before restoring {man['op_id']}")
        save_manifest(ctx, pre)
        restored, same = [], 0
        for rel, meta in man["pre_tree"].items():
            src = os.path.join(bdir, "pre", "claude-code-sessions", rel)
            dst = os.path.join(ctx.inst.sessions_root, rel)
            if os.path.exists(dst) and sha256_file(dst) == meta["sha256"]:
                same += 1
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(src, "rb") as f:
                atomic_write(dst, f.read(), overwrite=True)
            if sha256_file(dst) != meta["sha256"]:
                raise ToolError(f"restore verification failed for {rel}")
            restored.append(rel)
        ctx.state.log({"event": "restore", "backup": man["op_id"], "restored": restored, "safety_backup": rb_id})
        return {"restored": restored, "unchanged": same, "safety_backup": rb_id,
                "note": "files created after the backup were left in place (restore never deletes)"}
    finally:
        ctx.state.release_lock()


# ---- verify -------------------------------------------------------------------------------------------------------------

def verify_store(ctx: Ctx, store: Store, deep: bool = False) -> dict:
    """Health report for the whole store + tool ledger consistency."""
    assessments = analysis.assess_all(store, deep=deep)
    counts: dict[str, int] = {}
    for a in assessments.values():
        counts[a.status] = counts.get(a.status, 0) + 1
    orphans = analysis.find_orphans(store)
    led = ctx.state.ledger().get("entries", {})
    ledger_issues = []
    by_path = {r.path: r for r in store.all_records()}
    for key, e in led.items():
        tp = e.get("target_path")
        if not tp or tp not in by_path:
            ledger_issues.append(f"ledger target {os.path.basename(tp or '?')} no longer exists "
                                 "(deleted in Desktop? it will not be re-created if a tombstone exists)")
            continue
        s_acct, s_org, cli = key.split("=>")[0].split("/")
        src = next((r for r in store.all_records() if r.account_id == s_acct and r.cli_id == cli), None)
        t = by_path[tp]
        if t.cli_id != cli and not src:
            ledger_issues.append(f"{t.local_id}: source session gone and target cli id moved")
    problems = []
    for a in store.accounts.values():
        for o in a.orgs.values():
            for u in o.unreadable:
                problems.append(f"Desktop quarantined a record: {u}")
            for u in o.stray_tmp:
                problems.append(f"stray temp file from an interrupted write: {u}")
    ops = ctx.state.list_ops()
    op_checks = []
    for m in ops:
        if m.get("kind") == "sync" and m.get("status") == "verified":
            for c in m.get("changes", []):
                if c["kind"] in ("create", "replace"):
                    cur = sha256_file(c["path"]) if os.path.exists(c["path"]) else None
                    st = "as written" if cur == c["post_sha256"] else ("missing" if cur is None else "modified since (normal after use)")
                    op_checks.append({"op_id": m["op_id"], "file": c["rel"], "state": st})
    return {"counts": counts,
            "assessments": {p: {"status": a.status, "reasons": a.reasons(("error", "warn"))} for p, a in assessments.items()},
            "orphans": orphans, "ledger_issues": ledger_issues, "problems": problems, "op_checks": op_checks,
            "shared": {cid: [r.local_id + "@" + r.account_id[:8] for r in rs] for cid, rs in analysis.shared_conversations(store).items()}}
