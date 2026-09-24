"""Builds a synchronisation plan (pure computation; never writes anything).

Strategy (why): the conversation lives once, in ~/.claude/projects/<project>/<cliSessionId>.jsonl, and is
shared by every account.  What is per-account is the Desktop *record* claude-code-sessions/<acct>/<org>/
local_<id>.json that makes a session visible in that account's Code tab.  So "synchronising" a session
means creating/updating a small, sanitised record in the target account that points at the SAME transcript.
The transcript is never copied, moved or rewritten.  See README for the full reasoning.
"""
from __future__ import annotations

import copy
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import schema
from .analysis import (HEALTHY, STALE, SYNCED, Assessment, assess, cli_claims, tombstone_hits)
from .common import (LOCAL_PREFIX, SAFE_ID_RE, ToolError, dumps_record, norm_path, sha256_bytes, sha256_file,
                     new_uuid)
from .state import State
from .store import Record, Store

MISSING = object()

# item actions
CREATE, UPDATE, NOOP, BLOCKED, CONFLICT_ = "create", "update", "noop", "blocked", "conflict"


@dataclass
class Options:
    include_archived: bool = False
    carry_unknown: bool = False
    preserve_local_id: bool = False        # reuse the source's local id instead of local_<cliSessionId>
    resurrect: bool = False                # allow re-creating a session the target account deleted
    clear_released_marker: bool = False    # remove '<cli>.desktop-released.json' next to the transcript
    force_update: bool = False             # push source progress even if the target record is ahead
    new_id_on_collision: bool = False      # if the wanted target file name is taken by another conversation
    target_org: str | None = None
    source_org: str | None = None
    deep_verify: bool = True
    drop_permission_grants: bool = False   # don't carry alwaysAllowedReasons / sessionPermissionUpdates


@dataclass
class FileOp:
    kind: str                      # create | replace | remove
    path: str
    root: str                      # "sessions" | "projects"
    new_bytes: bytes | None
    pre_sha256: str | None         # required current content hash (None = must not exist)
    post_sha256: str | None
    note: str = ""


@dataclass
class Change:
    field: str
    old: Any
    new: Any
    note: str = ""


@dataclass
class PlanItem:
    source: Record
    assessment: Assessment
    action: str = BLOCKED
    label: str = ""                # human status (New / Already synchronized / Update / Blocked / Conflict ...)
    reasons: list[str] = field(default_factory=list)      # why blocked / conflicting
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)
    target: Record | None = None
    target_local_id: str | None = None
    target_path: str | None = None
    new_raw: dict | None = None
    changes: list[Change] = field(default_factory=list)
    dropped: dict = field(default_factory=dict)
    ops: list[FileOp] = field(default_factory=list)
    carried_snapshot: dict | None = None     # what the ledger will remember as "base"

    def summary_dict(self):
        s = self.source
        return {
            "source_local_id": s.local_id, "cli_id": s.cli_id, "title": s.title, "cwd": s.cwd,
            "action": self.action, "label": self.label, "reasons": self.reasons, "warnings": self.warnings,
            "infos": self.infos, "target_local_id": self.target_local_id, "target_path": self.target_path,
            "changes": [{"field": c.field, "old": _brief(c.old), "new": _brief(c.new), "note": c.note} for c in self.changes],
            "dropped": self.dropped,
            "ops": [{"kind": o.kind, "path": o.path, "note": o.note,
                     "bytes": len(o.new_bytes) if o.new_bytes is not None else None} for o in self.ops],
        }


def _brief(v, n=90):
    if v is MISSING:
        return "<absent>"
    t = repr(v)
    return t if len(t) <= n else t[:n] + "...(%d chars)" % len(t)


