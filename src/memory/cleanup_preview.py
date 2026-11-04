"""Render an offline, read-only review of a fact snapshot and memory changelog."""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import sys
from typing import Optional


_SOURCES = ("auto", "manual", "reflection")
_CATEGORIES = ("ai_insight", "habit", "location", "milestone", "other",
               "preference", "relationship", "taboo")
_INACTIVE_STATES = ("archived", "inactive", "superseded")
_LOW_WEIGHT = 0.3


def _finite_number(value):
    if type(value) in (int, float) and -math.inf < value < math.inf:
        return value
    return None


def _valid_id(value):
    return isinstance(value, str) and bool(value.strip())


def _code_id(value):
    # JSON quoting keeps whitespace and control characters unambiguous.
    literal = json.dumps(value).replace("`", r"\u0060")
    return f"`{literal}`"


def _ids(values):
    return ", ".join(_code_id(value) for value in sorted(values)) or "None"


@dataclass(frozen=True)
class _Fact:
    id: str
    content: str
    source: str
    category: str
    state: str
    protected: bool
    weight: Optional[float]
    updated_at: Optional[float]

    @property
    def normalized_text(self):
        # Leave case, line boundaries, punctuation, signs and all tokens intact.
        return re.sub(r"[ \t]+", " ", self.content.strip())

    @classmethod
    def from_record(cls, record, index, as_of):
        if not isinstance(record, dict) or not _valid_id(record.get("id")):
            raise ValueError(f"Fact record {index} must have a nonempty string ID.")
        content = record.get("content", "")
        if not isinstance(content, str):
            raise ValueError(f"Fact record {index} must have string content.")
        source = record.get("source", "manual")
        category = record.get("category", "other")
        status = record.get("status", "active")
        archived = record.get("archived", False)
        expiry = record.get("expires_at")
        if archived is True or status in _INACTIVE_STATES:
            state = "inactive"
        elif archived is not False or status != "active":
            state = "unknown"
        elif expiry is None:
            state = "active"
        elif as_of is None or _finite_number(expiry) is None:
            state = "unknown"
        else:
            state = "active" if expiry > as_of else "expired"
        return cls(
            record["id"], content, source if source in _SOURCES else "unknown",
            category if category in _CATEGORIES else "unknown", state,
            source == "manual" or bool(record.get("is_manual", True)) or bool(record.get("pinned", False)),
            _finite_number(record.get("weight")), _finite_number(record.get("updated_at")),
        )


@dataclass(frozen=True, order=True)
class _Run:
    id: str
    rolled_back: bool
    sources: tuple
    generated: tuple


def _read_runs(changelog):
    if not isinstance(changelog, list):
        raise ValueError("Memory changelog must be a list of run objects.")
    runs = []
    for index, entry in enumerate(changelog, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"Changelog record {index} must be an object.")
        rollback = entry.get("rollback_data")
        if rollback is None:
            continue
        consolidation = rollback.get("consolidation") if isinstance(rollback, dict) else None
        if not isinstance(rollback, dict) or (consolidation is not None and not isinstance(consolidation, dict)):
            raise ValueError(f"Changelog record {index} has invalid rollback data.")
        if consolidation is None:
            continue
        sources = consolidation.get("vectors_removed", [])
        generated = consolidation.get("added", [])
        for ids in (sources, generated):
            if not isinstance(ids, list) or not all(_valid_id(value) for value in ids):
                raise ValueError(f"Changelog record {index} has invalid consolidation IDs.")
        if not (sources or generated):
            continue
        if not _valid_id(entry.get("run_id")) or type(entry.get("rolled_back", False)) is not bool:
            raise ValueError(f"Changelog record {index} has an invalid run ID or rollback flag.")
        runs.append(_Run(
            entry["run_id"], entry.get("rolled_back", False),
            tuple(sorted(set(sources))), tuple(sorted(set(generated))),
        ))
    return sorted(set(runs))


def _append_consolidation(lines, batches, repeated_batches, reused_sources, fact_ids):
    lines.extend([
        "", "## Consolidation history — review only", "",
        "A shared source ID set records repeated inputs. Each run's generated summaries need an independent review.",
        "Source IDs are recorded vector targets. Current vector availability cannot be determined from these inputs.",
        "",
        f"- Sources recorded in multiple runs — {_ids(reused_sources)}",
        "",
    ])
    if not batches:
        lines.append("No consolidation batches recorded in the supplied changelog.")
    for sources, runs in sorted(batches.items()):
        missing = (set(sources) | {fid for run in runs for fid in run.generated}) - fact_ids
        if not sources:
            label = "Runs without recorded source IDs"
        else:
            label = "Repeated exact source batch" if sources in repeated_batches else "Batch"
        lines.extend([f"### {label}", "", f"- Source IDs — {_ids(sources)}"])
        for run in runs:
            state = "rolled back" if run.rolled_back else "recorded"
            lines.append(f"- Run {_code_id(run.id)} ({state}). Generated IDs — {_ids(run.generated)}")
        if missing:
            lines.append(f"- Missing from snapshot — {_ids(missing)}")
        lines.append("")


