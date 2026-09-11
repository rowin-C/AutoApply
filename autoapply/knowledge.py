"""Persistent knowledge base of Easy Apply fields the engine couldn't answer.

Every run funnels the "needs your input" question labels here, deduped by a
normalized form and stamped with first/last-seen and a seen counter. As the
user hands over answers for config/answers.yaml, matching entries flip from
"open" to "covered", so coverage grows until nothing is left.

Stored as a human-editable YAML next to answers.yaml.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import yaml

from .settings import CONFIG_DIR

log = logging.getLogger("aa.knowledge")

KNOWLEDGE_FILE = CONFIG_DIR / "knowledge.yaml"

_PUNCT = re.compile(r"[:*()\[\]]")
_SPACE = re.compile(r"\s+")


def norm(label: str) -> str:
    """Mirror the apply engine's label normalization."""
    return _SPACE.sub(" ", _PUNCT.sub(" ", (label or "").lower())).strip()


@dataclass
class KnowledgeEntry:
    label: str
    normalized: str
    times_seen: int
    first_seen: str
    last_seen: str
    status: str = "open"  # "open" | "covered"
    answer_key: str | None = None


@dataclass
class Knowledge:
    updated: str = ""
    entries: list[KnowledgeEntry] = field(default_factory=list)

    def by_normalized(self) -> dict[str, KnowledgeEntry]:
        return {e.normalized: e for e in self.entries}


def load_knowledge() -> Knowledge:
    if not KNOWLEDGE_FILE.exists():
        return Knowledge()
    raw = yaml.safe_load(KNOWLEDGE_FILE.read_text(encoding="utf-8")) or {}
    entries = [
        KnowledgeEntry(
            label=str(e.get("label", "")),
            normalized=str(e.get("normalized", "")),
            times_seen=int(e.get("times_seen", 1)),
            first_seen=str(e.get("first_seen", "")),
            last_seen=str(e.get("last_seen", "")),
            status=str(e.get("status", "open")),
            answer_key=e.get("answer_key"),
        )
        for e in raw.get("entries", [])
    ]
    return Knowledge(updated=str(raw.get("updated", "")), entries=entries)


def save_knowledge(knowledge: Knowledge) -> None:
    payload = {
        "updated": knowledge.updated,
        "entries": [asdict(e) for e in knowledge.entries],
    }
    KNOWLEDGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = KNOWLEDGE_FILE.with_suffix(".yaml.tmp")
    try:
        tmp.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        os.replace(tmp, KNOWLEDGE_FILE)
    except Exception as exc:
        log.warning("knowledge save failed: %s", exc)


def _matching_key(label_norm: str, answers: dict) -> str | None:
    """First answers.yaml key (normalized, >=3 chars, non-empty) inside the label."""
    for key, value in answers.items():
        kn = norm(key)
        if kn and len(kn) >= 3 and (value or "").strip() and kn in label_norm:
            return key
    return None


def absorb_unknown_fields(
    unknown_fields: list[tuple[str, list[str]]],
    answers: dict | None = None,
    when: str | None = None,
) -> Knowledge:
    """Merge every "needs your input" label into the knowledge base.

    Returns the updated knowledge for immediate rendering. Idempotent: re-runs
    only bump counters. Entries covered by an existing answer key are marked.
    """
    knowledge = load_knowledge()
    when = when or datetime.now(UTC).date().isoformat()
    answers = answers or {}
    by = knowledge.by_normalized()
    changed = False

    for _, labels in unknown_fields:
        for raw_label in labels:
            label = _SPACE.sub(" ", (raw_label or "").strip())
            if not label:
                continue
            key = norm(label)
            entry = by.get(key)
            if entry:
                entry.times_seen += 1
                entry.last_seen = when
                changed = True
            else:
                entry = KnowledgeEntry(
                    label=label,
                    normalized=key,
                    times_seen=1,
                    first_seen=when,
                    last_seen=when,
                )
                knowledge.entries.append(entry)
                by[key] = entry
                changed = True

    for entry in knowledge.entries:
        key = _matching_key(entry.normalized, answers)
        if key and entry.status != "covered":
            entry.status = "covered"
            entry.answer_key = key
            changed = True
        elif not key and entry.status == "covered":
            entry.status = "open"
            entry.answer_key = None
            changed = True

    if changed:
        knowledge.updated = when
        save_knowledge(knowledge)
        log.info("knowledge: %d open · %d covered", *coverage(knowledge))
    return knowledge


def coverage(knowledge: Knowledge) -> tuple[int, int]:
    open_n = sum(1 for e in knowledge.entries if e.status == "open")
    covered = len(knowledge.entries) - open_n
    return open_n, covered


def render(knowledge: Knowledge) -> str:
    """Plain-text summary for `aa knowledge` and the daily report."""
    if not knowledge.entries:
        return "Knowledge base: empty (no fields flagged yet)."
    open_n, covered = coverage(knowledge)
    lines = [
        f"Knowledge base: {open_n} need input · {covered} covered · last updated {knowledge.updated}"
    ]
    open_entries = [e for e in knowledge.entries if e.status == "open"]
    covered_entries = [e for e in knowledge.entries if e.status == "covered"]
    if open_entries:
        lines.append("")
        lines.append("Need your input:")
        for e in sorted(open_entries, key=lambda e: -e.times_seen):
            lines.append(
                f"  • {e.label}  (seen {e.times_seen}×, since {e.first_seen})"
            )
    if covered_entries:
        lines.append("")
        lines.append("Covered by answers.yaml:")
        for e in sorted(covered_entries, key=lambda e: -e.times_seen):
            lines.append(f"  • {e.label}  (key: {e.answer_key})")
    return "\n".join(lines)