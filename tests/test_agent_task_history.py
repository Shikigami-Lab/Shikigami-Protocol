"""Offline provenance contracts; all persistence uses synthetic temporary data."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from src.agent.link_store import LinkStore
from src.memory.conversation_store import ConversationStore
from src.llm.message_normalization import normalize_messages
from src.prompt.segments.history import HistorySegment, _result_with_time_sources


class TaskHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-task-history-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def chat(self):
        store = ConversationStore(str(self.root), max_turns=0)
        self.addCleanup(store.close)
        return store

    def links(self):
        store = LinkStore(self.root / "links.db")
        self.addCleanup(store.close)
        return store

    def test_search_tasks_by_original_request_and_artifact_within_current_profile(self):
        from src.agent.task_search import TaskSearchArgs, search_tasks

        links = self.links()
        history = self.chat()
        session = SimpleNamespace(profile_id="alpha", id="alpha", storage_root=self.root, _store=history)

        def finish(run_id, profile_id="alpha", *, origin="user", request="", artifact="", tool="write", summary=""):
            links.begin_run(run_id, profile_id, run_id, origin=origin)
            if request:
                mid = history.append("user", request, agent_run_id=run_id)
                links.bind_messages(run_id, user_message_id=mid)
            links.append_event(run_id, {"type": "tool_call", "tool_name": tool})
            links.append_event(run_id, {"type": "status", "result": {
                "terminal_type": "completed",
                "artifacts": [{"path": artifact, "size": 123}] if artifact else [],
            }})
            links.finish_run(run_id, "completed", publish_result=bool(summary), outcome={"summary": summary})

        finish("old", request="Compare the riverside trail", artifact="sandbox/route.md")
        finish("long", request="x" * 6000 + " keyword at the end")
        finish("lookup-only", request="Search the riverside trail task", tool="mcp__shikigami__task_search")
        for i in range(35):
            finish("unrelated-" + str(i))
        finish("reflection", origin="reflection", artifact="sandbox/riverside_notes.md",
               summary="Saved private-sample.md in the workspace")
        finish("foreign", "beta", artifact="sandbox/riverside_notes.md")

        def find(query, include_artifacts=True):
            return search_tasks(links, session, TaskSearchArgs(query=query),
                                include_artifacts=include_artifacts)["matches"]

        self.assertEqual([row["run_id"] for row in find("riverside")], ["reflection", "old"])
        self.assertEqual(find("route.md")[0]["artifacts"][0]["path"], "sandbox/route.md")
        self.assertEqual([row["run_id"] for row in find("riverside", False)], ["old"])
        self.assertEqual(find("route.md", False), [])
        self.assertEqual(find("private-sample.md", False), [])
        self.assertEqual(find("private-sample.md")[0]["run_id"], "reflection")
        self.assertIn("keyword at the end", find("keyword")[0]["request_excerpt"])
        history.delete_by_id(find("riverside")[-1]["user_message_id"])
        self.assertEqual(find("route.md"), [])
        links.invalidate("alpha")
        self.assertEqual(find("riverside"), [])

    def test_old_message_schema_migrates_and_partial_survives_all_reads(self):
        with closing(sqlite3.connect(self.root / "chat_records.db")) as db, db:
            db.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "role TEXT NOT NULL, content TEXT NOT NULL, timestamp REAL NOT NULL)")
            db.execute("INSERT INTO messages VALUES(7,'user','legacy',1)")
        store = self.chat()
        self.assertEqual(store.get_all_with_id()[0]["generation_status"], "completed")
        self.assertIsNone(store.get_all_with_id()[0]["agent_run_id"])
        mid = store.append("assistant", "unfinished", "AI", agent_run_id="run-1",
                           generation_status="cancelled")
        self.assertGreater(mid, 7)
        store.close()
        store = self.chat()
        for rows in (store.get_all_with_id(), store.get_page_with_id(5),
                     store.search_messages(), store.get_recent(), store.get_recent_with_ts(),
                     store.get_recent_by_sender(sender="AI"), store.get_recent_since(0),
                     store.get_recent_since_with_ts(0)):
            item = next(row for row in rows if row["content"] == "unfinished")
            self.assertEqual((item["id"], item["agent_run_id"], item["generation_status"]),
                             (mid, "run-1", "cancelled"))
        for rows in (store.get_all(), store.get_last(1)):
            item = rows[-1]
            self.assertEqual((item.id, item.agent_run_id, item.generation_status),
                             (mid, "run-1", "cancelled"))
            self.assertEqual(item.to_llm_dict(), {"role": "assistant", "content": "unfinished"})
        store.save_all(store.get_all())
        self.assertEqual([row["id"] for row in store.get_all_with_id()], [7, mid])
        self.assertEqual(store.get_last(1)[0].generation_status, "cancelled")

    def test_recent_retains_partials_with_metadata_and_emotion_excludes_them(self):
        store = self.chat()
        store.append("user", "request")
        store.append("assistant", "complete reply")
        for status in ("cancelled", "failed"):
            store.append("assistant", "partial " + status, generation_status=status)
        self.assertEqual([m["content"] for m in store.get_recent()],
                         ["request", "complete reply", "partial cancelled", "partial failed"])
        self.assertEqual([m["generation_status"] for m in store.get_recent()],
                         ["completed", "completed", "cancelled", "failed"])
        self.assertEqual(store.get_recent_ai_messages(10), ["complete reply"])
        self.assertEqual(len(store.get_recent_with_ts()), 4)

    def test_append_legacy_signature_and_invalid_status_no_insert(self):
        store = self.chat()
        self.assertIsInstance(store.append("user", "hello", "human"), int)
        for status in ("running", "", None, []):
            with self.assertRaises(ValueError):
                store.append("assistant", "bad", generation_status=status)
        self.assertEqual(store.count_all(), 1)
        store.append("assistant", "failed fragment", generation_status="failed")
        self.assertEqual(store.get_last(1)[0].generation_status, "failed")

    def test_prompt_status_is_trusted_metadata_not_speech(self):
        source = [dict(role="assistant", content="partial", timestamp=1,
                       generation_status="cancelled"),
                  dict(role="assistant", content="failed part", timestamp=2,
                       generation_status="failed")]
        result = _result_with_time_sources(
            [{"role": m["role"], "content": m["content"]} for m in source], source, "")
        wire = normalize_messages(result.messages)
        self.assertIn("cancelled", wire[0]["content"])
        self.assertIn("failed", wire[0]["content"])
        self.assertIn("未完成", wire[0]["content"])
        self.assertEqual(wire[-1]["content"], "partial\n\nfailed part")
        self.assertEqual(normalize_messages(wire), wire)

    def test_real_single_history_does_not_lose_status_in_merge_projection(self):
        store = self.chat()
        store.append("assistant", "partial", generation_status="failed")
        ctx = SimpleNamespace(extras={}, profile={}, store=store, user_msg="next",
                              n_history_turns=10,
                              session=SimpleNamespace(conversation_store=store))
        wire = normalize_messages(HistorySegment().build(ctx).messages)
        self.assertIn("failed", wire[0]["content"])
        self.assertEqual(wire[-1]["content"], "partial")

    def test_single_provenance_survives_enabled_group_manager_and_configured_cap(self):
        store = self.chat()
        store.append("user", "old")
        store.append("assistant", "old reply")
        store.append("user", "request")
        store.append("assistant", "partial", generation_status="cancelled")
        manager = SimpleNamespace(get_group_ids_for_profile=lambda pid: [])
        ctx = SimpleNamespace(extras={"app": SimpleNamespace(state=SimpleNamespace(group_manager=manager))},
                              profile={"memory_config": {"merged_history_max_messages": 2}},
                              store=store, user_msg="next", n_history_turns=10,
                              session=SimpleNamespace(conversation_store=store, profile_id="synthetic"))
        wire = normalize_messages(HistorySegment().build(ctx).messages)
        self.assertIn("cancelled", wire[0]["content"])
        self.assertEqual([m["content"] for m in wire[1:]], ["request", "partial"])
        ctx.extras = {}
        wire = normalize_messages(HistorySegment().build(ctx).messages)
        self.assertEqual([m["content"] for m in wire[1:]], ["request", "partial"])

    def test_old_link_schema_additive_migration(self):
        with closing(sqlite3.connect(self.root / "links.db")) as db, db:
            db.executescript("""
                CREATE TABLE profiles(profile_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0);
                INSERT INTO profiles VALUES('p',0);
                CREATE TABLE runs(ordinal INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL UNIQUE,
                    profile_id TEXT NOT NULL, session_id TEXT NOT NULL, origin TEXT NOT NULL,
                    revision INTEGER NOT NULL, state TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0,
                    last_seq INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, error TEXT);
                INSERT INTO runs(run_id,profile_id,session_id,origin,revision,state,created_at,updated_at)
                    VALUES('old','p','s','user',0,'completed',1,1);
            """)
        store = self.links()
        row = store.get_run("old")
        self.assertIsNone(row["user_message_id"])
        self.assertIsNone(row["assistant_message_id"])
        self.assertIsNone(row["parent_run_id"])
        # Legacy completed chats remain explicitly readable, but are not tasks
        # for automatic continuation without a tool/action or parent link.
        self.assertEqual(store.task_context("p", "old")["run_id"], "old")
        self.assertEqual(store.task_context("p"), {})
        store.close()
        self.assertEqual(self.links().get_run("old")["created_at"], 1)

    def test_bind_and_parent_are_durable_idempotent_and_conflict_safe(self):
        store = self.links()
        store.begin_run("parent", "p", "sp")
        store.bind_messages("parent", user_message_id=3, assistant_message_id=4)
        store.finish_run("parent", "cancelled")
        store.begin_run("child", "p", "sc")
        store.set_parent("child", "parent")
        store.set_parent("child", "parent")
        store.bind_messages("child", user_message_id=5)
        store.bind_messages("child", assistant_message_id=6)
        store.bind_messages("child", user_message_id=5)
        with self.assertRaises(ValueError):
            store.bind_messages("child", user_message_id=99)
        for value in (True, 0, -1, "5", 2**63):
            with self.assertRaises(ValueError):
                store.bind_messages("child", assistant_message_id=value)
        store.close()
        row = self.links().get_run("child")
        self.assertEqual((row["parent_run_id"], row["user_message_id"], row["assistant_message_id"]),
                         ("parent", 5, 6))

    def test_cross_profile_active_future_and_stale_parents_forbidden(self):
        store = self.links()
        store.begin_run("a", "p", "sa")
        with self.assertRaises(ValueError):
            store.set_parent("a", "a")
        store.begin_run("other", "q", "sq")
        with self.assertRaises(ValueError):
            store.set_parent("other", "a")
        store.finish_run("a", "completed")
        with self.assertRaises(ValueError):
            store.set_parent("other", "a")
        store.begin_run("b", "p", "sb")
        with self.assertRaises(ValueError):
            store.set_parent("a", "b")
        store.finish_run("b", "completed")
        with self.assertRaises(ValueError):
            store.set_parent("a", "b")
        store.invalidate("p")
        store.begin_run("fresh", "p", "sf")
        with self.assertRaises(ValueError):
            store.set_parent("fresh", "a")
        with self.assertRaises(ValueError):
            store.bind_messages("a", user_message_id=1)
        self.assertEqual(store.task_context("p"), {})
        for profile, run in (("p", "a"), ("q", "a"), ("p", "fresh")):
            with self.assertRaises(ValueError):
                store.task_context(profile, run)

    def test_context_keeps_real_tools_uncertainty_no_thinking_or_replay(self):
        store = self.links()
        store.begin_run("r", "p", "s")
        store.bind_messages("r", user_message_id=1, assistant_message_id=2)
        call = {"type": "tool_call", "call_id": "c", "tool_name": "timer_create",
                "result": {"arguments": {"seconds": 10}}}
        result = {"type": "tool_result", "call_id": "c", "tool_name": "timer_create",
                  "summary": "timer created", "result": {"is_error": False}}
        store.append_event("r", call)
        store.append_event("r", result)
        store.append_event("r", {"type": "thinking", "summary": "DO NOT EXPOSE"})
        self.assertEqual(store.claim_action("r", "c", "timer_create", {"seconds": 10})["status"], "new")
        store.complete_action("r", "c", {"timer_id": "synthetic"})
        store.claim_action("r", "u", "timer_cancel", {})
        store.fail_action("r", "u", "transport lost; outcome unknown")
        store.finish_run("r", "failed", "connection lost")
        store.close()
        store = self.links()
        before = store.get_run("r")
        ctx = store.task_context("p")
        self.assertEqual(ctx["events"], [dict(call, seq=1), dict(result, seq=2)])
        self.assertNotIn("DO NOT EXPOSE", json.dumps(ctx))
        self.assertEqual(ctx["assistant_message_id"], 2)
        self.assertEqual(ctx["actions"][1]["status"], "unknown")
        self.assertEqual(store.task_context("p"), ctx)
        self.assertEqual(store.get_run("r"), before)
        self.assertEqual(store.claim_action("r", "c", "timer_create", {"seconds": 10})["status"], "done")
        self.assertEqual(store.claim_action("r", "u", "timer_cancel", {})["status"], "pending")
        store.begin_run("hidden", "p", "sh", origin="reflection")
        store.finish_run("hidden", "completed")
        store.begin_run("running", "p", "sr")
        self.assertEqual(store.task_context("p")["run_id"], "r")

    def test_context_events_are_bounded_and_recovery_does_not_replay(self):
        store = self.links()
        store.begin_run("r", "p", "s")
        for i in range(105):
            store.append_event("r", {"type": "tool_result", "call_id": str(i),
                                     "result": {"is_error": True}, "error": "synthetic failure"})
        store.claim_action("r", "pending", "timer_create", {})
        self.assertEqual(store.recover_interrupted(), ["r"])
        self.assertEqual(store.recover_interrupted(), [])
        context = store.task_context("p")
        self.assertEqual(len(context["events"]), 100)
        self.assertEqual(context["events"][0]["seq"], 6)
        self.assertTrue(context["events_truncated"])
        self.assertEqual(context["actions"][0]["status"], "unknown")

    def test_context_removes_nested_model_thinking_but_preserves_tool_evidence(self):
        store = self.links()
        store.begin_run("r", "p", "s")
        result = {"timer_id": "synthetic", "thinking": "private model trace",
                  "nested": [{"reasoning_content": "private nested trace", "ok": True}]}
        store.append_event("r", {"type": "tool_result", "result": result})
        store.claim_action("r", "c", "timer_create", {})
        store.complete_action("r", "c", result)
        store.finish_run("r", "completed")
        context = store.task_context("p")
        expected = {"timer_id": "synthetic", "nested": [{"ok": True}]}
        self.assertEqual(context["events"][0]["result"], expected)
        self.assertEqual(context["actions"][0]["result"], expected)
        self.assertNotIn("private", json.dumps(context))

    def test_current_revision_context_survives_reopen_and_skips_active_child(self):
        store = self.links()
        store.begin_run("old", "p", "s-old")
        store.finish_run("old", "completed")
        store.invalidate("p")
        store.begin_run("prior", "p", "s-prior")
        store.finish_run("prior", "failed")
        store.begin_run("active", "p", "s-active")
        store.set_parent("active", "prior")
        store.close()
        store = self.links()
        self.assertEqual(store.task_context("p")["run_id"], "prior")
        self.assertEqual(store.task_context("p")["revision"], 1)
        self.assertEqual(store.get_run("active")["state"], "running")
        with self.assertRaises(ValueError):
            store.task_context("p", "old")

    def test_default_continuation_skips_completed_greeting_after_real_tool_task(self):
        store = self.links()
        store.begin_run("task", "p", "s-task")
        store.append_event("task", {"type": "tool_call", "tool_name": "timer_create", "call_id": "c"})
        store.append_event("task", {"type": "tool_result", "call_id": "c", "result": {"timer_id": "t"}})
        store.finish_run("task", "completed")
        store.begin_run("hello", "p", "s-hello")
        store.bind_messages("hello", user_message_id=3, assistant_message_id=4)
        store.finish_run("hello", "completed")
        store.begin_run("current", "p", "s-current")
        self.assertEqual(store.task_context("p")["run_id"], "task")
        self.assertEqual(store.task_context("p", "hello")["run_id"], "hello")
        store.close()
        self.assertEqual(self.links().task_context("p")["run_id"], "task")

    def test_default_continuation_keeps_receipts_parents_and_noncompleted_runs(self):
        store = self.links()
        store.begin_run("receipt", "p", "s-receipt")
        store.claim_action("receipt", "c", "timer_create", {})
        store.complete_action("receipt", "c", {"timer_id": "t"})
        store.finish_run("receipt", "completed")
        self.assertEqual(store.task_context("p")["run_id"], "receipt")
        store.begin_run("child", "p", "s-child")
        store.set_parent("child", "receipt")
        store.finish_run("child", "completed")
        self.assertEqual(store.task_context("p")["run_id"], "child")
        for state in ("cancelled", "failed"):
            store.begin_run(state, "p", "s-" + state)
            store.finish_run(state, state)
            self.assertEqual(store.task_context("p")["run_id"], state)
        store.invalidate("p")
        store.begin_run("hello", "p", "s-hello")
        store.finish_run("hello", "completed")
        self.assertEqual(store.task_context("p"), {})

    def test_default_task_detection_uses_top_level_type_not_payload_text(self):
        store = self.links()
        for i, event in enumerate((
            {"type": "status", "result": {"type": "tool_call"}},
            {"type": "status", "summary": 'quoted {"type":"tool_call"}'},
            {"type": "toolXcall"},
            {"type": "tool_result", "result": {"is_error": False}},
        )):
            run = "not-task-" + str(i)
            store.begin_run(run, "p", "s-" + run)
            store.append_event(run, event)
            store.finish_run(run, "completed")
        self.assertEqual(store.task_context("p"), {})

    def test_default_task_detection_handles_older_call_outside_context_event_tail(self):
        store = self.links()
        store.begin_run("task", "p", "s-task")
        store.append_event("task", {"type": "tool_call"})
        for _ in range(105):
            store.append_event("task", {"type": "status", "status": "working"})
        store.finish_run("task", "completed")
        for i in range(105):
            run = "hello-" + str(i)
            store.begin_run(run, "p", "s-" + run)
            store.finish_run(run, "completed")
        context = store.task_context("p")
        self.assertEqual(context["run_id"], "task")
        self.assertTrue(context["events_truncated"])
        self.assertEqual(context["events"], [])


if __name__ == "__main__":
    unittest.main()