def _retention_order(fact):
    weight = fact.weight if fact.weight is not None else -math.inf
    updated_at = fact.updated_at if fact.updated_at is not None else -math.inf
    return -weight, -updated_at, fact.id


def _append_exact_groups(lines, groups, by_id, held_ids):
    lines.extend([
        "", "## Exact normalized-text groups", "",
        "Matching trims outer whitespace and collapses spaces and tabs within lines. "
        "Verify provenance, tags and other metadata before choosing any cleanup.",
        "",
    ])
    if not groups:
        lines.append("No exact normalized-text groups.")
    candidate_count = 0
    pilot = []
    for ids in groups:
        lines.append(f"- Members — {_ids(ids)}")
        if ids & held_ids:
            lines.append("  - Review-only group due to an ID collision or consolidation history.")
            continue
        members = [by_id[fid][0] for fid in sorted(ids)]
        if any(fact.state == "unknown" or fact.source == "unknown" for fact in members):
            lines.append("  - Review-only group due to an unrecognized status or source.")
            continue
        active = [fact for fact in members if fact.state == "active"]
        keep = {fact.id for fact in active if fact.protected}
        if active and not keep:
            keep.add(min(active, key=_retention_order).id)
        extras = {fact.id for fact in active} - keep
        lines.append(f"  - Keep — {_ids(keep)}. Extra active IDs for review — {_ids(extras)}.")
        if extras:
            candidate_count += len(extras)
            pilot.append((min(extras), keep))
    return candidate_count, pilot


def _append_inventory(lines, facts, sample_limit):
    dated = [fact for fact in facts if fact.updated_at is not None and fact.updated_at > 0]
    low_weight = sorted(
        (fact for fact in dated if fact.weight is not None and 0 <= fact.weight < _LOW_WEIGHT),
        key=lambda fact: (fact.updated_at, fact.weight, fact.id),
    )
    lines.extend([
        "", "## Broader inventory", "",
        "This inventory covers ungrouped active auto and reflection facts outside protection, "
        "ID collisions and recorded consolidation history.",
        "",
        f"- Ungrouped review pool — {len(facts)}",
        f"- Low-weight dated review pool — {len(low_weight)} (weight below {_LOW_WEIGHT})",
        f"- Missing or invalid update time — {len(facts) - len(dated)}",
        "- Sample IDs — " + (", ".join(_code_id(fact.id) for fact in low_weight[:sample_limit]) or "None"),
        "",
        "| Source | Category | Records | Low-weight dated |",
        "| --- | --- | ---: | ---: |",
    ])
    counts = Counter((fact.source, fact.category) for fact in facts)
    low_counts = Counter((fact.source, fact.category) for fact in low_weight)
    for (source, category), count in sorted(counts.items()):
        lines.append(f"| {source} | {category} | {count} | {low_counts[source, category]} |")
    lines.extend([
        "",
        "Start with one extra per exact-match group. For broader reduction, review the oldest low-weight "
        "sample for temporary context and obsolete facts, then expand by source and category after a user review. "
        "Check dates, dependencies and retrieval impact before approving changes.",
    ])


def _append_text(lines, facts):
    lines.extend(["", "## Original text from the fact snapshot", ""])
    for fact in sorted(facts, key=lambda item: (item.id, item.content)):
        longest = max((len(match) for match in re.findall(r"`+", fact.content)), default=0)
        fence = "`" * max(3, longest + 1)
        lines.extend([f"### {_code_id(fact.id)}", "", fence, fact.content, fence, ""])