@dataclass
class Plan:
    id: str
    source_account: str
    target_account: str
    target_org: str | None
    options: Options
    items: list[PlanItem] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)          # plan-level problems (no item can run)
    warnings: list[str] = field(default_factory=list)
    preconditions: dict[str, str | None] = field(default_factory=dict)   # path -> sha256 or None
    transcripts: dict[str, dict] = field(default_factory=dict)          # path -> {size, mtime_ms}

    @property
    def runnable(self) -> list[PlanItem]:
        return [i for i in self.items if i.action in (CREATE, UPDATE) and i.ops]

    def counts(self) -> dict:
        c: dict[str, int] = {}
        for i in self.items:
            c[i.action] = c.get(i.action, 0) + 1
        return c

    def to_dict(self):
        return {"id": self.id, "source_account": self.source_account, "target_account": self.target_account,
                "target_org": self.target_org, "counts": self.counts(), "blockers": self.blockers,
                "warnings": self.warnings, "items": [i.summary_dict() for i in self.items],
                "options": self.options.__dict__}


# ---- selection ------------------------------------------------------------------------------------

def select_sessions(store: Store, account_id: str, refs: list[str] | None = None, projects: list[str] | None = None,
                    select_all: bool = False, include_archived: bool = False,
                    source_org: str | None = None) -> list[Record]:
    acct = store.accounts[account_id]
    recs = [r for o in acct.orgs.values() if not source_org or o.org_id.startswith(source_org.lower())
            for r in o.records]
    out: list[Record] = []
    seen: set[str] = set()

    def add(r):
        if r.path not in seen:
            seen.add(r.path)
            out.append(r)

    if select_all:
        for r in recs:
            if include_archived or not r.archived:
                add(r)
    for ref in refs or []:
        rl = ref.strip().lower()
        hits = [r for r in recs if rl in (r.local_id.lower(), r.stem.lower(), (r.cli_id or "").lower())]
        if not hits:
            hits = [r for r in recs if r.local_id.lower().startswith(rl) or r.stem.lower().startswith(rl)
                    or (r.cli_id or "").lower().startswith(rl)]
        if not hits:
            hits = [r for r in recs if r.title.lower() == rl]
        if not hits:
            hits = [r for r in recs if rl in r.title.lower()]
        if not hits:
            raise ToolError(f"No session in account {account_id[:8]} matches '{ref}'.")
        if len(hits) > 1:
            raise ToolError(f"'{ref}' matches {len(hits)} sessions; be more specific:\n" +
                            "\n".join(f"  {r.local_id}  {r.title!r}  {r.cwd}" for r in hits))
        add(hits[0])
    for pr in projects or []:
        key = norm_path(pr)
        hits = [r for r in recs if norm_path(r.cwd) == key or norm_path(r.g("originCwd")) == key]
        if not hits:
            raise ToolError(f"No session in account {account_id[:8]} belongs to project '{pr}'.")
        for r in hits:
            if include_archived or not r.archived:
                add(r)
    return out


def resolve_target_org(store: Store, target_account: str, wanted: str | None) -> tuple[str | None, str | None]:
    """(org_id, problem)."""
    acct = store.accounts[target_account]
    if wanted:
        m = [o for o in list(acct.orgs) + list(acct.paired_orgs) if o.startswith(wanted.lower())]
        m = sorted(set(m))
        if len(m) != 1:
            return None, f"--target-org '{wanted}' matches {len(m)} organisations of the target account."
        return m[0], None
    strong = sorted(acct.paired_orgs)
    if len(strong) == 1:
        return strong[0], None
    if len(strong) > 1:
        return None, ("target account has several organisations with evidence of use ("
                      + ", ".join(o[:8] for o in strong) + "); choose one with --target-org")
    if len(acct.orgs) == 1:
        oid = next(iter(acct.orgs))
        return None, (f"only weak evidence that org {oid[:8]} belongs to the target account (folder exists but has no "
                      "records / no skills-plugin pairing). Sign in to Claude Desktop with that account once, or pass "
                      "--target-org to confirm.")
    return None, "target account has no organisation folder; sign in to Claude Desktop with it and open the Code tab once."


# ---- helpers ------------------------------------------------------------------------------------------

