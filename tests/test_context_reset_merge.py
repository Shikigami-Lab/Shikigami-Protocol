"""A profile reset limits only that profile's merged prompt, not shared history."""
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from src.memory.conversation_store import ConversationStore
from src.memory.merged_history import get_merged_recent_messages


class ContextResetMergeTests(unittest.TestCase):
    def test_group_speech_stays_visible_to_other_role_after_single_reset(self):
        root = self.enterContext(TemporaryDirectory())
        a = ConversationStore(root + "/a")
        b = ConversationStore(root + "/b")
        group = ConversationStore(root + "/group")
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.addCleanup(group.close)
        a.append("user", "old single")
        old_id = group.append("user", "old shared group")
        with group._conn:
            group._conn.execute("UPDATE messages SET timestamp=100 WHERE id=?", (old_id,))
        sessions = [SimpleNamespace(id=key, profile_id=key, conversation_store=store)
                    for key, store in (("a", a), ("b", b))]
        manager = SimpleNamespace(get_group_ids_for_profile=lambda _: ["shared"],
                                  get_group=lambda _: {"display_name": "Shared"},
                                  get_conversation_store=lambda _: group)
        app = SimpleNamespace(state=SimpleNamespace(group_manager=manager))
        a.reset_context(since_ts=200)
        self.assertEqual(get_merged_recent_messages(sessions[0], app, {}, 20), [])
        self.assertEqual([row["content"] for row in get_merged_recent_messages(sessions[1], app, {}, 20)],
                         ["old shared group"])
        new_id = group.append("user", "new shared group")
        with group._conn:
            group._conn.execute("UPDATE messages SET timestamp=300 WHERE id=?", (new_id,))
        self.assertEqual([row["content"] for row in get_merged_recent_messages(sessions[0], app, {}, 20)],
                         ["new shared group"])
        self.assertEqual(len(group.memory_turn_batch_after(0, 20)), 2)
