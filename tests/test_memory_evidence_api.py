from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import memory
from src.memory.conversation_store import ConversationStore
from src.memory.extraction_batches import message_hash
from src.memory.long_term_store import LongTermFact


class MemoryEvidenceApiTests(IsolatedAsyncioTestCase):
    async def test_slice_group_membership_and_stale_sources(self):
        root = self.enterContext(TemporaryDirectory())
        single = ConversationStore(root, max_turns=0)
        group = ConversationStore(root + "/group", max_turns=0)
        self.addCleanup(single.close)
        self.addCleanup(group.close)
        id = single.append("user", "before right answer after")
        group_id = group.append("assistant", "group source")
        timestamp = single.get_all_with_id()[0]["timestamp"]
        group_ts = group.get_all_with_id()[0]["timestamp"]
        fact = LongTermFact("Supported evidence", id="fact", evidence=[
            {"source": "single", "id": id, "timestamp": timestamp, "role": "user",
             "start_char": 7, "end_char": 19, "content_hash": message_hash("before right answer after")},
            {"source": "group:allowed", "id": group_id, "timestamp": group_ts, "role": "assistant"},
            {"source": "group:denied", "id": group_id, "timestamp": group_ts, "role": "assistant"},
            {"source": "single", "id": id, "timestamp": timestamp - 1, "role": "user"},
        ])
        session = SimpleNamespace(profile_id="fixture", conversation_store=single)
        app = FastAPI()
        app.include_router(memory.router)
        app.state.session_manager = SimpleNamespace(get_by_id=lambda key: session if key == "fixture" else None)
        app.state.group_manager = SimpleNamespace(get_group_ids_for_profile=lambda _: ["allowed"],
                                                  get_conversation_store=lambda _: group)
        manager = SimpleNamespace(_profile_id="fixture", facts=SimpleNamespace(get_by_id=lambda _: fact))
        with patch.object(memory, "_require_manager", return_value=manager):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
                response = await client.get("/memory/fixture/facts/fact/evidence")
                self.assertEqual(response.status_code, 200)
                items = response.json()["items"]
                self.assertEqual([(row["available"], row.get("content")) for row in items],
                                 [(True, "right answer"), (True, "group source"), (False, None), (False, None)])
                self.assertEqual((items[0]["start_char"], items[0]["end_char"]), (7, 19))
                self.assertEqual((await client.get("/memory/other/facts/fact/evidence")).status_code, 404)