PERMISSION_GRANT_FIELDS = ("alwaysAllowedReasons", "sessionPermissionUpdates")


def carried_view(raw: dict, opts: Options) -> tuple[dict, dict]:
    """(carried fields in source order, dropped-by-category)."""
    cls = schema.classify_keys(raw.keys())
    carried = {k: copy.deepcopy(raw[k]) for k in raw if k in cls["carry"] or (opts.carry_unknown and k in cls["unknown"])}
    if opts.drop_permission_grants:
        for k in PERMISSION_GRANT_FIELDS:
            carried.pop(k, None)
    dropped = {"org_specific": cls["org"], "runtime": cls["runtime"],
               "unknown": [] if opts.carry_unknown else cls["unknown"]}
    return carried, {k: v for k, v in dropped.items() if v}


def _lineage(t: Record, s: Record) -> str:
    """Relationship of target record T to source record S: same | source_ahead | target_ahead | diverged."""
    if t.cli_id == s.cli_id:
        if s.last_activity > t.last_activity:
            return "source_ahead"
        if s.last_activity < t.last_activity:
            return "target_ahead"
        return "same"
    if t.cli_id in s.prior_ids:
        return "source_ahead"
    if s.cli_id in t.prior_ids:
        return "target_ahead"
    return "diverged"


def _merge_update(s: Record, t: Record, base: dict | None, opts: Options, relation: str
                  ) -> tuple[dict, list[Change], list[str], list[str]]:
    """3-way merge of carried fields.  Returns (new_raw, changes, warnings, conflicts)."""
    sc, _ = carried_view(s.raw, opts)
    new = copy.deepcopy(t.raw)
    changes: list[Change] = []
    warns: list[str] = []
    conflicts: list[str] = []
    take_progress = relation == "source_ahead" or (opts.force_update and relation in ("target_ahead", "same"))

    for f_ in sc:
        if f_ in schema.VOLATILE:
            continue
        sv = sc[f_]
        tv = t.raw.get(f_, MISSING)
        bv = (base or {}).get(f_, MISSING) if base is not None else MISSING
        if tv is not MISSING and sv == tv:
            continue
        if f_ in schema.IMMUTABLE:
            if tv is not MISSING:
                conflicts.append(f"'{f_}' differs ({_brief(tv)} vs {_brief(sv)}): these are different conversations")
                continue
            new[f_] = sv
            changes.append(Change(f_, MISSING, sv, "added"))
            continue
        if f_ in schema.PROGRESS:
            if take_progress:
                val = sv
                if f_ == "priorCliSessionIds":
                    val = list(sv)
                    if t.cli_id and t.cli_id != s.cli_id and t.cli_id not in val:
                        val.append(t.cli_id)
                    for x in t.prior_ids:
                        if x not in val:
                            val.append(x)
                new[f_] = val
                changes.append(Change(f_, tv, val, "source is ahead" if relation == "source_ahead" else "forced"))
            elif tv is MISSING:
                new[f_] = sv
                changes.append(Change(f_, MISSING, sv, "added"))
            continue
        # non-progress descriptive/config fields
        if tv is MISSING:
            new[f_] = sv
            changes.append(Change(f_, MISSING, sv, "added"))
        elif bv is not MISSING and tv == bv and sv != bv:
            new[f_] = sv
            changes.append(Change(f_, tv, sv, "changed on source, untouched on target"))
        elif bv is not MISSING and sv == bv:
            continue                                  # only target changed -> keep target's
        elif bv is not MISSING:
            warns.append(f"'{f_}' changed on both sides since last sync; kept the target's value")
        # no baseline: keep target's value silently (informational only)
    # a progress take of cliSessionId must also move priorCliSessionIds consistently
    return new, changes, warns, conflicts


