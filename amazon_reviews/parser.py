"""HTML parsing for Amazon review pages.

Pure functions over HTML strings (no browser), so they can be unit-tested
against saved pages in tests/fixtures. All selectors live at the top of this
module: when Amazon changes its markup, this is the one place to update.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

from bs4 import BeautifulSoup, Tag

# --- Selectors (primary first, fallbacks after) ------------------------------

REVIEW_SELECTORS = [
    '[data-hook="review"]',
    'div[id^="customer_review-"]',
    "li.review.aok-relative",
]
RATING_SELECTORS = [
    '[data-hook="review-star-rating"]',
    '[data-hook="cmps-review-star-rating"]',  # reviews from other countries
    'i[class*="a-star-"]',
]
TITLE_SELECTORS = ['[data-hook="review-title"]', "a.review-title", "span.review-title"]
BODY_SELECTORS = ['[data-hook="review-body"]', "span.review-text", "div.review-text"]
DATE_SELECTORS = ['[data-hook="review-date"]', "span.review-date"]
AUTHOR_SELECTORS = [".a-profile-name", '[data-hook="review-author"]']
VERIFIED_SELECTORS = [
    '[data-hook="avp-badge"]',
    '[data-hook="avp-badge-linkless"]',
]
HELPFUL_SELECTORS = ['[data-hook="helpful-vote-statement"]', "span.cr-vote-text"]
VARIANT_SELECTORS = ['[data-hook="format-strip"]', '[data-hook="format-strip-linkless"]']
IMAGE_SELECTORS = [
    '[data-hook="review-image-tile"]',
    ".review-image-tile-section img",
    "img.review-image-tile",
]
VIDEO_SELECTORS = [
    '[data-hook="review-video"]',
    ".video-block",
    'div[id^="video-block"]',
    "video",
    "[data-video-url]",
]

# Pagination controls.
NEXT_PAGE_SELECTORS = ["ul.a-pagination li.a-last a", '[data-hook="pagination-bar"] li.a-last a']
NEXT_DISABLED_SELECTORS = ["ul.a-pagination li.a-last.a-disabled"]
SHOW_MORE_SELECTORS = ['[data-hook="show-more-button"]', '[data-hook="see-more-reviews-button"]']

# Blocking pages. We only *detect* these and stop; we never try to solve them.
CAPTCHA_SELECTORS = ['form[action*="validateCaptcha"]', "#captchacharacters"]
CAPTCHA_MARKERS = [
    "Enter the characters you see below",
    "make sure you're not a robot",
    "make sure you&#39;re not a robot",
    "Type the characters you see in this image",
]
LOGIN_SELECTORS = ['form[name="signIn"]', "#ap_email", "#ap_password", "#ap_email_login"]

REVIEWS_PER_FULL_PAGE = 10

# --- Data model --------------------------------------------------------------


@dataclass
class Review:
    asin: str
    review_id: str
    rating: float | None
    title: str
    body: str
    review_date: str | None  # ISO yyyy-mm-dd
    reviewer_name: str
    verified_purchase: bool
    helpful_votes: int
    variant: str
    size: str
    color: str
    has_images: bool
    has_videos: bool
    country: str
    review_url: str
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return asdict(self)


COLUMNS = list(Review.__dataclass_fields__)

# --- Small helpers -----------------------------------------------------------

_MONTHS = {
    m.lower(): i
    for i, names in enumerate(
        [
            ("January", "Jan"), ("February", "Feb"), ("March", "Mar"), ("April", "Apr"),
            ("May",), ("June", "Jun"), ("July", "Jul"), ("August", "Aug"),
            ("September", "Sep", "Sept"), ("October", "Oct"), ("November", "Nov"), ("December", "Dec"),
        ],
        start=1,
    )
    for m in names
}
_DMY = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\.?,?\s+(\d{4})")  # 5 October 2024 (en-IN / en-GB)
_MDY = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})")  # October 5, 2024 (en-US)
_REVIEWED_IN = re.compile(r"Reviewed in\s+(?:the\s+)?(.+?)\s+on\s+(.+)", re.IGNORECASE)
_RATING = re.compile(r"(\d+(?:[.,]\d+)?)\s*out of\s*5")
_STAR_CLASS = re.compile(r"a-star-(?:mini-)?(\d)(?:-(\d))?")
_REVIEW_ID = re.compile(r"\b(R[0-9A-Z]{8,20})\b")
_WS = re.compile(r"[ \t\r\f\v]+")


def _first(node: Tag, selectors: list[str]) -> Tag | None:
    for sel in selectors:
        found = node.select_one(sel)
        if found is not None:
            return found
    return None


def _any(node: Tag, selectors: list[str]) -> bool:
    return _first(node, selectors) is not None


def _text(node: Tag | None, sep: str = " ") -> str:
    if node is None:
        return ""
    lines = [_WS.sub(" ", line).strip() for line in node.get_text(sep).splitlines()]
    return "\n".join(line for line in lines if line).strip()


def parse_review_date(text: str) -> tuple[str | None, str]:
    """'Reviewed in India on 5 October 2024' -> ('2024-10-05', 'India')."""
    text = _WS.sub(" ", text or "").strip()
    country, date_part = "", text
    m = _REVIEWED_IN.search(text)
    if m:
        country, date_part = m.group(1).strip(), m.group(2).strip()
    for rx, order in ((_DMY, "dmy"), (_MDY, "mdy")):
        dm = rx.search(date_part)
        if not dm:
            continue
        if order == "dmy":
            day, mon, year = dm.group(1), dm.group(2), dm.group(3)
        else:
            mon, day, year = dm.group(1), dm.group(2), dm.group(3)
        month = _MONTHS.get(mon.lower())
        if month:
            try:
                return date(int(year), month, int(day)).isoformat(), country
            except ValueError:
                pass
    return None, country


def parse_rating(node: Tag | None) -> float | None:
    if node is None:
        return None
    m = _RATING.search(node.get_text(" "))
    if m:
        return float(m.group(1).replace(",", "."))
    classes = " ".join(node.get("class", []))
    m = _STAR_CLASS.search(classes)
    if m:
        return float(f"{m.group(1)}.{m.group(2) or 0}")
    return None


def parse_helpful_votes(text: str) -> int:
    """'One person found this helpful' -> 1, '1,234 people found this helpful' -> 1234."""
    text = (text or "").strip().lower()
    if not text:
        return 0
    m = re.search(r"([\d,]+)", text)
    if m:
        return int(m.group(1).replace(",", ""))
    if text.startswith(("one", "a person")):
        return 1
    return 0


def parse_variant(node: Tag | None) -> tuple[str, str, str]:
    """Format strip ('Colour: Blue | Size: M') -> (variant, size, color)."""
    if node is None:
        return "", "", ""
    parts = [p.strip() for p in re.split(r"\s*\|\s*", node.get_text("|")) if p.strip()]
    variant = " | ".join(parts)
    size = color = ""
    for p in parts:
        key, _, val = p.partition(":")
        k = key.strip().lower()
        if "size" in k and not size:
            size = val.strip()
        elif k in ("colour", "color", "colour name", "color name") and not color:
            color = val.strip()
    return variant, size, color


def _review_id(node: Tag) -> str:
    for attr in ("id", "data-review-id"):
        m = _REVIEW_ID.search(node.get(attr, "") or "")
        if m:
            return m.group(1)
    for a in node.select('a[href*="customer-reviews/"]'):
        m = _REVIEW_ID.search(a.get("href", ""))
        if m:
            return m.group(1)
    return ""


def _clean_title(node: Tag | None) -> str:
    if node is None:
        return ""
    node = BeautifulSoup(str(node), "lxml")  # copy, so we can strip children
    for junk in node.select("i, .a-icon-alt, .a-letter-space, script, style"):
        junk.decompose()
    original = node.select_one(".cr-original-review-content")
    return _text(original or node)


def _clean_body(node: Tag | None) -> str:
    if node is None:
        return ""
    node = BeautifulSoup(str(node), "lxml")
    for junk in node.select(
        'script, style, .video-block, [data-hook="review-video"], '
        '[data-hook="expand-collapse-read-more-less"], .cr-translate-this-review-section'
    ):
        junk.decompose()
    original = node.select_one(".cr-original-review-content")
    body = _text(original or node, sep="\n")
    return re.sub(r"\n?(Read more|Read less)\s*$", "", body).strip()


# --- Public API --------------------------------------------------------------


def make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def find_review_nodes(soup: BeautifulSoup) -> list[Tag]:
    for sel in REVIEW_SELECTORS:
        nodes = soup.select(sel)
        if nodes:
            return nodes
    return []


def parse_review(node: Tag, asin: str, base_url: str = "https://www.amazon.in") -> Review | None:
    review_id = _review_id(node)
    if not review_id:
        return None
    review_date, country = parse_review_date(_text(_first(node, DATE_SELECTORS)))
    variant, size, color = parse_variant(_first(node, VARIANT_SELECTORS))
    return Review(
        asin=asin,
        review_id=review_id,
        rating=parse_rating(_first(node, RATING_SELECTORS)),
        title=_clean_title(_first(node, TITLE_SELECTORS)),
        body=_clean_body(_first(node, BODY_SELECTORS)),
        review_date=review_date,
        reviewer_name=_text(_first(node, AUTHOR_SELECTORS)),
        verified_purchase=_any(node, VERIFIED_SELECTORS),
        helpful_votes=parse_helpful_votes(_text(_first(node, HELPFUL_SELECTORS))),
        variant=variant,
        size=size,
        color=color,
        has_images=_any(node, IMAGE_SELECTORS),
        has_videos=_any(node, VIDEO_SELECTORS),
        country=country,
        review_url=f"{base_url.rstrip('/')}/gp/customer-reviews/{review_id}/?ASIN={asin}",
    )


def parse_reviews(html: str, asin: str, base_url: str = "https://www.amazon.in") -> list[Review]:
    """All reviews on a page, deduped by review_id, in page order."""
    seen: set[str] = set()
    out: list[Review] = []
    for node in find_review_nodes(make_soup(html)):
        review = parse_review(node, asin, base_url)
        if review and review.review_id not in seen:
            seen.add(review.review_id)
            out.append(review)
    return out


def detect_block(html: str, url: str = "") -> str | None:
    """Return 'captcha', 'login' or None. Detection only; never bypass."""
    soup = make_soup(html)
    if "validateCaptcha" in url or _any(soup, CAPTCHA_SELECTORS):
        return "captcha"
    # Text markers only count on pages without reviews, so a review that happens
    # to say "not a robot" can't trigger a false stop.
    if not find_review_nodes(soup) and any(m in html for m in CAPTCHA_MARKERS):
        return "captcha"
    if "/ap/signin" in url or _any(soup, LOGIN_SELECTORS):
        return "login"
    return None


def is_signed_in(html: str) -> bool | None:
    """True/False from the nav greeting ('Hello, Asha' vs 'Hello, sign in'); None if absent."""
    soup = make_soup(html)
    greet = soup.select_one("#nav-link-accountList-nav-line-1") or soup.select_one("#nav-link-accountList")
    if greet is None:
        return None
    return "sign in" not in greet.get_text(" ").lower()


def next_navigation(html: str, reviews_on_page: int) -> str | None:
    """How to reach the next page: 'url' (pageNumber+1), 'show_more' (click), or None (last page)."""
    soup = make_soup(html)
    if _any(soup, NEXT_DISABLED_SELECTORS):
        return None
    if _any(soup, NEXT_PAGE_SELECTORS):
        return "url"
    if _any(soup, SHOW_MORE_SELECTORS):
        return "show_more"
    # No recognisable control: a short page is the last one; otherwise try pageNumber+1
    # (an empty or repeated page then ends pagination).
    return "url" if reviews_on_page >= REVIEWS_PER_FULL_PAGE else None
