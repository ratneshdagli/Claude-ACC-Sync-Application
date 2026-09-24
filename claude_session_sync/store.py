"""Read-only discovery of Claude Desktop's per-account session store and Claude Code's transcript store."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import environment, winproc
from .common import (LOCAL_PREFIX, TOMBSTONE_PREFIX, is_uuid, norm_path, read_json, sha256_bytes,
                     encode_project_dir)

RECORD_MAX_BYTES = 10 * 1024 * 1024  # Claude Desktop skips larger records


@dataclass
class Record:
    account_id: str
    org_id: str
    path: str
    filename: str
    size: int
    mtime_ms: int
    sha256: str
    raw: dict | None = None
    parse_error: str | None = None
    oversized: bool = False

    @property
    def local_id(self) -> str:                 # e.g. "local_16f94682-..."
        return self.filename[:-5]

    @property
    def stem(self) -> str:                     # id without the "local_" prefix (tombstones use this form)
        lid = self.local_id
        return lid[len(LOCAL_PREFIX):] if lid.startswith(LOCAL_PREFIX) else lid

    def g(self, key, default=None):
        return (self.raw or {}).get(key, default)

    @property
    def cli_id(self) -> str | None:
        v = self.g("cliSessionId")
        return v if isinstance(v, str) and v else None

    @property
    def prior_ids(self) -> list[str]:
        v = self.g("priorCliSessionIds")
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

    @property
    def cwd(self) -> str | None:
        return self.g("cwd") if isinstance(self.g("cwd"), str) else None

    @property
    def title(self) -> str:
        t = self.g("title")
        return t if isinstance(t, str) and t else "(untitled)"

    @property
    def last_activity(self) -> int:
        v = self.g("lastActivityAt")
        return int(v) if isinstance(v, (int, float)) else 0

    @property
    def created(self) -> int:
        v = self.g("createdAt")
        return int(v) if isinstance(v, (int, float)) else 0

    @property
    def archived(self) -> bool:
        return bool(self.g("isArchived"))


@dataclass
class OrgDir:
    account_id: str
    org_id: str
    path: str
    records: list[Record] = field(default_factory=list)
    tombstones: dict[str, str] = field(default_factory=dict)      # stem -> raw content (ms timestamp)
    tombstone_paths: dict[str, str] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)           # *.json.unreadable-<ts> quarantined by Desktop
    other_files: list[str] = field(default_factory=list)
    stray_tmp: list[str] = field(default_factory=list)


@dataclass
class Account:
    id: str
    orgs: dict[str, OrgDir] = field(default_factory=dict)
    label: str | None = None
    email: str | None = None
    display_name: str | None = None
    is_active: bool = False
    paired_orgs: dict[str, str] = field(default_factory=dict)      # org_id -> evidence string

    def name(self) -> str:
        return self.label or self.email or self.id[:8]


@dataclass
class TranscriptRef:
    cli_id: str
    jsonl: list[str] = field(default_factory=list)
    sidecar_dirs: list[str] = field(default_factory=list)
    released_markers: list[str] = field(default_factory=list)


@dataclass
class LiveProc:
    pid: int
    cli_id: str
    host_session_id: str | None
    status: str | None
    entrypoint: str | None
    cwd: str | None


@dataclass
class Store:
    inst: environment.Installation
    accounts: dict[str, Account] = field(default_factory=dict)
    transcripts: dict[str, TranscriptRef] = field(default_factory=dict)
    orphan_jsonl: list[str] = field(default_factory=list)          # non-uuid .jsonl names
    live: dict[str, LiveProc] = field(default_factory=dict)        # cli_id -> proc
    active_account: str | None = None
    warnings: list[str] = field(default_factory=list)
    unknown_dirs: list[str] = field(default_factory=list)
    scanned_at: float = field(default_factory=time.time)

    # ---- lookups ----
    def all_records(self):
        for a in self.accounts.values():
            for o in a.orgs.values():
                for r in o.records:
                    yield r

    def find_account(self, ref: str) -> Account:
        """Resolve an account by full id, unique id prefix, label, email or display name."""
        from .common import ToolError
        ref_l = (ref or "").strip().lower()
        if not ref_l:
            raise ToolError("Empty account reference.")
        exact = [a for a in self.accounts.values()
                 if ref_l in (a.id.lower(), (a.label or "").lower(), (a.email or "").lower())]
        if len(exact) == 1:
            return exact[0]
        cand = [a for a in self.accounts.values()
                if a.id.lower().startswith(ref_l) or (a.email or "").lower().startswith(ref_l)
                or (a.label or "").lower().startswith(ref_l)
                or ref_l in (a.email or "").lower().split("@")[0]]
        if len(cand) == 1:
            return cand[0]
        if not cand:
            raise ToolError(f"No account matches '{ref}'. Known: " +
                            ", ".join(f"{a.id[:8]}({a.name()})" for a in self.accounts.values()))
        raise ToolError(f"Account reference '{ref}' is ambiguous: " + ", ".join(a.id for a in cand))

    def tombstoned_in_account(self, acct_id: str) -> dict[str, tuple[str, str]]:
        """stem -> (org_id, path) for every tombstone in any org dir of the account."""
        out: dict[str, tuple[str, str]] = {}
        a = self.accounts.get(acct_id)
        if not a:
            return out
        for o in a.orgs.values():
            for stem, p in o.tombstone_paths.items():
                out[stem] = (o.org_id, p)
        return out


# ---- scanning ----------------------------------------------------------------------------------

def _read_config_active(data_root: str) -> str | None:
    try:
        d = read_json(os.path.join(data_root, "config.json"))
        v = d.get("lastKnownAccountUuid")
        return v if isinstance(v, str) and is_uuid(v) else None
    except Exception:
        return None


def _read_cli_identity(claude_home: str) -> dict | None:
    """The CLI's own login (~/.claude.json oauthAccount): identity fields only, never tokens."""
    for p in (os.path.join(claude_home, ".claude.json"), os.path.join(os.path.expanduser("~"), ".claude.json")):
        try:
            d = read_json(p)
            o = d.get("oauthAccount")
            if isinstance(o, dict) and is_uuid(str(o.get("accountUuid", ""))):
                return {k: o.get(k) for k in ("accountUuid", "emailAddress", "organizationUuid", "displayName")}
        except Exception:
            continue
    return None


