"""Regression tests: ASE hint segments must be injected exactly once.

The ASE engine injects hint segments (morning greeting, long-silence
check-in, ASE-targeted custom segments) via _compute_ase_segment_injections
→ greeting_hint, owning user content overrides and trigger state. The
pipeline must skip them in mode="ase" so a heartbeat never injects the
same hint twice.
"""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.prompt import pipeline
from src.prompt.base import PromptSegment, SegmentResult
from src.prompt.segment_config import CustomSegmentDef, SegmentConfig
from src.prompt.segments.ase.morning_greeting import AseMorningGreetingSegment


class _AseHint(PromptSegment):
    segment_id = "test_ase_hint"
    inject_into = "ase"
    priority = 50
    label = "ASE 提示（引擎注入）"
    ase_engine_injected = True

    def build(self, ctx):
        return SegmentResult(messages=[{"role": "system", "content": "HINT-CONTENT"}])


class _AseScene(PromptSegment):
    segment_id = "test_ase_scene"
    inject_into = "ase"
    priority = 60
    label = "ASE 场景（管线注入）"

    def build(self, ctx):
        return SegmentResult(messages=[{"role": "system", "content": "SCENE-CONTENT"}])


def _session(store):
    return SimpleNamespace(
        profile_id="offline-ase-dedup",
        display_name="Alice",
        storage_root="",
        last_user_message_time=time.time(),
        conversation_store=store,
        get_segment_last_fired=lambda sid: 0.0,
        set_segment_last_fired=lambda *a: None,
        get_segment_last_fired_date=lambda sid: "",
        set_segment_last_fired_date=lambda *a: None,
    )


def _store():
    return SimpleNamespace(
        get_recent_with_ts=lambda n: [],
        get_recent=lambda n: [],
        total_user_messages_count=lambda u: 0,
    )


def _build(segments, seg_cfg, **kwargs):
    session = _session(_store())
    with patch.object(pipeline._profile_loader, "load", return_value={}), \
            patch.object(pipeline, "load_segment_config", return_value=seg_cfg), \
            patch.object(pipeline, "get_registered", return_value=segments), \
            patch.object(pipeline, "_segment_instances", {}):
        return pipeline.build_messages(session, "", session.conversation_store, **kwargs)


class AseDedupTests(unittest.TestCase):
    def test_engine_injected_segment_is_skipped_in_ase_mode(self):
        messages = _build([_AseHint, _AseScene], SegmentConfig(), pipeline_mode="ase")
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        self.assertNotIn("HINT-CONTENT", system)
        self.assertIn("SCENE-CONTENT", system)

    def test_real_morning_greeting_is_skipped_in_ase_mode(self):
        # 7 AM on 2026-11-10: inside the default 5–10 trigger window, so
        # without the skip the greeting would be injected by the pipeline.
        seven_am = 1780534800.0
        with patch("src.prompt.pipeline.time.time", return_value=seven_am):
            messages = _build([AseMorningGreetingSegment], SegmentConfig(), pipeline_mode="ase")
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        self.assertNotIn(AseMorningGreetingSegment.DEFAULT_CONTENT, system)

    def test_ase_targeted_custom_segment_is_skipped_in_ase_mode(self):
        cfg = SegmentConfig(custom_segments=[
            CustomSegmentDef(segment_id="c_ase", label="ase", content="CUSTOM-ASE",
                             inject_into="ase"),
            CustomSegmentDef(segment_id="c_chat", label="chat", content="CUSTOM-CHAT",
                             inject_into="chat"),
        ])
        messages = _build([], cfg, pipeline_mode="ase")
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        self.assertNotIn("CUSTOM-ASE", system)
        self.assertIn("CUSTOM-CHAT", system)

    def test_chat_mode_is_unaffected_by_the_marker(self):
        # A chat-targeted segment with the marker still fires in chat mode:
        # the skip applies only to pipeline_mode="ase".
        class _ChatHint(_AseHint):
            segment_id = "test_chat_hint"
            inject_into = "chat"
        messages = _build([_ChatHint], SegmentConfig(), pipeline_mode="chat")
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        self.assertIn("HINT-CONTENT", system)


if __name__ == "__main__":
    unittest.main()
