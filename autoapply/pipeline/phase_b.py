"""Phase B — detail fetch for captured listings.

For listings sitting at status=new (and within the daily detail budget),
open the posting, extract the full JD, and classify the application path:

  inline   -> board-native apply (LinkedIn Easy Apply)
  redirect -> external apply link captured + ATS platform detected

Guardrails (login/captcha/block) during detail fetch mark the listing
`needs_manual` instead of failing the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlmodel import select

from ..db import Listing, ListingStatus, get_session, register_spend, remaining_budget
from ..errors import AccountBlocked, CaptchaDetected, LoginRequired
from ..settings import Settings
from ..sources import detect_ats
from ..sources.linkedin import detect_apply_path, extract_jd

log = logging.getLogger("aa.phase_b")


@dataclass
class DetailReport:
    fetched: int = 0
    guarded: int = 0
    errors: int = 0
    budget_left: int = 0


def run_phase_b(
    settings: Settings, headless: bool = True, limit: int | None = None
) -> DetailReport:
    source = "linkedin"  # V1 scope
    caps = settings.caps.for_source(source)
    budget_cap = settings.caps.global_.daily_detail_budget
    report = DetailReport()

    from ..browser import Browser

    with Browser(source, caps, headless=headless) as browser:
        db = get_session()
        remaining = remaining_budget(db, "detail_fetch", budget_cap)
        remaining = 0 if remaining is None else remaining
        report.budget_left = remaining
        if remaining <= 0:
            log.warning("daily detail budget exhausted (%s/day)", budget_cap)
            return report
        if limit is not None:
            remaining = min(remaining, limit)

        listings = db.exec(
            select(Listing).where(
                Listing.source == source,
                Listing.status == ListingStatus.NEW.value,
            )
        ).all()[:remaining]

        for listing in listings:
            consumed = False
            try:
                browser.assert_clear()
                browser.page.goto(listing.url, wait_until="domcontentloaded")
                browser.assert_clear()
                result = _fetch_and_classify(browser, listing)
            except (LoginRequired, CaptchaDetected, AccountBlocked) as exc:
                report.guarded += 1
                listing.status = ListingStatus.NEEDS_MANUAL.value
                listing.reason = f"guardrail: {exc}"
                log.warning("guardrail on %s: %s", listing.url, exc)
            except Exception as exc:
                report.errors += 1
                listing.reason = f"detail error: {exc}"
                log.warning("detail fetch failed for %s: %s", listing.url, exc)
            else:
                consumed = True
                report.fetched += 1
                listing.full_jd = result["full_jd"]
                listing.apply_path = result["apply_path"]
                listing.external_url = result["external_url"]
                listing.ats_type = result["ats_type"]
                listing.status = ListingStatus.DETAILED.value
                db.add(listing)
                db.commit()
                register_spend(db, "detail_fetch")
                report.budget_left -= 1
                if report.budget_left <= 0:
                    break
            if not consumed:
                db.add(listing)
                db.commit()
            browser.sleep_detail()
    return report


def _fetch_and_classify(browser, listing: Listing) -> dict:
    browser.page.wait_for_timeout(2500)  # let async detail pane render
    jd = extract_jd(browser.page)
    path = detect_apply_path(browser.page)
    result = {
        "full_jd": jd,
        "apply_path": path["apply_path"],
        "external_url": path["external_url"],
        "ats_type": None,
    }
    if path["external_url"]:
        result["ats_type"] = detect_ats(path["external_url"])
    if result["apply_path"] == "unknown":
        log.info("apply-path unknown for %s", listing.url)
    return result
