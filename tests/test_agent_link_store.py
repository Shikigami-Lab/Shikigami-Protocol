"""Offline linkage contracts: explicit temporary DBs, synthetic IDs and results."""

import importlib.util
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor


SOURCE = Path(__file__).resolve().parents[1] / "src" / "agent" / "link_store.py"


def load_store():
    # Do not import the application (or its package initialization) in storage tests.
    spec = importlib.util.spec_from_file_location("isolated_link_store", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LinkStore


class LinkStoreTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SOURCE.is_file(), "durable LinkStore implementation is missing")
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-agent-links-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "links.sqlite3"
        self.cls = load_store()
        self.store = self.open_store()

    def open_store(self):
        store = self.cls(self.path)
        self.addCleanup(store.close)
        return store

    def begin(self, run="r1", profile="p1"):
        return self.store.begin_run(run, profile, "session-" + run)

    def race(self, functions):
        barrier = threading.Barrier(len(functions))

        def invoke(function):
            barrier.wait(timeout=10)
            try:
                return function()
            except ValueError as error:
                return error

        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(invoke, functions))

    def test_run_lifecycle_and_profile_scoping_survive_reopen(self):
        self.assertEqual(self.store.revision("p1"), 0)
        run = self.begin()
        self.assertEqual((run["run_id"], run["revision"], run["state"]), ("r1", 0, "running"))
        self.assertIsNone(self.store.get_run("r1", "p2"))
        self.assertIsNone(self.store.get_run("unknown"))
        self.store.finish_run("r1", "completed")
        self.store.close()
        reopened = self.open_store()
        self.assertEqual(reopened.get_run("r1")["state"], "completed")
        self.assertTrue(reopened.is_current("r1", "p1"))
        self.assertFalse(reopened.is_current("r1", "p2"))
        self.assertEqual(reopened.list_runs("p2"), [])

    def test_duplicate_run_and_busy_profile_rejected_without_affecting_other_profile(self):
        self.begin()
        for run, profile in [("r1", "p2"), ("r2", "p1")]:
            with self.assertRaises(ValueError):
                self.begin(run, profile)
        self.begin("r3", "p2")
        self.store.finish_run("r1", "cancelled")
        self.begin("r2")
        with self.assertRaises(ValueError):
            self.begin("r1", "p3")

    def test_two_connections_racing_profile_start_have_one_winner(self):
        other = self.open_store()
        results = self.race([
            lambda: self.begin(),
            lambda: other.begin_run("r2", "p1", "s2"),
        ])
        self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
        self.assertEqual(len(self.store.list_runs("p1")), 1)

    def test_event_sequence_dedup_native_identity_and_decoded_list(self):
        self.begin()
        event = {"type": "tool_result", "native_event_id": "native-1",
                 "tool_name": "timer", "result": {"timer_id": "synthetic-t1"}}
        first = self.store.append_event("r1", event)
        self.assertEqual(first["seq"], 1)
        self.assertEqual(self.store.append_event("r1", dict(event)), first)
        with self.assertRaises(ValueError):
            self.store.append_event("r1", dict(event, result={"timer_id": "different"}))
        self.assertEqual(self.store.append_event("r1", {"type": "status", "status": "ready"})["seq"], 2)
        self.store.finish_run("r1", "completed")
        self.begin("r2")
        runs = self.store.list_runs("p1")
        self.assertEqual([run["run_id"] for run in runs], ["r2", "r1"])
        self.assertEqual(runs[1]["events"][0]["result"], {"timer_id": "synthetic-t1"})
        self.assertEqual(len(self.store.list_runs("p1", 1)), 1)
        self.assertEqual(self.store.list_runs("p1", 0), [])

    def test_parallel_events_are_monotonic_across_connections(self):
        self.begin()
        other = self.open_store()
        functions = [lambda i=i: (self.store if i % 2 else other).append_event(
            "r1", {"type": "status", "event_id": f"event-{i}", "status": "running"})
            for i in range(16)]
        results = self.race(functions)
        self.assertEqual(sorted(event["seq"] for event in results), list(range(1, 17)))

    def test_invalidate_tombstones_all_old_runs_and_blocks_late_writes_after_restart(self):
        self.begin("done")
        self.store.finish_run("done", "completed")
        self.begin()
        self.begin("other", "p2")
        self.store.enqueue_result("p1", "r1", "e1", {"timer_id": "t1"})
        revision, active = self.store.invalidate("p1")
        self.assertEqual((revision, active), (1, ["r1"]))
        self.store.close()
        store = self.open_store()
        for run in ["done", "r1"]:
            self.assertTrue(store.get_run(run)["stale"])
            self.assertFalse(store.is_current(run, "p1"))
        self.assertTrue(store.is_current("other", "p2"))
        self.assertEqual(store.peek_results("p1", 10), [])
        for operation in [
            lambda: store.append_event("r1", {"type": "status"}),
            lambda: store.claim_action("r1", "c1", "timer", {}),
            lambda: store.finish_run("r1", "completed"),
            lambda: store.enqueue_result("p1", "r1", "e2", {}),
        ]:
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(store.begin_run("new", "p1", "new-session")["revision"], 1)

    def test_start_invalidate_race_never_leaves_current_run_on_old_revision(self):
        other = self.open_store()
        self.race([lambda: self.begin(), lambda: other.invalidate("p1")])
        run = self.store.get_run("r1")
        self.assertEqual(self.store.revision("p1"), 1)
        if self.store.is_current("r1", "p1"):
            self.assertEqual(run["revision"], 1)
        else:
            self.assertTrue(run["stale"])

    def test_stale_session_cannot_be_rebound_to_new_run_or_another_profile(self):
        self.begin()
        self.store.finish_run("r1", "completed")
        with self.assertRaises(ValueError):
            self.store.begin_run("cross-profile", "p2", "session-r1")
        # Same-profile continuation is permitted only while revision is current.
        self.store.begin_run("continuation", "p1", "session-r1")
        self.store.invalidate("p1")
        self.store.close()
        store = self.open_store()
        for profile in ["p1", "p2"]:
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                store.begin_run("new-run", profile, "session-r1")
        self.assertIsNone(store.get_run("new-run"))
        self.assertEqual(store.begin_run("fresh", "p1", "new-session")["revision"], 1)

    def test_finish_racing_invalidate_cannot_revive_a_stale_run(self):
        self.begin()
        other = self.open_store()
        self.race([lambda: self.store.finish_run("r1", "completed"), lambda: other.invalidate("p1")])
        self.assertTrue(self.store.get_run("r1")["stale"])
        self.assertFalse(other.is_current("r1", "p1"))

    def test_late_known_action_result_is_recorded_but_cannot_publish_after_invalidation(self):
        self.begin()
        self.store.claim_action("r1", "c1", "timer", {})
        self.store.invalidate("p1")
        self.assertEqual(self.store.complete_action("r1", "c1", {"timer_id": "late"})["status"], "done")
        with self.assertRaises(ValueError):
            self.store.enqueue_result("p1", "r1", "late", {"timer_id": "late"})
        with self.assertRaises(ValueError):
            self.store.claim_action("r1", "c1", "timer", {})

    def test_recovery_is_explicit_fails_interrupted_runs_and_preserves_unknown_receipts(self):
        self.begin()
        self.store.claim_action("r1", "call1", "timer", {"seconds": 5})
        self.store.close()
        store = self.open_store()
        self.assertEqual(store.get_run("r1")["state"], "running")
        self.assertEqual(store.recover_interrupted(), ["r1"])
        self.assertEqual(store.get_run("r1")["state"], "failed")
        self.assertEqual(store.recover_interrupted(), [])
        recovered = store.peek_results('p1')
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]['payload']['result']['state'], 'failed')
        self.assertNotIn('model_requests', recovered[0]['payload']['result'])
        self.assertEqual(store.claim_action("r1", "call1", "timer", {"seconds": 5})["status"], "pending")
        with self.assertRaises(ValueError):
            store.claim_action("r1", "call2", "timer", {})
        store.begin_run("r2", "p1", "s2")

    def test_receipt_duplicate_command_race_and_canonical_arguments(self):
        self.begin()
        other = self.open_store()
        results = self.race([
            lambda: self.store.claim_action("r1", "c1", "timer", {"seconds": 5, "label": "test"}),
            lambda: other.claim_action("r1", "c1", "timer", {"label": "test", "seconds": 5}),
        ])
        self.assertEqual(sorted(item["status"] for item in results), ["new", "pending"])
        for tool, args in [("timer", {"seconds": 6}), ("different", {"seconds": 5, "label": "test"})]:
            with self.assertRaises(ValueError):
                other.claim_action("r1", "c1", tool, args)
        result = {"timer_id": "native-timer-1", "seconds": 5}
        self.store.complete_action("r1", "c1", result)
        other.complete_action("r1", "c1", result)
        self.store.close()
        self.assertEqual(self.open_store().claim_action(
            "r1", "c1", "timer", {"seconds": 5, "label": "test"}), {"status": "done", "result": result})

    def test_failed_receipts_never_authorize_replay_and_success_cannot_be_overwritten(self):
        self.begin()
        self.store.claim_action("r1", "c1", "timer", {})
        self.store.fail_action("r1", "c1", "transport disconnected; effect unknown")
        self.assertEqual(self.store.claim_action("r1", "c1", "timer", {})["status"], "pending")
        self.store.complete_action("r1", "c1", {"timer_id": "t1"})
        with self.assertRaises(ValueError):
            self.store.complete_action("r1", "c1", {"timer_id": "t2"})
        with self.assertRaises(ValueError):
            self.store.fail_action("r1", "c1", "late error")
        with self.assertRaises(ValueError):
            self.store.complete_action("r1", "missing", {})
        self.begin("r2", "p2")
        self.assertEqual(self.store.claim_action("r2", "c1", "timer", {})["status"], "new")

    def test_find_timer_run_after_outbox_ack_and_restart_is_read_only(self):
        self.begin()
        result = {"timer_id": "synthetic-expiry-timer", "seconds": 5}
        self.store.claim_action("r1", "create", "timer_create", {"seconds": 5})
        self.store.complete_action("r1", "create", result)
        row_id = self.store.enqueue_result("p1", "r1", "created", result)
        self.store.finish_run("r1", "completed")
        self.store.acknowledge_results([row_id])
        self.store.close()
        store = self.open_store()
        self.assertEqual(store.peek_results("p1", 10), [])
        before = self.path.read_bytes()
        self.assertEqual(store.find_timer_run("p1", result["timer_id"]), "r1")
        self.assertEqual(store.find_timer_run("p1", result["timer_id"]), "r1")
        self.assertIsNone(store.find_timer_run("p1", "unknown-timer"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_find_timer_run_is_profile_scoped_and_stale_after_restart(self):
        self.begin()
        self.store.claim_action("r1", "create", "timer_create", {})
        self.store.complete_action("r1", "create", {"timer_id": "shared-synthetic-id"})
        self.assertIsNone(self.store.find_timer_run("p2", "shared-synthetic-id"))
        self.begin("r2", "p2")
        self.store.claim_action("r2", "create", "timer_create", {})
        self.store.complete_action("r2", "create", {"timer_id": "shared-synthetic-id"})
        self.assertEqual(self.store.find_timer_run("p1", "shared-synthetic-id"), "r1")
        self.assertEqual(self.store.find_timer_run("p2", "shared-synthetic-id"), "r2")
        self.store.invalidate("p1")
        self.store.close()
        store = self.open_store()
        store.begin_run("new-revision", "p1", "fresh-session")
        self.assertIsNone(store.find_timer_run("p1", "shared-synthetic-id"))
        self.assertEqual(store.find_timer_run("p2", "shared-synthetic-id"), "r2")

    def test_find_timer_run_requires_done_timer_create_receipt(self):
        self.begin()
        self.store.claim_action("r1", "pending", "timer_create", {"timer_id": "pending-timer"})
        self.store.fail_action("r1", "pending", "effect unknown")
        self.store.enqueue_result("p1", "r1", "outbox-only", {"timer_id": "outbox-timer"})
        self.store.claim_action("r1", "other-tool", "timer_cancel", {})
        self.store.complete_action("r1", "other-tool", {"timer_id": "cancelled-timer"})
        for index, result in enumerate([None, "scalar", [], {}, {"result": {"timer_id": "nested-timer"}}]):
            call_id = f"nonmatching-{index}"
            self.store.claim_action("r1", call_id, "timer_create", {})
            self.store.complete_action("r1", call_id, result)
        for timer_id in ["pending-timer", "outbox-timer", "cancelled-timer", "nested-timer"]:
            with self.subTest(timer_id=timer_id):
                self.assertIsNone(self.store.find_timer_run("p1", timer_id))
        for bad in [None, "", " ", "x" * 257, "bad\x00id", 3]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.store.find_timer_run("p1", bad)
                with self.assertRaises(ValueError):
                    self.store.find_timer_run(bad, "timer")

    def test_outbox_is_read_only_until_ack_deduplicated_and_survives_restart(self):
        self.begin()
        payload = {"tool_name": "timer", "result": {"timer_id": "t1"}}
        first_id = self.store.enqueue_result("p1", "r1", "native-result-1", payload)
        self.assertEqual(self.store.enqueue_result("p1", "r1", "native-result-1", payload), first_id)
        with self.assertRaises(ValueError):
            self.store.enqueue_result("p1", "r1", "native-result-1", {"different": True})
        with self.assertRaises(ValueError):
            self.store.enqueue_result("p2", "r1", "wrong-profile", payload)
        expected = [{"id": first_id, "payload": payload}]
        before = self.path.read_bytes()
        self.assertEqual(self.store.peek_results("p1", 10), expected)
        self.assertEqual(self.store.peek_results("p1", 10), expected)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.peek_results("p2", 10), [])
        self.store.close()
        store = self.open_store()
        self.assertEqual(store.peek_results("p1", 10), expected)
        store.acknowledge_results([first_id])
        store.acknowledge_results([first_id])
        store.enqueue_result("p1", "r1", "native-result-1", payload)
        self.assertEqual(store.peek_results("p1", 10), [])

    def test_outbox_parallel_duplicate_delivery_has_one_row(self):
        self.begin()
        other = self.open_store()
        ids = self.race([lambda: self.store.enqueue_result("p1", "r1", "e1", {"ok": True}),
                         lambda: other.enqueue_result("p1", "r1", "e1", {"ok": True})])
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(len(self.store.peek_results("p1", 10)), 1)

    def test_shared_store_serializes_duplicate_action_claims(self):
        self.begin()
        results = self.race([lambda: self.store.claim_action("r1", "same-call", "timer", {})
                             for _ in range(12)])
        self.assertEqual(sum(result["status"] == "new" for result in results), 1)
        self.assertEqual(sum(result["status"] == "pending" for result in results), 11)

    def test_event_duplicate_race_aliases_and_restart(self):
        self.begin()
        other = self.open_store()
        results = self.race([
            lambda: self.store.append_event("r1", {"type": "status", "id": "same"}),
            lambda: other.append_event("r1", {"type": "status", "event_id": "same"}),
        ])
        self.assertEqual(results[0], results[1])
        self.store.close()
        store = self.open_store()
        store.finish_run("r1", "completed")
        self.assertEqual(store.append_event("r1", {"type": "status", "native_event_id": "same"}), results[0])
        self.assertEqual(store.get_run("r1")["last_seq"], 1)

    def test_event_window_bounded_without_forgetting_old_native_ids(self):
        self.begin()
        for index in range(105):
            self.store.append_event("r1", {"type": "status", "id": str(index)})
        run = self.store.get_run("r1")
        self.assertEqual(len(run["events"]), 100)
        self.assertEqual((run["events"][0]["seq"], run["last_seq"]), (6, 105))
        self.assertEqual(self.store.append_event("r1", {"type": "status", "id": "0"})["seq"], 1)
        self.assertEqual(self.store.append_event("r1", {"type": "status"})["seq"], 106)

    def test_reads_do_not_create_profiles_and_ack_validation_is_atomic(self):
        self.assertEqual(self.store.revision("not-created"), 0)
        self.assertEqual(self.store.peek_results("not-created", 10), [])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 0)
        self.begin()
        row_id = self.store.enqueue_result("p1", "r1", "e1", {"ok": True})
        with self.assertRaises(ValueError):
            self.store.acknowledge_results([row_id, "invalid"])
        self.assertEqual(len(self.store.peek_results("p1", 10)), 1)
        for bad in [-1, 101, True, "10"]:
            for operation in [self.store.peek_results, self.store.list_runs]:
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    operation("p1", bad)

    def test_only_argument_fingerprint_persists_and_invalid_receipt_data_does_not_mutate(self):
        self.begin()
        args = {"seconds": 5, "label": "synthetic-private-argument-marker"}
        self.store.claim_action("r1", "c1", "timer", args)
        for operation in [
            lambda: self.store.complete_action("r1", "c1", {"api_key": "fake"}),
            lambda: self.store.fail_action("r1", "c1", "Bearer synthetic-credential"),
            lambda: self.store.finish_run("r1", "failed", "api_key=synthetic-credential"),
            lambda: self.store.claim_action("r1", "c2", "timer", {"apiKey": "fake"}),
        ]:
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(self.store.claim_action("r1", "c1", "timer", args), {"status": "pending"})
        self.assertEqual(self.store.claim_action("r1", "c2", "timer", {}), {"status": "new"})
        self.assertEqual(self.store.get_run("r1")["state"], "running")
        with closing(sqlite3.connect(self.path)) as db:
            dump = "\n".join(db.iterdump())
        self.assertNotIn(args["label"], dump)
        self.assertNotIn("synthetic-credential", dump)

    def test_json_byte_node_and_depth_limits_leave_no_outbox_rows(self):
        self.begin()
        deep = {"leaf": True}
        for _ in range(10):
            deep = {"nested": deep}
        for payload in [deep, {"items": [0] * 513}, {"items": ["字" * 2000] * 4}]:
            with self.assertRaises(ValueError):
                self.store.enqueue_result("p1", "r1", "invalid", payload)
        self.assertEqual(self.store.peek_results("p1", 10), [])
        # Native timer result remains a small, decoded JSON object.
        result = {"timer_id": "fixture-t1", "seconds": 5, "active": True}
        row = self.store.enqueue_result("p1", "r1", "native-timer-result", result)
        self.assertEqual(self.store.peek_results("p1", 10), [{"id": row, "payload": result}])

    def test_ids_are_opaque_sql_parameters_not_paths(self):
        opaque = "../角色'; DROP TABLE runs;--"
        self.store.begin_run(opaque, opaque, opaque)
        self.assertEqual(self.store.get_run(opaque)["session_id"], opaque)
        self.assertTrue(self.store.is_current(opaque, opaque))
        for bad in [None, "", "   ", "x" * 257, "bad\x00id", 3]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.revision(bad)

    def test_json_is_bounded_and_rejects_secrets_raw_chat_and_nonfinite_numbers(self):
        self.begin()
        cycle = {}
        cycle["self"] = cycle
        invalid = [{"api_key": "synthetic-secret"}, {"nested": {"Authorization": "synthetic-secret"}},
                   {"messages": [{"role": "user", "content": "raw chat"}]},
                   {"summary": "x" * 20000}, {"n": float("nan")}, {"n": float("inf")},
                   {1: "non-string-key"}, {"unsupported": object()}, cycle]
        for payload in invalid:
            with self.subTest(payload_type=type(payload).__name__), self.assertRaises(ValueError):
                self.store.enqueue_result("p1", "r1", "invalid", payload)
        with self.assertRaises(ValueError):
            self.store.append_event("r1", {"type": "reply", "text": "raw model reply"})
        with self.assertRaises(ValueError):
            self.store.append_event("r1", {"type": "status", "seq": 9000})
        self.assertEqual(self.store.append_event("r1", {"type": "status"})["seq"], 1)
        self.assertEqual(self.store.peek_results("p1", 10), [])

    def test_terminal_state_validation_and_close_are_idempotent(self):
        self.begin()
        with self.assertRaises(ValueError):
            self.store.finish_run("r1", "running")
        self.store.finish_run("r1", "failed", "synthetic failure")
        self.store.finish_run("r1", "failed", "synthetic failure")
        with self.assertRaises(ValueError):
            self.store.finish_run("r1", "completed")
        with self.assertRaises(ValueError):
            self.store.append_event("r1", {"type": "status"})
        self.store.close()
        self.store.close()
        with self.assertRaises(RuntimeError):
            self.store.revision("p1")

    def test_module_import_has_no_files_or_application_imports(self):
        script = ("import importlib.util, pathlib, sys; "
                  f"s=importlib.util.spec_from_file_location('store', {str(SOURCE)!r}); "
                  "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                  "assert not list(pathlib.Path('.').iterdir()); "
                  "assert 'server' not in sys.modules and 'src.core' not in sys.modules")
        with tempfile.TemporaryDirectory(prefix="synthetic-link-import-") as directory:
            result = subprocess.run([sys.executable, "-B", "-c", script], cwd=directory,
                                    capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_atomic_action_publish_survives_restart_and_acknowledged_retry(self):
        self.begin(profile="bound-profile")
        self.store.claim_action("r1", "c1", "timer_create", {})
        result = {"timer_id": "synthetic-t1"}
        receipt = self.store.complete_action("r1", "c1", result, publish_result=True)
        self.assertEqual(receipt, {"status": "done", "result": result})
        expected = self.store.peek_results("bound-profile")
        self.assertEqual(len(expected), 1)
        self.assertEqual(expected[0]["payload"], {"tool": "timer_create", "result": result})
        self.assertEqual(self.store.peek_results("p1"), [])
        self.store.close()
        store = self.open_store()
        self.assertEqual(store.peek_results("bound-profile"), expected)
        self.assertEqual(store.complete_action("r1", "c1", result, publish_result=True), receipt)
        self.assertEqual(store.peek_results("bound-profile"), expected)
        self.assertEqual(store.enqueue_result(
            "bound-profile", "r1", "c1", expected[0]["payload"]), expected[0]["id"])
        store.acknowledge_results([expected[0]["id"]])
        store.complete_action("r1", "c1", result, publish_result=True)
        self.assertEqual(store.peek_results("bound-profile"), [])
        with self.assertRaisesRegex(ValueError, "action result cannot be changed"):
            store.complete_action("r1", "c1", {"timer_id": "changed"}, publish_result=True)

    def test_atomic_action_insert_failure_rolls_back_receipt_and_retries_after_restart(self):
        self.begin()
        self.store.claim_action("r1", "c1", "timer_create", {})
        self.store.fail_action("r1", "c1", "effect uncertain")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TRIGGER reject_outbox BEFORE INSERT ON outbox
                BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected outbox failure"):
            self.store.complete_action("r1", "c1", {"ok": True}, publish_result=True)
        self.store.close()
        store = self.open_store()
        self.assertEqual(store.claim_action("r1", "c1", "timer_create", {}),
                         {"status": "pending", "error": "effect uncertain"})
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNone(db.execute("SELECT result FROM actions").fetchone()[0])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
            db.execute("DROP TRIGGER reject_outbox")
        store.complete_action("r1", "c1", {"ok": True}, publish_result=True)
        self.assertEqual(len(store.peek_results("p1")), 1)

    def test_atomic_action_existing_outbox_conflict_rolls_back_receipt(self):
        self.begin()
        self.store.claim_action("r1", "c1", "timer", {})
        self.store.enqueue_result("p1", "r1", "c1", {"different": True})
        before = self.store.peek_results("p1")
        with self.assertRaisesRegex(ValueError, "event key reused"):
            self.store.complete_action("r1", "c1", {"ok": True}, publish_result=True)
        self.assertEqual(self.store.claim_action("r1", "c1", "timer", {}), {"status": "pending"})
        self.assertEqual(self.store.peek_results("p1"), before)

    def test_atomic_action_can_publish_existing_done_or_late_terminal_receipt(self):
        self.begin()
        for call in ["already-done", "late"]:
            self.store.claim_action("r1", call, "timer", {})
        self.store.complete_action("r1", "already-done", None)
        self.store.finish_run("r1", "cancelled")
        self.assertEqual(self.store.peek_results("p1"), [])
        for call in ["already-done", "late"]:
            self.store.complete_action("r1", call, None, publish_result=True)
        self.assertEqual([item["payload"] for item in self.store.peek_results("p1")],
                         [{"tool": "timer", "result": None}] * 2)

    def test_atomic_publish_rejects_stale_run_but_default_allows_late_receipt(self):
        self.begin()
        self.store.claim_action("r1", "c1", "timer", {})
        self.store.invalidate("p1")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.complete_action("r1", "c1", {}, publish_result=True)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.finish_run("r1", "cancelled", publish_result=True)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM actions").fetchone()[0], "pending")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        self.assertEqual(self.store.complete_action("r1", "c1", {}),
                         {"status": "done", "result": {}})
        self.assertEqual(self.store.peek_results("p1"), [])

    def test_atomic_finish_payloads_survive_restart_and_acknowledged_retry(self):
        for state, error, count in [("completed", None, 0), ("cancelled", None, 2),
                                    ("failed", "synthetic failure", 3)]:
            with self.subTest(state=state):
                self.store.begin_run(state, state, "session-" + state, origin="reflection")
                kwargs = {"publish_result": True, "model_requests": count}
                run = self.store.finish_run(state, state, error, **kwargs)
                self.assertEqual((run["state"], run["error"]), (state, error))
                result = {"state": state, "model_requests": count}
                if error is not None:
                    result["error"] = error
                expected = self.store.peek_results(state)
                self.assertEqual(len(expected), 1)
                self.assertEqual(expected[0]["payload"], {"tool": "agent_task", "result": result})
                self.assertEqual(self.store.peek_results("other-profile"), [])
                self.store.close()
                self.store = self.open_store()
                self.assertEqual(self.store.get_run(state), run)
                self.assertEqual(self.store.peek_results(state), expected)
                self.assertEqual(self.store.finish_run(state, state, error, **kwargs), run)
                self.assertEqual(self.store.enqueue_result(
                    state, state, "task-finished", expected[0]["payload"]), expected[0]["id"])
                self.store.acknowledge_results([expected[0]["id"]])
                self.store.finish_run(state, state, error, **kwargs)
                self.assertEqual(self.store.peek_results(state), [])
                with self.assertRaisesRegex(ValueError, "event key reused"):
                    self.store.finish_run(state, state, error, publish_result=True, model_requests=count + 1)
                with self.assertRaisesRegex(ValueError, "terminal outcome cannot be changed"):
                    self.store.finish_run(state, state, "different error", **kwargs)

    def test_atomic_finish_insert_failure_rolls_back_run_and_retries_after_restart(self):
        before = self.begin()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TRIGGER reject_outbox BEFORE INSERT ON outbox
                BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected outbox failure"):
            self.store.finish_run("r1", "failed", "synthetic failure", publish_result=True, model_requests=4)
        self.store.close()
        store = self.open_store()
        self.assertEqual(store.get_run("r1"), before)
        self.assertEqual(store.peek_results("p1"), [])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
            db.execute("DROP TRIGGER reject_outbox")
        store.finish_run("r1", "failed", "synthetic failure", publish_result=True, model_requests=4)
        self.assertEqual(store.get_run("r1")["state"], "failed")
        self.assertEqual(len(store.peek_results("p1")), 1)

    def test_atomic_finish_existing_outbox_conflict_rolls_back_run(self):
        before = self.begin()
        self.store.enqueue_result("p1", "r1", "task-finished", {"different": True})
        outbox = self.store.peek_results("p1")
        with self.assertRaisesRegex(ValueError, "event key reused"):
            self.store.finish_run("r1", "completed", publish_result=True)
        self.assertEqual(self.store.get_run("r1"), before)
        self.assertEqual(self.store.peek_results("p1"), outbox)

    def test_finish_defaults_do_not_publish_and_can_later_publish_same_outcome(self):
        self.begin()
        before = self.store.finish_run("r1", "completed")
        self.assertEqual(self.store.peek_results("p1"), [])
        self.assertEqual(self.store.finish_run("r1", "completed", publish_result=False), before)
        self.assertEqual(self.store.peek_results("p1"), [])
        self.assertEqual(self.store.finish_run("r1", "completed", publish_result=True), before)
        self.assertEqual(self.store.peek_results("p1")[0]["payload"],
                         {"tool": "agent_task", "result": {"state": "completed", "model_requests": 0}})

    def test_model_request_count_rejects_noninteger_or_negative_without_mutation(self):
        before = self.begin()
        for count in [-1, True, False, 1.5, "2", None]:
            for publish in [False, True]:
                with self.subTest(count=count, publish=publish), self.assertRaises(ValueError):
                    self.store.finish_run("r1", "completed", publish_result=publish, model_requests=count)
        self.assertEqual(self.store.get_run("r1"), before)
        self.assertEqual(self.store.peek_results("p1"), [])

    def test_atomic_publish_keeps_bounded_summary_validation(self):
        before = self.begin()
        self.store.claim_action("r1", "c1", "timer", {})
        for result in [{"messages": []}, {"summary": "Bearer synthetic-credential"},
                       {"items": [0] * 509}]:
            with self.subTest(result=result), self.assertRaises(ValueError):
                self.store.complete_action("r1", "c1", result, publish_result=True)
        with self.assertRaises(ValueError):
            self.store.finish_run("r1", "failed", "Bearer synthetic-credential", publish_result=True)
        self.assertEqual(self.store.claim_action("r1", "c1", "timer", {}), {"status": "pending"})
        self.assertEqual(self.store.get_run("r1"), before)
        self.assertEqual(self.store.peek_results("p1"), [])
        # The existing standalone receipt budget remains unchanged.
        self.store.complete_action("r1", "c1", {"items": [0] * 509})

    def test_atomic_duplicate_completions_across_connections_keep_one_outbox_row(self):
        self.begin()
        self.store.claim_action("r1", "c1", "timer", {})
        other = self.open_store()
        results = self.race([
            lambda store=store: store.complete_action("r1", "c1", {}, publish_result=True)
            for store in [self.store, other]])
        self.assertEqual(results, [{"status": "done", "result": {}}] * 2)
        results = self.race([
            lambda store=store: store.finish_run("r1", "completed", publish_result=True)
            for store in [self.store, other]])
        self.assertTrue(all(isinstance(result, dict) for result in results))
        self.assertEqual(len(self.store.peek_results("p1")), 2)


if __name__ == "__main__":
    unittest.main()