def scan_records(org: OrgDir) -> None:
    try:
        names = sorted(os.listdir(org.path))
    except OSError as e:
        org.other_files.append(f"<unreadable dir: {e}>")
        return
    for n in names:
        p = os.path.join(org.path, n)
        if os.path.isdir(p):
            org.other_files.append(n + "/")
            continue
        if n.startswith(TOMBSTONE_PREFIX):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as tf:
                    txt = tf.read().strip()
            except OSError:
                txt = ""
            stem = n[len(TOMBSTONE_PREFIX):]
            org.tombstones[stem] = txt
            org.tombstone_paths[stem] = p
        elif n.startswith(LOCAL_PREFIX) and n.endswith(".json"):
            try:
                st = os.stat(p)
                raw_bytes = b""
                rec = Record(org.account_id, org.org_id, p, n, st.st_size, int(st.st_mtime * 1000), "")
                if st.st_size > RECORD_MAX_BYTES:
                    rec.oversized = True
                    rec.parse_error = "record larger than 10 MB (Claude Desktop skips it)"
                else:
                    with open(p, "rb") as f:
                        raw_bytes = f.read()
                    rec.sha256 = sha256_bytes(raw_bytes)
                    try:
                        txt = raw_bytes[3:] if raw_bytes.startswith(b"\xef\xbb\xbf") else raw_bytes
                        obj = json.loads(txt.decode("utf-8"))
                        if isinstance(obj, dict):
                            rec.raw = obj
                        else:
                            rec.parse_error = "top-level JSON value is not an object"
                    except Exception as e:
                        rec.parse_error = f"invalid JSON: {e}"
                org.records.append(rec)
            except OSError as e:
                org.other_files.append(f"{n} <unreadable: {e}>")
        elif ".json.unreadable-" in n:
            org.unreadable.append(p)
        elif n.startswith(".cssync-") and n.endswith(".tmp"):
            org.stray_tmp.append(p)
        else:
            org.other_files.append(n)


