"""Rules-based qualifying / match scoring (LLM-ready seam).

Until the LLM scorer lands, this is pure, auditable rules: hard exclusions,
must-skill presence, title matching, location/remote, salary floor, and
experience. Every decision carries a human-readable reason string that
`aa review` surfaces.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlmodel import select

from ..db import Listing, ListingStatus, get_session
from ..settings import ProfileConfig

log = logging.getLogger("aa.qualify")


@dataclass
class QualifyResult:
    eligible: bool
    score: float
    reasons: list[str]


_SALARY_PAT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(k|c|l|m|cr|crore|lakh|lpa|ctc)?\b", re.IGNORECASE
)
_SALARY_MULT = {
    "k": 1_000,
    "m": 1_000_000,
    "l": 100_000,
    "lakh": 100_000,
    "lpa": 100_000,
    "c": 10_000_000,
    "cr": 10_000_000,
    "crore": 10_000_000,
    "ctc": 1,
}
_YEARS_PAT = re.compile(
    r"(\d{1,2})\+?\s*(?:to|-)\s*\d{1,2}?|\b(\d{1,2})\s*(?:\+)?\s*years?|\b(\d{1,2})[+]?\s*(?:yrs|yr)\b",
    re.IGNORECASE,
)


def normalized_salary_values(raw: str) -> list[float]:
    """Parse salary text like '$100K–$120K' or '₹12L - ₹18L per annum'."""
    out: list[float] = []
    for match in _SALARY_PAT.finditer(raw or ""):
        number, suffix = match.groups()
        mult = _SALARY_MULT.get((suffix or "").lower(), 1)
        out.append(float(number) * mult)
    return out


def extract_years(jd: str) -> int | None:
    if not jd:
        return None
    found = [int(g) for g in _YEARS_PAT.findall(jd) for g in g if g]
    return max(found) if found else None


def qualify(listing: Listing, profile: ProfileConfig) -> QualifyResult:
    title = (listing.title or "").lower()
    company = (listing.company or "").lower()
    haystack = f"{listing.title} {listing.company} {listing.location} {listing.full_jd}".lower()

    for emp in profile.excluded_employers:
        if emp.lower() and emp.lower() in company:
            return QualifyResult(False, 0.0, [f"excluded employer '{emp}'"])
    for kw in profile.excluded_keywords:
        if kw.lower() and kw.lower() in haystack:
            return QualifyResult(False, 0.0, [f"excluded keyword '{kw}' in posting"])
    if profile.exclude_intern and ("intern" in title or "internship" in title):
        return QualifyResult(False, 0.0, ["internship/intern mentioned in title"])

    reasons: list[str] = []
    score = 0.0

    title_ok = any(t.lower() in title for t in profile.target_titles)
    if title_ok:
        score += 3.5
        reasons.append("title matches target")
    elif profile.target_titles:
        reasons.append("title does not match target titles")

    jd_present = bool((listing.full_jd or "").strip())
    if profile.must_skills:
        missing = [
            s for s in profile.must_skills if s.lower() and s.lower() not in haystack
        ]
        if jd_present and missing:
            return QualifyResult(
                False, 0.0, [f"missing must-skills: {', '.join(missing)}"]
            )
        if not missing:
            score += 2.0
            reasons.append("all must-skills present")
        else:
            reasons.append("JD absent on listing; must-skills unverified")

    present_nice = []
    if profile.nice_skills and jd_present:
        present_nice = [
            s for s in profile.nice_skills if s.lower() and s.lower() in haystack
        ]
        if present_nice:
            score += 2.0 * len(present_nice) / len(profile.nice_skills)
            reasons.append(f"nice-skills: {', '.join(present_nice)}")

    location_blob = f"{listing.location} {listing.full_jd}".lower()
    if profile.remote_ok and "remote" in location_blob:
        score += 1.0
        reasons.append("remote-friendly")
    elif profile.locations and any(
        l.lower() and l.lower() in (listing.location or "").lower()
        for l in profile.locations
    ):
        score += 1.0
        reasons.append("location matches")

    salary_vals = normalized_salary_values(listing.salary_raw)
    if salary_vals and profile.salary_floor is not None:
        highest = max(salary_vals)
        if highest >= profile.salary_floor:
            score += 1.0
            reasons.append(f"salary range ok (max {highest:,.0f})")
        else:
            reasons.append(f"salary below floor (max {highest:,.0f})")

    years = extract_years(listing.full_jd)
    if profile.min_experience_years and years is not None:
        if years >= profile.min_experience_years:
            score += 1.0
            reasons.append(f"experience ~{years}y ok")
        else:
            reasons.append(f"experience ~{years}y below minimum")

    return QualifyResult(True, round(min(score, 10.0), 1), reasons)


def run_qualify(settings) -> tuple[int, int]:
    """Score all DETAILED listings, moving them to queued/skipped."""
    source = "linkedin"
    db = get_session()
    queued = skipped = 0
    listings = db.exec(
        select(Listing).where(
            Listing.source == source,
            Listing.status == ListingStatus.DETAILED.value,
        )
    ).all()
    for listing in listings:
        result = qualify(listing, settings.profile)
        listing.match_score = result.score
        listing.reason = "; ".join(result.reasons)
        listing.status = (
            ListingStatus.QUEUED.value
            if result.eligible
            else ListingStatus.SKIPPED.value
        )
        db.add(listing)
        if result.eligible:
            queued += 1
        else:
            skipped += 1
    db.commit()
    log.info("qualified: %d queued, %d skipped", queued, skipped)
    return queued, skipped