def build_plan(store: Store, state: State, source_account: str, target_account: str, records: list[Record],
               opts: Options) -> Plan:
    plan = Plan(id=uuid.uuid4().hex[:12], source_account=source_account, target_account=target_account,
                target_org=None, options=opts)
    if source_account == target_account:
        plan.blockers.append("source and target are the same account")
        return plan
    org_id, problem = resolve_target_org(store, target_account, opts.target_org)
    if problem:
        plan.blockers.append(problem)
        return plan
    plan.target_org = org_id
    tgt_acct = store.accounts[target_account]
    tgt_org = tgt_acct.orgs.get(org_id)
    root = store.inst.sessions_root
    tgt_dir = os.path.join(root, target_account, org_id)
    if tgt_org is None:
        plan.warnings.append(f"target org folder {org_id[:8]} does not exist yet; it will be created "
                             f"(pairing evidence: {tgt_acct.paired_orgs.get(org_id)})")
    if tgt_org is not None and tgt_org.stray_tmp:
        plan.warnings.append("stray .cssync-*.tmp files from an interrupted run exist in the target folder")
    claims = cli_claims(store)
    ledger = state.ledger().get("entries", {})
    tomb = store.tombstoned_in_account(target_account)
    tgt_records = [r for o in tgt_acct.orgs.values() for r in o.records]

    for s in records:
        a = assess(store, s, deep=opts.deep_verify, claims=claims)
        item = PlanItem(source=s, assessment=a)
        plan.items.append(item)
        _plan_one(store, plan, item, opts, tgt_dir, tgt_org, tgt_records, tomb, ledger, claims)

    # preconditions: everything the plan reads or writes must be unchanged at apply time
    for it in plan.items:
        plan.preconditions[it.source.path] = it.source.sha256
        if it.target:
            plan.preconditions[it.target.path] = it.target.sha256
        for op in it.ops:
            plan.preconditions[op.path] = op.pre_sha256
        tp = it.assessment.transcript_path
        if tp and os.path.exists(tp):
            st = os.stat(tp)
            plan.transcripts[tp] = {"size": st.st_size, "mtime_ms": int(st.st_mtime * 1000)}
    return plan


