"""Real REST routes with ASGI transport and synthetic sessions; no network/model."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx

from src.tools.todo.api import router
from src.tools.todo.store import TodoStore


class TodoAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shikigami-todo-api-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sessions = {name: SimpleNamespace(id=name, profile_id=name,
                         storage_root=str(self.root / name)) for name in ("alpha", "beta")}
        self.lookups = []

        def get_by_id(identity):
            self.lookups.append(identity)
            return self.sessions.get(identity)

        self.app = FastAPI()
        self.app.state.session_manager = SimpleNamespace(get_by_id=get_by_id,
                                                        get_current=lambda: self.sessions["beta"])
        self.app.include_router(router)
        for target, replacement in (
            ("src.tools.todo.api.log_tool_todo", None),
            ("httpx.AsyncHTTPTransport.handle_async_request", AsyncMock(side_effect=AssertionError("No network"))),
        ):
            guard = patch(target) if replacement is None else patch(target, replacement)
            guard.start()
            self.addCleanup(guard.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    def store(self, profile="alpha"):
        return TodoStore(self.sessions[profile].storage_root)

    async def test_legacy_routes_and_ui_can_manage_both_owners(self):
        for owner in ("user", "ai"):
            response = await self.client.post("/sessions/alpha/todos", json={"content": "task", "owner": owner,
                                                                          "due_date": 123.0, "priority": 5})
            self.assertEqual(response.status_code, 201)
            item = response.json()
            self.assertEqual(item["owner"], owner)
            changed = await self.client.patch(f'/sessions/alpha/todos/{item["id"]}',
                                              json={"done": True, "content": "edited"})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.json()["todo"]["owner"], owner)
            self.assertTrue(changed.json()["todo"]["done"])
        listing = await self.client.get("/sessions/alpha/todos")
        self.assertEqual({t["owner"] for t in listing.json()["todos"]}, {"user", "ai"})
        removed = await self.client.delete(f'/sessions/alpha/todos/{item["id"]}')
        self.assertEqual(removed.json(), {"ok": True})
        default = await self.client.post("/sessions/alpha/todos", json={"content": "default user"})
        self.assertEqual(default.json()["owner"], "user")

    async def test_clear_done_defaults_to_user_and_can_explicitly_target_ai(self):
        for owner in ("user", "ai"):
            item = self.store().add(owner, owner=owner)
            self.store().update(item.id, done=True)
        pending = self.store().add("pending AI", owner="ai")
        response = await self.client.delete("/sessions/alpha/todos")
        self.assertEqual(response.json(), {"ok": True, "removed": 1})
        self.assertEqual({t.owner for t in self.store().get_all()}, {"ai"})
        response = await self.client.delete("/sessions/alpha/todos", params={"owner": "ai"})
        self.assertEqual(response.json(), {"ok": True, "removed": 1})
        self.assertEqual([t.id for t in self.store().get_all()], [pending.id])
        response = await self.client.delete("/sessions/alpha/todos", params={"owner": "other"})
        self.assertEqual(response.status_code, 422)

    async def test_session_scoping_never_uses_current_profile_or_group_identity(self):
        item = self.store().add("alpha plan", owner="ai", item_id="same-id")
        other = self.store("beta").add("beta todo", item_id=item.id)
        changed = await self.client.patch(f"/sessions/alpha/todos/{item.id}", json={"done": True})
        self.assertEqual(changed.status_code, 200)
        self.assertFalse(self.store("beta").get_by_id(other.id).done)
        for method, suffix, kwargs in (("GET", "", {}), ("POST", "", {"json": {"content": "x"}}),
                                       ("PATCH", "/same-id", {"json": {"done": True}}),
                                       ("DELETE", "/same-id", {}), ("DELETE", "", {})):
            response = await self.client.request(method, "/sessions/missing-group/todos" + suffix, **kwargs)
            self.assertEqual(response.status_code, 404)
        self.assertFalse((self.root / "missing-group").exists())
        self.lookups.clear()
        response = await self.client.get("/sessions/group:synthetic/todos")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.lookups, [])

    async def test_other_session_item_ids_and_missing_ids_are_not_found(self):
        item = self.store("beta").add("beta only")
        for identity in (item.id, "unknown"):
            for method, kwargs in (("PATCH", {"json": {"done": True}}), ("DELETE", {})):
                response = await self.client.request(method, f"/sessions/alpha/todos/{identity}", **kwargs)
                self.assertEqual(response.status_code, 404)
        self.assertIsNotNone(self.store("beta").get_by_id(item.id))

    async def test_same_origin_guard_covers_all_mutation_routes(self):
        item = self.store().add("keep")
        operations = (("POST", "", {"json": {"content": "new"}}),
                      ("PATCH", f"/{item.id}", {"json": {"done": True}}),
                      ("DELETE", f"/{item.id}", {}), ("DELETE", "", {}))
        raw = (self.root / "alpha" / "todos.json").read_bytes()
        for origin in ("https://evil.invalid", "https://testserver", "http://testserver.evil.invalid"):
            for method, suffix, kwargs in operations:
                response = await self.client.request(method, "/sessions/alpha/todos" + suffix,
                                                     headers={"Origin": origin}, **kwargs)
                self.assertEqual(response.status_code, 403)
                self.assertEqual((self.root / "alpha" / "todos.json").read_bytes(), raw)
        for headers in ({}, {"Origin": "http://testserver"}):
            response = await self.client.post("/sessions/alpha/todos", json={"content": "allowed"}, headers=headers)
            self.assertEqual(response.status_code, 201)

    async def test_rest_cannot_write_owner_or_host_metadata_through_patch(self):
        item = self.store().add("plan", owner="ai", source_path="fox_ledger/plan.md", last_run_id="host-run")
        for key, value in (("owner", "user"), ("source_path", "../secret"), ("evidence_path", "proof.md"),
                           ("last_run_id", "invented"), ("storage_root", "elsewhere"), ("in_progress", True)):
            response = await self.client.patch(f"/sessions/alpha/todos/{item.id}", json={key: value})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store().get_by_id(item.id).to_dict(), item.to_dict())
        for key in ("source_path", "evidence_path", "last_run_id", "storage_root", "item_id"):
            response = await self.client.post("/sessions/alpha/todos", json={"content": "bad", key: "untrusted"})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.store().get_all()), 1)

    async def test_rest_content_and_owner_validation(self):
        item = self.store().add("keep")
        for body in ({"content": "x", "owner": "other"}, {"content": "x" * 2001}):
            response = await self.client.post("/sessions/alpha/todos", json=body)
            self.assertEqual(response.status_code, 422)
        for content in ("", "   "):
            response = await self.client.post("/sessions/alpha/todos", json={"content": content})
            self.assertEqual(response.status_code, 400)
        for content in ("", "   ", "x" * 2001):
            response = await self.client.patch(f"/sessions/alpha/todos/{item.id}", json={"content": content})
            self.assertIn(response.status_code, (400, 422))
        response = await self.client.post("/sessions/alpha/todos", json={"content": "x" * 2000})
        self.assertEqual(response.status_code, 201)

    async def test_corrupt_storage_and_write_errors_propagate_without_overwrite(self):
        self.store().add("original")
        path = self.root / "alpha" / "todos.json"
        path.write_bytes(b"{broken")
        with self.assertRaises(ValueError):
            await self.client.post("/sessions/alpha/todos", json={"content": "new"})
        self.assertEqual(path.read_bytes(), b"{broken")
        path.write_bytes(b"[]")
        with patch("src.tools.todo.store.os.replace", side_effect=OSError("synthetic disk failure")):
            with self.assertRaisesRegex(OSError, "synthetic disk failure"):
                await self.client.post("/sessions/alpha/todos", json={"content": "new"})
        self.assertEqual(path.read_bytes(), b"[]")


if __name__ == "__main__":
    unittest.main()
