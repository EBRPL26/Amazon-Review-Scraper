#!/usr/bin/env python3
"""Amazon review scraper. Run `python scraper.py --help`."""

import sys

from amazon_reviews.cli import main

if __name__ == "__main__":
    sys.exit(main())