def _plan_one(store: Store, plan: Plan, item: PlanItem, opts: Options, tgt_dir: str, tgt_org, tgt_records: list[Record],
              tomb: dict, ledger: dict, claims: dict):
    s, a = item.source, item.assessment
    item.dropped = {}
    # ---- gate on the source's own health ---------------------------------------------------------
    if s.raw is None:
        item.action, item.label = BLOCKED, "Needs review"
        item.reasons.append("source record is unreadable")
        return
    hard = []
    for f in a.flags:
        if f.severity == "info":
            item.infos.append(f.message)
        elif f.code == "stale-activity":
            item.warnings.append(f.message + " (target will get the record's values; transcript is still complete)")
        elif f.code in ("tombstoned",):
            hard.append(("Deleted/tombstoned", f.message))
        elif f.code in ("no-transcript",):
            hard.append(("Missing transcript", f.message))
        elif f.code in ("not-one-to-one", "id-mismatch", "local-id-collision") or (
                f.code == "transcript-integrity" and "different session id" in f.message):
            hard.append(("Conflict", f.message))
        elif f.code == "released-marker" and opts.clear_released_marker:
            item.warnings.append("released marker will be removed (explicit option)")
        elif f.severity in ("error", "warn"):
            hard.append(("Needs review", f.message))
    if a.live:
        hard.append(("Needs review", "the session is running right now; stop it (or let it finish) before syncing"))
    if hard:
        item.action = CONFLICT_ if hard[0][0] == "Conflict" else BLOCKED
        item.label = hard[0][0]
        item.reasons = [m for _, m in hard]
        return

    carried, dropped = carried_view(s.raw, opts)
    item.dropped = dropped
    if opts.carry_unknown and schema.classify_keys(s.raw.keys())["unknown"]:
        item.warnings.append("unclassified fields carried over (--carry-unknown): " +
                             ", ".join(schema.classify_keys(s.raw.keys())["unknown"]))
    item.carried_snapshot = carried
    grants = [k for k in PERMISSION_GRANT_FIELDS if carried.get(k)]
    if grants or carried.get("permissionMode") in ("bypassPermissions", "acceptEdits", "auto"):
        n_rules = len(carried.get("alwaysAllowedReasons") or [])
        n_dirs = sum(len(u.get("directories", [])) for u in (carried.get("sessionPermissionUpdates") or [])
                     if isinstance(u, dict))
        item.warnings.append(f"session permission settings travel with it: mode '{carried.get('permissionMode')}', "
                             f"{n_rules} always-allowed rule(s), {n_dirs} granted folder(s) "
                             "(--drop-permission-grants keeps the mode but drops the rules/folders)")
    cli = s.cli_id
    assert cli

    # ---- what does the target account already have for this conversation? ------------------------
    same_cli = [t for t in tgt_records if t.cli_id == cli]
    related = [t for t in tgt_records if t.cli_id != cli and (t.cli_id in s.prior_ids or cli in t.prior_ids)]
    existing: Record | None = None
    if len(same_cli) > 1:
        item.action, item.label = CONFLICT_, "Conflict"
        item.reasons.append("target account has several records for this CLI session: " +
                            ", ".join(t.local_id for t in same_cli))
        return
    if same_cli:
        existing = same_cli[0]
    elif len(related) == 1:
        existing = related[0]
    elif len(related) > 1:
        item.action, item.label = CONFLICT_, "Conflict"
        item.reasons.append("several target records belong to this conversation's lineage: " +
                            ", ".join(t.local_id for t in related))
        return

    tomb_keys = [k for k in sorted({cli, s.stem}) if k in tomb]

    if existing is None:
        # ---------------- CREATE -------------------------------------------------------------------
        want_local = s.local_id if opts.preserve_local_id else LOCAL_PREFIX + cli
        if tomb_keys or (LOCAL_PREFIX and want_local[len(LOCAL_PREFIX):] in tomb):
            keys = sorted(set(tomb_keys + ([want_local[len(LOCAL_PREFIX):]] if want_local[len(LOCAL_PREFIX):] in tomb else [])))
            if not opts.resurrect:
                item.action, item.label = BLOCKED, "Deleted/tombstoned"
                item.reasons.append("the target account deleted this session on purpose (tombstone for "
                                    + ", ".join(k[:8] for k in keys) + "); it will not be resurrected. "
                                    "Use --resurrect to override explicitly.")
                return
            for k in keys:
                p = tomb[k][1]
                item.ops.append(FileOp("remove", p, "sessions", None, sha256_file(p), None,
                                       "remove deletion tombstone (explicit --resurrect)"))
            item.warnings.append("RESURRECTING a session the target account had deleted (explicit --resurrect)")
        if not SAFE_ID_RE.match(want_local):
            item.action, item.label = BLOCKED, "Needs review"
            item.reasons.append(f"unsafe local id {want_local!r}")
            return
        tpath = os.path.join(tgt_dir, want_local + ".json")
        if os.path.exists(tpath) or any(r.local_id == want_local for r in tgt_records):
            if opts.new_id_on_collision:
                want_local = LOCAL_PREFIX + new_uuid()
                tpath = os.path.join(tgt_dir, want_local + ".json")
                item.warnings.append("wanted file name was taken by a different conversation; using a fresh id")
            else:
                item.action, item.label = CONFLICT_, "Conflict"
                item.reasons.append(f"target already has {want_local}.json but it points at a different conversation; "
                                    "nothing overwritten (use --new-id-on-collision to create under a fresh id)")
                item.ops = []
                return
        # global local-id collisions with other accounts (different conversations)
        for r in store.all_records():
            if r.account_id != plan.target_account and r.local_id == want_local and r.cli_id != cli:
                item.warnings.append(f"local id {want_local} is used by account {r.account_id[:8]} for another conversation")
        new_raw = {"sessionId": want_local}
        new_raw.update(carried)
        # never inherit the source's runtime error/limit state; already excluded by classification
        data = dumps_record(new_raw)
        item.ops.append(FileOp("create", tpath, "sessions", data, None, sha256_bytes(data),
                               f"new record {want_local}.json ({len(data)} bytes)"))
        item.action, item.label = CREATE, "New"
        item.target_local_id, item.target_path, item.new_raw = want_local, tpath, new_raw
        for k, v in carried.items():
            item.changes.append(Change(k, MISSING, v, "carried"))
    else:
        # ---------------- UPDATE / NOOP ------------------------------------------------------------
        item.target, item.target_local_id, item.target_path = existing, existing.local_id, existing.path
        if existing.raw is None:
            item.action, item.label = BLOCKED, "Needs review"
            item.reasons.append(f"target record {existing.local_id} is unreadable ({existing.parse_error})")
            return
        if existing.org_id != plan.target_org:
            item.action, item.label = NOOP, SYNCED
            item.warnings.append(f"target account already has this conversation under another organisation "
                                 f"({existing.org_id[:8]}); nothing done")
            return
        if any(k in tomb for k in (existing.stem, cli)):
            item.action, item.label = BLOCKED, "Deleted/tombstoned"
            item.reasons.append("target has BOTH a record and a deletion tombstone for this session; needs manual review")
            return
        rel = _lineage(existing, s)
        if rel == "diverged":
            item.action, item.label = CONFLICT_, "Conflict"
            item.reasons.append(f"target record points at CLI session {existing.cli_id[:8]} which is unrelated to the "
                                f"source's {cli[:8]} (no lineage link either way)")
            return
        led_key = State.ledger_key(s.account_id, s.org_id, cli, plan.target_account, plan.target_org)
        base = (ledger.get(led_key) or {}).get("base")
        if base is None:
            item.infos.append("no earlier sync recorded for this pair; comparing directly")
        new_raw, changes, warns, conflicts = _merge_update(s, existing, base, opts, rel)
        item.warnings += warns
        if conflicts:
            item.action, item.label = CONFLICT_, "Conflict"
            item.reasons += conflicts
            return
        # target's own transcript must exist if the cli id moves
        if new_raw.get("cliSessionId") != existing.cli_id and not store.transcripts.get(new_raw.get("cliSessionId"), None):
            item.action, item.label = BLOCKED, "Missing transcript"
            item.reasons.append("new CLI session transcript missing")
            return
        if rel == "target_ahead" and not opts.force_update:
            item.infos.append("target record is AHEAD of the source (more recent activity); use the reverse direction "
                              "to bring the source up to date, or --force-update to overwrite progress fields")
        if new_raw == existing.raw:
            item.action, item.label = NOOP, SYNCED
            item.new_raw = new_raw
            return
        data = dumps_record(new_raw)
        item.ops.append(FileOp("replace", existing.path, "sessions", data, existing.sha256, sha256_bytes(data),
                               f"update {existing.local_id}.json ({len(changes)} field(s))"))
        item.action, item.label = UPDATE, "Update"
        item.new_raw, item.changes = new_raw, changes
        item.carried_snapshot = carried

    # ---- released marker next to the transcript (both create and update paths) ---------------------
    ref = store.transcripts.get(cli)
    if ref and ref.released_markers and item.action in (CREATE, UPDATE):
        if opts.clear_released_marker:
            for mp in ref.released_markers:
                item.ops.append(FileOp("remove", mp, "projects", None, sha256_file(mp), None,
                                       "remove '.desktop-released.json' marker (explicit option)"))

    # ---- soft warnings ---------------------------------------------------------------------------------
    for t in tgt_org.records if tgt_org else []:
        if t is not item.target and t.cli_id != cli and t.title == s.title and norm_path(t.cwd) == norm_path(s.cwd):
            item.warnings.append(f"target has another session with the same title and folder ({t.local_id}); "
                                 "different conversation, so it is left alone")
            break
