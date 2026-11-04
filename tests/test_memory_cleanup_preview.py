import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.memory.cleanup_preview import render_cleanup_preview


def fact(fact_id, content, **metadata):
    return {
        "id": fact_id, "content": content, "source": "auto", "is_manual": False,
        "category": "preference", "weight": 0.2, "updated_at": 100, **metadata,
    }


def run(run_id, sources=(), added=(), **metadata):
    return {
        "run_id": run_id,
        "rollback_data": {"consolidation": {
            "vectors_removed": list(sources), "added": list(added),
        }},
        "summary": {"consolidation": {"added_contents": ["SECRET history"]}},
        **metadata,
    }


def section(report, title):
    return report.split("## " + title + "\n", 1)[1].split("\n## ", 1)[0]


class MemoryCleanupPreviewTests(unittest.TestCase):
    def test_normalization_preserves_numbers_dates_signs_negation_and_case(self):
        facts = [
            fact("f_a", " SECRET attends  on 2026-11-05 at +10. "),
            fact("f_b", "SECRET attends on 2026-11-05 at +10."),
            fact("f_c", "SECRET attends on 2026-11-05 at -10."),
            fact("f_d", "SECRET attends on 2026-11-06 at +10."),
            fact("f_e", "SECRET never attends on 2026-11-05 at +10."),
            fact("f_f", "SECRET attends on 2026-11-05 at +100."),
            fact("F_A", "Secret attends on 2026-11-05 at +10."),
        ]

        report = render_cleanup_preview({"facts": facts}, [])
        groups = section(report, "Exact normalized-text groups")
        self.assertIn('Members — `"f_a"`, `"f_b"`', groups)
        self.assertEqual(groups.count("Members —"), 1)
        self.assertIn("Exact ID collisions — 0", report)
        self.assertNotIn("SECRET", report)

    def test_protection_inactive_and_id_collisions_are_excluded_from_pilot(self):
        facts = [
            fact("f_auto", "shared"),
            fact("f_manual", "shared", source="manual"),
            fact("f_pinned", "pinned only", pinned=True),
            {"id": "f_legacy", "content": "legacy"},
            fact("f_flag", "manually flagged", is_manual=True),
            fact("f_archived", "shared", status="archived"),
            fact("f_collision", "collision"),
            fact("f_collision", "different collision", pinned=True),
            fact("f_blank", " "),
        ]

        report = render_cleanup_preview({"facts": facts}, [])
        self.assertIn("Total fact records — 9", report)
        self.assertIn("Active fact records — 8", report)
        self.assertIn("Protected fact IDs — 5", report)
        self.assertIn('Keep — `"f_manual"`', report)
        self.assertIn('`"f_collision"` — 2 records', report)
        pilot = section(report, "Exact-match pilot")
        self.assertIn('Review `"f_auto"`. Keep `"f_manual"`.', pilot)
        for held_id in ("f_manual", "f_pinned", "f_flag", "f_legacy", "f_archived", "f_collision", "f_blank"):
            self.assertNotIn(f'Review `"{held_id}"`', pilot)
            self.assertNotIn(held_id, section(report, "Broader inventory"))

    def test_repeated_batch_uses_exact_source_id_set_and_holds_generated_summaries(self):
        facts = [
            fact("f_1", "source"),
            fact("f_10", "different source"),
            fact("f_s1", "same summary"),
            fact("f_s2", "same summary"),
            fact("f_s3", "reused source"),
            fact("f_extra", "same summary"),
        ]
        changelog = [
            run("run_b", ("f_missing", "f_1"), ("f_s1",)),
            run("run_a", ("f_1", "f_missing"), ("f_s2",), rolled_back=True),
            run("run_c", ("f_10", "f_missing"), ("f_s3",)),
            run("run_d", (), ("f_s3",)),
            run("run_e"),
        ]

        report = render_cleanup_preview(facts, changelog)
        self.assertIn("Active fact records — 6", report)
        self.assertIn("Repeated exact source batches — 1", report)
        self.assertIn("Source IDs recorded in multiple runs — 2", report)
        self.assertIn("Generated summary IDs in snapshot — 3", report)
        self.assertIn("Missing referenced fact IDs — 1", report)
        groups = section(report, "Consolidation history — review only")
        self.assertIn('Source IDs — `"f_1"`, `"f_missing"`', groups)
        self.assertIn('Run `"run_a"` (rolled back). Generated IDs — `"f_s2"`', groups)
        self.assertIn('Run `"run_b"` (recorded). Generated IDs — `"f_s1"`', groups)
        self.assertIn('Run `"run_d"` (recorded). Generated IDs — `"f_s3"`', groups)
        self.assertIn('Missing from snapshot — `"f_missing"`', groups)
        self.assertIn('Sources recorded in multiple runs — `"f_1"`, `"f_missing"`', groups)
        self.assertIn("Review-only group", section(report, "Exact normalized-text groups"))
        self.assertIn("0 candidate IDs", section(report, "Exact-match pilot"))
        self.assertNotIn("SECRET history", report)
        self.assertEqual(report, render_cleanup_preview(facts[::-1], changelog[::-1]))

    def test_same_run_repeated_in_changelog_does_not_make_a_repeated_batch(self):
        entry = run("run_a", ("f_1", "f_1"), ("f_s1", "f_s1"))
        report = render_cleanup_preview([], [entry, entry])
        self.assertIn("Consolidation run records — 1", report)
        self.assertIn("Repeated exact source batches — 0", report)
        self.assertIn("Source IDs recorded in multiple runs — 0", report)

    def test_id_display_escapes_markdown_without_collapsing_distinct_ids(self):
        facts = [fact("f`1", "shared"), fact(" f`1", "shared")]
        report = render_cleanup_preview(facts, [])
        self.assertIn('Members — `" f\\u00601"`, `"f\\u00601"`', report)
        self.assertIn("Distinct fact IDs — 2", report)

    def test_broader_inventory_prioritizes_oldest_low_weight_facts_and_limits_sample(self):
        facts = [
            fact("recent", "recent", updated_at=200),
            fact("older", "older", updated_at=50),
            fact("heavy", "heavy", weight=0.7, updated_at=10),
            fact("undated", "undated", updated_at=None),
            fact("invalid_weight", "invalid", weight=float("nan")),
            fact("unknown_source", "unknown", source="SECRET source"),
        ]
        report = render_cleanup_preview(facts, [], sample_limit=1)
        inventory = section(report, "Broader inventory")
        self.assertIn("Low-weight dated review pool — 2", inventory)
        self.assertIn('Sample IDs — `"older"`', inventory)
        self.assertNotIn('`"recent"`', inventory)
        self.assertNotIn("SECRET source", report)

    def test_lifecycle_counts_use_explicit_snapshot_time(self):
        facts = [
            fact("archived", "archived", archived=True),
            fact("expired", "expired", expires_at=100),
            fact("future", "future", expires_at=101),
            fact("invalid", "invalid", expires_at="SECRET"),
            fact("legacy", "legacy"),
        ]
        report = render_cleanup_preview(facts, [], as_of=100)
        self.assertIn("Active fact records — 2", report)
        self.assertIn("Inactive fact records — 2", report)
        self.assertIn("Expired fact records — 1", report)
        self.assertIn("Unknown-status fact records — 1", report)
        inventory = section(report, "Broader inventory")
        self.assertIn('Sample IDs — `"future"`, `"legacy"`', inventory)
        self.assertNotIn("SECRET", report)
        without_time = render_cleanup_preview(facts, [])
        self.assertIn("Active fact records — 1", without_time)
        self.assertIn("Unknown-status fact records — 3", without_time)

    def test_text_opt_in_preserves_original_text_and_cannot_break_out_of_fence(self):
        original = "SECRET  文本\n```\n<img src=https://example.invalid/tracker>"
        facts = [fact("f_text", original, emotional_note="SECRET note")]
        snapshot = copy.deepcopy(facts)
        self.assertNotIn("SECRET", render_cleanup_preview(facts, []))
        report = render_cleanup_preview(facts, [], include_text=True)
        self.assertIn("\n````\n" + original + "\n````\n", report)
        self.assertNotIn("SECRET note", report)
        self.assertEqual(facts, snapshot)

    def test_malformed_inputs_fail_without_echoing_fact_data(self):
        for facts, changelog in [
            ({"facts": "SECRET"}, []),
            ([{"content": "SECRET"}], []),
            ([], {"summary": "SECRET"}),
            ([], [run("run", ("source",), (None,))]),
        ]:
            with self.subTest():
                with self.assertRaises(ValueError) as error:
                    render_cleanup_preview(facts, changelog)
                self.assertNotIn("SECRET", str(error.exception))

    def test_cli_reads_snapshots_and_writes_markdown_to_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            facts_path = Path(directory) / "facts.json"
            log_path = Path(directory) / "changelog.json"
            facts_bytes = json.dumps({"facts": [fact("f_cli", "SECRET 中文")]}).encode()
            log_bytes = b"[]"
            facts_path.write_bytes(facts_bytes)
            log_path.write_bytes(log_bytes)
            command = [
                sys.executable, "-B", "-m", "src.memory.cleanup_preview",
                str(facts_path), str(log_path),
            ]
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, check=True)
            self.assertIn(b"# Fact cleanup preview", result.stdout)
            self.assertNotIn(b"SECRET", result.stdout)
            result = subprocess.run(
                [*command, "--include-text", "--as-of", "100"],
                stdin=subprocess.DEVNULL, capture_output=True, check=True,
            )
            self.assertIn("SECRET 中文", result.stdout.decode("utf-8"))
            self.assertEqual(facts_path.read_bytes(), facts_bytes)
            self.assertEqual(log_path.read_bytes(), log_bytes)


if __name__ == "__main__":
    unittest.main()
