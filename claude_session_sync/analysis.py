"""Health assessment for session records, transcripts and cross-record relationships (read-only)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from . import schema
from .common import norm_path
from .store import Record, Store, inspect_transcript

# status vocabulary (also used by the GUI)
HEALTHY = "Healthy"
MISSING = "Missing transcript"
STALE = "Stale metadata"
CONFLICT = "Conflict"
DELETED = "Deleted/tombstoned"
REVIEW = "Needs review"
SYNCED = "Already synchronized"

_RANK = {DELETED: 0, MISSING: 1, CONFLICT: 2, REVIEW: 3, STALE: 4, HEALTHY: 5}
STALE_TOLERANCE_MS = 10 * 60 * 1000


@dataclass
class Flag:
    severity: str        # error | warn | info
    code: str
    message: str
    status: str | None = None   # status this flag contributes to

    def to_dict(self):
        return {"severity": self.severity, "code": self.code, "message": self.message}


@dataclass
class Assessment:
    record: Record
    flags: list[Flag] = field(default_factory=list)
    transcript_path: str | None = None
    transcript: dict | None = None
    live: bool = False
    status: str = HEALTHY

    def add(self, sev, code, msg, status=None):
        self.flags.append(Flag(sev, code, msg, status))

    def finalize(self):
        cands = [f.status for f in self.flags if f.status]
        self.status = min(cands, key=lambda s: _RANK[s]) if cands else HEALTHY
        return self

    def reasons(self, min_sev=("error", "warn")):
        return [f.message for f in self.flags if f.severity in min_sev]


def tombstone_hits(store: Store, rec: Record) -> list[tuple[str, str, str]]:
    """Tombstones in the record's own account (any org) that name this record: [(what, org_id, path)]."""
    hits = []
    tomb = store.tombstoned_in_account(rec.account_id)
    for what, key in (("local id", rec.stem), ("CLI session id", rec.cli_id)):
        if key and key in tomb:
            hits.append((what, tomb[key][0], tomb[key][1]))
    return hits


def cli_claims(store: Store) -> dict[str, list[Record]]:
    """cli id -> all records (any account) whose current cliSessionId is that id."""
    m: dict[str, list[Record]] = {}
    for r in store.all_records():
        if r.cli_id:
            m.setdefault(r.cli_id, []).append(r)
    return m


def resolve_transcript(store: Store, cli_id: str | None) -> list[str]:
    ref = store.transcripts.get(cli_id or "")
    return list(ref.jsonl) if ref else []


