from autoapply.db import Listing
from autoapply.pipeline.qualify import (
    extract_years,
    normalized_salary_values,
    qualify,
)
from autoapply.settings import ProfileConfig

PROFILE = ProfileConfig(
    target_titles=["Python Backend Engineer", "Backend Engineer"],
    must_skills=["Python"],
    nice_skills=["Docker", "AWS", "SQL"],
    locations=["Bangalore", "Remote"],
    remote_ok=True,
    salary_floor=1_000_000,
    min_experience_years=2.0,
    excluded_employers=["Adecco", "Tek Systems"],
    excluded_keywords=["contract", "fresher"],
)


def listing(**kw) -> Listing:
    base = {
        "source": "linkedin",
        "job_id": "1",
        "url": "https://linkedin.com/jobs/view/1",
        "dedupe_key": "linkedin:1",
        "title": "Python Backend Engineer",
        "company": "Acme",
        "location": "Bangalore",
        "salary_raw": "₹12L - ₹18L per annum",
        "full_jd": "We need Python, SQL, Docker. 3+ years experience required.",
    }
    base.update(kw)
    return Listing(**base)


def test_happy_path_queues_and_scores():
    result = qualify(listing(), PROFILE)
    assert result.eligible is True
    assert result.score >= 8.0
    assert any("must-skills present" in r for r in result.reasons)


def test_excluded_employer_disqualifies():
    result = qualify(listing(company="Adecco India Pvt"), PROFILE)
    assert result.eligible is False
    assert "excluded employer" in result.reasons[0]


def test_excluded_keyword_disqualifies():
    result = qualify(listing(title="Python Backend Engineer (Contract)"), PROFILE)
    assert result.eligible is False


def test_intern_disqualifies():
    result = qualify(listing(title="Python Backend Intern"), PROFILE)
    assert result.eligible is False


def test_missing_must_skill_disqualifies_when_jd_present():
    # Title must not carry the skill itself, or it satisfies must-skills.
    bad = listing(
        title="Software Developer", full_jd="We need Java and Go. 3+ years experience."
    )
    result = qualify(bad, PROFILE)
    assert result.eligible is False
    assert "missing must-skills" in result.reasons[0]


def test_no_jd_does_not_hard_fail():
    result = qualify(listing(title="Software Developer", full_jd=""), PROFILE)
    assert result.eligible is True
    assert any("must-skills unverified" in r.lower() for r in result.reasons)


def test_salary_below_floor():
    result = qualify(listing(salary_raw="₹4L - ₹6L per annum"), PROFILE)
    assert any("below floor" in r for r in result.reasons)


def test_salary_parsing():
    vals = normalized_salary_values("$100K - $130K a year")
    assert vals == [100_000.0, 130_000.0]
    assert normalized_salary_values("₹15 LPA - ₹20 LPA")[0] == 1_500_000.0
    assert normalized_salary_values("") == []


def test_experience_extraction():
    assert extract_years("5+ years of experience") == 5
    assert extract_years("3 to 5 years in backend") in (3, 5)
    assert extract_years("no mention here") is None
