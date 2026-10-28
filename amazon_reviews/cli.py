"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from logging.handlers import RotatingFileHandler

from .config import load_settings, resolve_asins

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"


def setup_logging(log_file: str, verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file.setFormatter(fmt)
    root.handlers[:] = [console, file]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scraper.py",
        description="Scrape the latest Amazon reviews for your ASINs into SQLite + CSV.",
    )
    p.add_argument("--config", default="config.yaml", help="settings file (default: config.yaml)")
    p.add_argument("--asin", action="append", default=[], help="ASIN or product URL; repeatable")
    p.add_argument("--asins-file", help="text file with one ASIN (or product URL) per line")
    p.add_argument("--max-pages", type=int, help="max review pages per ASIN (default 5)")
    p.add_argument("--since", type=date.fromisoformat, help="only keep reviews on/after YYYY-MM-DD")
    p.add_argument("--headful", action="store_true", help="show the browser window")
    p.add_argument("--full", action="store_true", help="don't stop at already-stored reviews")
    p.add_argument("--google-sheet", action="store_true", help="also append new reviews to Google Sheets")
    p.add_argument("--login", action="store_true", help="one-time manual sign-in (opens a browser)")
    p.add_argument("--probe", action="store_true", help="investigate logged-out vs logged-in access for --asin")
    p.add_argument("--no-delay", action="store_true", help=argparse.SUPPRESS)  # tests only
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    if args.max_pages is not None:
        settings.max_pages = args.max_pages
    if args.since is not None:
        settings.since = args.since
    if args.headful:
        settings.headful = True
    if args.full:
        settings.incremental = False
    if args.no_delay:
        settings.delay_seconds = (0.0, 0.0)

    setup_logging(settings.log_file, args.verbose)
    log = logging.getLogger("amazon_reviews")

    try:
        asins = resolve_asins(settings, args.asin, args.asins_file)
    except (ValueError, OSError) as e:
        log.error("event=bad_input error=%r", str(e))
        return 1

    from . import runner  # imported late so --help works without Playwright installed

    if args.login:
        return runner.login(settings, asins[0] if asins else None)
    if not asins:
        log.error("event=no_asins hint='pass --asin, --asins-file, or list them in config.yaml / asins.txt'")
        return 1
    if args.probe:
        return runner.probe(settings, asins[0])
    return runner.run(settings, asins, push_sheets=args.google_sheet)


if __name__ == "__main__":
    sys.exit(main())
