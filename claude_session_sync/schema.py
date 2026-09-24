"""Field classification for Claude Desktop's per-account session records, plus a probe that checks the
installed app still uses the storage conventions this tool relies on.

Classification was derived from (a) all records found on the reference machine and (b) the shipped
Claude Desktop 2.7032 main-process bundle (SessionTombstones, session index scan, importCliSession,
delete-session teardown).  It is deliberately conservative:

CARRY    conversation identity / continuity data.  Copied to the target record.
ORG      per-account / per-organisation snapshots (connector lists, enabled connector tools, prompt/tool
         surface snapshots).  Never copied: they describe the *source* account's org and Claude Desktop
         regenerates them for the target account on the next turn.
RUNTIME  transient run-state (errors such as "You've hit your session limit", remote-control links...).
         Never copied: copying "limit reached" onto the target would make it look failed.
BLOCKER  fields that make a record not a plain local session (worktrees, SSH/WSL, scheduled tasks,
         parent/child links, imported staging).  The session is reported as "Needs review" and skipped.
Anything else is UNKNOWN: dropped from the copy (and listed) unless --carry-unknown is given.
"""
from __future__ import annotations

import json
import os
import re
import struct
from dataclasses import dataclass, field

CARRY = [
    "cliSessionId", "priorCliSessionIds", "unarchivedCliSessionId", "preClearCliSessionId",
    "cwd", "originCwd", "additionalDirectories",
    "createdAt", "lastActivityAt", "lastFocusedAt", "latestUserFrameAt",
    "model", "effort", "effortInherited", "agent", "thinkingSummariesWanted", "sessionSettings",
    "isArchived", "isStarred", "title", "titleSource", "titleTurn",
    "permissionMode", "bypassChosenInApp", "autoChosenInApp", "chromePermissionMode",
    "alwaysAllowedReasons", "sessionPermissionUpdates",
    "classifierSummaryEnabled", "reportFindingsCard", "spawnSeed",
    "completedTurns", "lastAssistantUuid", "lastSpawnRootDetected",
    "rewindEdges", "transcriptModelStates", "writtenBranches",
    "devIntents", "devIntentTriggers",
]

ORG = [
    "remoteMcpServersConfig", "enabledMcpTools", "promptAppendSnapshot", "toolSurfaceSnapshot",
    "withheldConnectorHosts", "remoteMcpServers",
]

RUNTIME = [
    "error", "errorAt", "errorCategory", "priorErrorMark", "processGoneReason",
    "bridgeSessionIds", "remoteControlAutoEligible", "steeredByRemoteClient", "hasLiveProcess",
    "pendingFirstStart", "autoArchiveHold", "manualKeepAwake", "indexedAt",
]

BLOCKERS = {
    "worktreePath": "uses an app-managed git worktree (worktree leases are per session id)",
    "worktreeName": "uses an app-managed git worktree",
    "worktreeLazy": "uses an app-managed git worktree",
    "keptWorktreeLeftover": "has a kept worktree leftover",
    "sshConfig": "is an SSH-remote session (transcript is not local)",
    "wslConfig": "is a WSL session (transcript is not local)",
    "scheduledTaskId": "was spawned by a scheduled task",
    "spawnedFrom": "is a child of another session (parent link is account-local)",
    "remoteControlSpawn": "is a remote-control spawned session",
    "importedFrom": "is an imported session (Desktop may reap its staged transcript)",
    "stagedTranscriptPath": "uses a Desktop-staged transcript",
    "violinBow": "uses a special session backend",
}

# Fields that legitimately differ between two copies of the same conversation without meaning
# "the conversation moved on"; ignored when deciding whether a target is in sync.
VOLATILE = {"lastFocusedAt", "latestUserFrameAt"}

