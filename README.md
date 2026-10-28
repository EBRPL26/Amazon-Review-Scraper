# Amazon Review Scraper (amazon.in)

Pulls the **latest** customer reviews for a list of your ASINs, stores them in SQLite, and
exports CSVs so you can track new ratings, complaints and sentiment. It's built to run once
a day, politely: one page at a time, with random 3–8 s pauses.

- **Playwright (Chromium) with a persistent browser profile.** You sign in by hand **once**
  in a visible window. Later runs (headless or `--headful`) reuse that saved session.
- **Incremental.** Each run stops as soon as it reaches a review it already has, so daily
  runs usually load only 1 page per ASIN.
- **Stops cleanly on CAPTCHA or login wall.** It logs the problem, skips that ASIN and tells
  you to re-authenticate. It never tries to solve or bypass a CAPTCHA.

## Why this approach

Since late 2024, Amazon has generally put the full reviews page (`/product-reviews/<ASIN>`)
behind sign-in. Logged out you usually get only the handful of "Top reviews" on the product
page, which can't be sorted by date or paginated. (`--probe` checks whether this is true
for your account and for the newer `/portal/customer-reviews/` URL.) Plain `requests` + BeautifulSoup would need you to copy cookies out
of your browser by hand and would break whenever they expire. A real browser with a saved
profile keeps the login working across runs. BeautifulSoup still does the parsing,
so the selectors can be unit-tested against saved HTML.

> **Check this for your account:** run `python scraper.py --probe --asin B0DLVGWRVJ`
> (see below). It loads the product page and both review-URL formats (pages 1 and 2),
> logged out and logged in. For each it reports how many reviews were visible, whether
> `sortBy=recent` ordering holds, whether `pageNumber=2` returns new reviews, and whether a
> CAPTCHA or login wall appeared. Every page's HTML is saved to `probe/`.

## Setup

Needs Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium        # downloads Chromium for Playwright (~150 MB, into your user cache)
```

## One-time login

```bash
python scraper.py --login --asin B0DLVGWRVJ
```

A Chromium window opens on amazon.in. Click **Hello, sign in**, log in as usual
(OTP/2FA included) and tick **Keep me signed in**. Then go back to the terminal and press
Enter. The tool loads the reviews page to confirm you're signed in.

- Your credentials are typed only into Amazon's own page. Nothing is hard-coded or stored
  by this tool except the browser profile, in `.auth/browser-profile/` (gitignored).
  Treat that folder like a password: it holds your session cookies.
- Re-run `--login` whenever a run reports `blocked:login` or `blocked:captcha`.

## Running

```bash
# All ASINs from config.yaml + asins.txt
python scraper.py

# Specific ASINs (repeatable; product URLs work too)
python scraper.py --asin B0DLVGWRVJ --asin https://www.amazon.in/dp/B0XXXXXXXX

# From a file, more pages, only reviews on/after a date
python scraper.py --asins-file asins.txt --max-pages 10 --since 2026-09-01

# Watch the browser; ignore stored reviews and re-scan up to --max-pages
python scraper.py --headful --full

# Investigate access (logged-out vs logged-in), saves HTML to probe/
python scraper.py --probe --asin B0DLVGWRVJ
```

| Flag | Meaning |
|---|---|
| `--asin` | ASIN or product URL, repeatable. Replaces the config list. |
| `--asins-file` | One ASIN/URL per line, `#` comments allowed. |
| `--max-pages` | Pages per ASIN (default 5; 10 reviews per page). |
| `--since YYYY-MM-DD` | Keep reviews on/after this date; stop at the first older one. |
| `--headful` | Show the browser window. |
| `--full` | Don't stop at already-stored reviews (still deduped). |
| `--google-sheet` | Also append new reviews to Google Sheets (see below). |
| `--login` / `--probe` | One-time sign-in / access investigation. |
| `--config` | Settings file (default `config.yaml`). |

All settings (delays, retries, paths, URL templates) are in `config.yaml`.

### How pagination stops

Per ASIN, the scraper opens the reviews page with `sortBy=recent`, then follows
`pageNumber=2, 3, …`. If the page uses a "Show more reviews" button instead, it clicks that.
It stops at the first of:

