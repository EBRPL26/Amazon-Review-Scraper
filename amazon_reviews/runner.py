"""Scrape orchestration: per-ASIN pagination, stop rules, exports, summary; plus --login and --probe."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import parser
from .browser import Browser, PageResult, PermanentError, TransientError, polite_delay
from .config import Settings
from .storage import ReviewStore, export_reviews

log = logging.getLogger("amazon_reviews")

REAUTH_HINT = "Re-authenticate with:  python scraper.py --login   (then re-run)"


@dataclass
class AsinStats:
    asin: str
    status: str = "ok"  # ok | blocked:captcha | blocked:login | error
    pages_scraped: int = 0
    reviews_seen: int = 0
    new_reviews: int = 0
    stop_reason: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _browser(settings: Settings, profile_dir: str | None, headless: bool) -> Browser:
    return Browser(
        profile_dir,
        headless=headless,
        timeout_s=settings.page_timeout_seconds,
        retries=settings.retries,
        executable_path=settings.browser_executable_path,
    )


def _page_url(settings: Settings, template: str, asin: str, page: int) -> str:
    return template.format(base=settings.base, asin=asin, page=page)


def _load_first_page(browser: Browser, settings: Settings, asin: str) -> tuple[PageResult, str]:
    """Try each reviews URL template until one shows reviews (or a CAPTCHA, which ends it)."""
    fallback: tuple[PageResult, str] | None = None
    for i, template in enumerate(settings.reviews_url_templates):
        if i:
            polite_delay(settings.delay_seconds)
        result = browser.load(_page_url(settings, template, asin, 1))
        block = parser.detect_block(result.html, result.url)
        if block == "captcha":
            return result, template
        if block is None and parser.find_review_nodes(parser.make_soup(result.html)):
            return result, template
        log.info("event=template_fallback asin=%s template=%d reason=%s", asin, i + 1, block or "no_reviews")
        if fallback is None or (block is None and parser.detect_block(fallback[0].html, fallback[0].url)):
            fallback = (result, template)
    assert fallback is not None, "reviews_url_templates is empty"
    return fallback


def scrape_asin(browser: Browser, store: ReviewStore, settings: Settings, asin: str) -> tuple[AsinStats, list]:
    stats = AsinStats(asin)
    known = store.known_ids(asin) if settings.incremental else set()
    since = settings.since.isoformat() if settings.since else None
    seen: set[str] = set()
    new_reviews: list = []
    log.info("event=asin_start asin=%s known_reviews=%d since=%s max_pages=%d", asin, len(known), since, settings.max_pages)

    try:
        result, template = _load_first_page(browser, settings, asin)
        signed_in = parser.is_signed_in(result.html)
        if signed_in is False:
            log.warning("event=not_signed_in asin=%s hint=%r", asin, REAUTH_HINT)
        page_no = 1
        while True:
            block = parser.detect_block(result.html, result.url)
            if block:
                stats.status = f"blocked:{block}"
                stats.stop_reason = "CAPTCHA / robot check" if block == "captcha" else "login wall"
                log.error(
                    "event=blocked asin=%s kind=%s url=%s -- stopping this ASIN. %s",
                    asin, block, result.url, REAUTH_HINT,
                )
                break

            stats.pages_scraped += 1
            page_reviews = parser.parse_reviews(result.html, asin, settings.base)
            fresh = [r for r in page_reviews if r.review_id not in seen]
            seen.update(r.review_id for r in fresh)
            stats.reviews_seen += len(fresh)

            stop = ""
            to_save = []
            for r in fresh:
                if since and r.review_date and r.review_date < since:
                    stop = f"reached review older than --since {since}"
                    continue
                if r.review_id in known:
                    stop = stop or "reached already-stored review"
                to_save.append(r)  # known ones too: refreshes helpful_votes / edits
            page_new = store.save(to_save)
            new_reviews += page_new
            stats.new_reviews += len(page_new)
            log.info(
                "event=page_scraped asin=%s page=%d reviews=%d fresh=%d new=%d url=%s",
                asin, stats.pages_scraped, len(page_reviews), len(fresh), len(page_new), result.url,
            )

            if not fresh:
                stats.stop_reason = "no more reviews"
                break
            if stop:
                stats.stop_reason = stop
                break
            if stats.pages_scraped >= settings.max_pages:
                stats.stop_reason = f"max pages ({settings.max_pages})"
                break
            nav = parser.next_navigation(result.html, len(fresh))
            if nav is None:
                stats.stop_reason = "last page"
                break

            polite_delay(settings.delay_seconds)
            if nav == "url":
                page_no += 1
                result = browser.load(_page_url(settings, template, asin, page_no))
            else:
                clicked = browser.click_show_more()
                if clicked is None:
                    stats.stop_reason = "last page"
                    break
                result = clicked
    except (TransientError, PermanentError) as e:
        stats.status = "error"
        stats.errors.append(str(e))
        stats.stop_reason = "error"
        log.error("event=asin_error asin=%s error=%r", asin, str(e))
    except Exception as e:  # keep going with the next ASIN
        stats.status = "error"
        stats.errors.append(f"{type(e).__name__}: {str(e).strip().splitlines()[0] if str(e).strip() else ''}")
        stats.stop_reason = "error"
        log.exception("event=asin_error asin=%s", asin)

    log.info(
        "event=asin_done asin=%s status=%s pages=%d new=%d stop=%r",
        asin, stats.status, stats.pages_scraped, stats.new_reviews, stats.stop_reason,
    )
    return stats, new_reviews


def format_summary(all_stats: list[AsinStats], new_csv: Path | None, total_rows: int) -> str:
    head = f"{'ASIN':<12} {'STATUS':<16} {'PAGES':>5} {'SEEN':>5} {'NEW':>5}  STOP REASON / ERRORS"
    lines = ["", "=" * 78, "RUN SUMMARY", "=" * 78, head, "-" * 78]
    for s in all_stats:
        extra = s.stop_reason + (f" | errors: {'; '.join(s.errors)}" if s.errors else "")
        lines.append(f"{s.asin:<12} {s.status:<16} {s.pages_scraped:>5} {s.reviews_seen:>5} {s.new_reviews:>5}  {extra}")
    lines.append("-" * 78)
    lines.append(
        f"{'TOTAL':<12} {'':<16} {sum(s.pages_scraped for s in all_stats):>5} "
        f"{sum(s.reviews_seen for s in all_stats):>5} {sum(s.new_reviews for s in all_stats):>5}"
    )
    lines.append(f"DB rows total: {total_rows}" + (f" | new reviews CSV: {new_csv}" if new_csv else ""))
    if any(s.status.startswith("blocked") for s in all_stats):
        lines.append(f"!! Some ASINs were blocked (CAPTCHA or login wall). {REAUTH_HINT}")
    lines.append("=" * 78)
    return "\n".join(lines)


def run(settings: Settings, asins: list[str], push_sheets: bool = False) -> int:
    run_id = datetime.now().strftime("%Y%m%d_%H%M")
    log.info("event=run_start run_id=%s asins=%s headful=%s", run_id, ",".join(asins), settings.headful)
    all_stats: list[AsinStats] = []
    all_new: list = []

    with ReviewStore(settings.db_path) as store:
        try:
            with _browser(settings, settings.profile_dir, headless=not settings.headful) as browser:
                for i, asin in enumerate(asins):
                    if i:
                        polite_delay(settings.delay_seconds)
                    stats, new = scrape_asin(browser, store, settings, asin)
                    store.record_run(run_id, stats.as_dict())
                    all_stats.append(stats)
                    all_new += new
        except Exception:
            log.exception("event=browser_error")
            if not all_stats:
                return 1

        total = store.export_all(settings.latest_csv)
        new_csv = Path(settings.exports_dir) / f"new_reviews_{run_id}.csv"
        export_reviews(all_new, new_csv)
        log.info("event=export latest_csv=%s rows=%d new_csv=%s new_rows=%d", settings.latest_csv, total, new_csv, len(all_new))

    if push_sheets or settings.google_sheets.enabled:
        from .sheets import push_new_reviews

        try:
            push_new_reviews(all_new, settings.google_sheets)
        except Exception as e:
            log.error("event=sheets_error error=%r", str(e))

    summary = format_summary(all_stats, new_csv, total)
    for line in summary.splitlines():
        log.info(line)

    if any(s.status.startswith("blocked") for s in all_stats):
        return 2
    return 1 if any(s.status == "error" for s in all_stats) else 0


def login(settings: Settings, asin: str | None) -> int:
    """Open a visible browser on the persistent profile; you sign in by hand once."""
    print(
        "\nA browser window will open on amazon.in.\n"
        "  1. Click 'Hello, sign in' and log in (OTP/2FA as usual). Tick 'Keep me signed in'.\n"
        "  2. Come back here and press Enter.\n"
        "Your password is typed only into Amazon's page; this tool never sees or stores it.\n"
        f"The session is kept in {settings.profile_dir}/ (gitignored).\n"
    )
    with _browser(settings, settings.profile_dir, headless=False) as browser:
        browser.page.goto(f"{settings.base}/", wait_until="domcontentloaded")
        input("Press Enter once you are signed in... ")
        target = (
            _page_url(settings, settings.reviews_url_templates[0], asin, 1) if asin else f"{settings.base}/"
        )
        result = browser.load(target)
        block = parser.detect_block(result.html, result.url)
        signed_in = parser.is_signed_in(result.html)
        n = len(parser.parse_reviews(result.html, asin or "", settings.base)) if asin else None
    if block or signed_in is False:
        print(f"\nNot signed in yet (signed_in={signed_in}, block={block}). Run --login again.")
        return 2
    print(f"\nSigned in. Session saved to {settings.profile_dir}/." + (f" Reviews visible on page 1: {n}" if asin else ""))
    return 0


def probe(settings: Settings, asin: str, out_dir: str = "probe") -> int:
    """Step-1 investigation: what is reachable logged out vs logged in. Saves each page's HTML."""
    out = Path(out_dir)
    out.mkdir(exist_ok=True)
    targets = [("product_page", f"{settings.base}/dp/{asin}")]
    for t, template in enumerate(settings.reviews_url_templates, start=1):
        for page in (1, 2):
            targets.append((f"template{t}_page{page}", _page_url(settings, template, asin, page)))

    rows = []
    modes = [("logged_out", None)]
    if Path(settings.profile_dir).exists():
        modes.append(("logged_in", settings.profile_dir))
    else:
        print(f"(no profile at {settings.profile_dir}; run --login first to probe the logged-in view)")

    for mode, profile in modes:
        with _browser(settings, profile, headless=not settings.headful) as browser:
            first_ids: dict[str, list[str]] = {}
            for i, (name, url) in enumerate(targets):
                if i:
                    polite_delay(settings.delay_seconds)
                row = {"mode": mode, "target": name}
                try:
                    res = browser.load(url)
                except Exception as e:
                    rows.append({**row, "error": str(e)[:80]})
                    continue
                path = out / f"{mode}_{name}.html"
                path.write_text(res.html, encoding="utf-8")
                reviews = parser.parse_reviews(res.html, asin, settings.base)
                ids = [r.review_id for r in reviews]
                first_ids[name] = ids
                dates = [r.review_date for r in reviews if r.review_date]
                row.update(
                    status=res.status,
                    block=parser.detect_block(res.html, res.url) or "-",
                    signed_in=parser.is_signed_in(res.html),
                    reviews=len(reviews),
                    newest=max(dates) if dates else "-",
                    sorted_recent=dates == sorted(dates, reverse=True) if len(dates) > 1 else "-",
                    next=parser.next_navigation(res.html, len(reviews)) or "-",
                    final_url=res.url[:90],
                    saved=str(path),
                )
                if name.endswith("page2"):
                    p1 = set(first_ids.get(name.replace("page2", "page1"), []))
                    row["page2_new_ids"] = len(set(ids) - p1)
                rows.append(row)

    print("\nPROBE RESULTS for", asin)
    for r in rows:
        print("  " + "  ".join(f"{k}={v}" for k, v in r.items()))
    print(
        "\nHow to read this: 'reviews' = review cards found; 'block' = captcha/login wall;\n"
        "'page2_new_ids' > 0 means pageNumber pagination works; 'sorted_recent' checks sortBy=recent.\n"
        f"Saved HTML is in {out}/ -- a logged-in reviews page makes a good extra test fixture."
    )
    return 0
