"""Tests for automatic mirroring + the Desktop-exit watcher (sandbox only; the real store is never touched)."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixtures import ACCT_A, ACCT_B, ORG_A, ORG_B, Env  # noqa: E402

from claude_session_sync import autosync, executor, schema, service  # noqa: E402
from claude_session_sync.common import ToolError, sha256_file  # noqa: E402

DAY = 86_400_000


class AutoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cssync-auto-")
        self.env = Env(self.tmp)
        self.ctx = service.make_ctx(self.env.data_root, self.env.home, self.env.state, log=lambda m: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_auto(self, **kw):
        return autosync.run_once(self.ctx, log=lambda m: None, **kw)

    def b_records(self):
        d = self.env.org_dir(ACCT_B, ORG_B)
        return sorted(f for f in os.listdir(d) if f.startswith("local_") and f.endswith(".json"))

    def a_records(self):
        d = self.env.org_dir(ACCT_A, ORG_A)
        return sorted(f for f in os.listdir(d) if f.startswith("local_") and f.endswith(".json"))

    def hashes(self, root):
        return executor.tree_snapshot(root)


class TestRunOnce(AutoBase):
    def test_mirrors_desktop_sessions_and_never_touches_transcripts(self):
        local, cli = self.env.add_session(title="Desktop one")
        self.env.write_transcript("99999999-9999-4999-8999-999999999999", r"D:\Proj\TerminalOnly")   # terminal-only
        before_tx = self.hashes(self.env.projects)
        res = self.run_auto()
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(res["created"], 1)
        self.assertEqual(self.b_records(), [f"local_{cli}.json"])          # only the Desktop session, none for the terminal one
        self.assertEqual(self.hashes(self.env.projects), before_tx)         # transcripts byte-identical
        rec = self.env.read_rec(ACCT_B, ORG_B, f"local_{cli}")
        self.assertEqual(rec["cliSessionId"], cli)
        for k in ("error", "remoteMcpServersConfig", "enabledMcpTools", "promptAppendSnapshot"):
            self.assertNotIn(k, rec)                                       # nothing account-specific carried over

    def test_second_run_is_a_noop_and_converges_both_ways(self):
        _, cli = self.env.add_session(title="S")
        self.assertEqual(self.run_auto()["created"], 1)
        snap = self.hashes(self.env.sessions)
        ops_before = len(self.ctx.state.list_ops())
        res = self.run_auto()
        self.assertEqual((res["created"], res["updated"], res["status"]), (0, 0, "ok"), res)
        self.assertEqual(self.hashes(self.env.sessions), snap)
        self.assertEqual(len(self.ctx.state.list_ops()), ops_before)        # no backup when nothing changes

        # progress made under B flows back to A, exactly once
        rec = self.env.read_rec(ACCT_B, ORG_B, f"local_{cli}")
        rec["lastActivityAt"] += 120_000
        rec["completedTurns"] = 9
        self.env.write_rec(ACCT_B, ORG_B, f"local_{cli}", rec)
        res = self.run_auto()
        self.assertEqual(res["updated"], 1, res)
        a_local = self.a_records()[0][:-5]
        a = self.env.read_rec(ACCT_A, ORG_A, a_local)
        self.assertEqual(a["completedTurns"], 9)
        self.assertIn("remoteMcpServersConfig", a)                         # A keeps its own connector snapshot
        snap = self.hashes(self.env.sessions)
        res = self.run_auto()
        self.assertEqual((res["created"], res["updated"]), (0, 0), res)
        self.assertEqual(self.hashes(self.env.sessions), snap)

    def test_target_deletion_is_respected(self):
        _, cli = self.env.add_session()
        self.env.tombstone(ACCT_B, ORG_B, cli)
        res = self.run_auto()
        self.assertEqual(res["created"], 0)
        self.assertEqual(self.b_records(), [])
        skipped = [s for r in res["routes"] for s in r["skipped"]]
        self.assertTrue(any("Deleted" in s["why"] for s in skipped), skipped)

    def test_age_window_and_archived(self):
        self.env.add_session(title="old", last=int(time.time() * 1000) - 30 * DAY)
        self.env.add_session(title="archived", archived=True)
        _, fresh = self.env.add_session(title="fresh")
        self.ctx.state.set_auto_config({"max_age_days": 7})
        self.run_auto()
        self.assertEqual(self.b_records(), [f"local_{fresh}.json"])
        self.ctx.state.set_auto_config({"max_age_days": 0, "include_archived": True})
        self.run_auto()
        self.assertEqual(len(self.b_records()), 3)

    def test_one_way_route_only_writes_target(self):
        self.env.add_session(acct=ACCT_A, org=ORG_A, title="from A")
        self.env.add_session(acct=ACCT_B, org=ORG_B, title="from B")
        self.ctx.state.set_auto_config({"routes": [{"from": ACCT_A, "to": ACCT_B}]})
        res = self.run_auto()
        self.assertEqual(res["created"], 1)
        self.assertEqual(len(self.a_records()), 1)                         # A untouched
        self.assertEqual(len(self.b_records()), 2)

    def test_dry_run_writes_nothing(self):
        self.env.add_session()
        snap = self.hashes(self.env.sessions)
        res = self.run_auto(dry_run=True)
        self.assertEqual(sum(len(r["created"]) for r in res["routes"]), 1)
        self.assertEqual(self.hashes(self.env.sessions), snap)
        self.assertEqual(self.ctx.state.list_ops(), [])


class TestSafetyGates(AutoBase):
    def test_refuses_while_desktop_runs(self):
        self.env.add_session()
        snap = self.hashes(self.env.sessions)
        self.ctx.is_live = lambda: True
        with mock.patch.object(autosync.desktop, "desktop_processes", return_value=[object()]):
            res = self.run_auto()
        self.assertEqual(res["status"], "skipped")
        self.assertIn("running", res["reason"])
        self.assertEqual(self.hashes(self.env.sessions), snap)

    def test_refuses_when_started_from_inside_desktop(self):
        self.env.add_session()
        self.ctx.is_live = lambda: True
        with mock.patch.object(autosync.desktop, "desktop_processes", return_value=[]), \
                mock.patch.object(autosync.desktop, "running_inside_desktop", return_value=True):
            res = self.run_auto()
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(self.b_records(), [])

    def test_refuses_when_installed_app_format_changed(self):
        self.env.add_session()
        self.ctx.is_live = lambda: True
        pr = schema.ProbeResult(status="partial", app_version="9.9", missing=["record prefix"])
        with mock.patch.object(autosync.desktop, "desktop_processes", return_value=[]), \
                mock.patch.object(autosync.desktop, "running_inside_desktop", return_value=False), \
                mock.patch.object(autosync.schema, "probe_app", return_value=pr):
            res = self.run_auto()
            self.assertEqual(res["status"], "skipped")
            self.assertIn("record prefix", res["reason"])
            self.assertEqual(self.b_records(), [])
            self.ctx.state.set_auto_config({"ignore_probe": True})
            res = self.run_auto()
        self.assertEqual(res["created"], 1)

    def test_config_validation(self):
        with self.assertRaises(ToolError):
            self.ctx.state.set_auto_config({"nonsense": 1})
        with self.assertRaises(ToolError):
            self.ctx.state.set_auto_config({"max_age_days": "abc"})
        with self.assertRaises(ToolError):
            self.ctx.state.set_auto_config({"poll_seconds": 0})
        self.assertEqual(self.ctx.state.set_auto_config({"max_age_days": "3"})["max_age_days"], 3)


class TestRetention(AutoBase):
    def test_only_auto_backups_are_pruned(self):
        # manual sync first (must survive pruning), then several automatic ones
        _, c0 = self.env.add_session(title="manual")
        plan = service.make_plan(self.ctx, service.load_store(self.ctx), ACCT_A, ACCT_B, [c0], None, False)
        executor.apply_plan(self.ctx, plan, service.load_store(self.ctx))            # origin "" = manual
        for i in range(4):
            self.env.add_session(title=f"auto {i}")
            time.sleep(1.1)                                                            # op ids are second-stamped
            self.run_auto()
        ops = self.ctx.state.list_ops()
        auto = [m for m in ops if m.get("origin") == "auto"]
        self.assertEqual(len(auto), 4)
        removed = executor.prune_auto_backups(self.ctx, 2)
        self.assertEqual(len(removed), 2)
        left = self.ctx.state.list_ops()
        self.assertEqual(len([m for m in left if m.get("origin") == "auto"]), 2)
        self.assertEqual(len([m for m in left if m.get("origin") == ""]), 1)          # manual op untouched
        self.assertEqual(executor.prune_auto_backups(self.ctx, 0), [])                # 0 = keep everything

    def test_rollback_of_an_automatic_operation_is_exact(self):
        self.env.add_session()
        before = self.hashes(self.env.sessions)
        self.run_auto()
        self.assertNotEqual(self.hashes(self.env.sessions), before)
        r = executor.rollback(self.ctx, None, service.load_store(self.ctx))
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.hashes(self.env.sessions), before)


class TestWatcher(AutoBase):
    def _start(self, running):
        self.stop = threading.Event()
        self.ctx.state.set_auto_config({"poll_seconds": 1, "settle_seconds": 1})
        self.t = threading.Thread(target=autosync.watch, kwargs=dict(
            ctx=self.ctx, log=lambda m: None, stop=self.stop, max_seconds=60, desktop_running=lambda: running["v"]),
            daemon=True)
        self.t.start()

    def _wait(self, cond, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.2)
        return False

    def test_syncs_after_desktop_closes_not_while_open(self):
        running = {"v": True}
        _, cli = self.env.add_session(title="made in A")
        self._start(running)
        try:
            self.assertTrue(self._wait(lambda: autosync.watcher_running(self.ctx.state) is not None))
            time.sleep(3)                                                   # Desktop "open": nothing may happen
            self.assertEqual(self.b_records(), [])
            running["v"] = False                                            # user quits Desktop
            self.assertTrue(self._wait(lambda: self.b_records() == [f"local_{cli}.json"]), "no sync after Desktop closed")
            st = self.ctx.state.read_status()
            self.assertTrue(self._wait(lambda: (self.ctx.state.read_status().get("last_run") or {}).get("created") == 1))
            # a second watcher must refuse to start
            with self.assertRaises(ToolError):
                autosync.watch(self.ctx, log=lambda m: None, stop=threading.Event(), max_seconds=1,
                               desktop_running=lambda: False)
            # Desktop opens again, more work happens in B, Desktop closes -> flows back to A
            running["v"] = True
            time.sleep(2)
            rec = self.env.read_rec(ACCT_B, ORG_B, f"local_{cli}")
            rec["lastActivityAt"] += 90_000
            rec["completedTurns"] = 12
            self.env.write_rec(ACCT_B, ORG_B, f"local_{cli}", rec)
            running["v"] = False
            a_local = self.a_records()[0][:-5]
            self.assertTrue(self._wait(lambda: self.env.read_rec(ACCT_A, ORG_A, a_local)["completedTurns"] == 12),
                            "progress did not flow back")
        finally:
            r = autosync.stop_watcher(self.ctx, wait=10)
            self.stop.set()
            self.t.join(timeout=10)
        self.assertTrue(r["stopped"])
        self.assertIsNone(autosync.watcher_running(self.ctx.state))
        self.assertFalse(os.path.exists(self.ctx.state.watch_lock_path))

    def test_desktop_reopening_cancels_a_pending_sync(self):
        running = {"v": True}
        self.env.add_session()
        self.ctx.state.set_auto_config({"poll_seconds": 1, "settle_seconds": 4})
        stop = threading.Event()
        t = threading.Thread(target=autosync.watch, kwargs=dict(
            ctx=self.ctx, log=lambda m: None, stop=stop, max_seconds=30, desktop_running=lambda: running["v"]), daemon=True)
        t.start()
        try:
            time.sleep(2)
            running["v"] = False
            time.sleep(1.5)                                                 # inside the settle window
            running["v"] = True                                             # Desktop came back
            time.sleep(6)
            self.assertEqual(self.b_records(), [])
        finally:
            stop.set()
            t.join(timeout=10)


class TestAutostart(AutoBase):
    def test_refused_inside_desktop(self):
        with mock.patch.object(autosync.desktop, "running_inside_desktop", return_value=True):
            with self.assertRaises(ToolError):
                autosync.autostart_set(self.ctx, True)
            with self.assertRaises(ToolError):
                autosync.start_watcher(self.ctx)

    @unittest.skipUnless(sys.platform == "win32", "Windows registry")
    def test_registry_roundtrip(self):
        with mock.patch.object(autosync, "RUN_VALUE", "ClaudeSessionSyncTest"), \
                mock.patch.object(autosync.desktop, "running_inside_desktop", return_value=False):
            try:
                r = autosync.autostart_set(self.ctx, True)
                self.assertTrue(r["enabled"])
                self.assertIn("watch", autosync.autostart_get())
                self.assertIn("--quiet", autosync.autostart_get())
            finally:
                autosync.autostart_set(self.ctx, False)
            self.assertIsNone(autosync.autostart_get())

    def test_watcher_command_is_windowless_python_or_exe(self):
        argv = autosync.watcher_argv("watch", "--quiet")
        self.assertTrue(argv[0].lower().endswith((".exe",)))
        self.assertIn("watch", " ".join(argv))


if __name__ == "__main__":
    unittest.main()
