"""Tests for the local-LLM semantic answer matching (offline, stubbed HTTP)."""

from __future__ import annotations

import json

import pytest

from autoapply import llm
from autoapply.settings import LlmConfig, LlmEquivalence

ANSWERS = {
    "current ctc": "120000",
    "expected ctc": "600000",
    "notice period": "15",
    "remote work": "Yes",
}


@pytest.fixture
def cfg():
    return LlmConfig(
        enabled=True,
        model="test-model",
        host="http://localhost:11434",
        timeout_seconds=5,
        min_confidence=75,
        equivalences=[
            LlmEquivalence(
                key="current ctc",
                any_of=["previous salary", "past salary", "last drawn"],
            )
        ],
        never_mix=[["expected ctc", "current ctc"]],
    )


@pytest.fixture
def online(monkeypatch):
    monkeypatch.setattr(llm, "llm_available", lambda cfg: True)


def _chat_json(monkeypatch, matches):
    monkeypatch.setattr(
        llm, "_chat", lambda cfg, prompt: json.dumps({"matches": matches})
    )


def test_equivalence_rule_needs_no_llm(cfg, monkeypatch):
    def _boom(cfg, prompt):
        raise AssertionError("LLM must not be consulted for rule hits")

    monkeypatch.setattr(llm, "_chat", _boom)
    out = llm.semantic_prefill(["What is your previous salary?"], ANSWERS, cfg)
    assert out == {
        "What is your previous salary?": {
            "key": "current ctc",
            "value": "120000",
            "confidence": 100,
            "source": "rule",
        }
    }


def test_llm_rephrase_maps(cfg, online, monkeypatch):
    _chat_json(
        monkeypatch,
        [{"question": "How soon can you join?", "key": "notice period", "confidence": 90}],
    )
    out = llm.semantic_prefill(["How soon can you join?"], ANSWERS, cfg)
    assert out["How soon can you join?"]["key"] == "notice period"
    assert out["How soon can you join?"]["source"] == "llm"


def test_llm_past_salary_allowed_by_verifier(cfg, online, monkeypatch):
    """Same-direction past-pay mapping passes even without the rule."""
    plain = cfg.model_copy(update={"equivalences": []})
    _chat_json(
        monkeypatch,
        [{"question": "What is your previous salary?", "key": "current ctc", "confidence": 90}],
    )
    out = llm.semantic_prefill(["What is your previous salary?"], ANSWERS, plain)
    assert out["What is your previous salary?"]["value"] == "120000"


def test_hallucinated_key_dropped(cfg, online, monkeypatch):
    _chat_json(
        monkeypatch,
        [{"question": "What is your date of birth?", "key": "date of birth", "confidence": 99}],
    )
    assert llm.semantic_prefill(["What is your date of birth?"], ANSWERS, cfg) == {}


def test_value_echo_resolves_to_key(cfg, online, monkeypatch):
    _chat_json(
        monkeypatch,
        [{"question": "How soon can you join?", "key": "15", "confidence": 88}],
    )
    out = llm.semantic_prefill(["How soon can you join?"], ANSWERS, cfg)
    assert out["How soon can you join?"]["key"] == "notice period"


def test_never_mix_blocks_expected_to_current(cfg, online, monkeypatch):
    _chat_json(
        monkeypatch,
        [{"question": "Expected CTC in INR?", "key": "current ctc", "confidence": 100}],
    )
    assert llm.semantic_prefill(["Expected CTC in INR?"], ANSWERS, cfg) == {}


def test_low_confidence_dropped(cfg, online, monkeypatch):
    _chat_json(
        monkeypatch,
        [{"question": "How soon can you join?", "key": "notice period", "confidence": 50}],
    )
    assert llm.semantic_prefill(["How soon can you join?"], ANSWERS, cfg) == {}


def test_non_numeric_value_rejected_for_numeric_question(cfg, online, monkeypatch):
    answers = dict(ANSWERS, **{"years of experience": "a few"})
    _chat_json(
        monkeypatch,
        [{"question": "How many years of experience?", "key": "years of experience", "confidence": 95}],
    )
    assert llm.semantic_prefill(["How many years of experience?"], answers, cfg) == {}


def test_malformed_reply_is_safe(cfg, online, monkeypatch):
    monkeypatch.setattr(llm, "_chat", lambda cfg, prompt: "not json at all")
    assert llm.semantic_prefill(["How soon can you join?"], ANSWERS, cfg) == {}


def test_none_reply_is_safe(cfg, online, monkeypatch):
    monkeypatch.setattr(llm, "_chat", lambda cfg, prompt: None)
    assert llm.semantic_prefill(["How soon can you join?"], ANSWERS, cfg) == {}


def test_llm_down_falls_back_silently(cfg, monkeypatch):
    monkeypatch.setattr(llm, "llm_available", lambda cfg: False)

    def _boom(cfg, prompt):
        raise AssertionError("no chat call when unavailable")

    monkeypatch.setattr(llm, "_chat", _boom)
    assert llm.semantic_prefill(["How soon can you join?"], ANSWERS, cfg) == {}


def test_disabled_config_short_circuits(monkeypatch):
    off = LlmConfig(enabled=False)
    assert llm.semantic_prefill(["Anything?"], ANSWERS, off) == {}


def test_empty_value_key_never_maps(cfg, online, monkeypatch):
    answers = dict(ANSWERS, nickname="")
    _chat_json(
        monkeypatch,
        [{"question": "What should we call you?", "key": "nickname", "confidence": 99}],
    )
    assert llm.semantic_prefill(["What should we call you?"], answers, cfg) == {}
