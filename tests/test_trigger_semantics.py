"""Trigger-mode convergence: one semantics, three call sites.

base.PromptSegment.is_triggered, pipeline._custom_triggered and
ase._ase_segment_is_triggered must agree on shared mode semantics.
"""
import time
from types import SimpleNamespace
from unittest import TestCase

from src.prompt.base import PromptSegment, SegmentResult, evaluate_trigger_mode
from src.prompt.pipeline import _custom_triggered
from src.prompt.segment_config import CustomSegmentDef
from src.core.ase import _ase_segment_is_triggered


class _Dummy(PromptSegment):
    segment_id = "dummy"
    priority = 50
    label = "dummy"

    def build(self, ctx):
        return SegmentResult(fired=False)


def _ctx(*, last_user_ts=None, fired_date="", user_turns=0, hour=None, recent=(), user_msg=""):
    return SimpleNamespace(
        session=SimpleNamespace(
            last_user_message_time=last_user_ts if last_user_ts is not None else time.time(),
            get_segment_last_fired_date=lambda segment_id: fired_date,
        ),
        store=SimpleNamespace(
            total_user_messages_count=lambda name: user_turns,
            get_recent=lambda n: list(recent),
        ),
        extras={"user_name": "u", **({"current_hour": hour} if hour is not None else {})},
        user_msg=user_msg,
    )


def _custom(mode, param=1.0, keywords=None):
    return CustomSegmentDef(segment_id="c1", label="c1", content="x",
                            trigger_mode=mode, trigger_param=param,
                            trigger_keywords=keywords)


class SharedSemanticsTests(TestCase):
    def test_time_window_wraps_midnight(self):
        self.assertTrue(evaluate_trigger_mode("time_window", 2310, current_hour=23))
        self.assertTrue(evaluate_trigger_mode("time_window", 2310, current_hour=10))
        self.assertFalse(evaluate_trigger_mode("time_window", 2310, current_hour=15))
        self.assertTrue(evaluate_trigger_mode("time_window", 510, current_hour=7))
        self.assertFalse(evaluate_trigger_mode("time_window", 510, current_hour=11))

    def test_cooldown_and_never_fired(self):
        now = time.time()
        self.assertTrue(evaluate_trigger_mode("cooldown", 30, now=now, last_fired=None))
        self.assertTrue(evaluate_trigger_mode("cooldown", 30, now=now, last_fired=now - 3600))
        self.assertFalse(evaluate_trigger_mode("cooldown", 30, now=now, last_fired=now - 60))

    def test_once_per_day_via_fired_today(self):
        self.assertTrue(evaluate_trigger_mode("once_per_day", 1.0, fired_today=False))
        self.assertFalse(evaluate_trigger_mode("once_per_day", 1.0, fired_today=True))
        self.assertTrue(evaluate_trigger_mode("once_per_day", 1.0, last_fired=None))

    def test_turn_count_modes(self):
        self.assertTrue(evaluate_trigger_mode("every_n_turns", 5, user_turn_count=4))
        self.assertFalse(evaluate_trigger_mode("every_n_turns", 5, user_turn_count=3))
        self.assertTrue(evaluate_trigger_mode("first_turn_only", 1.0, user_turn_count=0))
        self.assertFalse(evaluate_trigger_mode("first_turn_only", 1.0, user_turn_count=2))
        self.assertFalse(evaluate_trigger_mode("every_n_turns", 5))  # no context

    def test_keyword_full_width_delimiters(self):
        self.assertTrue(evaluate_trigger_mode(
            "keyword", 10, trigger_keywords="天气，下雨\n带伞", keyword_texts=["今天下雨了"]))
        self.assertFalse(evaluate_trigger_mode(
            "keyword", 10, trigger_keywords="天气，下雨", keyword_texts=["聊点别的"]))

    def test_unknown_mode_fires(self):
        self.assertTrue(evaluate_trigger_mode("nonexistent_mode", 1.0))


