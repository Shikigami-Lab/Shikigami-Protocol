import json
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from dotenv import dotenv_values, load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from src.api import agent, memory, preferences, profiles_import, sessions, settings_ext, tools
from src.config.app_config import AppConfig
from src.llm import registry
from src.memory import embedding
from src.memory.embedding import EmbeddingProvider, resolve_embedding_api_key


class SettingsIntegrityTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(tmp)
        self.profiles = self.root / "profiles"
        self.profiles.mkdir()
        self.env = self.root / ".env"
        self.yaml = self.root / "app.yaml"
        stack.enter_context(patch.dict(os.environ, {"PYTHON_DOTENV_DISABLED": "1"}, clear=True))
        stack.enter_context(patch.object(settings_ext, "_ENV_FILE", str(self.env)))
        stack.enter_context(patch.object(embedding, "_EMBEDDING_ENV_FILE", str(self.env)))
        stack.enter_context(patch.object(settings_ext, "_YAML_PATH", str(self.yaml)))
        stack.enter_context(patch.object(settings_ext, "_PROFILES_DIR", str(self.profiles)))
        stack.enter_context(patch.object(registry, "_ROLE_PROVIDERS", {}))
        self.app = FastAPI()
        self.app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"])
        self.app.middleware("http")(settings_ext.guard_settings_access)
        self.app.include_router(settings_ext.router)
        for module in (agent, memory, preferences, sessions, tools):
            self.app.include_router(module.router)
        stack.enter_context(patch.object(preferences, "_PREFS_FILE", str(self.root / "prefs.json")))
        self.config = self.app.state.config = AppConfig()
        self.client = stack.enter_context(TestClient(self.app, base_url="http://127.0.0.1:7788"))

    def test_embedding_test_and_save_reject_retargeting_saved_key(self):
        path = "/settings/embedding"
        saved = {"provider": "openai_compat", "base_url": "https://old.invalid/v1", "api_key": "fake-saved"}
        self.assertEqual(200, self.client.post(path, json=saved).status_code)
        current = {**saved, "api_key": "", "model": "new-model"}
        with patch.object(settings_ext, "_run_embedding_test", return_value=(True, 1, None)) as probe:
            self.assertEqual(200, self.client.post(path + "/test", json=current).status_code)
            self.assertEqual("fake-saved", probe.call_args.args[0]["api_key"])
            probe.reset_mock()
            changed = {**current, "base_url": "https://new.invalid/v1"}
            self.assertEqual(400, self.client.post(path + "/test", json=changed).status_code)
            probe.assert_not_called()
        self.assertEqual(400, self.client.post(path, json=changed).status_code)
        self.assertEqual(saved["base_url"], self.client.get(path).json()["base_url"])
        self.assertEqual("fake-saved", dotenv_values(self.env)["EMBEDDING_API_KEY"])
        self.assertEqual(200, self.client.post(path, json={**changed, "api_key": "fake-new"}).status_code)

    def test_embedding_runtime_uses_binding_and_does_not_borrow_google_keys(self):
        path = "/settings/embedding"
        os.environ["GEMINI_API_KEY"] = "fake-google"
        saved = {"provider": "openai_compat", "base_url": "https://custom.invalid/v1", "api_key": "fake-dedicated"}
        self.assertEqual(200, self.client.post(path, json=saved).status_code)
        with patch("openai.OpenAI") as client, patch.object(EmbeddingProvider, "_embed_compat", return_value=[[1.0]]):
            EmbeddingProvider(self.config.memory["embedding"])
            self.assertEqual("fake-dedicated", client.call_args.kwargs["api_key"])
            EmbeddingProvider({"provider": "openai_compat", "base_url": "https://other.invalid/v1"})
            self.assertEqual("sk-placeholder", client.call_args.kwargs["api_key"])
        with patch.object(EmbeddingProvider, "_embed_gemini", return_value=[[1.0]]):
            gemini = EmbeddingProvider({"provider": "gemini"})
        self.assertEqual("fake-google", gemini._api_key)

    def test_embedding_restart_rejects_inherited_key_paired_with_dotenv_binding(self):
        path = "/settings/embedding"
        saved = {"provider": "openai_compat", "base_url": "https://b.invalid/v1", "api_key": "fake-b"}
        self.assertEqual(200, self.client.post(path, json=saved).status_code)
        for inherited_key, expected in (("fake-a", ""), (None, "fake-b")):
            inherited = {"EMBEDDING_API_KEY": inherited_key} if inherited_key else {}
            with self.subTest(inherited=inherited), patch.dict(os.environ, inherited, clear=True):
                self.assertTrue(load_dotenv(self.env, override=False))
                loaded = AppConfig.load(str(self.yaml)).memory["embedding"]
                loaded.pop(embedding.EMBEDDING_KEY_MANAGED)
                self.assertEqual(expected, resolve_embedding_api_key(loaded))
        with patch.dict(os.environ, {"EMBEDDING_API_KEY": "fake-legacy"}, clear=True):
            self.assertEqual("fake-legacy", resolve_embedding_api_key({"provider": "gemini"}))

    def test_embedding_managed_replacement_and_clear_ignore_inherited_key_after_restart(self):
        path = "/settings/embedding"
        a = {"provider": "openai_compat", "base_url": "https://a.invalid/v1", "api_key": "fake-a"}
        self.assertEqual(200, self.client.post(path, json=a).status_code)

        def restart(expected, url):
            with patch.dict(os.environ, {
                "EMBEDDING_API_KEY": "fake-a", "GEMINI_API_KEY": "fake-google",
            }, clear=True):
                load_dotenv(self.env, override=False)
                loaded = AppConfig.load(str(self.yaml)).memory["embedding"]
                self.assertEqual(url, loaded["base_url"])
                self.assertEqual(expected, resolve_embedding_api_key(loaded))
                self.assertEqual("", resolve_embedding_api_key({**loaded, "base_url": a["base_url"]}))
                self.assertEqual("fake-google", resolve_embedding_api_key({
                    **loaded, "provider": "gemini", "base_url": "",
                }))

        for host, supplied, expected in (("b", "fake-b", "fake-b"), ("c", " ", "")):
            with self.subTest(host=host):
                url = f"https://{host}.invalid/v1"
                self.assertEqual(200, self.client.post(path, json={**a, "base_url": url, "api_key": supplied}).status_code)
                restart(expected, url)

    def test_embedding_yaml_failure_restores_endpoint_key_pair_for_replace_and_clear(self):
        path = "/settings/embedding"
        old = {"provider": "openai_compat", "base_url": "https://a.invalid/v1", "api_key": "fake-a"}
        self.assertEqual(200, self.client.post(path, json=old).status_code)
        before = self.yaml.read_bytes(), self.env.read_bytes()
        for proposed in ("fake-b", " "):
            with self.subTest(proposed=proposed):
                with patch.object(settings_ext, "_save_yaml", side_effect=OSError("synthetic YAML failure")):
                    with self.assertRaises(OSError):
                        self.client.post(path, json={**old, "base_url": "https://b.invalid/v1", "api_key": proposed})
                self.assertEqual(before, (self.yaml.read_bytes(), self.env.read_bytes()))
                self.assertEqual(old["base_url"], self.client.get(path).json()["base_url"])
                self.assertEqual("fake-a", resolve_embedding_api_key(self.config.memory["embedding"]))

    def test_embedding_legacy_custom_gemini_endpoint_and_legacy_key_are_kept(self):
        self.config.memory["embedding"] = {
            "provider": "gemini", "gemini_base_url": "https://old.invalid/v1",
            "gemini_api_key": "fake-legacy",
        }
        path = "/settings/embedding"
        result = self.client.get(path).json()
        self.assertEqual("https://old.invalid/v1", result["base_url"])
        self.assertTrue(result["has_key"])
        self.assertNotIn("fake-legacy", self.client.get("/settings/memory").text)
        self.assertEqual(200, self.client.post(path, json=result).status_code)
        self.assertEqual("fake-legacy", dotenv_values(self.env)["EMBEDDING_API_KEY"])

    def test_proxy_masking_split_round_trip_and_legacy_updates(self):
        path = "/settings/system/env"
        self.env.write_text(
            "HTTP_PROXY=http://user:secret@plain.invalid:1234\n"
            "HTTPS_PROXY=https://other:password@secure.invalid:4321\n",
            encoding="utf-8",
        )
        result = self.client.get(path)
        self.assertNotIn("secret", result.text)
        self.assertNotIn("password", result.text)
        data = result.json()
        self.assertTrue(data["https_proxy"].startswith("https://"))
        legacy = {key: data[key] for key in ("proxy_enabled", "proxy_url")}
        self.assertEqual(200, self.client.post(path, json=legacy).status_code)
        self.assertEqual("https://other:password@secure.invalid:4321", dotenv_values(self.env)["HTTPS_PROXY"])
        res = self.client.post(path, json={
            "proxy_enabled": True, "http_proxy": data["http_proxy"], "https_proxy": "socks5h://new.invalid:2222"
        })
        self.assertEqual(200, res.status_code)
        self.assertEqual({"ok": True, **self.client.get(path).json()}, res.json())
        self.assertEqual("http://user:secret@plain.invalid:1234", dotenv_values(self.env)["HTTP_PROXY"])
        self.assertEqual("socks5h://new.invalid:2222", dotenv_values(self.env)["HTTPS_PROXY"])
        self.assertEqual(200, self.client.post(path, json={
            "proxy_enabled": True, "proxy_url": "changed.invalid:8080"
        }).status_code)
        self.assertEqual("http://changed.invalid:8080", dotenv_values(self.env)["HTTP_PROXY"])
        self.assertEqual("http://changed.invalid:8080", dotenv_values(self.env)["HTTPS_PROXY"])

    def test_invalid_env_values_leave_every_file_unchanged(self):
        self.env.write_text("UNRELATED=keep\n", encoding="utf-8")
        self.yaml.write_text("{}\n", encoding="utf-8")
        before = self.env.read_bytes(), self.yaml.read_bytes()
        for path, payload in (
            ("/settings/system/env", {"proxy_enabled": True, "proxy_url": "proxy.invalid:8080\nINJECTED=bad"}),
            ("/settings/system/env", {"proxy_enabled": True, "http_proxy": "http://good.invalid:8080",
                                      "https_proxy": "https://bad.invalid:8080\r"}),
            ("/settings/embedding", {"api_key": "fake\nINJECTED=bad"}),
            ("/settings/llm/presets", {"name": "bad\nKEY", "api_key": "fake"}),
            ("/settings/llm/presets", {"name": "valid", "api_key": "fake\0key"}),
        ):
            with self.subTest(path=path, payload=payload):
                self.assertEqual(400, self.client.post(path, json=payload).status_code)
                self.assertEqual(before, (self.env.read_bytes(), self.yaml.read_bytes()))

    def test_preset_advanced_fields_round_trip_and_env_name_collision(self):
        path = "/settings/llm/presets"
        preset = {"name": "my preset", "base_url": "https://model.invalid/v1", "api_key": "fake-one",
                  "max_tokens": 96, "extra_body": {"reasoning": {"enabled": False}}}
        self.assertEqual(200, self.client.post(path, json=preset).status_code)
        self.assertEqual(409, self.client.post(path, json={**preset, "name": "MY_PRESET", "api_key": "fake-two"}).status_code)
        self.assertEqual("fake-one", dotenv_values(self.env)["MY_PRESET_API_KEY"])
        edit = {"name": "my preset", "base_url": preset["base_url"], "model": "changed"}
        self.assertEqual(200, self.client.put(path + "/my%20preset", json=edit).status_code)
        loaded = AppConfig.load(str(self.yaml)).llm_presets["my preset"]
        visible = self.client.get(path).json()["presets"]["my preset"]
        for field in ("max_tokens", "extra_body"):
            self.assertEqual(preset[field], loaded[field])
            self.assertEqual(preset[field], visible[field])
        self.assertEqual(200, self.client.put(path + "/my%20preset", json={
            **edit, "max_tokens": None, "extra_body": None,
        }).status_code)
        visible = self.client.get(path).json()["presets"]["my preset"]
        self.assertIsNone(visible["max_tokens"])
        self.assertIsNone(visible["extra_body"])

    def test_preset_yaml_failure_does_not_publish_key_for_uncommitted_endpoint(self):
        path = "/settings/llm/presets"
        for name, old_key, new_name in (("keyed", "fake-a", "keyed"), ("keyless", "", "keyless"),
                                        ("rename", "fake-a", "renamed"), (None, "", "new")):
            with self.subTest(name=name):
                if name:
                    self.assertEqual(200, self.client.post(path, json={
                        "name": name, "base_url": "https://a.invalid", "api_key": old_key,
                    }).status_code)
                before = self.yaml.read_bytes(), self.env.read_bytes()
                body = {"name": new_name, "base_url": "https://b.invalid", "api_key": "fake-b"}
                with patch.object(settings_ext, "_save_yaml", side_effect=OSError("synthetic YAML failure")):
                    with self.assertRaises(OSError):
                        if name:
                            self.client.put(f"{path}/{name}", json=body)
                        else:
                            self.client.post(path, json=body)
                self.assertEqual(before, (self.yaml.read_bytes(), self.env.read_bytes()))
                if name:
                    reloaded = AppConfig.load(str(self.yaml)).llm_presets[name]
                    self.assertEqual(("https://a.invalid", old_key), (reloaded["base_url"], reloaded["api_key"]))
                if new_name != name:
                    self.assertNotIn(new_name, AppConfig.load(str(self.yaml)).llm_presets)

    def test_preset_env_failure_restores_yaml_and_shared_legacy_key(self):
        path = "/settings/llm/presets"
        original = {"type": "openai_compat", "base_url": "https://a.invalid", "api_key": ""}
        with self.yaml.open("w", encoding="utf-8") as f:
            json.dump({"llm_presets": {"shared key": original, "SHARED_KEY": original}}, f)
        self.env.write_text("# unchanged\nSHARED_KEY_API_KEY='fake-shared'\n", encoding="utf-8")
        os.environ["SHARED_KEY_API_KEY"] = "fake-shared"
        self.config.llm_presets = AppConfig.load(str(self.yaml)).llm_presets
        before = self.yaml.read_bytes(), self.env.read_bytes()
        change = {"name": "isolated", "base_url": "https://b.invalid", "api_key": "fake-b"}
        real_replace = os.replace

        def reject_final_key(src, dest):
            if Path(dest) == self.env and b"fake-b" in Path(src).read_bytes():
                raise OSError("synthetic dotenv failure")
            real_replace(src, dest)

        with patch.object(os, "replace", side_effect=reject_final_key), self.assertRaises(OSError):
            self.client.put(f"{path}/shared%20key", json=change)
        self.assertEqual(before, (self.yaml.read_bytes(), self.env.read_bytes()))
        self.assertEqual(200, self.client.put(f"{path}/shared%20key", json=change).status_code)
        env = dotenv_values(self.env)
        self.assertEqual("fake-shared", env["SHARED_KEY_API_KEY"])
        self.assertEqual("fake-b", env["ISOLATED_API_KEY"])

    def test_preset_restart_uses_file_key_and_ignores_inherited_key_during_retarget(self):
        path = "/settings/llm/presets"
        self.assertEqual(200, self.client.post(path, json={
            "name": "managed", "base_url": "https://a.invalid", "api_key": "fake-a",
        }).status_code)
        y, data = settings_ext._load_yaml()
        data["llm_presets"]["legacy"] = {"base_url": "https://legacy.invalid", "api_key": ""}
        settings_ext._save_yaml(y, data)

        def restart(expected):
            with patch.dict(os.environ, {
                "MANAGED_API_KEY": "fake-a",
                "LEGACY_API_KEY": "fake-inherited",
            }, clear=True):
                load_dotenv(self.env, override=False)
                loaded = AppConfig.load(str(self.yaml))
                self.assertEqual("fake-a", loaded.llm_presets["managed"]["api_key"])
                settings_ext.load_managed_preset_keys(loaded)
                self.assertEqual("https://b.invalid", loaded.llm_presets["managed"]["base_url"])
                self.assertEqual(expected, loaded.llm_presets["managed"]["api_key"])
                self.assertEqual("fake-inherited", loaded.llm_presets["legacy"]["api_key"])

        real_write = settings_ext._write_env_file

        def observe_after_yaml(updates):
            if updates.get("MANAGED_API_KEY"):
                restart("")
            real_write(updates)

        with patch.object(settings_ext, "_write_env_file", side_effect=observe_after_yaml):
            self.assertEqual(200, self.client.put(f"{path}/managed", json={
                "name": "managed", "base_url": "https://b.invalid", "api_key": "fake-b",
            }).status_code)
        restart("fake-b")

    def test_preset_create_restores_absent_files_when_first_dotenv_write_fails(self):
        real_replace = os.replace

        def reject_dotenv(src, dest):
            if Path(dest) == self.env:
                raise OSError("synthetic dotenv failure")
            real_replace(src, dest)

        with patch.object(os, "replace", side_effect=reject_dotenv), self.assertRaises(OSError):
            self.client.post("/settings/llm/presets", json={
                "name": "new", "base_url": "https://b.invalid", "api_key": "fake-b",
            })
        self.assertFalse(self.yaml.exists())
        self.assertFalse(self.env.exists())
        self.assertNotIn("NEW_API_KEY", os.environ)
        self.assertNotIn("new", self.config.llm_presets)

    def test_analysis_online_matches_startup_selection_and_unregisters_stale(self):
        self.config.default_llm = "active"
        self.config.llm_presets = {"active": {"model": "fake-active"}, "aux": {"model": "fake-aux"}}
        path = "/settings/secondary_models"
        with patch.object(registry, "get_provider", return_value=object()) as provider:
            self.assertEqual(200, self.client.post(path, json={
                "analysis_enabled": True, "analysis_preset": "", "analysis_max_tokens": 55,
            }).status_code)
            self.assertEqual("fake-active", provider.call_args.args[0]["model"])
            self.assertEqual(55, provider.call_args.args[1]["max_tokens"])
            self.assertIsNotNone(registry.get_provider_for_role("analysis"))
            for name in ("__none__", "missing"):
                self.assertEqual(200, self.client.post(path, json={
                    "analysis_enabled": True, "analysis_preset": name,
                }).status_code)
                self.assertIsNone(registry.get_provider_for_role("analysis"))
                registry.register_role_provider("analysis", object())

    def test_profile_windows_traversal_and_junction_are_rejected(self):
        external = self.root / "external"
        external.mkdir()
        card = external / "card.json"
        card.write_text('{"display_name":"synthetic"}', encoding="utf-8")
        result = self.client.get("/profiles/..%5Cexternal%5Ccard/special_dates")
        self.assertEqual(400, result.status_code)
        alias = self.profiles / "linked"
        if os.name == "nt":
            subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(alias), str(external)],
                           check=True, capture_output=True)
        else:
            alias.symlink_to(external, target_is_directory=True)
        self.assertEqual(400, self.client.get("/profiles/linked/special_dates").status_code)
        self.assertEqual('{"display_name":"synthetic"}', card.read_text(encoding="utf-8"))

    def test_profile_memory_explicit_null_removes_only_specified_overrides(self):
        (self.profiles / "example.json").write_text(
            '{"memory_config":{"enabled":true,"vector_enabled":true,"max_history_turns":30,'
            '"embedding":{"provider":"local","device":"cpu"}}}',
            encoding="utf-8",
        )
        path = "/profiles/example/memory_config"
        self.app.state.memory_managers = {"example": object()}
        self.assertEqual(200, self.client.put(path, json={
            "enabled": False, "max_history_turns": None, "embedding_provider": None,
        }).status_code)
        self.assertEqual({"memory_config": {
            "enabled": False, "vector_enabled": True, "embedding": {"device": "cpu"},
        }}, self.client.get(path).json())
        self.assertNotIn("example", self.app.state.memory_managers)

    def test_delete_profile_retry_yields_and_stops_if_profile_was_recreated(self):
        card = self.profiles / "example.json"
        card.write_text("{}", encoding="utf-8")
        data_dir = self.profiles / "example"
        data_dir.mkdir()
        self.app.state.session_manager = SimpleNamespace(_sessions={}, _current_id=None, remove_session=Mock())

        async def recreate(_delay):
            card.write_text('{"display_name":"recreated"}', encoding="utf-8")

        with patch.object(settings_ext.shutil, "rmtree", side_effect=[PermissionError, None]) as remove, \
                patch.object(settings_ext.asyncio, "sleep", new=AsyncMock(side_effect=recreate)) as sleep:
            self.assertEqual(200, self.client.delete("/profiles/example").status_code)
        sleep.assert_awaited_once_with(0.5)
        remove.assert_called_once()
        self.assertTrue(card.exists())
        self.assertTrue(data_dir.exists())

    def test_adjacent_settings_routes_reject_cross_site_before_dispatch(self):
        for method, path in (
            ("PATCH", "/api/agent/settings"),
            ("GET", "/api/tools"),
            ("PATCH", "/api/profiles/example/tool_configs"),
            ("POST", "/api/profiles/example/trend/fetch"),
            ("POST", "/api/preferences"),
            ("POST", "/api/setup/pip-install"),
        ):
            with self.subTest(path=path):
                response = self.client.request(method, path, headers={"Sec-Fetch-Site": "cross-site"}, json={})
                self.assertEqual(403, response.status_code)
        same = {"Origin": "http://127.0.0.1:7788"}
        self.assertEqual(200, self.client.post("/api/preferences", headers=same, json={"theme": "test"}).status_code)
        self.assertEqual("test", self.client.get("/api/preferences", headers=same).json()["theme"])

    def test_cross_origin_memory_and_session_delete_cannot_reach_stores(self):
        clear = Mock()
        self.app.state.session_manager = SimpleNamespace(
            get_by_id=Mock(return_value=SimpleNamespace(conversation_store=SimpleNamespace(clear=clear))),
        )
        with patch.object(memory, "_require_manager", return_value=SimpleNamespace(
            facts=SimpleNamespace(clear_all=Mock(return_value=1)), prune_curation_index=AsyncMock(),
        )) as manager, patch.object(memory, "log_fact_clear_all"):
            for path in ("/memory/example/facts/all", "/sessions/example/history"):
                self.assertEqual(403, self.client.delete(path, headers={"Origin": "http://8.8.8.8"}).status_code)
            clear.assert_not_called()
            manager.assert_not_called()
            lan = "http://192.168.1.15:7788"
            for path in ("/memory/example/facts/all", "/sessions/example/history"):
                self.assertEqual(200, self.client.delete(lan + path, headers={"Origin": lan}).status_code)
            clear.assert_called_once()
            manager.assert_called_once()

    def test_cross_origin_memory_delete_is_guarded_under_asgi_root_path(self):
        with TestClient(self.app, base_url="http://127.0.0.1:7788", root_path="/prefix") as client:
            response = client.delete("/prefix/memory/example/facts/all", headers={"Origin": "http://8.8.8.8"})
        self.assertEqual(403, response.status_code)

    def test_tavern_import_leaves_predictable_temp_alias_untouched(self):
        target = self.root / "unrelated.txt"
        target.write_text("untouched", encoding="utf-8")
        alias = self.profiles / "imported.json.tmp"
        os.link(target, alias)
        with patch.object(profiles_import, "PROFILES_DIR", str(self.profiles)):
            profiles_import._save_profile("imported", {"display_name": "Imported"})
        self.assertEqual("untouched", target.read_text(encoding="utf-8"))
        self.assertTrue(alias.exists())
        self.assertTrue((self.profiles / "imported.json").is_file())

    def test_settings_reject_cross_origin_even_with_cors_and_dns_rebinding(self):
        path = "/settings/system/env"
        with patch.object(settings_ext, "_ENV_FILE", str(self.env)):
            for origin in ("http://8.8.8.8", "http://127.0.0.1:7789", "null", "http://["):
                self.assertEqual(403, self.client.get(path, headers={"Origin": origin}).status_code)
            self.assertEqual(403, self.client.post(path, headers={"Sec-Fetch-Site": "cross-site"}, json={}).status_code)
            self.assertEqual(403, self.client.get("http://rebind.invalid:7788" + path,
                                                  headers={"Origin": "http://rebind.invalid:7788"}).status_code)
            for host in ("127.0.0.1", "192.168.1.15", "[::1]"):
                origin = f"http://{host}:7788"
                self.assertEqual(200, self.client.get(path, headers={
                    "Host": f"{host}:7788", "Origin": origin,
                }).status_code)
            self.assertEqual(200, self.client.get(path).status_code)
            os.environ["SHIKIGAMI_SETTINGS_TRUSTED_HOSTS"] = "other.invalid, app.example.invalid"
            tunnel = "https://app.example.invalid"
            self.assertEqual(200, self.client.get(tunnel + path, headers={"Origin": tunnel}).status_code)
            self.assertEqual(403, self.client.get(tunnel + path, headers={"Origin": "http://app.example.invalid"}).status_code)
