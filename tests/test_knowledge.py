"""Tests for the persistent field knowledge base."""

from __future__ import annotations

import pytest

from autoapply import knowledge


@pytest.fixture
def tmp_knowledge(tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge, "KNOWLEDGE_FILE", tmp_path / "knowledge.yaml")
    if knowledge.KNOWLEDGE_FILE.exists():
        knowledge.KNOWLEDGE_FILE.unlink()
    return knowledge.KNOWLEDGE_FILE


def _refresh():
    return knowledge.load_knowledge()


def test_absorb_adds_and_dedupes_fields(tmp_knowledge):
    knowledge.absorb_unknown_fields(
        [("Acme", ["Current CTC", "Expected CTC"])], when="2026-09-01"
    )
    k = _refresh()
    assert {e.label for e in k.entries} == {"Current CTC", "Expected CTC"}
    assert all(e.times_seen == 1 for e in k.entries)
    assert all(e.status == "open" for e in k.entries)

    knowledge.absorb_unknown_fields(
        [("Acme", ["Current CTC"])], when="2026-09-02"
    )
    k = _refresh()
    assert len(k.entries) == 2
    ctc = next(e for e in k.entries if e.label == "Current CTC")
    assert ctc.times_seen == 2
    assert ctc.first_seen == "2026-09-01"
    assert ctc.last_seen == "2026-09-02"


def test_absorb_marks_covered_by_answer_key(tmp_knowledge):
    knowledge.absorb_unknown_fields([("Acme", ["Current CTC"])], when="2026-09-01")
    assert _refresh().entries[0].status == "open"

    knowledge.absorb_unknown_fields([], {"current ctc": "10 LPA"})
    k = _refresh()
    assert k.entries[0].status == "covered"
    assert k.entries[0].answer_key == "current ctc"
    assert knowledge.coverage(k) == (0, 1)


def test_empty_answer_does_not_cover(tmp_knowledge):
    knowledge.absorb_unknown_fields([("Acme", ["Current CTC"])], when="2026-09-01")
    knowledge.absorb_unknown_fields([], {"current ctc": ""})
    assert _refresh().entries[0].status == "open"


def test_two_char_key_does_not_cover(tmp_knowledge):
    knowledge.absorb_unknown_fields([("Acme", ["Are you dog friendly?"])], when="2026-09-01")
    knowledge.absorb_unknown_fields([], {"xy": "Yes"})
    assert _refresh().entries[0].status == "open"


def test_norm_mirrors_apply_engine():
    assert knowledge.norm("State your rank in Advanced JEE ?") == (
        "state your rank in advanced jee ?"
    )
    assert knowledge.norm("Mobile phone number*") == "mobile phone number"
    assert knowledge.norm("Years of Exp: (React.js)") == "years of exp react.js"


def test_render_reports_counts(tmp_knowledge):
    knowledge.absorb_unknown_fields(
        [("Acme", ["Current CTC", "Expected CTC"])], answers={"current ctc": "9 LPA"}
    )
    text = knowledge.render(_refresh())
    assert "1 need input" in text and "1 covered" in text
    assert "Current CTC" in text
    assert "key: current ctc" in text