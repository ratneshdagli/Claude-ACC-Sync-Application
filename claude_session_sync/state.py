"""The tool's own state: labels, sync ledger, operation log, lock, backups directory.

Default location: %USERPROFILE%\\.claude-session-sync
(deliberately NOT under %APPDATA%/%LOCALAPPDATA%: when this tool is launched from inside Claude Desktop's
package identity those folders are redirected into the app package and would be deleted on uninstall).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from .common import ToolError, atomic_write, iso_now, read_json
from .winproc import pid_alive as winproc_pid_alive


def default_state_dir() -> str:
    return os.environ.get("CLAUDE_SESSION_SYNC_HOME") or os.path.join(os.path.expanduser("~"), ".claude-session-sync")


class State:
    def __init__(self, root: str | None = None):
        self.root = root or default_state_dir()
        self.backups = os.path.join(self.root, "backups")
        self.reports = os.path.join(self.root, "reports")
        self.cache = os.path.join(self.root, "cache")
        self.config_path = os.path.join(self.root, "config.json")
        self.ledger_path = os.path.join(self.root, "ledger.json")
        self.oplog_path = os.path.join(self.root, "oplog.jsonl")
        self.lock_path = os.path.join(self.root, "lock")

    def ensure(self):
        for d in (self.root, self.backups, self.reports, self.cache):
            os.makedirs(d, exist_ok=True)

    # ---- generic json store ----
    def _load(self, path, default):
        try:
            return read_json(path)
        except FileNotFoundError:
            return default
        except Exception as e:
            raise ToolError(f"State file {path} is unreadable ({e}); fix or move it aside.")

    def _save(self, path, obj):
        self.ensure()
        atomic_write(path, json.dumps(obj, indent=1, ensure_ascii=False).encode("utf-8"), overwrite=True)

    # ---- labels ----
    def labels(self) -> dict:
        return self._load(self.config_path, {}).get("labels", {})

    def set_label(self, account_id: str, label: str | None = None, email: str | None = None):
        cfg = self._load(self.config_path, {})
        lab = cfg.setdefault("labels", {}).setdefault(account_id, {})
        if label is not None:
            lab["label"] = label
        if email is not None:
            lab["email"] = email
        self._save(self.config_path, cfg)

    def learn_identity(self, store) -> None:
        """Persist e-mail/name we can prove (CLI login identity) so it survives account switches."""
        cfg = self._load(self.config_path, {})
        changed = False
        for a in store.accounts.values():
            if a.email:
                lab = cfg.setdefault("labels", {}).setdefault(a.id, {})
                if lab.get("email") != a.email and not lab.get("email"):
                    lab["email"] = a.email
                    changed = True
        if changed:
            self._save(self.config_path, cfg)

    # ---- automatic sync: settings + watcher files ----
    AUTO_DEFAULTS = {
        "accounts": [],            # account ids mirrored among each other ([] = every account with an organisation)
        "routes": [],              # explicit one-way routes [{"from": id, "to": id}]; overrides "accounts"
        "max_age_days": 7,         # only sessions active within this many days (0 = no limit)
        "include_archived": False,
        "keep_backups": 40,        # newest automatic backups to keep (0 = keep all)
        "settle_seconds": 8,       # wait after Desktop exits before touching the store
        "poll_seconds": 3,
        "on_start": True,          # catch up when the watcher starts and Desktop is closed
        "ignore_probe": False,     # run even if the installed Claude Desktop no longer contains the expected constants
    }

    @property
    def watch_lock_path(self) -> str:
        return os.path.join(self.root, "watch.lock")

    @property
    def watch_stop_path(self) -> str:
        return os.path.join(self.root, "watch.stop")

    @property
    def watch_status_path(self) -> str:
        return os.path.join(self.root, "watch-status.json")

    @property
    def watch_log_path(self) -> str:
        return os.path.join(self.root, "watch.log")

    def auto_config(self) -> dict:
        cfg = dict(self.AUTO_DEFAULTS)
        cfg.update({k: v for k, v in self._load(self.config_path, {}).get("auto", {}).items() if k in self.AUTO_DEFAULTS})
        return cfg

    def set_auto_config(self, updates: dict) -> dict:
        cur = self._load(self.config_path, {})
        auto = dict(cur.get("auto", {}))
        for k, v in updates.items():
            if k not in self.AUTO_DEFAULTS:
                raise ToolError(f"unknown auto-sync setting '{k}'")
            d = self.AUTO_DEFAULTS[k]
            if isinstance(d, bool):
                v = bool(v)
            elif isinstance(d, int):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise ToolError(f"'{k}' must be a number")
                if v < 0 or (k in ("settle_seconds", "poll_seconds") and v < 1):
                    raise ToolError(f"'{k}' out of range")
            elif isinstance(d, list):
                if not isinstance(v, list):
                    raise ToolError(f"'{k}' must be a list")
            auto[k] = v
        cur["auto"] = auto
        self._save(self.config_path, cur)
        return self.auto_config()

    def read_status(self) -> dict:
        try:
            return read_json(self.watch_status_path)
        except Exception:
            return {}

    def write_status(self, obj: dict) -> None:
        try:
            self._save(self.watch_status_path, obj)
        except Exception:
            pass

    # ---- ledger ----
    def ledger(self) -> dict:
        return self._load(self.ledger_path, {"version": 1, "entries": {}})

    def save_ledger(self, led: dict):
        self._save(self.ledger_path, led)

    @staticmethod
    def ledger_key(src_acct, src_org, cli_id, tgt_acct, tgt_org) -> str:
        return f"{src_acct}/{src_org}/{cli_id}=>{tgt_acct}/{tgt_org}"

    # ---- op log ----
    def log(self, event: dict):
        self.ensure()
        event = {"ts": iso_now(), **event}
        with open(self.oplog_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_log(self, limit: int = 200) -> list[dict]:
        try:
            with open(self.oplog_path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
            return [json.loads(x) for x in lines if x.strip()]
        except FileNotFoundError:
            return []

    # ---- operations / backups ----
    def list_ops(self) -> list[dict]:
        out = []
        if not os.path.isdir(self.backups):
            return out
        for d in sorted(os.listdir(self.backups)):
            mp = os.path.join(self.backups, d, "manifest.json")
            if os.path.exists(mp):
                try:
                    m = read_json(mp)
                    m["_dir"] = os.path.join(self.backups, d)
                    out.append(m)
                except Exception:
                    out.append({"op_id": d, "kind": "unreadable", "_dir": os.path.join(self.backups, d)})
        return out

    # ---- lock (one writer at a time) ----
    def acquire_lock(self):
        self.ensure()
        for _ in range(2):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    json.dump({"pid": os.getpid(), "since": iso_now()}, f)
                return
            except FileExistsError:
                try:
                    info = read_json(self.lock_path)
                    if not winproc_pid_alive(int(info.get("pid", 0))):
                        os.remove(self.lock_path)      # stale lock from a crashed run
                        continue
                except Exception:
                    pass
                raise ToolError("Another claude-session-sync operation is running (lock file present: "
                                f"{self.lock_path}).")

    def release_lock(self):
        try:
            os.remove(self.lock_path)
        except OSError:
            pass