1. no new reviews on the page / "Next page" disabled,
2. `--max-pages` reached,
3. a review older than `--since` (that review isn't stored),
4. a review already in `reviews.db` (incremental mode, the default),
5. a CAPTCHA or login wall: stop the ASIN, log `event=blocked`, carry on with the next ASIN.

Timeouts, HTTP 5xx and network errors are retried up to 3 times, with exponential backoff
(≈5 s, 10 s, 20 s).

Both review-URL formats are in `config.yaml` (`reviews_url_templates`). On page 1 they're
tried in order, and the first one that shows reviews is used for the rest of the pages.
If `--probe` shows only one of them works for you, put that one first.

## Output

| File | Contents |
|---|---|
| `reviews.db` | SQLite, the source of truth. Table `reviews`, unique on `(asin, review_id)`, plus `first_seen_at`. Table `runs`: one row per ASIN per run. |
| `reviews_latest.csv` | Full export of all stored reviews, newest first per ASIN. |
| `exports/new_reviews_YYYYMMDD_HHMM.csv` | Only the reviews that were new in that run (header-only if none). |
| `scraper.log` | Rotating log, same lines as the console (`event=… key=value` format). |

Columns: `asin, review_id, rating, title, body, review_date (ISO), reviewer_name,
verified_purchase, helpful_votes, variant, size, color, has_images, has_videos, country,
review_url, scraped_at`. CSVs are UTF-8 with BOM, so Excel shows ₹ and Hindi text correctly.

When a review you already have is seen again, its `helpful_votes`, `title` and `body` are
updated in place (people vote and edit after posting).

Handy queries:

```bash
sqlite3 reviews.db "SELECT asin, review_date, rating, title FROM reviews WHERE rating <= 2 ORDER BY review_date DESC LIMIT 20;"
sqlite3 reviews.db "SELECT asin, ROUND(AVG(rating),2), COUNT(*) FROM reviews WHERE review_date >= date('now','-30 day') GROUP BY asin;"
```

Every run ends with a summary like:

```
ASIN         STATUS           PAGES  SEEN   NEW  STOP REASON / ERRORS
B0DLVGWRVJ   ok                   1    10     2  reached already-stored review
B0XXXXXXXX   blocked:captcha      0     0     0  CAPTCHA / robot check
!! Some ASINs were blocked (CAPTCHA or login wall). Re-authenticate with:  python scraper.py --login
```

Exit codes: `0` all OK, `1` errors (network, unexpected page), `2` at least one ASIN was
blocked, so re-run `--login`. Schedulers can alert on non-zero.

## Scheduling daily

The browser profile can only be used by one browser at a time, so don't run the
scheduled job while a `--login` window is open.

**Linux / macOS (cron).** `crontab -e`, then add (runs 09:17 daily):

```
17 9 * * * cd /path/to/Amazon-Review-Scraper && .venv/bin/python scraper.py >> cron.log 2>&1
```

**Windows (Task Scheduler).** Create `run_daily.bat` in the project folder:

```bat
@echo off
cd /d C:\path\to\Amazon-Review-Scraper
.venv\Scripts\python.exe scraper.py
```

Then register it (runs 09:17 daily, as you, only when you're logged in):

```bat
schtasks /Create /SC DAILY /ST 09:17 /TN "AmazonReviewScraper" /TR "C:\path\to\Amazon-Review-Scraper\run_daily.bat"
```

You can also use the Task Scheduler GUI: *Create Basic Task → Daily → Start a program →*
`run_daily.bat`, with *Start in* set to the project folder.

If scheduled headless runs start hitting CAPTCHAs, add `--headful` to the command. A
visible browser on your own machine is the least bot-like setup, and it is also the most
reliable.

## Optional: Google Sheets

Off by default.

1. `pip install gspread`
2. Create a Google Cloud service account, download its JSON key to
   `.auth/google-service-account.json`, and share your sheet with the service account's
   `client_email` (Editor).
3. In `config.yaml`, set `google_sheets.spreadsheet_id` (the long ID in the sheet URL).
   Then either set `enabled: true` or pass `--google-sheet` on runs.

Each run appends its new reviews to the `new_reviews` worksheet, adding a header row the
first time.

## Tests

```bash
python -m unittest discover -s tests -v      # or: pytest
```

- `tests/test_parser.py` parses `tests/fixtures/reviews_page_sample.html` and checks every
  field, plus CAPTCHA and login-wall detection. **If Amazon changes its markup, this is
  what breaks first.**
- `tests/test_end_to_end.py` runs the real Playwright flow against a local fake server that
  serves the fixtures. It covers pagination, the incremental stop, `--since`, `--max-pages`,
  CAPTCHA/login stops and 5xx retries. It's skipped if Chromium isn't installed.

**About the fixtures.** They are synthetic: hand-written to match the structure of Amazon's
review markup, with made-up products, people and text. To catch live selector changes, add
a real page:

1. Run `python scraper.py --probe --asin B0DLVGWRVJ`.
2. Copy `probe/logged_in_template1_page1.html` to `tests/fixtures/real_reviews_page.html`.
3. Add a test asserting that it yields 10 reviews with non-empty core fields. Copy
   `test_every_review_has_core_fields` and point it at the new file.

All selectors are listed at the top of `amazon_reviews/parser.py`, so a markup change is
fixed in one place.

## Project layout

```
scraper.py               entry point (python scraper.py --help)
config.yaml              settings; asins.txt for the ASIN list
amazon_reviews/
  cli.py                 flags, logging
  runner.py              per-ASIN pagination + stop rules, summary, --login, --probe
  browser.py             Playwright persistent profile, retries/backoff, show-more clicks
  parser.py              BeautifulSoup parsing; all selectors; CAPTCHA/login detection
  storage.py             SQLite + CSV export
  sheets.py              optional Google Sheets push
tests/                   parser + end-to-end tests, fixtures/
.auth/                   browser profile + keys (gitignored, created on first --login)
```

## Troubleshooting

- **"Browser profile … is in use".** Close any Chromium window still open from `--login`
  and run again.
- **`not_signed_in` warning, or `blocked:login`.** The session expired or Amazon logged you
  out. Run `--login` again.
- **Hindi page text / dates not parsed.** URLs include `language=en_IN` and the browser
  sends `Accept-Language: en-IN`. If your account language is set to Hindi, switch it to
  English on amazon.in.
- **0 reviews but no block.** The selectors may be out of date. Run `--probe`, open the saved
  HTML, and update the selector lists in `parser.py`.

Use this on your own listings, at a modest pace. The defaults (sequential, 3–8 s between
pages, a few pages per day) are meant to look like a person checking their reviews.