def render_cleanup_preview(fact_json, changelog_json, *, include_text=False, sample_limit=10, as_of=None):
    """Return Markdown from decoded Shikigami JSON snapshots.

    Facts may use the store's {"facts": [...]} envelope or a bare list.
    Legacy facts without lifecycle metadata count as active. Supply a Unix
    timestamp for expiry checks. Facts with an expiry and no reference time
    have unknown status. Lineage is limited to the supplied changelog.
    Sample limits apply to the review samples only.
    """
    if type(sample_limit) is not int or sample_limit < 0:
        raise ValueError("Sample limit must be a nonnegative integer.")
    if as_of is not None and _finite_number(as_of) is None:
        raise ValueError("Reference time must be a finite Unix timestamp.")
    records = fact_json.get("facts") if isinstance(fact_json, dict) else fact_json
    if not isinstance(records, list):
        raise ValueError("Fact snapshot must be a list or an object with a facts list.")
    facts = [_Fact.from_record(record, index, as_of) for index, record in enumerate(records, 1)]
    by_id = defaultdict(list)
    by_text = defaultdict(set)
    for fact in facts:
        by_id[fact.id].append(fact)
        normalized = fact.normalized_text
        if normalized:
            by_text[normalized].add(fact.id)
    fact_ids = set(by_id)
    protected = {fact.id for fact in facts if fact.protected}
    collisions = {fid for fid, members in by_id.items() if len(members) > 1}
    groups = sorted((ids for ids in by_text.values() if len(ids) > 1), key=sorted)
    grouped = set().union(*groups)

    runs = _read_runs(changelog_json)
    batches = defaultdict(list)
    for run in runs:
        batches[run.sources].append(run)
    repeated_batches = {sources for sources, members in batches.items()
                        if sources and len({run.id for run in members}) > 1}
    source_runs = defaultdict(set)
    for run in runs:
        for fid in run.sources:
            source_runs[fid].add(run.id)
    source_ids = set(source_runs)
    reused_sources = {fid for fid, run_ids in source_runs.items() if len(run_ids) > 1}
    generated_ids = {fid for run in runs for fid in run.generated}
    historical = source_ids | generated_ids
    active_states = Counter(fact.state for fact in facts)
    sources = Counter(fact.source for fact in facts)
    categories = Counter(fact.category for fact in facts)
    lines = [
        "# Fact cleanup preview", "",
        "Counts describe the supplied snapshots. Legacy records without lifecycle metadata count as active. "
        "Consolidation history may be incomplete. All candidates require review.",
        ("Expiry checks use Unix timestamp " + str(as_of) + "." if as_of is not None
         else "Supply a reference timestamp to classify records with expiry dates."),
        "",
        "## Counts", "",
        f"- Total fact records — {len(facts)}",
        f"- Distinct fact IDs — {len(fact_ids)}",
        f"- Active fact records — {active_states['active']}",
        f"- Inactive fact records — {active_states['inactive'] + active_states['expired']}",
        f"- Expired fact records — {active_states['expired']}",
        f"- Unknown-status fact records — {active_states['unknown']}",
        f"- Protected fact IDs — {len(protected)}",
        f"- Exact ID collisions — {len(collisions)}",
        f"- Blank-content fact records — {sum(not fact.content.strip() for fact in facts)}",
        f"- Exact normalized-text groups — {len(groups)} ({len(grouped)} distinct IDs)",
        f"- Consolidation run records — {len(runs)} ({sum(run.rolled_back for run in runs)} rolled back)",
        f"- Repeated exact source batches — {len(repeated_batches)}",
        f"- Source IDs recorded in multiple runs — {len(reused_sources)}",
        f"- Consolidation source IDs in snapshot — {len(source_ids & fact_ids)}",
        f"- Generated summary IDs in snapshot — {len(generated_ids & fact_ids)}",
        f"- Missing referenced fact IDs — {len(historical - fact_ids)}",
        "- Sources — " + (", ".join(f"{key} ({count})" for key, count in sorted(sources.items())) or "None"),
        "- Categories — " + (", ".join(f"{key} ({count})" for key, count in sorted(categories.items())) or "None"),
        "", "## Protected and colliding IDs", "",
        f"- Protected (manual or pinned) — {_ids(protected)}",
    ]
    lines.extend(f"- {_code_id(fid)} — {len(by_id[fid])} records" for fid in sorted(collisions))
    held_ids = collisions | historical
    candidate_count, pilot = _append_exact_groups(lines, groups, by_id, held_ids)
    lines.extend([
        "", "## Exact-match pilot", "",
        f"{candidate_count} candidate IDs across {len(pilot)} groups. "
        "The sample gives one extra ID per group and favors retaining active protected records, "
        "then higher weight and a newer update time when choosing a keeper.",
        "",
    ])
    for extra, keep in sorted(pilot, key=lambda item: item[0])[:sample_limit]:
        lines.append(f"- Review {_code_id(extra)}. Keep {_ids(keep)}.")
    _append_consolidation(lines, batches, repeated_batches, reused_sources, fact_ids)
    excluded = protected | held_ids | grouped
    inventory = [
        fact for fact in facts
        if fact.id not in excluded and fact.state == "active"
        and fact.source in ("auto", "reflection") and fact.content.strip()
    ]
    _append_inventory(lines, inventory, sample_limit)
    if include_text:
        _append_text(lines, facts)
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("facts", type=Path, help="Path to the fact snapshot JSON")
    parser.add_argument("changelog", type=Path, help="Path to memory_changelog.json")
    parser.add_argument("--include-text", action="store_true", help="Include original fact content")
    parser.add_argument("--sample-limit", type=int, default=10, help="Maximum IDs per review sample")
    parser.add_argument("--as-of", type=float, help="Unix timestamp for expiry checks")
    args = parser.parse_args(argv)
    snapshots = []
    for path, label in ((args.facts, "fact snapshot"), (args.changelog, "memory changelog")):
        try:
            with path.open(encoding="utf-8-sig") as stream:
                snapshots.append(json.load(stream))
        except (OSError, ValueError):
            parser.error(f"Could not read the {label} as UTF-8 JSON.")
    try:
        report = render_cleanup_preview(
            *snapshots, include_text=args.include_text, sample_limit=args.sample_limit, as_of=args.as_of,
        )
    except ValueError as error:
        parser.error(str(error))
    sys.stdout.buffer.write(report.encode("utf-8", errors="backslashreplace"))


if __name__ == "__main__":
    main()