def assess(store: Store, rec: Record, deep: bool = False, claims: dict | None = None) -> Assessment:
    a = Assessment(rec)
    claims = claims if claims is not None else cli_claims(store)

    if rec.parse_error or rec.raw is None:
        a.add("error", "unreadable", f"record cannot be parsed: {rec.parse_error}", REVIEW)
        return a.finalize()

    for k in schema.REQUIRED:
        if not isinstance(rec.raw.get(k), str) or not rec.raw.get(k):
            a.add("error", "missing-field", f"required field '{k}' missing or not a string", REVIEW)
    if rec.raw.get("sessionId") != rec.local_id:
        a.add("error", "id-mismatch",
              f"file name says {rec.local_id} but the record's sessionId is {rec.raw.get('sessionId')!r}", CONFLICT)

    cli = rec.cli_id
    if not cli:
        return a.finalize()

    # deletion / tombstones ------------------------------------------------------------------
    for what, org, p in tombstone_hits(store, rec):
        a.add("error", "tombstoned",
              f"a deletion tombstone names this session's {what} (org {org[:8]}): the session was deleted on purpose",
              DELETED)
    # tombstones in *other* accounts are informational
    for oid, acct in store.accounts.items():
        if oid == rec.account_id:
            continue
        for o in acct.orgs.values():
            if cli in o.tombstones or rec.stem in o.tombstones:
                a.add("info", "tombstoned-elsewhere",
                      f"account {oid[:8]} has a deletion tombstone for this session")

    # transcript -----------------------------------------------------------------------------
    paths = resolve_transcript(store, cli)
    if not paths:
        a.add("error", "no-transcript", f"transcript {cli}.jsonl was not found under the Claude Code projects folder",
              MISSING)
    else:
        if len(paths) > 1:
            a.add("warn", "duplicate-transcript", f"{len(paths)} copies of {cli}.jsonl exist in different project folders",
                  REVIEW)
        a.transcript_path = paths[0]
        needle = rec.g("lastAssistantUuid") if deep else None
        t = inspect_transcript(paths[0], cli, deep=deep, needle=needle if isinstance(needle, str) else None)
        a.transcript = t
        for prob in t.get("problems", []):
            sev = "error" if "different session id" in prob else "warn"
            a.add(sev, "transcript-integrity", prob, CONFLICT if "different session id" in prob else REVIEW)
        if rec.cwd and t.get("cwds"):
            if norm_path(rec.cwd) not in {norm_path(c) for c in t["cwds"]}:
                a.add("warn", "cwd-mismatch",
                      f"record cwd {rec.cwd!r} is not among the folders recorded in the transcript ({t['cwds'][0]!r})",
                      REVIEW)
        # stale: transcript has activity well past the record
        from .store import iso_to_ms
        lm = iso_to_ms(t.get("last_ts"))
        if lm and rec.last_activity and lm - rec.last_activity > STALE_TOLERANCE_MS and cli not in store.live:
            a.add("warn", "stale-activity",
                  f"transcript has activity {int((lm - rec.last_activity) / 60000)} min newer than the record's lastActivityAt",
                  STALE)
        exp = os.path.basename(os.path.dirname(paths[0]))
        from .common import encode_project_dir
        if rec.cwd and exp != encode_project_dir(rec.cwd):
            a.add("info", "project-dir", f"transcript lives in project folder '{exp}' (expected '{encode_project_dir(rec.cwd)}')")

    # released marker (Desktop wrote it when a session referencing this transcript was deleted) ----
    ref = store.transcripts.get(cli)
    if ref and ref.released_markers:
        a.add("warn", "released-marker",
              "a '.desktop-released.json' marker sits next to the transcript: some Desktop session that used it was "
              "deleted, which lets Claude Code's cleanup sweep remove the transcript", REVIEW)

    # lineage / uniqueness ----------------------------------------------------------------------
    same_account = [r for r in claims.get(cli, []) if r.account_id == rec.account_id and r.path != rec.path]
    if same_account:
        a.add("error", "not-one-to-one",
              "another record in the same account points at the same CLI session: " +
              ", ".join(f"{r.local_id}" for r in same_account) +
              " (local <-> CLI mapping is not one-to-one)", CONFLICT)
    if cli in rec.prior_ids:
        a.add("warn", "self-prior", "cliSessionId also appears in its own priorCliSessionIds", STALE)
    for r in store.all_records():
        if r.account_id == rec.account_id and r.path != rec.path and cli in r.prior_ids:
            a.add("warn", "superseded",
                  f"{r.local_id} lists this CLI session as a prior id: this record points at an older link of the chain",
                  STALE)
    for r in store.all_records():
        if r.account_id != rec.account_id and r.local_id == rec.local_id and r.cli_id and r.cli_id != cli:
            a.add("error", "local-id-collision",
                  f"account {r.account_id[:8]} uses the same local id for a DIFFERENT conversation ({r.cli_id[:8]})",
                  CONFLICT)

    # other findings -----------------------------------------------------------------------------
    cls = schema.classify_keys(rec.raw.keys())
    for k in cls["blocker"]:
        a.add("warn", "blocker-field", f"session {schema.BLOCKERS[k]} ('{k}')", REVIEW)
    if cls["unknown"]:
        a.add("info", "unknown-fields", "unclassified fields (would not be copied): " + ", ".join(cls["unknown"]))
    if rec.g("error"):
        a.add("info", "record-error", f"session ended with: {rec.g('error')}")
    lp = store.live.get(cli)
    if lp:
        a.live = True
        a.add("info", "live", f"a Claude Code process (pid {lp.pid}, {lp.status}) is running this session right now")
    org = store.accounts[rec.account_id].orgs[rec.org_id]
    if any(os.path.basename(u).startswith(rec.local_id) for u in org.unreadable):
        a.add("warn", "quarantined", "Desktop quarantined an earlier copy of this record as '.unreadable'", REVIEW)
    return a.finalize()


def assess_all(store: Store, deep: bool = False) -> dict[str, Assessment]:
    claims = cli_claims(store)
    return {r.path: assess(store, r, deep=deep, claims=claims) for r in store.all_records()}


def find_orphans(store: Store) -> dict[str, list]:
    """Transcripts nobody references / sidecars left behind / tombstones without a record."""
    referenced: set[str] = set()
    for r in store.all_records():
        if r.cli_id:
            referenced.add(r.cli_id)
        referenced.update(r.prior_ids)
        for k in ("unarchivedCliSessionId", "preClearCliSessionId"):
            v = r.g(k)
            if isinstance(v, str):
                referenced.add(v)
    tomb = {s for a in store.accounts.values() for o in a.orgs.values() for s in o.tombstones}
    orphan_tx, tombstoned_tx, sidecar_only = [], [], []
    for cid, ref in store.transcripts.items():
        if ref.jsonl:
            if cid in tomb:
                tombstoned_tx.append(ref.jsonl[0])
            elif cid not in referenced:
                orphan_tx.append(ref.jsonl[0])
        elif ref.sidecar_dirs or ref.released_markers:
            sidecar_only.append(cid)
    dangling_tomb = []
    live_stems = {r.stem for r in store.all_records()} | {r.cli_id for r in store.all_records() if r.cli_id}
    for a in store.accounts.values():
        for o in a.orgs.values():
            for s in o.tombstones:
                if s in live_stems:
                    dangling_tomb.append(o.tombstone_paths[s])   # tombstone AND live record coexist
    return {"orphan_transcripts": orphan_tx, "tombstoned_transcripts": tombstoned_tx,
            "sidecar_or_marker_only": sidecar_only, "tombstone_with_live_record": dangling_tomb}


def shared_conversations(store: Store) -> dict[str, list[Record]]:
    """cli id -> records in >=2 accounts (the intended result of a sync)."""
    out = {}
    for cid, recs in cli_claims(store).items():
        if len({r.account_id for r in recs}) > 1:
            out[cid] = recs
    return out