def scan_transcripts(projects_root: str, store: Store) -> None:
    if not os.path.isdir(projects_root):
        return
    for proj in sorted(os.listdir(projects_root)):
        pd = os.path.join(projects_root, proj)
        if not os.path.isdir(pd):
            continue
        try:
            entries = os.listdir(pd)
        except OSError:
            continue
        for n in entries:
            p = os.path.join(pd, n)
            if n.endswith(".jsonl"):
                cid = n[:-6]
                if is_uuid(cid):
                    store.transcripts.setdefault(cid, TranscriptRef(cid)).jsonl.append(p)
                else:
                    store.orphan_jsonl.append(p)
            elif n.endswith(".desktop-released.json"):
                cid = n[:-len(".desktop-released.json")]
                if is_uuid(cid):
                    store.transcripts.setdefault(cid, TranscriptRef(cid)).released_markers.append(p)
            elif is_uuid(n) and os.path.isdir(p):
                store.transcripts.setdefault(n, TranscriptRef(n)).sidecar_dirs.append(p)


def scan_live(claude_home: str, store: Store) -> None:
    d = os.path.join(claude_home, "sessions")
    if not os.path.isdir(d):
        return
    for n in os.listdir(d):
        if not n.endswith(".json"):
            continue
        try:
            o = read_json(os.path.join(d, n))
            pid = int(o.get("pid"))
            cid = o.get("sessionId")
            if isinstance(cid, str) and winproc.pid_alive(pid, o.get("procStart")):
                store.live[cid] = LiveProc(pid, cid, o.get("hostSessionId"), o.get("status"),
                                           o.get("entrypoint"), o.get("cwd"))
        except Exception:
            continue


def scan(inst: environment.Installation, labels: dict | None = None) -> Store:
    store = Store(inst)
    labels = labels or {}
    root = inst.sessions_root
    if inst.data_root:
        store.active_account = _read_config_active(inst.data_root)
    if root and os.path.isdir(root):
        for a_name in sorted(os.listdir(root)):
            ap = os.path.join(root, a_name)
            if not os.path.isdir(ap):
                continue
            if not is_uuid(a_name):
                store.unknown_dirs.append(ap)
                continue
            acct = store.accounts.setdefault(a_name.lower(), Account(a_name.lower()))
            for o_name in sorted(os.listdir(ap)):
                op = os.path.join(ap, o_name)
                if not os.path.isdir(op):
                    continue
                if not is_uuid(o_name):
                    store.unknown_dirs.append(op)
                    continue
                org = OrgDir(a_name.lower(), o_name.lower(), op)
                scan_records(org)
                acct.orgs[o_name.lower()] = org
    # pair evidence from local-agent-mode-sessions/skills-plugin/<org>/<account>
    if inst.data_root:
        sp = os.path.join(inst.data_root, "local-agent-mode-sessions", "skills-plugin")
        if os.path.isdir(sp):
            for org_name in os.listdir(sp):
                for acct_name in (os.listdir(os.path.join(sp, org_name)) if os.path.isdir(os.path.join(sp, org_name)) else []):
                    if is_uuid(org_name) and is_uuid(acct_name) and acct_name.lower() in store.accounts:
                        store.accounts[acct_name.lower()].paired_orgs[org_name.lower()] = "skills-plugin folder"
    for acct in store.accounts.values():
        for oid, org in acct.orgs.items():
            if org.records:
                acct.paired_orgs.setdefault(oid, "has session records")
    # identity labels
    ident = _read_cli_identity(inst.claude_home or "")
    for acct in store.accounts.values():
        acct.is_active = (acct.id == store.active_account)
        lab = labels.get(acct.id) or {}
        acct.label = lab.get("label")
        acct.email = lab.get("email")
        if ident and ident["accountUuid"].lower() == acct.id:
            acct.email = acct.email or ident.get("emailAddress")
            acct.display_name = ident.get("displayName")
    if inst.projects_root:
        scan_transcripts(inst.projects_root, store)
    if inst.claude_home:
        scan_live(inst.claude_home, store)
    if inst.sessions_root and not store.accounts:
        store.warnings.append("No accounts found under claude-code-sessions.")
    return store


