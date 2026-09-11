"""Long-lived browser sessions with persistent per-source profiles.

Each source gets its own Playwright *persistent context* (a real profile
directory under data/profiles/<source>). You log in once via `aa login`;
every later run reuses that profile — cookies, local storage, and device
state survive. Headless automation rides on an account *you* established.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Self

from playwright.sync_api import Page, sync_playwright

from .errors import AccountBlocked, CaptchaDetected, LoginRequired
from .settings import PROFILE_DIR, SourceCaps
from .utils import pacing

log = logging.getLogger("aa.browser")

SOURCE_HOME: dict[str, str] = {
    "linkedin": "https://www.linkedin.com/feed/",
    "indeed": "https://in.indeed.com/",
    "naukri": "https://www.naukri.com/",
}

_LOGIN_PROBES: dict[str, str] = {
    "linkedin": "https://www.linkedin.com/feed/",
    "indeed": "https://in.indeed.com/jobs",
    "naukri": "https://www.naukri.com/",
}

# Guardrail markers (expanded per source later; generic set here).
_CAPTCHA_PHRASES = [
    "verify you're a human",
    "confirm you're a human",
    "complete a captcha",
    "solve the captcha",
]
_LOGIN_SELECTORS = [
    "form[action*='login']",
    "div.authwall-content",
    "#authwall",
    "input[name='session_key']",  # linkedin login form
]
_BLOCK_MARKERS = ["checkpoint", "challenges", "security-verification", "challenge", "captcha"]


class Browser(AbstractContextManager):
    """Context manager wrapping one persistent context for a source."""

    def __init__(
        self,
        source: str,
        caps: SourceCaps,
        headless: bool = True,
        profile_dir: Path | None = None,
    ) -> None:
        self.source = source
        self.caps = caps
        self.headless = headless
        self.profile_dir = profile_dir or PROFILE_DIR / source
        self.context = None
        self.page: Page | None = None
        self._pw = None

    def __enter__(self) -> Self:
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1440, "height": 900},
            locale="en-IN",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        self.page = (
            self.context.pages[0] if self.context.pages else self.context.new_page()
        )
        self.page.set_default_timeout(30_000)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self._pw is not None:
                self._pw.stop()

    # ---- pacing shortcuts -------------------------------------------
    def sleep_search(self) -> None:
        pacing.human_delay(self.caps.search_delay_seconds)

    def sleep_detail(self) -> None:
        pacing.human_delay(self.caps.detail_delay_seconds)

    def sleep_scroll(self) -> None:
        pacing.human_delay(self.caps.scroll_pause_seconds)

    # ---- guardrail detection ----------------------------------------
    def detect_guardrail(self, page: Page | None = None) -> str | None:
        """Return 'login' | 'block' | 'captcha' when a guardrail is on screen."""
        page = page or self.page
        url = page.url.lower()
        if any(m in url for m in _BLOCK_MARKERS):
            return "block"
        for phrase in _CAPTCHA_PHRASES:
            try:
                if page.get_by_text(phrase, exact=False).count():
                    return "captcha"
            except Exception:
                continue
        for sel in _LOGIN_SELECTORS:
            try:
                if page.locator(sel).count():
                    return "login"
            except Exception:
                continue
        return None

    def assert_clear(self, page: Page | None = None) -> None:
        """Raise the right error for whatever guardrail is present, if any."""
        kind = self.detect_guardrail(page)
        if kind == "login":
            raise LoginRequired(
                f"{self.source} is not logged in. Run `aa login {self.source}` first."
            )
        if kind == "captcha":
            raise CaptchaDetected(f"CAPTCHA at {page.url if page else self.page.url}")
        if kind == "block":
            raise AccountBlocked(f"{self.source} served a block page.")


def verify_logged_in(source: str, page: Page) -> bool:
    if source not in _LOGIN_PROBES:
        raise ValueError(f"no login probe configured for source: {source}")
    probe = _LOGIN_PROBES[source]
    page.goto(probe, wait_until="domcontentloaded")
    markers = _LOGIN_SELECTORS
    for sel in markers:
        try:
            if page.locator(sel).count():
                return False
        except Exception:
            continue
    return True


def bootstrap_login(source: str, profile_dir: Path | None = None) -> None:
    """Open a visible browser; the user logs in manually, then presses Enter."""
    profile_dir = profile_dir or PROFILE_DIR / source
    profile_dir.mkdir(parents=True, exist_ok=True)
    log.info(
        "Opening %s in a visible browser. Log in manually, then press Enter.", source
    )
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1440, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(SOURCE_HOME[source], wait_until="domcontentloaded")
        input(
            "\n>>> Once you are logged in, press Enter here to finish and save the session.\n"
        )
        logged_in = verify_logged_in(source, page)
        if not logged_in:
            log.error(
                "Login not detected for %s. Profile was NOT saved as authenticated.",
                source,
            )
        context.close()
    log.info("Profile saved at %s (authenticated=%s)", profile_dir, logged_in)
