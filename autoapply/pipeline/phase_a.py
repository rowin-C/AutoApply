"""Phase A — cheap listing capture.

Take searches from config, scroll each results page per caps, parse cards,
upsert into the DB (dedupe by source+key). Honors the global per-run caps:
search-combo budget and listing budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..db import Listing, get_session, make_dedupe_key, upsert_listing
from ..settings import Settings
from ..sources import get_adapter

log = logging.getLogger("aa.phase_a")


@dataclass
class CaptureReport:
    seen: int = 0
    new: int = 0
    updated: int = 0


def run_phase_a(settings: Settings, headless: bool = True) -> CaptureReport:
    report = CaptureReport()
    source = "linkedin"  # V1 scope; adapter registry drives this later
    adapter = get_adapter(source)
    caps = settings.caps.apply_global_max_searches(settings.caps.for_source(source))
    listing_budget = settings.caps.global_.max_listings_per_run

    from ..browser import Browser

    with Browser(source, caps, headless=headless) as browser:
        browser.assert_clear()
        db = get_session()
        for search in settings.searches.searches:
            if report.seen >= listing_budget:
                log.info("listing budget %d hit; stopping capture", listing_budget)
                return report

            cards = adapter.capture_listings(browser, search)
            for card in cards:
                if report.seen >= listing_budget:
                    log.info("listing budget reaches; stopping capture")
                    break
                report.seen += 1
                listing = Listing(
                    source=card.source,
                    job_id=card.job_id,
                    url=card.url,
                    dedupe_key=make_dedupe_key(
                        card.source,
                        card.job_id,
                        card.title,
                        card.company,
                        card.location,
                    ),
                    title=card.title,
                    company=card.company,
                    location=card.location,
                    salary_raw=card.salary_raw,
                    posted_ago=card.posted_ago,
                )
                try:
                    is_new, _ = upsert_listing(db, listing)
                except Exception as exc:  # keep the run alive on one bad card
                    log.warning("upsert failed for %s: %s", card.url, exc)
                    continue
                if is_new:
                    report.new += 1
                else:
                    report.updated += 1
            log.info(
                "search '%s': %d cards, seen=%d new=%d",
                search.name,
                len(cards),
                report.seen,
                report.new,
            )
    return report