class CallSiteAgreementTests(TestCase):
    def test_time_window_agrees_across_sites(self):
        now = time.time()
        seg = _Dummy()
        ctx = _ctx(hour=15)
        custom = _custom("time_window", 510)
        ase = lambda: _ase_segment_is_triggered("time_window", 510, 15, 0.0, {}, "c1", {})
        self.assertFalse(seg.is_triggered("time_window", 510, True, ctx))
        self.assertFalse(_custom_triggered(custom, ctx))
        self.assertFalse(ase())
        ctx9 = _ctx(hour=9)
        self.assertTrue(seg.is_triggered("time_window", 510, True, ctx9))
        self.assertTrue(_custom_triggered(custom, ctx9))
        self.assertTrue(_ase_segment_is_triggered("time_window", 510, 9, 0.0, {}, "c1", {}))
        _ = now

    def test_cooldown_agrees(self):
        seg = _Dummy()
        seg._last_fired = time.time() - 60  # fired a minute ago
        self.assertFalse(seg.is_triggered("cooldown", 30, True))
        # Custom segments are stateless: cooldown behaves as never-fired.
        self.assertTrue(_custom_triggered(_custom("cooldown", 30), _ctx()))
        # ASE: numeric state younger than the cooldown blocks firing.
        self.assertFalse(_ase_segment_is_triggered(
            "cooldown", 30, 12, 0.0, {"c1": time.time() - 60}, "c1", {}))
        self.assertTrue(_ase_segment_is_triggered(
            "cooldown", 30, 12, 0.0, {"c1": time.time() - 3600}, "c1", {}))

    def test_once_per_day_agrees(self):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        seg = _Dummy()
        self.assertFalse(seg.is_triggered("once_per_day", 1.0, True, _ctx(fired_date=today)))
        self.assertFalse(_custom_triggered(_custom("once_per_day"), _ctx(fired_date=today)))
        self.assertFalse(_ase_segment_is_triggered(
            "once_per_day", 1.0, 12, 0.0, {"c1": time.time()}, "c1", {}))
        self.assertTrue(seg.is_triggered("once_per_day", 1.0, True, _ctx(fired_date="2020-01-01")))
        self.assertTrue(_custom_triggered(_custom("once_per_day"), _ctx(fired_date="2020-01-01")))

    def test_every_n_turns_agrees_between_chat_sites(self):
        seg = _Dummy()
        self.assertTrue(seg.is_triggered("every_n_turns", 5, True, _ctx(user_turns=4)))
        self.assertTrue(_custom_triggered(_custom("every_n_turns", 5), _ctx(user_turns=4)))
        self.assertFalse(seg.is_triggered("every_n_turns", 5, True, _ctx(user_turns=3)))
        # ASE has no turn context: always allowed.
        self.assertTrue(_ase_segment_is_triggered("every_n_turns", 5, 12, 0.0, {}, "c1", {}))

    def test_first_after_silence_agrees(self):
        old = time.time() - 3600  # user silent for 60 minutes
        seg = _Dummy()
        self.assertTrue(seg.is_triggered("first_after_silence", 30, True, _ctx(last_user_ts=old)))
        self.assertTrue(_custom_triggered(_custom("first_after_silence", 30), _ctx(last_user_ts=old)))
        # ASE: silence long enough and not yet fired this silence round.
        self.assertTrue(_ase_segment_is_triggered(
            "first_after_silence", 30, 12, 3600.0, {}, "c1", {}, last_user_ts=old))
        # ASE: already fired within this silence round does not retrigger.
        self.assertFalse(_ase_segment_is_triggered(
            "first_after_silence", 30, 12, 7200.0, {"c1": old + 100}, "c1", {}, last_user_ts=old))
        # ASE: silence too short.
        self.assertFalse(_ase_segment_is_triggered(
            "first_after_silence", 30, 12, 60.0, {}, "c1", {}, last_user_ts=old))
