"""Daily one-shot: capture -> detail -> qualify -> apply -> notify Telegram.

This is what the startup service runs (`aa daily`). It respects every cap and
budget from caps.yaml, so re-running the same day is harmless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..apply import run_apply
from ..db import get_session, init_db, remaining_budget
from ..errors import AutoApplyError
from ..knowledge import coverage, load_knowledge
from ..settings import Settings
from .phase_a import run_phase_a
from .phase_b import run_phase_b
from .qualify import run_qualify

log = logging.getLogger("aa.daily")


@dataclass
class DailyReport:
    searched: int = 0
    seen: int = 0
    cards_new: int = 0
    updated: int = 0
    detailed: int = 0
    detail_guarded: int = 0
    detail_errors: int = 0
    queued: int = 0
    skipped: int = 0
    applied: int = 0
    apply_budget: int = 0
    apply_budget_left: int = 0
    dry_run: bool = False
    unknown_fields: list[tuple[str, list[str]]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    applied_detail: list[dict] = field(default_factory=list)
    # {company, title, filled: [{label, value, source}]} for the audit summary


def run_daily(settings: Settings, headed: bool = False, dry_run: bool = False) -> DailyReport:
    init_db()
    report = DailyReport(
        dry_run=dry_run, apply_budget=settings.caps.global_.daily_apply_budget
    )

    try:
        phase = run_phase_a(settings, headless=not headed)
        report.searched = len(settings.searches.searches)
        report.seen = phase.seen
        report.cards_new = phase.new
        report.updated = phase.updated
    except AutoApplyError as exc:
        report.notes.append(f"Capture failed: {exc}")
        return report

    try:
        detail = run_phase_b(settings, headless=not headed)
        report.detailed = detail.fetched
        report.detail_guarded = detail.guarded
        report.detail_errors = detail.errors
    except AutoApplyError as exc:
        report.notes.append(f"Detail failed: {exc}")

    try:
        report.queued, report.skipped = run_qualify(settings)
    except AutoApplyError as exc:
        report.notes.append(f"Qualify failed: {exc}")

    try:
        apply = run_apply(settings, headless=not headed, dry_run=dry_run)
        report.applied = apply.applied
        report.apply_budget_left = apply.budget_left
        report.unknown_fields = apply.unknown_fields
        report.applied_detail = apply.applied_detail
        report.notes.extend(apply.notes)
    except AutoApplyError as exc:
        report.notes.append(f"Apply failed: {exc}")

    return report


def format_daily_report(report: DailyReport, settings: Settings) -> str:
    day = datetime.now(UTC).date().isoformat()
    suffix = " (DRY RUN — nothing submitted)" if report.dry_run else ""
    lines = [
        f"AutoApply {day}{suffix}",
        "",
        f"• Searches: {report.searched} → {report.seen} cards ({report.cards_new} new)",
        f"• Details: {report.detailed} fetched ({report.detail_guarded} guarded, {report.detail_errors} errors)",
        f"• Qualified: {report.queued} queued · {report.skipped} filtered",
        f"• Applied: {report.applied}/{report.apply_budget}"
        + (f" · {report.apply_budget_left} budget left" if not report.dry_run else " (dry run)"),
    ]

    if report.applied_detail:
        lines.append("")
        lines.append("Applied — answers written:")
        for job in report.applied_detail[:6]:
            lines.append(f"  • {job['company']} — {job['title']}")
            for f in (job.get("filled") or [])[:6]:
                lines.append(
                    f"      {f['label'][:60]} → {f['value']} [{f['source']}]"
                )
        if len(report.applied_detail) > 6:
            lines.append(f"  … and {len(report.applied_detail) - 6} more")

    if report.unknown_fields:
        lines.append("")
        lines.append("Needs your input (could not auto-answer):")
        for company, fields in report.unknown_fields[:8]:
            lines.append(f"  • {company}: {', '.join(fields) or 'see listing'}")
        if len(report.unknown_fields) > 8:
            lines.append(f"  … and {len(report.unknown_fields) - 8} more")

    if report.notes and not report.dry_run:
        lines.append("")
        lines.append("Notes:")
        for n in report.notes[:8]:
            lines.append(f"  • {n}")

    knowledge = load_knowledge()
    if knowledge.entries:
        open_n, covered = coverage(knowledge)
        lines.append("")
        lines.append(
            f"Knowledge: {open_n} fields need your input · {covered} covered "
            "(see config/knowledge.yaml)"
        )

    db = get_session()
    detail_left = (
        remaining_budget(db, "detail_fetch", settings.caps.global_.daily_detail_budget)
        or 0
    )
    db.close()
    lines.append("")
    lines.append(
        f"Budgets left: detail {detail_left} · apply "
        f"{report.apply_budget_left if not report.dry_run else report.apply_budget}"
    )
    return "\n".join(lines)