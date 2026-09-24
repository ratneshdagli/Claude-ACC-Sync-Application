"""Builds a fake Claude Desktop data folder + Claude Code projects folder for tests."""
from __future__ import annotations

import json
import os
import time
import uuid

from claude_session_sync.common import encode_project_dir

ACCT_A = "11111111-1111-4111-8111-111111111111"
ACCT_B = "22222222-2222-4222-8222-222222222222"
ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ORG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class Env:
    def __init__(self, tmp: str):
        self.tmp = tmp
        self.data_root = os.path.join(tmp, "data")
        self.home = os.path.join(tmp, "claude_home")
        self.state = os.path.join(tmp, "state")
        self.sessions = os.path.join(self.data_root, "claude-code-sessions")
        self.projects = os.path.join(self.home, "projects")
        os.makedirs(self.sessions)
        os.makedirs(self.projects)
        os.makedirs(os.path.join(self.home, "sessions"))
        # both accounts exist; B has been used once (skills-plugin pairing evidence)
        for org, acct in ((ORG_A, ACCT_A), (ORG_B, ACCT_B)):
            os.makedirs(os.path.join(self.data_root, "local-agent-mode-sessions", "skills-plugin", org, acct))
            os.makedirs(os.path.join(self.sessions, acct, org))
        with open(os.path.join(self.data_root, "config.json"), "w") as f:
            json.dump({"lastKnownAccountUuid": ACCT_B}, f)

    def org_dir(self, acct, org):
        return os.path.join(self.sessions, acct, org)

    def add_session(self, acct=ACCT_A, org=ORG_A, cwd=r"D:\Proj\One", title="My session", cli=None, local=None,
                    transcript=True, lines=5, extra=None, archived=False, last=None):
        cli = cli or str(uuid.uuid4())
        local = local or "local_" + str(uuid.uuid4())
        now = last or int(time.time() * 1000) - 3_600_000
        rec = {
            "sessionId": local, "cliSessionId": cli, "cwd": cwd, "originCwd": cwd,
            "lastFocusedAt": now + 5, "createdAt": now - 100_000, "lastActivityAt": now,
            "model": "claude-opus-4-8", "effort": "medium", "isArchived": archived, "title": title,
            "titleSource": "auto", "permissionMode": "bypassPermissions", "completedTurns": 4,
            "lastAssistantUuid": "aaaaaaaa-0000-4000-8000-%012d" % 1,
            "error": "You've hit your session limit", "errorAt": now,
            "remoteMcpServersConfig": [{"uuid": "x", "name": "Gmail", "url": "https://x", "tools": []}],
            "enabledMcpTools": {"conn:tool": True},
            "promptAppendSnapshot": {"append": "hello", "cliVersion": "2.1.0", "settingsKey": "k"},
            "toolSurfaceSnapshot": {"appVersion": "1"}, "steeredByRemoteClient": False,
            "alwaysAllowedReasons": [], "sessionPermissionUpdates": [],
        }
        rec.update(extra or {})
        with open(os.path.join(self.org_dir(acct, org), local + ".json"), "wb") as f:
            f.write(json.dumps(rec, separators=(",", ":")).encode())
        if transcript:
            self.write_transcript(cli, cwd, lines, rec["lastAssistantUuid"], now)
        return local, cli

    def write_transcript(self, cli, cwd, lines=5, last_uuid=None, ts_ms=None):
        d = os.path.join(self.projects, encode_project_dir(cwd))
        os.makedirs(d, exist_ok=True)
        ts_ms = ts_ms or int(time.time() * 1000) - 3_600_000
        out = []
        for i in range(lines):
            iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts_ms / 1000 - (lines - i)))
            out.append(json.dumps({"type": "user" if i % 2 == 0 else "assistant", "sessionId": cli, "cwd": cwd,
                                   "timestamp": iso, "uuid": last_uuid if i == lines - 1 else str(uuid.uuid4()),
                                   "message": {"content": "hi %d" % i}}))
        p = os.path.join(d, cli + ".jsonl")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(out) + "\n")
        os.makedirs(os.path.join(d, cli, "subagents"), exist_ok=True)
        with open(os.path.join(d, cli, "subagents", "agent-1.meta.json"), "w") as f:
            f.write("{}")
        return p

    def tombstone(self, acct, org, stem, ts="1790101904359"):
        with open(os.path.join(self.org_dir(acct, org), "deleted_" + stem), "w") as f:
            f.write(ts)

    def read_rec(self, acct, org, local):
        with open(os.path.join(self.org_dir(acct, org), local + ".json"), "rb") as f:
            return json.loads(f.read())

    def write_rec(self, acct, org, local, obj):
        with open(os.path.join(self.org_dir(acct, org), local + ".json"), "wb") as f:
            f.write(json.dumps(obj, separators=(",", ":")).encode())
