"""Answers-map matching + Telegram/daily report logic (no browser needed)."""

from __future__ import annotations

import pytest

from autoapply import errors
from autoapply.apply import ApplyReport
from autoapply.notify import telegram as telegram_mod
from autoapply.pipeline.daily import DailyReport, format_daily_report
from autoapply.settings import AnswersConfig, TelegramConfig, load_settings


def test_answers_match_case_insensitive_substring() -> None:
    cfg = AnswersConfig(values={"expected salary": "18 LPA", "notice period": "90 days"})
    assert cfg.match("Expected Salary:") == "18 LPA"
    assert cfg.match("   notice period  *") == "90 days"
    assert cfg.match("are you willing to relocate?") is None
    assert cfg.match("") is None


def test_answers_short_keys_ignored() -> None:
    cfg = AnswersConfig(values={"io": "3"})  # key too short to match safely
    assert cfg.match("location: full-time") is None  # "io" in "location" must not hit


def test_telegram_disabled_returns_false() -> None:
    cfg = TelegramConfig(enabled=False)
    assert telegram_mod.send_message("hello", cfg) is False


def test_telegram_enabled_without_creds_raises() -> None:
    cfg = TelegramConfig(enabled=True)
    with pytest.raises(errors.ConfigError):
        telegram_mod.send_message("hello", cfg)


def test_telegram_test_disabled_is_false() -> None:
    assert telegram_mod.test_telegram(TelegramConfig(enabled=False)) is False


def test_format_daily_report_basic() -> None:
    settings = load_settings()
    report = DailyReport(
        searched=2,
        seen=33,
        cards_new=30,
        detailed=25,
        queued=14,
        skipped=11,
        applied=3,
        apply_budget=10,
        apply_budget_left=7,
        unknown_fields=[("Kuku", ["Notice period", "Salary expectations"])],
    )
    text = format_daily_report(report, settings)
    assert "AutoApply" in text
    assert "Applied: 3/10" in text
    assert "Kuku" in text
    assert "Needs your input" in text


def test_format_daily_report_dry_run() -> None:
    settings = load_settings()
    report = DailyReport(dry_run=True, searched=1, applied=4, apply_budget=10)
    text = format_daily_report(report, settings)
    assert "DRY RUN" in text
    assert "none spent" in text or "dry run" in text


def test_apply_report_plain() -> None:
    r = ApplyReport(applied=1, unknown=0, guarded=0, errors=0, budget_left=9)
    assert r.applied == 1 and r.budget_left == 9