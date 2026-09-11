"""LinkedIn jobs discovery adapter.

Resilience strategy: LinkedIn A/B-tests card layouts relentlessly, so every
field is extracted through a list of selector candidates with a text-based
fallback, and any use of a fallback is logged (that log line tells us what
to fix when the UI changes — never a crash).

Phases:
  - search URL built per (keywords, location, filters) combo
  - results lazy-load via scroll within the results list
  - cards parsed by walking anchors whose href matches /jobs/view/<id>
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from ..settings import SearchFilter, SearchProfile
from .base import ListingCard

log = logging.getLogger("aa.linkedin")

SEARCH_URL = "https://www.linkedin.com/jobs/search"

_VIEWJOB_RE = re.compile(r"/jobs/view/(\d+)")

# f_TPR filter values (posted within).
_TPR_MAP = {1: "r86400", 7: "r604800", 30: "r2592000"}
# f_E mapping (experience). LinkedIn: 2=entry, 3=associate/mid, 4=mid-senior.
_FE_MAP = {"entry": "2", "mid": "3", "senior": "4"}


def build_search_url(
    keywords: str, location: str, filters: SearchFilter, page: int = 0
) -> str:
    params = {
        "keywords": keywords,
        "location": location,
        "start": str(page * 25),
    }
    if filters.posted_days is not None:
        for days, token in _TPR_MAP.items():
            if filters.posted_days <= days:
                params["f_TPR"] = token
                break
        else:
            params["f_TPR"] = "r2592000"
    if filters.remote_only:
        params["f_WT"] = "2"
    if filters.experience:
        token = _FE_MAP.get(filters.experience)
        if token:
            params["f_E"] = token
    return f"{SEARCH_URL}?{urlencode(params)}"


def extract_job_id(url: str) -> str | None:
    match = _VIEWJOB_RE.search(url or "")
    return match.group(1) if match else None


_EXTRACT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const text = (el) => (el && el.innerText && el.innerText.trim()) ? el.innerText.trim() : '';
  const first = (card, sels) => {
    for (const s of sels) {
      const el = card.querySelector(s);
      const t = text(el);
      if (t) return t;
    }
    return '';
  };
  const titleSels = ['.base-search-card__title', '.artdeco-entity-lockup__title'];
  const companySels = ['.artdeco-entity-lockup__subtitle', '.base-search-card__subtitle', '.job-card-container__primary-description'];
  const locationSels = ['.job-card-container__metadata-wrapper', '.artdeco-entity-lockup__caption', '.base-search-card__metadata'];
  const salarySels = ['.job-search-card__salary-info', '.job-card-container__salary', "span[class*='salary']"];
  document.querySelectorAll('a[href*="jobs/view/"]').forEach((a) => {
    const href = a.href.split('?')[0];
    if (!href || seen.has(href)) return;
    seen.add(href);
    const card = a.closest('.job-card-container, .base-card, .job-card-list') || a.parentElement;
    // Anchor text is "Title\nTitle with verification" (dupe + suffix) — first line is clean.
    const anchorText = (text(a) || '').split('\n')[0].replace(/\s*with verification\s*$/i, '').trim();
    const title = anchorText || first(card, titleSels);
    const company = first(card, companySels);
    let location = first(card, locationSels);
    if (!company && location.includes('·')) {
      const parts = location.split('·');       // "Company · Location" fallback
      company = parts[0].trim();
      location = parts.slice(1).join('·').trim();
    }
    const timeEl = card.querySelector('time');
    const posted = timeEl ? (text(timeEl) || (timeEl.getAttribute('datetime') || '')) : '';
    const idm = href.match(/\/jobs\/view\/(\d+)/);
    out.push({
      href, title, company, location,
      salary: first(card, salarySels), posted,
      jobId: idm ? idm[1] : null,
    });
  });
  return out;
}
"""


def _cards_from_page(page) -> list[ListingCard]:
    """Extract job cards via a single DOM pass (layout-agnostic)."""
    cards: list[ListingCard] = []
    try:
        rows = page.evaluate(_EXTRACT_JS)
    except Exception as exc:  # resilience: never crash a run on a page hiccup
        log.warning("card extraction JS failed: %s", exc)
        return cards
    for row in (rows or []):
        cards.append(
            ListingCard(
                source="linkedin",
                url=row["href"],
                title=row["title"],
                company=row["company"],
                location=row["location"],
                salary_raw=row["salary"],
                posted_ago=row["posted"] or None,
                job_id=row["jobId"],
            )
        )
    if not cards:
        log.warning("No cards parsed — LinkedIn layout likely changed")
    return cards


class LinkedInAdapter:
    source = "linkedin"

    def build_search_url(
        self, keywords: str, location: str, filters: SearchFilter
    ) -> str:
        return build_search_url(keywords, location, filters)

    def capture_listings(self, browser, search: SearchProfile) -> list[ListingCard]:
        cards: list[ListingCard] = []
        pages = max(1, browser.caps.pages_per_search)
        combo_limit = browser.caps.max_searches_per_run
        combos_used = 0
        for keyword in search.keyword_combos():
            for location in search.locations:
                if combo_limit is not None and combos_used >= combo_limit:
                    log.info("search-combo budget reached (%s)", combo_limit)
                    return cards
                combos_used += 1
                url = build_search_url(keyword, location, search.filters)
                browser.assert_clear()
                browser.page.goto(url, wait_until="domcontentloaded")
                browser.assert_clear()
                _scroll_to_load(browser, pages)
                cards.extend(_cards_from_page(browser.page))
                if len(cards) >= 30:  # pragmatic guard, haul cap is enforced upstream
                    return cards
                browser.sleep_search()
        return cards


