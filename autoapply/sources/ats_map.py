"""Hostname -> ATS platform detection.

This is how the "two company websites" problem becomes a solved category:
most career sites are powered by one of these platforms. We capture the
external apply URL at discovery time and tag it here, so the Step-2
company-site applier can pick the right handler from the tag alone.
"""

from __future__ import annotations

_ATS_TABLE: list[tuple[str, str]] = [
    ("lever.co", "lever"),
    ("greenhouse.io", "greenhouse"),
    ("myworkdayjobs.com", "workday"),
    ("workday.com", "workday"),
    ("successfactors.com", "successfactors"),
    ("sap.com", "successfactors"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("bamboohr.com", "bamboohr"),
    ("jazzhr.com", "jazzhr"),
    ("icims.com", "icims"),
    ("jobvite.com", "jobvite"),
    ("ashbyhq.com", "ashby"),
    ("recruitee.com", "recruitee"),
    ("teamtailor.com", "teamtailor"),
    ("workable.com", "workable"),
    ("peoplebox", "peoplesoft"),
    ("peoplesoft", "peoplesoft"),
]


def detect_ats(url: str) -> str | None:
    """Return the ATS platform name for a URL, if recognizable."""
    needle = (url or "").lower()
    for chunk, platform in _ATS_TABLE:
        if chunk in needle:
            return platform
    return None


def known_external(url: str) -> bool:
    """Quick check: is this apply link a recognizable ATS (vs. an unknown company site)?"""
    return detect_ats(url) is not None
