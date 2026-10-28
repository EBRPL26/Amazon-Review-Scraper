"""End-to-end: real Playwright + Chromium against a local fake 'Amazon' serving the fixtures.

Checks pagination, incremental stop, --since, CAPTCHA/login stop, 5xx retry, exports.
Skipped automatically if Playwright/Chromium isn't installed.
"""

import csv
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from amazon_reviews.config import Settings
from amazon_reviews.storage import ReviewStore

try:
    from amazon_reviews import runner
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        p.chromium.launch().close()
    HAVE_BROWSER = True
except Exception:  # noqa: BLE001
    HAVE_BROWSER = False

FIXTURES = Path(__file__).parent / "fixtures"
PAGES = {name: (FIXTURES / f"{name}.html").read_bytes() for name in
         ("reviews_page_sample", "reviews_page2_sample", "captcha_sample", "signin_sample")}
EMPTY = b"<html><body><div id='cm_cr-review_list'></div></body></html>"


class FakeAmazon(BaseHTTPRequestHandler):
    hits: dict[str, int] = {}

    def log_message(self, *args):
        pass

    def _send(self, body: bytes, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        page = parse_qs(url.query).get("pageNumber", ["1"])[0]
        key = f"{url.path}?pageNumber={page}"
        FakeAmazon.hits[key] = FakeAmazon.hits.get(key, 0) + 1
        if url.path.startswith("/ap/signin"):
            return self._send(PAGES["signin_sample"])
        if "B000LOGIN1" in url.path:
            self.send_response(302)
            self.send_header("Location", "/ap/signin?openid.return_to=x")
            return self.end_headers()
        if "B000CAPTCH" in url.path:
            return self._send(PAGES["captcha_sample"])
        if "B000FLAKY1" in url.path and FakeAmazon.hits[key] == 1:
            return self._send(b"<html>Service Unavailable</html>", 503)
        if url.path.startswith("/product-reviews/"):
            return self._send({"1": PAGES["reviews_page_sample"], "2": PAGES["reviews_page2_sample"]}.get(page, EMPTY))
        return self._send(EMPTY, 404)


@unittest.skipUnless(HAVE_BROWSER, "Playwright Chromium not available")
class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAmazon)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.no_sleep = mock.patch("amazon_reviews.browser.time.sleep")
        cls.no_sleep.start()

    @classmethod
    def tearDownClass(cls):
        cls.no_sleep.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeAmazon.hits = {}
        self.tmp = Path(tempfile.mkdtemp())

    def settings(self, **kw) -> Settings:
        s = Settings(
            base_url=self.base,
            delay_seconds=(0, 0),
            retries=2,
            page_timeout_seconds=10,
            profile_dir=str(self.tmp / "profile"),
            db_path=str(self.tmp / "reviews.db"),
            latest_csv=str(self.tmp / "reviews_latest.csv"),
            exports_dir=str(self.tmp / "exports"),
        )
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def stats(self, s: Settings) -> dict:
        with ReviewStore(s.db_path) as store:
            rows = store.conn.execute("SELECT asin, status, pages_scraped, new_reviews, stop_reason FROM runs").fetchall()
        return {r[0]: r[1:] for r in rows}

    def csv_rows(self, path) -> list[dict]:
        with open(path, encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    def test_paginates_then_incremental_run_stops_early(self):
        s = self.settings()
        self.assertEqual(runner.run(s, ["B000TEST01"]), 0)
        self.assertEqual(self.stats(s)["B000TEST01"], ("ok", 2, 8, "last page"))
        latest = self.csv_rows(s.latest_csv)
        self.assertEqual(len(latest), 8)
        self.assertEqual(latest[0]["review_date"], "2026-10-21")
        self.assertEqual(latest[0]["verified_purchase"], "True")
        new_csvs = list(Path(s.exports_dir).glob("new_reviews_*.csv"))
        self.assertEqual(len(self.csv_rows(new_csvs[0])), 8)

        for f in new_csvs:
            f.unlink()
        self.assertEqual(runner.run(s, ["B000TEST01"]), 0)
        with ReviewStore(s.db_path) as store:
            last = store.conn.execute("SELECT pages_scraped, new_reviews, stop_reason FROM runs ORDER BY rowid DESC").fetchone()
        self.assertEqual(last, (1, 0, "reached already-stored review"))
        self.assertEqual(len(self.csv_rows(s.latest_csv)), 8)  # no duplicates

    def test_since_and_max_pages(self):
        from datetime import date

        s = self.settings(since=date(2026, 10, 10))
        runner.run(s, ["B000TEST01"])
        self.assertEqual(self.stats(s)["B000TEST01"], ("ok", 1, 3, "reached review older than --since 2026-10-10"))

        s2 = self.settings(db_path=str(self.tmp / "b.db"), max_pages=1)
        runner.run(s2, ["B000TEST01"])
        self.assertEqual(self.stats(s2)["B000TEST01"], ("ok", 1, 5, "max pages (1)"))

    def test_blocks_stop_the_asin_and_others_continue(self):
        s = self.settings()
        code = runner.run(s, ["B000CAPTCH", "B000LOGIN1", "B000TEST01"])
        self.assertEqual(code, 2)
        st = self.stats(s)
        self.assertEqual(st["B000CAPTCH"][:3], ("blocked:captcha", 0, 0))
        self.assertEqual(st["B000LOGIN1"][:3], ("blocked:login", 0, 0))
        self.assertEqual(st["B000TEST01"][0], "ok")
        # CAPTCHA: stopped at the first page, no second URL template tried.
        self.assertEqual(sum(v for k, v in FakeAmazon.hits.items() if "B000CAPTCH" in k), 1)

    def test_retries_5xx(self):
        s = self.settings()
        self.assertEqual(runner.run(s, ["B000FLAKY1"]), 0)
        self.assertEqual(FakeAmazon.hits["/product-reviews/B000FLAKY1/?pageNumber=1"], 2)  # 503, then OK
        self.assertEqual(self.stats(s)["B000FLAKY1"], ("ok", 2, 8, "last page"))


if __name__ == "__main__":
    unittest.main()
