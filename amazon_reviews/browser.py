"""Playwright browser with a persistent, reusable login profile."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from . import parser

log = logging.getLogger("amazon_reviews.browser")

# Wait for either reviews or a page we must stop on, whichever shows up first.
READY_SELECTOR = ", ".join(
    parser.REVIEW_SELECTORS[:1]
    + parser.CAPTCHA_SELECTORS
    + parser.LOGIN_SELECTORS[:2]
    + ['[data-hook="cr-filter-info-review-rating-count"]', "#cm_cr-review_list", "#nav-logo"]
)


class TransientError(Exception):
    """Timeouts and 5xx: worth retrying."""


class PermanentError(Exception):
    """4xx and similar: retrying won't help."""


@dataclass
class PageResult:
    url: str
    status: int | None
    html: str


class Browser:
    def __init__(
        self,
        profile_dir: str | Path | None,
        headless: bool = True,
        timeout_s: float = 45.0,
        retries: int = 3,
        executable_path: str | None = None,
        backoff_base_s: float = 5.0,
    ):
        """profile_dir=None gives a throwaway logged-out profile (used by --probe)."""
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self.headless = headless
        self.timeout_ms = timeout_s * 1000
        self.retries = retries
        self.executable_path = executable_path
        self.backoff_base_s = backoff_base_s
        self._pw = self._browser = self.context = self.page = None

    def __enter__(self) -> Browser:
        self._pw = sync_playwright().start()
        opts = dict(
            headless=self.headless,
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
        )
        launch = {"executable_path": self.executable_path} if self.executable_path else {}
        try:
            if self.profile_dir:
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                self.context = self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir), **launch, **opts
                )
            else:
                self._browser = self._pw.chromium.launch(headless=self.headless, **launch)
                self.context = self._browser.new_context(**{k: v for k, v in opts.items() if k != "headless"})
        except PlaywrightError as e:
            self._pw.stop()
            if "ProcessSingleton" in str(e) or "already in use" in str(e):
                raise RuntimeError(
                    f"Browser profile {self.profile_dir} is in use by another browser window. Close it and retry."
                ) from e
            raise
        self.context.set_default_timeout(self.timeout_ms)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.context:
                self.context.close()
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _backoff(self, attempt: int, err: Exception) -> None:
        wait = self.backoff_base_s * (2 ** (attempt - 1)) + random.uniform(0, 1)
        log.warning("event=retry attempt=%d wait_s=%.1f error=%r", attempt, wait, _first_line(err)[:200])
        time.sleep(wait)

    def _wait_ready(self) -> None:
        try:
            self.page.wait_for_selector(READY_SELECTOR, timeout=min(self.timeout_ms, 15000))
        except PlaywrightTimeoutError:
            pass  # parse whatever loaded; the caller decides

    def load(self, url: str) -> PageResult:
        """Navigate with retry + exponential backoff on timeouts and 5xx."""
        for attempt in range(1, self.retries + 2):
            try:
                resp = self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                status = resp.status if resp else None
                if status is not None and status >= 500:
                    raise TransientError(f"HTTP {status}")
                if status is not None and status >= 400:
                    raise PermanentError(f"HTTP {status} for {url}")
                self._wait_ready()
                return PageResult(self.page.url, status, self.page.content())
            except (PlaywrightTimeoutError, TransientError, PlaywrightError) as e:
                # Timeouts, 5xx and network errors (net::ERR_*) are retried; other browser errors are not.
                if isinstance(e, PlaywrightError) and not isinstance(e, PlaywrightTimeoutError) and "net::ERR_" not in str(e):
                    raise
                if attempt > self.retries:
                    raise TransientError(f"gave up after {attempt} attempts: {_first_line(e)}") from e
                self._backoff(attempt, e)
        raise AssertionError("unreachable")

    def click_show_more(self) -> PageResult | None:
        """Click a 'Show more reviews' button and wait for more reviews to render."""
        sel = ", ".join(parser.SHOW_MORE_SELECTORS)
        review_sel = parser.REVIEW_SELECTORS[0]
        for attempt in range(1, self.retries + 2):
            before = self.page.locator(review_sel).count()
            button = self.page.locator(sel).first
            if button.count() == 0:
                return None
            try:
                button.click()
                self.page.wait_for_function(
                    "([s, n]) => document.querySelectorAll(s).length > n",
                    arg=[review_sel, before],
                    timeout=self.timeout_ms,
                )
                return PageResult(self.page.url, None, self.page.content())
            except PlaywrightTimeoutError as e:
                html = self.page.content()
                if parser.detect_block(html, self.page.url):
                    return PageResult(self.page.url, None, html)
                if attempt > self.retries:
                    raise TransientError(f"show-more gave up after {attempt} attempts: {e}") from e
                self._backoff(attempt, e)
        raise AssertionError("unreachable")


def _first_line(err: Exception) -> str:
    return str(err).strip().splitlines()[0] if str(err).strip() else type(err).__name__


def polite_delay(bounds: tuple[float, float]) -> None:
    lo, hi = bounds
    if hi <= 0:
        return
    wait = random.uniform(lo, hi)
    log.debug("event=delay seconds=%.1f", wait)
    time.sleep(wait)
