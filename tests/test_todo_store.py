"""Todo persistence/command regressions using synthetic temporary profiles only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.tools.todo.commands import handle_todo
from src.tools.todo.segment import TodosSegment
from src.tools.todo.store import TodoStore


class TodoStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shikigami-todo-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "todos.json"
        self.store = TodoStore(str(self.root))

    def test_legacy_read_does_not_rewrite_and_roundtrip_preserves_fields(self):
        legacy = {"id": "old", "content": "legacy task", "done": False,
                  "created_at": 123.5, "due_date": 456.0, "priority": 5}
        raw = json.dumps([legacy], indent=4).encode()
        self.path.write_bytes(raw)
        store = TodoStore(str(self.root))
        self.assertEqual(store.get_by_id("old").owner, "user")
        self.assertEqual(self.path.read_bytes(), raw)
        store.add("new task", owner="ai")
        saved = json.loads(self.path.read_text(encoding="utf-8"))[0]
        self.assertEqual({key: saved[key] for key in legacy}, legacy)
        self.assertEqual({key: saved[key] for key in
                          ("owner", "source_path", "evidence_path", "last_run_id")},
                         {"owner": "user", "source_path": "", "evidence_path": "", "last_run_id": ""})

    def test_missing_file_reads_do_not_create_storage(self):
        store = TodoStore(str(self.root / "absent"))
        self.assertEqual(store.get_all(), [])
        self.assertEqual(store.get_pending(), [])
        self.assertIsNone(store.get_by_id("missing"))
        self.assertFalse((self.root / "absent").exists())

    def test_corrupt_input_is_preserved_by_reads_and_every_mutation(self):
        for raw in (b'{broken', b'{}', b'[null]', b'[{"content":"missing id"}]',
                    b'[{"id":"x","owner":"other"}]'):
            self.path.write_bytes(raw)
            for operation in (self.store.get_all, self.store.get_pending,
                              lambda: self.store.get_by_id("x"),
                              lambda: self.store.add("new"),
                              lambda: self.store.update("x", done=True),
                              lambda: self.store.delete_by_id("x"),
                              lambda: self.store.complete("x"),
                              lambda: self.store.delete("x"), self.store.clear_done):
                with self.subTest(raw=raw, operation=operation):
                    with self.assertRaises((ValueError, TypeError, KeyError)):
                        operation()
                    self.assertEqual(self.path.read_bytes(), raw)
            with self.assertRaises((ValueError, TypeError, KeyError)):
                TodoStore(str(self.root))

    def test_failed_atomic_save_leaves_original_and_removes_tempfile(self):
        item = self.store.add("original")
        raw = self.path.read_bytes()
        for target in ("os.replace", "os.fsync", "json.dump"):
            with self.subTest(target=target):
                with patch("src.tools.todo.store." + target, side_effect=OSError("synthetic write failure")):
                    with self.assertRaisesRegex(OSError, "synthetic write failure"):
                        self.store.update(item.id, content="must not persist", done=True)
                self.assertEqual(self.path.read_bytes(), raw)
                self.assertEqual(list(self.root.iterdir()), [self.path])
                self.assertEqual(self.store.get_by_id(item.id).content, "original")
                self.assertFalse(self.store.get_by_id(item.id).done)

    def test_stale_instances_refresh_all_read_methods_and_keep_mutations(self):
        stale = TodoStore(str(self.root))
        item = self.store.add("first", priority=5)
        self.assertEqual(stale.get_by_id(item.id).content, "first")
        self.assertEqual([t.id for t in stale.get_all()], [item.id])
        self.assertEqual([t.id for t in stale.get_pending()], [item.id])
        other = stale.add("second", owner="ai")
        self.store.update(item.id, content="changed")
        stale.update(other.id, done=True)
        self.assertEqual(self.store.get_by_id(item.id).content, "changed")
        self.assertTrue(self.store.get_by_id(other.id).done)
        self.assertEqual(len(self.store.get_all()), 2)

    def test_concurrent_canonical_alias_instances_do_not_lose_writes(self):
        alias = self.root / "nested"
        alias.mkdir()
        stores = [TodoStore(str(self.root)), TodoStore(str(alias / ".."))]
        self.assertIs(stores[0]._lock, stores[1]._lock)
        barrier = Barrier(2)

        def write(index):
            barrier.wait(timeout=5)
            for number in range(20):
                stores[index].add(f"writer {index}/{number}", item_id=f"{index}-{number}")

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(write, range(2)))
        self.assertEqual(len(self.store.get_all()), 40)

    def test_concurrent_deterministic_insert_is_idempotent(self):
        stores = [TodoStore(str(self.root)) for _ in range(2)]
        barrier = Barrier(2)

        def insert(store):
            barrier.wait(timeout=5)
            return store.add("plan", owner="ai", item_id="run-plan", last_run_id="run-1")

        with ThreadPoolExecutor(max_workers=2) as pool:
            items = list(pool.map(insert, stores))
        self.assertEqual(items[0].to_dict(), items[1].to_dict())
        self.store.update("run-plan", done=True, evidence_path="fox_ledger/result.md")
        replay = self.store.add("retry must not overwrite", owner="ai", item_id="run-plan")
        self.assertTrue(replay.done)
        self.assertEqual(replay.content, "plan")
        self.assertEqual(len(self.store.get_all()), 1)
        with self.assertRaises(ValueError):
            self.store.add("wrong actor", owner="user", item_id="run-plan")

    def test_owner_guarded_update_delete_and_metadata(self):
        user = self.store.add("user todo")
        ai = self.store.add("AI plan", owner="ai", source_path=r"fox_ledger\draft.md", last_run_id="run-1")
        self.assertEqual(ai.source_path, "fox_ledger/draft.md")
        stale = TodoStore(str(self.root))
        self.assertIsNone(stale.update(user.id, done=True, expected_owner="ai"))
        self.assertIsNone(stale.delete_by_id(user.id, expected_owner="ai"))
        updated = stale.update(ai.id, done=True, expected_owner="ai",
                               evidence_path="fox_ledger/./result.md", last_run_id="run-2")
        self.assertEqual(updated.evidence_path, "fox_ledger/result.md")
        self.assertEqual(updated.last_run_id, "run-2")
        self.assertEqual(updated.source_path, ai.source_path)
        self.assertFalse(self.store.get_by_id(user.id).done)
        with self.assertRaises(TypeError):
            self.store.update(ai.id, owner="user")
        self.assertEqual(self.store.get_by_id(ai.id).owner, "ai")
        self.assertIsNotNone(stale.delete_by_id(ai.id, expected_owner="ai"))
        self.assertIsNone(self.store.get_by_id(ai.id))

    def test_owner_guard_uses_latest_disk_state(self):
        item = self.store.add("old identity", owner="ai", item_id="stable")
        stale = TodoStore(str(self.root))
        self.store.delete_by_id(item.id)
        self.store.add("replacement", owner="user", item_id=item.id)
        self.assertIsNone(stale.update(item.id, done=True, expected_owner="ai"))
        self.assertIsNone(stale.delete_by_id(item.id, expected_owner="ai"))
        self.assertEqual(self.store.get_by_id(item.id).content, "replacement")
        self.assertFalse(self.store.get_by_id(item.id).done)

    def test_fuzzy_commands_only_affect_users_pending_first(self):
        ai = self.store.add("shared AI", owner="ai")
        old = self.store.add("shared completed user")
        self.store.update(old.id, done=True)
        user = self.store.add("shared pending user")
        self.assertEqual(self.store.complete("SHARED").id, user.id)
        self.assertFalse(self.store.get_by_id(ai.id).done)
        self.assertEqual(self.store.delete("shared").id, old.id)
        self.assertEqual(self.store.delete("shared").id, user.id)
        self.assertIsNone(self.store.complete("shared"))
        self.assertIsNone(self.store.delete("shared"))
        self.assertIsNotNone(self.store.get_by_id(ai.id))

    def test_pending_and_clear_done_owner_filters(self):
        user = self.store.add("user", priority=1)
        ai = self.store.add("ai", owner="ai", priority=5)
        self.assertEqual([t.id for t in self.store.get_pending()], [ai.id, user.id])
        self.assertEqual([t.id for t in self.store.get_pending(owner="user")], [user.id])
        self.assertEqual([t.id for t in self.store.get_pending(owner="ai")], [ai.id])
        self.store.update(user.id, done=True)
        self.store.update(ai.id, done=True)
        self.assertEqual(self.store.clear_done(owner="user"), 1)
        self.assertIsNotNone(self.store.get_by_id(ai.id))
        self.assertEqual(self.store.clear_done(), 1)

    def test_content_owner_and_path_validation_without_file_reads(self):
        item = self.store.add("x" * 2000, owner="ai", source_path="fox_ledger//missing.md")
        self.assertEqual(item.source_path, "fox_ledger/missing.md")
        for content in ("", "   ", "x" * 2001):
            with self.subTest(content_length=len(content)):
                with self.assertRaises(ValueError):
                    self.store.add(content)
                with self.assertRaises(ValueError):
                    self.store.update(item.id, content=content)
        for owner in ("AI", "other", "", None):
            with self.subTest(owner=owner):
                with self.assertRaises(ValueError):
                    self.store.add("bad owner", owner=owner)
        for operation in (lambda: self.store.get_pending(owner="other"),
                          lambda: self.store.clear_done(owner="other"),
                          lambda: self.store.update(item.id, expected_owner="other"),
                          lambda: self.store.delete_by_id(item.id, expected_owner="other")):
            with self.assertRaises(ValueError):
                operation()
        for path in ("../secret", "fox_ledger/../secret", r"fox_ledger\..\secret", "/secret",
                     r"C:\secret", "C:secret", r"\\server\share", "fox_ledger/a:stream", "x\x00y"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self.store.add("bad path", source_path=path)
                with self.assertRaises(ValueError):
                    self.store.update(item.id, done=True, evidence_path=path)
        self.assertFalse(self.store.get_by_id(item.id).done)
        self.assertEqual(len(self.store.get_all()), 1)

    def test_slash_list_and_chat_segment_exclude_ai_plans(self):
        self.store.add("USER RESPONSIBILITY")
        self.store.add("AI PRIVATE PLAN", owner="ai")
        completed = self.store.add("AI COMPLETED PLAN", owner="ai")
        self.store.update(completed.id, done=True)
        session = SimpleNamespace(id="synthetic", storage_root=str(self.root))
        result = asyncio.run(handle_todo("list", "", session, None))
        self.assertIn("USER RESPONSIBILITY", result.context_for_llm)
        self.assertNotIn("AI PRIVATE PLAN", result.context_for_llm)
        self.assertNotIn("AI COMPLETED PLAN", result.context_for_llm)
        messages = TodosSegment().build(SimpleNamespace(session=session)).messages
        self.assertIn("USER RESPONSIBILITY", messages[0]["content"])
        self.assertNotIn("AI PRIVATE PLAN", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
