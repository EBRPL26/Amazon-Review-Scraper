"""Settings: defaults < config.yaml < CLI flags."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from datetime import date
from pathlib import Path

import yaml

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_ASIN_IN_URL = re.compile(r"/(?:dp|product-reviews|customer-reviews|gp/product)/([A-Z0-9]{10})")

DEFAULT_URL_TEMPLATES = [
    "{base}/product-reviews/{asin}/?ie=UTF8&reviewerType=all_reviews&sortBy=recent&pageNumber={page}&language=en_IN",
    "{base}/portal/customer-reviews/{asin}/?_encoding=UTF8&ie=UTF8&reviewerType=all_reviews&sortBy=recent&pageNumber={page}&language=en_IN",
]


@dataclass
class GoogleSheetsSettings:
    enabled: bool = False
    service_account_file: str = ".auth/google-service-account.json"
    spreadsheet_id: str = ""
    worksheet: str = "new_reviews"


@dataclass
class Settings:
    marketplace: str = "amazon.in"
    base_url: str | None = None  # override for testing; default https://www.<marketplace>
    asins: list[str] = field(default_factory=list)
    asins_file: str | None = "asins.txt"
    max_pages: int = 5
    since: date | None = None
    headful: bool = False
    incremental: bool = True
    delay_seconds: tuple[float, float] = (3.0, 8.0)
    retries: int = 3
    page_timeout_seconds: float = 45.0
    profile_dir: str = ".auth/browser-profile"
    browser_executable_path: str | None = None
    db_path: str = "reviews.db"
    latest_csv: str = "reviews_latest.csv"
    exports_dir: str = "exports"
    log_file: str = "scraper.log"
    reviews_url_templates: list[str] = field(default_factory=lambda: list(DEFAULT_URL_TEMPLATES))
    google_sheets: GoogleSheetsSettings = field(default_factory=GoogleSheetsSettings)

    @property
    def base(self) -> str:
        return (self.base_url or f"https://www.{self.marketplace}").rstrip("/")


def normalize_asin(raw: str) -> str | None:
    raw = raw.strip()
    m = _ASIN_IN_URL.search(raw)
    candidate = (m.group(1) if m else raw).upper()
    return candidate if ASIN_RE.match(candidate) else None


def read_asins_file(path: str | Path) -> list[str]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        asin = normalize_asin(line)
        if asin is None:
            raise ValueError(f"{path}: not an ASIN or product URL: {line!r}")
        out.append(asin)
    return out


def _parse_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def load_settings(config_path: str | Path | None) -> Settings:
    s = Settings()
    if config_path and Path(config_path).exists():
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        known = {f.name for f in fields(Settings)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"{config_path}: unknown settings: {', '.join(sorted(unknown))}")
        for key, value in raw.items():
            if key == "google_sheets":
                s.google_sheets = GoogleSheetsSettings(**(value or {}))
            elif key == "since":
                s.since = _parse_date(value)
            elif key == "delay_seconds":
                lo, hi = value
                s.delay_seconds = (float(lo), float(hi))
            elif key == "asins":
                s.asins = [a for a in (normalize_asin(str(v)) for v in value or []) if a]
            elif value is not None or key in ("asins_file", "base_url", "browser_executable_path"):
                setattr(s, key, value)
    return s


def resolve_asins(s: Settings, cli_asins: list[str], cli_asins_file: str | None) -> list[str]:
    """--asin / --asins-file replace the configured list; otherwise use config asins + asins_file."""
    if cli_asins or cli_asins_file:
        asins = []
        for a in cli_asins:
            norm = normalize_asin(a)
            if norm is None:
                raise ValueError(f"not an ASIN or product URL: {a!r}")
            asins.append(norm)
        if cli_asins_file:
            asins += read_asins_file(cli_asins_file)
    else:
        asins = list(s.asins)
        if s.asins_file and Path(s.asins_file).exists():
            asins += read_asins_file(s.asins_file)
    return list(dict.fromkeys(asins))  # dedupe, keep order