# ---- transcript inspection -------------------------------------------------------------------

def _tail(path: str, n: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - n))
        return f.read()


def inspect_transcript(path: str, cli_id: str, deep: bool = False, needle: str | None = None) -> dict:
    """Integrity facts about one transcript.  Never modifies it.

    quick: head/tail sample.  deep: parse every line (also searches for ``needle`` such as a message uuid).
    """
    info: dict[str, Any] = {"path": path, "ok": True, "problems": [], "size": None}
    try:
        st = os.stat(path)
        info["size"] = st.st_size
        info["mtime_ms"] = int(st.st_mtime * 1000)
        if st.st_size == 0:
            info["ok"] = False
            info["problems"].append("transcript is empty")
            return info
        ids: set = set()
        cwds: set = set()
        first_ts = last_ts = None
        bad = 0
        lines = 0
        found_needle = needle is None

        def feed(line: bytes, is_edge: bool):
            nonlocal first_ts, last_ts, bad, lines, found_needle
            if not line.strip():
                return
            lines += 1
            if needle and not found_needle and needle.encode() in line:
                found_needle = True
            try:
                o = json.loads(line)
            except Exception:
                bad += 1
                return
            if isinstance(o, dict):
                sid = o.get("sessionId")
                if isinstance(sid, str):
                    ids.add(sid)
                c = o.get("cwd")
                if isinstance(c, str):
                    cwds.add(c)
                ts = o.get("timestamp")
                if isinstance(ts, str):
                    first_ts = first_ts or ts
                    last_ts = ts

        if deep:
            with open(path, "rb") as f:
                for line in f:
                    feed(line, False)
            info["lines"] = lines
            info["bad_lines"] = bad
            if bad:
                info["ok"] = False
                info["problems"].append(f"{bad} unparsable line(s)")
        else:
            with open(path, "rb") as f:
                head = f.read(256 * 1024)
            tail = _tail(path, 256 * 1024)
            hl = head.split(b"\n")
            if st.st_size > len(head):
                hl = hl[:-1]                   # last head line may be cut
            for ln in hl:
                feed(ln, True)
            if st.st_size > 512 * 1024:
                tl = tail.split(b"\n")[1:]     # first tail line may be cut
            else:
                tl = []
            for ln in tl:
                feed(ln, True)
            # last complete line must parse
            last = [x for x in tail.split(b"\n") if x.strip()]
            if last:
                try:
                    json.loads(last[-1])
                except Exception:
                    info["ok"] = False
                    info["problems"].append("last line is not valid JSON (transcript may be mid-write or truncated)")
        info["ends_with_newline"] = _tail(path, 1) == b"\n"
        if not info["ends_with_newline"]:
            info["problems"].append("does not end with a newline (mid-write?)")
        info["session_ids"] = sorted(ids)
        info["cwds"] = sorted(cwds)
        info["first_ts"], info["last_ts"] = first_ts, last_ts
        if ids and ids != {cli_id}:
            info["ok"] = False
            info["problems"].append("contains entries for a different session id: " + ", ".join(sorted(ids - {cli_id})[:3]))
        if needle:
            info["needle_found"] = found_needle if deep else None
            if deep and not found_needle:
                info["ok"] = False
                info["problems"].append("record's lastAssistantUuid was not found in the transcript")
    except OSError as e:
        info["ok"] = False
        info["problems"].append(f"unreadable: {e}")
    return info


def iso_to_ms(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)
    except Exception:
        return None


def expected_project_dir(cwd: str | None) -> str | None:
    return encode_project_dir(cwd) if cwd else None