def _scroll_to_load(browser, rounds: int) -> None:
    page = browser.page
    for _ in range(max(1, rounds)):
        scrolled = False
        try:
            scrolled = bool(
                page.evaluate(
                    """() => {
                        const a = document.querySelector('a[href*="jobs/view/"]');
                        if (!a) return false;
                        let el = a.parentElement;
                        while (el) {
                            if (el.scrollHeight > el.clientHeight + 50) {
                                el.scrollTop = el.scrollHeight;
                                return true;
                            }
                            el = el.parentElement;
                        }
                        return false;
                    }"""
                )
            )
        except Exception:
            scrolled = False
        if not scrolled:
            try:
                page.mouse.wheel(0, 3000)
            except Exception:
                pass
        browser.sleep_scroll()


# ---- Phase B helpers (detail page) --------------------------------------

_JD_SELECTORS = [
    ".show-more-less-html__content",
    ".jobs-description-content__text",
    ".jobs-box__html-content",
    "article",
]

_EASY_HINTS = ("easy apply", "easy-apply", "easyapply")
_EASY_SELECTORS = [
    "button[data-easy-apply]",
    "button[aria-label*='easy apply' i]",
    "button[aria-label*='easy apply' a]",
    "button.jobs-apply-button",
]
# Unified scan of every apply-signaling element (classes are hashed now, so
# match on semantics: aria-label / inner text).
_APPLY_SCAN_JS = r"""
() => {
  const out = [];
  const isApply = (t) => /apply/.test(t.toLowerCase());
  document.querySelectorAll('button, a[href^="http"], a[href^="/jobs"]').forEach((e) => {
    const label = ((e.getAttribute('aria-label') || '') + ' ' + (e.innerText || '')).trim();
    if (!isApply(label)) return;
    out.push({ href: e.getAttribute('href') || '', label: label.slice(0, 120) });
  });
  return out;
}
"""


def _decode_safety_url(href: str) -> str:
    """LinkedIn wraps external applies in safety/go?url=<enc>. Recover target."""
    if "linkedin.com/safety/go" not in href:
        return href
    url = parse_qs(urlparse(href).query).get("url")
    return unquote(url[0]) if url else href


_JD_HEADING_JS = r"""
() => {
  const targets = [
    'about the job', 'job details', 'about this role',
    'description', 'job description',
  ];
  const sels = 'main h2, main h3, main h4, main [role="heading"]';
  for (const h of document.querySelectorAll(sels)) {
    const t = (h.innerText || '').trim().toLowerCase();
    if (!targets.includes(t)) continue;
    const sib = h.parentElement ? h.parentElement.nextElementSibling : null;
    if (sib && sib.innerText && sib.innerText.trim().length > 40) {
      return sib.innerText.trim();
    }
  }
  return null;
}
"""


def extract_jd(page) -> str:
    """Pull the job description text off a LinkedIn job detail page."""
    try:
        text = page.evaluate(_JD_HEADING_JS)
        if text:
            return text
    except Exception:
        pass
    for sel in _JD_SELECTORS:  # legacy layouts fallback
        node = page.locator(sel).first
        try:
            if node.count():
                text = node.inner_text()
                if text and text.strip():
                    return text.strip()
        except Exception:
            continue
    return ""


def detect_apply_path(page) -> dict:
    """Classify the apply action: inline (Easy Apply) vs redirect (external site).

    Read-only — never clicks the apply button here (that's Step 2's job).
    Returns {"apply_path", "external_url", "ats_type"}.
    """
    result = {"apply_path": "unknown", "external_url": None, "ats_type": None}
    page.wait_for_timeout(1500)

    # 1) Explicit Easy Apply affordances.
    for sel in _EASY_SELECTORS:
        nodes = page.locator(sel)
        try:
            count = nodes.count()
        except Exception:
            continue
        for i in range(count):
            el = nodes.nth(i)
            try:
                label = (el.get_attribute("aria-label") or "").lower()
                text = el.inner_text().lower()
                if any(h in label or h in text for h in _EASY_HINTS):
                    result["apply_path"] = "inline"
                    return result
            except Exception:
                continue

    # 2) Semantic scan (handles hashed classes on current LinkedIn).
    try:
        rows = page.evaluate(_APPLY_SCAN_JS) or []
    except Exception:
        return result

    for row in rows:
        label = row.get("label") or ""
        if "easy apply" in label.lower() or "without leaving linkedin" in label.lower():
            result["apply_path"] = "inline"
            return result

    for row in rows:
        href = (row.get("href") or "").strip()
        if not href or "apply" not in ((row.get("label") or "").lower()):
            continue
        target = _decode_safety_url(href)
        if "linkedin.com" in target and "linkedin.com/safety/go" not in target:
            continue
        result["apply_path"] = "redirect"
        result["external_url"] = target
        return result
    return result
