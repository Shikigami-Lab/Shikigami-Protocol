from unittest import TestCase

from src.memory.extraction_batches import plan_batch


def row(id, content, source="single", role="user", timestamp=None):
    return {"source": source, "id": id, "content": content,
            "timestamp": timestamp if timestamp is not None else id, "role": role}


class ExtractionBatchTests(TestCase):
    def test_long_message_resumes_and_cursor_commits_only_on_full_coverage(self):
        original = row(2, "é" * 7000 + "important correction")
        parts, checked, cursors, offsets = plan_batch(
            {"single": [original, row(3, "later")]}, {"single": 1}, {}, limit=7000,
        )
        self.assertEqual("".join(item["content"] for item in parts), "é" * 7000)
        self.assertEqual(checked, [original])
        self.assertEqual(cursors["single"], 1)
        self.assertEqual(offsets["single"]["offset"], 7000)
        self.assertEqual(parts[0]["reference"], "single:2#0-6000")
        parts, checked, cursors, offsets = plan_batch(
            {"single": [original, row(3, "later")]}, cursors, offsets, limit=7000,
        )
        self.assertEqual([item["content"] for item in parts], ["important correction", "later"])
        self.assertEqual(parts[0]["reference"], "single:2#7000-7020")
        self.assertEqual(parts[0]["start_char"], 7000)
        self.assertEqual(cursors["single"], 3)
        self.assertFalse(offsets)

    def test_edited_or_deleted_partial_message_does_not_skip_new_text(self):
        before = row(2, "before" * 8000)
        _, _, cursors, offsets = plan_batch({"single": [before]}, {"single": 1}, {}, limit=100)
        for changed in (row(2, "edited"), row(3, "following")):
            parts, _, after, pending = plan_batch({"single": [changed]}, cursors, offsets, limit=100)
            self.assertEqual([item["content"] for item in parts], [changed["content"]])
            self.assertEqual(after["single"], changed["id"])
            self.assertFalse(pending)

    def test_per_source_id_order_even_when_message_timestamps_are_reordered(self):
        items = [row(4, "first", timestamp=20), row(5, "second", timestamp=5)]
        parts, _, cursors, offsets = plan_batch({"single": items}, {"single": 3}, {}, limit=5)
        self.assertEqual([item["id"] for item in parts], [4])
        self.assertEqual(cursors["single"], 4)
        self.assertFalse(offsets)

    def test_failed_reply_skipped_without_consuming_the_budget(self):
        bad = {**row(3, "x" * 8000, role="assistant"), "generation_status": "failed"}
        parts, checked, cursors, offsets = plan_batch(
            {"single": [bad, row(4, "user")]}, {"single": 2}, {}, limit=4,
        )
        self.assertEqual([item["reference"] for item in parts], ["single:4"])
        self.assertEqual([item["id"] for item in checked], [4])
        self.assertEqual(cursors["single"], 4)
        self.assertFalse(offsets)