# Progress fields: on update these follow the side that is *ahead*.
PROGRESS = ["cliSessionId", "priorCliSessionIds", "lastActivityAt", "completedTurns", "lastAssistantUuid",
            "rewindEdges", "transcriptModelStates", "writtenBranches", "unarchivedCliSessionId",
            "preClearCliSessionId"]

IMMUTABLE = {"createdAt", "cwd", "originCwd"}  # differing values = a different conversation

REQUIRED = ["sessionId", "cliSessionId", "cwd"]


def classify_keys(keys) -> dict[str, list[str]]:
    out = {"carry": [], "org": [], "runtime": [], "blocker": [], "unknown": []}
    for k in keys:
        if k == "sessionId":
            continue
        if k in CARRY:
            out["carry"].append(k)
        elif k in ORG:
            out["org"].append(k)
        elif k in RUNTIME:
            out["runtime"].append(k)
        elif k in BLOCKERS:
            out["blocker"].append(k)
        else:
            out["unknown"].append(k)
    return out


# ---- probing the installed application -------------------------------------------------------

MARKERS = {
    "session folder name": b'"claude-code-sessions"',
    "record prefix": b'"local_"',
    "tombstone prefix": b'"deleted_"',
    "released marker": b".desktop-released.json",
    "archived index": b"archived-sessions.idx",
    "cliSessionId field": b"cliSessionId",
    "priorCliSessionIds field": b"priorCliSessionIds",
}


@dataclass
class ProbeResult:
    status: str = "unverified"        # verified | partial | unverified
    app_version: str | None = None
    found: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)
    detail: str = ""


def probe_app(install_location: str | None, app_version: str | None, cache_dir: str | None = None) -> ProbeResult:
    """Scan the app's JavaScript bundle (read-only) for the storage constants this tool depends on."""
    pr = ProbeResult(app_version=app_version)
    if cache_dir and app_version:
        cp = os.path.join(cache_dir, f"probe-{app_version}.json")
        try:
            with open(cp, "r", encoding="utf-8") as f:
                d = json.load(f)
            return ProbeResult(**d)
        except Exception:
            pass
    if not install_location:
        pr.detail = "installation folder unknown"
        return pr
    asar = os.path.join(install_location, "app", "resources", "app.asar")
    if not os.path.exists(asar):
        pr.detail = "app.asar not found / not readable"
        return pr
    try:
        with open(asar, "rb") as f:
            f.read(4)
            hs, = struct.unpack("<I", f.read(4))
            f.read(4)
            jl, = struct.unpack("<I", f.read(4))
            hdr = json.loads(f.read(jl))
            base = 8 + hs
            targets = []

            def walk(n, path=""):
                for k, v in n.get("files", {}).items():
                    q = path + "/" + k
                    if "files" in v:
                        walk(v, q)
                    elif q.endswith((".js", ".cjs", ".mjs")) and "node_modules" not in q and "offset" in v:
                        targets.append((v["offset"], v["size"]))

            walk(hdr)
            found = {k: 0 for k in MARKERS}
            for off, size in targets:
                if size > 64 * 1024 * 1024:
                    continue
                f.seek(base + int(off))
                data = f.read(size)
                for k, pat in MARKERS.items():
                    if pat in data:
                        found[k] += 1
        pr.found = {k: v for k, v in found.items() if v}
        pr.missing = [k for k, v in found.items() if not v]
        pr.status = "verified" if not pr.missing else ("partial" if pr.found else "unverified")
        pr.detail = ("all storage conventions present in the installed app" if not pr.missing
                     else "missing in app bundle: " + ", ".join(pr.missing))
    except Exception as e:  # unreadable asar etc.
        pr.detail = f"could not read app bundle: {type(e).__name__}: {e}"
        return pr
    if cache_dir and app_version and pr.status != "unverified":
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(os.path.join(cache_dir, f"probe-{app_version}.json"), "w", encoding="utf-8") as f:
                json.dump(pr.__dict__, f)
        except OSError:
            pass
    return pr
