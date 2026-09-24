import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixtures import ACCT_A, ACCT_B, ORG_A, ORG_B, Env  # noqa: E402

from claude_session_sync import analysis, executor, planner, service  # noqa: E402
from claude_session_sync.common import ToolError, atomic_write, sha256_file  # noqa: E402
from claude_session_sync.planner import Options  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cssync-test-")
        self.env = Env(self.tmp)
        self.ctx = service.make_ctx(self.env.data_root, self.env.home, self.env.state, log=lambda m: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def store(self):
        return service.load_store(self.ctx)

    def plan(self, refs=None, all_=False, **opt):
        return service.make_plan(self.ctx, self.store(), ACCT_A, ACCT_B, refs, None, all_, Options(**opt))

    def apply(self, plan, **kw):
        return executor.apply_plan(self.ctx, plan, self.store(), **kw)

    def snap(self):
        return executor.tree_snapshot(self.env.sessions), executor.tree_snapshot(self.env.projects)


class TestDiscovery(Base):
    def test_accounts_orgs_sessions(self):
        self.env.add_session(title="one")
        self.env.add_session(title="two", cwd=r"D:\Proj\Two")
        s = self.store()
        self.assertEqual(set(s.accounts), {ACCT_A, ACCT_B})
        self.assertEqual(s.active_account, ACCT_B)
        self.assertEqual(len(s.accounts[ACCT_A].orgs[ORG_A].records), 2)
        self.assertIn(ORG_B, s.accounts[ACCT_B].paired_orgs)

    def test_status_matrix(self):
        ok, ok_cli = self.env.add_session(title="ok")
        miss, _ = self.env.add_session(title="missing", transcript=False)
        dele, dele_cli = self.env.add_session(title="deleted")
        self.env.tombstone(ACCT_A, ORG_A, dele_cli)
        c1, cli = self.env.add_session(title="dup1")
        c2, _ = self.env.add_session(title="dup2", cli=cli, transcript=False)
        stale, _ = self.env.add_session(title="stale")
        rr = self.env.read_rec(ACCT_A, ORG_A, stale)
        rr["lastActivityAt"] -= 3_600_000
        self.env.write_rec(ACCT_A, ORG_A, stale, rr)
        rev, _ = self.env.add_session(title="wt", extra={"worktreePath": "C:\\x"})
        st = self.store()
        asses = {r.local_id: analysis.assess(st, r).status for r in st.all_records()}
        self.assertEqual(asses[ok], analysis.HEALTHY)
        self.assertEqual(asses[miss], analysis.MISSING)
        self.assertEqual(asses[dele], analysis.DELETED)
        self.assertEqual(asses[c1], analysis.CONFLICT)
        self.assertEqual(asses[c2], analysis.CONFLICT)
        self.assertEqual(asses[stale], analysis.STALE)
        self.assertEqual(asses[rev], analysis.REVIEW)

    def test_orphans_and_marker(self):
        _, cli = self.env.add_session()
        self.env.write_transcript("99999999-9999-4999-8999-999999999999", r"D:\Proj\One")
        marker = os.path.join(self.env.projects, "D--Proj-One", cli + ".desktop-released.json")
        with open(marker, "w") as f:
            f.write('{"v":1}')
        st = self.store()
        orph = analysis.find_orphans(st)
        self.assertEqual(len(orph["orphan_transcripts"]), 1)
        rec = st.all_records().__next__()
        self.assertEqual(analysis.assess(st, rec).status, analysis.REVIEW)

    def test_transcript_pointing_elsewhere_is_conflict(self):
        _, cli = self.env.add_session()
        p = os.path.join(self.env.projects, "D--Proj-One", cli + ".jsonl")
        with open(p, "w") as f:
            f.write(json.dumps({"sessionId": "someone-else", "cwd": r"D:\Proj\One"}) + "\n")
        st = self.store()
        self.assertEqual(analysis.assess(st, next(st.all_records())).status, analysis.CONFLICT)

    def test_truncated_transcript(self):
        _, cli = self.env.add_session()
        p = os.path.join(self.env.projects, "D--Proj-One", cli + ".jsonl")
        with open(p, "ab") as f:
            f.write(b'{"type":"user","sessionId":"' + cli.encode() + b'","tim')
        st = self.store()
        a = analysis.assess(st, next(st.all_records()))
        self.assertEqual(a.status, analysis.REVIEW)


class TestPlanNoWrite(Base):
    def test_preview_does_not_write(self):
        self.env.add_session(title="x")
        before = self.snap()
        p = self.plan(all_=True)
        self.assertEqual(p.counts(), {"create": 1})
        self.assertEqual(before, self.snap())
        self.assertFalse(os.path.exists(self.ctx.state.root) and os.listdir(self.ctx.state.backups) if os.path.exists(self.ctx.state.root) else False)


class TestSync(Base):
    def test_create_strips_org_and_runtime_and_verifies(self):
        loc, cli = self.env.add_session(title="hello")
        before_sess, before_proj = self.snap()
        res = self.apply(self.plan(["hello"]))
        self.assertEqual(res["status"], "verified")
        self.assertTrue(all(c["ok"] for c in res["verification"]))
        t = self.env.read_rec(ACCT_B, ORG_B, "local_" + cli)
        self.assertEqual(t["cliSessionId"], cli)
        self.assertEqual(t["sessionId"], "local_" + cli)
        for k in ("remoteMcpServersConfig", "enabledMcpTools", "promptAppendSnapshot", "toolSurfaceSnapshot",
                  "error", "errorAt", "steeredByRemoteClient"):
            self.assertNotIn(k, t)
        self.assertEqual(t["title"], "hello")
        after_sess, after_proj = self.snap()
        self.assertEqual(before_proj, after_proj, "transcripts / project store must be byte-identical")
        changed = {k for k in after_sess if before_sess.get(k, {}).get("sha256") != after_sess[k]["sha256"]}
        self.assertEqual(changed, {f"{ACCT_B}/{ORG_B}/local_{cli}.json"})
        # source untouched
        self.assertEqual(self.env.read_rec(ACCT_A, ORG_A, loc)["error"], "You've hit your session limit")
        # ledger
        self.assertEqual(len(self.ctx.state.ledger()["entries"]), 1)

    def test_idempotent_second_sync(self):
        self.env.add_session(title="hello")
        self.apply(self.plan(["hello"]))
        p2 = self.plan(["hello"])
        self.assertEqual(p2.counts(), {"noop": 1})
        self.assertEqual(p2.items[0].label, analysis.SYNCED)
        self.assertEqual(p2.runnable, [])

    def test_update_flow_three_way(self):
        loc, cli = self.env.add_session(title="hello")
        self.apply(self.plan(["hello"]))
        # user renames on the target; source progresses
        t = self.env.read_rec(ACCT_B, ORG_B, "local_" + cli)
        t["title"] = "renamed on B"
        self.env.write_rec(ACCT_B, ORG_B, "local_" + cli, t)
        s = self.env.read_rec(ACCT_A, ORG_A, loc)
        s["lastActivityAt"] += 600_000
        s["completedTurns"] = 9
        self.env.write_rec(ACCT_A, ORG_A, loc, s)
        p = self.plan(["hello"])
        self.assertEqual(p.counts(), {"update": 1})
        self.apply(p)
        t = self.env.read_rec(ACCT_B, ORG_B, "local_" + cli)
        self.assertEqual(t["completedTurns"], 9)
        self.assertEqual(t["lastActivityAt"], s["lastActivityAt"])
        self.assertEqual(t["title"], "renamed on B", "target-side rename must survive")
        self.assertEqual(len(os.listdir(os.path.join(self.ctx.state.backups))), 2)

    def test_target_ahead_not_overwritten(self):
        loc, cli = self.env.add_session(title="hello")
        self.apply(self.plan(["hello"]))
        t = self.env.read_rec(ACCT_B, ORG_B, "local_" + cli)
        t["lastActivityAt"] += 999_999
        t["completedTurns"] = 50
        self.env.write_rec(ACCT_B, ORG_B, "local_" + cli, t)
        p = self.plan(["hello"])
        self.assertEqual(p.counts(), {"noop": 1})
        self.assertTrue(any("AHEAD" in i for i in p.items[0].infos))
        self.assertEqual(self.plan(["hello"], force_update=True).counts(), {"update": 1})

    def test_lineage_cli_id_moves_forward(self):
        loc, cli = self.env.add_session(title="hello")
        self.apply(self.plan(["hello"]))
        new_cli = "33333333-3333-4333-8333-333333333333"
        self.env.write_transcript(new_cli, r"D:\Proj\One", 6, "aaaaaaaa-0000-4000-8000-000000000001")
        s = self.env.read_rec(ACCT_A, ORG_A, loc)
        s.update({"cliSessionId": new_cli, "priorCliSessionIds": [cli], "lastActivityAt": s["lastActivityAt"] + 1000})
        self.env.write_rec(ACCT_A, ORG_A, loc, s)
        p = self.plan(["hello"])
        self.assertEqual(p.counts(), {"update": 1})
        self.apply(p)
        t = self.env.read_rec(ACCT_B, ORG_B, "local_" + cli)
        self.assertEqual(t["cliSessionId"], new_cli)
        self.assertEqual(t["priorCliSessionIds"], [cli])
        # transcripts still exist, both
        self.assertTrue(os.path.exists(os.path.join(self.env.projects, "D--Proj-One", cli + ".jsonl")))

    def test_diverged_lineage_is_conflict(self):
        loc, cli = self.env.add_session(title="hello")
        other, ocli = self.env.add_session(acct=ACCT_B, org=ORG_B, title="hello", cli=None,
                                           local="local_" + cli)   # same file name, different conversation
        p = self.plan(["hello"])
        self.assertEqual(p.items[0].action, planner.CONFLICT_)
        self.assertEqual(p.runnable, [])

    def test_name_collision_new_id_option(self):
        loc, cli = self.env.add_session(title="hello")
        self.env.add_session(acct=ACCT_B, org=ORG_B, title="other", local="local_" + cli)
        p = self.plan(["hello"], new_id_on_collision=True)
        self.assertEqual(p.counts(), {"create": 1})
        self.assertNotEqual(p.items[0].target_local_id, "local_" + cli)

    def test_tombstone_on_target_blocks_and_resurrect(self):
        loc, cli = self.env.add_session(title="hello")
        self.env.tombstone(ACCT_B, ORG_B, cli)
        p = self.plan(["hello"])
        self.assertEqual(p.items[0].label, analysis.DELETED)
        self.assertEqual(p.runnable, [])
        p2 = self.plan(["hello"], resurrect=True)
        self.assertEqual(p2.counts(), {"create": 1})
        self.apply(p2)
        self.assertFalse(os.path.exists(os.path.join(self.env.org_dir(ACCT_B, ORG_B), "deleted_" + cli)))
        # rollback restores tombstone and removes record
        op = executor.rollback(self.ctx, None, self.store())
        self.assertTrue(op["ok"])
        self.assertTrue(os.path.exists(os.path.join(self.env.org_dir(ACCT_B, ORG_B), "deleted_" + cli)))

    def test_source_tombstoned_blocked(self):
        loc, cli = self.env.add_session(title="hello")
        self.env.tombstone(ACCT_A, ORG_A, cli)
        self.assertEqual(self.plan(["hello"]).items[0].label, analysis.DELETED)

    def test_missing_transcript_blocked(self):
        self.env.add_session(title="hello", transcript=False)
        self.assertEqual(self.plan(["hello"]).items[0].label, analysis.MISSING)

    def test_blocker_fields_blocked(self):
        self.env.add_session(title="hello", extra={"sshConfig": {"host": "x"}})
        self.assertEqual(self.plan(["hello"]).runnable, [])

    def test_unknown_fields_dropped_unless_asked(self):
        loc, cli = self.env.add_session(title="hello", extra={"futureField": 1})
        p = self.plan(["hello"])
        self.assertIn("futureField", p.items[0].dropped["unknown"])
        self.assertNotIn("futureField", p.items[0].new_raw)
        p2 = self.plan(["hello"], carry_unknown=True)
        self.assertIn("futureField", p2.items[0].new_raw)

    def test_permission_grants_warned_and_droppable(self):
        self.env.add_session(title="hello", extra={"alwaysAllowedReasons": ["Bash\u0000x"],
                             "sessionPermissionUpdates": [{"type": "addDirectories", "directories": ["D:/x"]}]})
        p = self.plan(["hello"])
        self.assertTrue(any("permission settings travel" in w for w in p.items[0].warnings))
        self.assertIn("alwaysAllowedReasons", p.items[0].new_raw)
        p2 = self.plan(["hello"], drop_permission_grants=True)
        self.assertNotIn("alwaysAllowedReasons", p2.items[0].new_raw)
        self.assertEqual(p2.items[0].new_raw["permissionMode"], "bypassPermissions")

    def test_released_marker_blocks_until_cleared(self):
        loc, cli = self.env.add_session(title="hello")
        m = os.path.join(self.env.projects, "D--Proj-One", cli + ".desktop-released.json")
        with open(m, "w") as fh:
            fh.write('{"v":1}')
        self.assertEqual(self.plan(["hello"]).runnable, [])
        p = self.plan(["hello"], clear_released_marker=True)
        self.assertEqual(len(p.runnable), 1)
        self.apply(p)
        self.assertFalse(os.path.exists(m))
        executor.rollback(self.ctx, None, self.store())
        self.assertTrue(os.path.exists(m))

    def test_source_target_same_and_org_resolution(self):
        self.env.add_session(title="hello")
        with self.assertRaises(ToolError):
            service.make_plan(self.ctx, self.store(), ACCT_A, ACCT_A, ["hello"])
        # target account with no evidence at all
        shutil.rmtree(os.path.join(self.env.data_root, "local-agent-mode-sessions"))
        p = self.plan(["hello"])
        self.assertTrue(p.blockers)

    def test_multiple_orgs_require_choice(self):
        self.env.add_session(title="hello")
        o2 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        os.makedirs(os.path.join(self.env.sessions, ACCT_B, o2))
        os.makedirs(os.path.join(self.env.data_root, "local-agent-mode-sessions", "skills-plugin", o2, ACCT_B))
        self.assertTrue(self.plan(["hello"]).blockers)
        self.assertEqual(self.plan(["hello"], target_org=ORG_B[:8]).counts(), {"create": 1})

    def test_project_selection_and_all(self):
        self.env.add_session(title="a", cwd=r"D:\Proj\One")
        self.env.add_session(title="b", cwd=r"D:\Proj\One")
        self.env.add_session(title="c", cwd=r"D:\Proj\Two")
        st = self.store()
        recs = planner.select_sessions(st, ACCT_A, projects=[r"d:\proj\one"])
        self.assertEqual(len(recs), 2)
        self.assertEqual(len(planner.select_sessions(st, ACCT_A, select_all=True)), 3)


class TestSafety(Base):
    def test_rollback_restores_exact_state(self):
        self.env.add_session(title="hello")
        before = self.snap()
        self.apply(self.plan(["hello"]))
        self.assertNotEqual(before, self.snap())
        r = executor.rollback(self.ctx, None, self.store())
        self.assertTrue(r["ok"], r)
        self.assertEqual(before, self.snap())
        self.assertEqual(self.ctx.state.ledger()["entries"], {})

    def test_rollback_refuses_modified_target(self):
        loc, cli = self.env.add_session(title="hello")
        self.apply(self.plan(["hello"]))
        t = self.env.read_rec(ACCT_B, ORG_B, "local_" + cli)
        t["title"] = "user edit"
        self.env.write_rec(ACCT_B, ORG_B, "local_" + cli, t)
        r = executor.rollback(self.ctx, None, self.store())
        self.assertFalse(r["ok"])
        self.assertTrue(os.path.exists(os.path.join(self.env.org_dir(ACCT_B, ORG_B), "local_" + cli + ".json")))
        r = executor.rollback(self.ctx, None, self.store(), force=True)
        self.assertTrue(r["ok"])

    def test_precondition_changed_between_preview_and_apply(self):
        loc, cli = self.env.add_session(title="hello")
        p = self.plan(["hello"])
        s = self.env.read_rec(ACCT_A, ORG_A, loc)
        s["title"] = "changed"
        self.env.write_rec(ACCT_A, ORG_A, loc, s)
        with self.assertRaises(ToolError):
            self.apply(p)

    def test_failed_write_auto_restores(self):
        loc, cli = self.env.add_session(title="hello")
        before = self.snap()
        p = self.plan(["hello"])
        real = executor.atomic_write

        def boom(path, data, **kw):
            if str(path).endswith(".json") and "local_" + cli in str(path):
                real(path, data[:-5], **kw)          # corrupt the write
                return
            return real(path, data, **kw)

        with mock.patch.object(executor, "atomic_write", boom):
            with self.assertRaises(ToolError):
                self.apply(p)
        self.assertEqual(before, self.snap(), "auto-rollback must restore the exact pre-state")

    def test_unexpected_extra_change_fails_verification(self):
        loc, cli = self.env.add_session(title="hello")
        p = self.plan(["hello"])
        real_do = executor._do_ops

        def sneaky(ctx, manifest, ops):
            real_do(ctx, manifest, ops)
            with open(os.path.join(self.env.org_dir(ACCT_B, ORG_B), "local_rogue.json"), "w") as fh:
                fh.write("{}")

        before = self.snap()
        with mock.patch.object(executor, "_do_ops", sneaky):
            with self.assertRaises(ToolError):
                self.apply(p)
        # rogue file is not ours to delete, but our record must be gone
        self.assertFalse(os.path.exists(os.path.join(self.env.org_dir(ACCT_B, ORG_B), "local_" + cli + ".json")))

    def test_desktop_gate_blocks_when_running(self):
        self.env.add_session(title="hello")
        p = self.plan(["hello"])
        with mock.patch.object(executor.Ctx, "is_live", lambda self: True), \
                mock.patch.object(executor.desktop, "desktop_processes", lambda inst: [object()]):
            with self.assertRaises(ToolError) as cm:
                self.apply(p)
        self.assertIn("Claude Desktop is running", str(cm.exception))
        self.assertEqual(os.listdir(self.env.org_dir(ACCT_B, ORG_B)), [])

    def test_atomic_write_no_clobber(self):
        f = os.path.join(self.tmp, "x.json")
        atomic_write(f, b"1", overwrite=False)
        with self.assertRaises(FileExistsError):
            atomic_write(f, b"2", overwrite=False)
        atomic_write(f, b"3", overwrite=True)
        with open(f, "rb") as fh:
            self.assertEqual(fh.read(), b"3")
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith(".cssync")], [])

    def test_standalone_backup_and_restore(self):
        loc, cli = self.env.add_session(title="hello")
        r = executor.standalone_backup(self.ctx, self.store(), include_transcripts=True)
        self.assertGreaterEqual(r["transcript_files"], 2)
        p = os.path.join(self.env.org_dir(ACCT_A, ORG_A), loc + ".json")
        os.remove(p)
        res = executor.restore_backup(self.ctx, r["op_id"], self.store())
        self.assertEqual(len(res["restored"]), 1)
        self.assertTrue(os.path.exists(p))

    def test_verify_store_and_report(self):
        self.env.add_session(title="hello")
        self.apply(self.plan(["hello"]))
        rep = executor.verify_store(self.ctx, self.store(), deep=True)
        self.assertEqual(rep["counts"], {"Healthy": 2})
        self.assertEqual(len(rep["shared"]), 1)
        d = service.diagnostic_report(self.ctx, self.store(), redact=True)
        self.assertNotIn("hello", json.dumps(d))
        self.assertIn("field_key_counts", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestEnvironmentAndProcesses(Base):
    def test_win32_install_detection_fallback(self):
        from claude_session_sync import environment
        la = os.path.join(self.tmp, "LocalAppData")
        os.makedirs(os.path.join(la, "AnthropicClaude", "app-1.2.3"))
        ap = os.path.join(self.tmp, "AppData")
        os.makedirs(os.path.join(ap, "Claude", "claude-code-sessions"))
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": la, "APPDATA": ap}), \
                mock.patch.object(environment, "_find_msix", lambda: None):
            inst = environment.detect()
        self.assertEqual(inst.kind, "win32")
        self.assertEqual(inst.version, "1.2.3")
        self.assertEqual(inst.data_root, os.path.join(ap, "Claude"))

    def test_account_reference_resolution(self):
        self.ctx.state.set_label(ACCT_A, "work", "a@example.com")
        st = self.store()
        self.assertEqual(st.find_account("work").id, ACCT_A)
        self.assertEqual(st.find_account("a@example.com").id, ACCT_A)
        self.assertEqual(st.find_account(ACCT_B[:6]).id, ACCT_B)
        with self.assertRaises(ToolError):
            st.find_account("zzz")

    def test_live_session_blocks_sync(self):
        from claude_session_sync import winproc
        loc, cli = self.env.add_session(title="hello")
        me = next(p for p in winproc.list_processes() if p.pid == os.getpid())
        with open(os.path.join(self.env.home, "sessions", f"{me.pid}.json"), "w") as f:
            json.dump({"pid": me.pid, "sessionId": cli, "procStart": str(me.start_ft), "status": "busy",
                       "entrypoint": "cli", "cwd": r"D:\Proj\One"}, f)
        st = self.store()
        self.assertIn(cli, st.live)
        p = self.plan(["hello"])
        self.assertEqual(p.runnable, [])
        self.assertTrue(any("running right now" in r for r in p.items[0].reasons))
        # stale registry entry (pid reused / process gone) is ignored
        with open(os.path.join(self.env.home, "sessions", f"{me.pid}.json"), "w") as f:
            json.dump({"pid": me.pid, "sessionId": cli, "procStart": "1"}, f)
        self.assertNotIn(cli, self.store().live)

    def test_cli_binary_is_not_desktop(self):
        from claude_session_sync import desktop, environment
        inst = environment.Installation(kind="msix", install_location=r"C:\Program Files\WindowsApps\Claude_1_x64__abc",
                                        exe_path=r"C:\Program Files\WindowsApps\Claude_1_x64__abc\app\claude.exe")
        self.assertTrue(desktop._is_desktop_exe(inst, inst.exe_path))
        self.assertFalse(desktop._is_desktop_exe(inst, r"C:\Users\x\AppData\Roaming\Claude\claude-code\2.1.280\claude.exe"))
        self.assertFalse(desktop._is_desktop_exe(inst, r"C:\Windows\notepad.exe"))

    def test_unsafe_allow_running_only_for_inactive_target(self):
        self.env.add_session(title="hello")
        p = self.plan(["hello"])                 # target = ACCT_B = the signed-in account in the fixture
        with mock.patch.object(executor.Ctx, "is_live", lambda self: True), \
                mock.patch.object(executor.desktop, "desktop_processes", lambda inst: [object()]):
            with self.assertRaises(ToolError):
                self.apply(p, allow_running=True)


class TestSchemaProbe(unittest.TestCase):
    def test_probe_reads_real_asar_format(self):
        import struct
        from claude_session_sync import schema
        d = tempfile.mkdtemp()
        try:
            js = b'x="claude-code-sessions";y="local_";z="deleted_";".desktop-released.json";"archived-sessions.idx";cliSessionId;priorCliSessionIds'
            hdr = json.dumps({"files": {"a.js": {"size": len(js), "offset": "0"}}}).encode()
            os.makedirs(os.path.join(d, "app", "resources"))
            with open(os.path.join(d, "app", "resources", "app.asar"), "wb") as f:
                f.write(struct.pack("<IIII", 4, len(hdr) + 8, len(hdr) + 4, len(hdr)) + hdr + js)
            r = schema.probe_app(d, "9.9.9", None)
            self.assertEqual(r.status, "verified", r)
            js2 = js.replace(b"deleted_", b"gone____")
            hdr = json.dumps({"files": {"a.js": {"size": len(js2), "offset": "0"}}}).encode()
            with open(os.path.join(d, "app", "resources", "app.asar"), "wb") as f:
                f.write(struct.pack("<IIII", 4, len(hdr) + 8, len(hdr) + 4, len(hdr)) + hdr + js2)
            r = schema.probe_app(d, "9.9.10", None)
            self.assertEqual(r.status, "partial")
            self.assertIn("tombstone prefix", r.missing)
        finally:
            shutil.rmtree(d, ignore_errors=True)
