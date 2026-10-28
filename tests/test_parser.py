"""Parser tests against saved HTML. If Amazon changes markup, these fail first.

Run:  python -m unittest discover -s tests -v     (or: pytest)
"""

import unittest
from pathlib import Path

from amazon_reviews import parser

FIXTURES = Path(__file__).parent / "fixtures"
ASIN = "B000TEST01"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class ParseSamplePageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reviews = parser.parse_reviews(load("reviews_page_sample.html"), ASIN)
        cls.by_id = {r.review_id: r for r in cls.reviews}

    def test_finds_all_reviews_deduped_in_order(self):
        self.assertEqual(
            [r.review_id for r in self.reviews],
            ["R1TESTAAAA0001", "R2TESTBBBB0002", "R3TESTCCCC0003", "R4TESTDDDD0004", "R5TESTEEEE0005"],
        )

    def test_every_review_has_core_fields(self):
        # The canary: if a selector breaks, one of these goes empty.
        for r in self.reviews:
            with self.subTest(review=r.review_id):
                self.assertIsInstance(r.rating, float)
                self.assertTrue(r.title)
                self.assertTrue(r.body)
                self.assertRegex(r.review_date or "", r"^\d{4}-\d{2}-\d{2}$")
                self.assertTrue(r.reviewer_name)
                self.assertTrue(r.country)

    def test_classic_review(self):
        r = self.by_id["R1TESTAAAA0001"]
        self.assertEqual(r.asin, ASIN)
        self.assertEqual(r.rating, 4.0)
        self.assertEqual(r.title, "Sturdy boxes, lid is a bit tight")
        self.assertEqual(r.body, "Good thick cardboard and the handles are strong.\nThe lid needs a firm push to close.")
        self.assertEqual(r.review_date, "2026-10-21")
        self.assertEqual(r.country, "India")
        self.assertEqual(r.reviewer_name, "Asha R.")
        self.assertTrue(r.verified_purchase)
        self.assertEqual(r.helpful_votes, 1)
        self.assertEqual(r.variant, "Colour: Beige | Size: Large (Pack of 3)")
        self.assertEqual(r.color, "Beige")
        self.assertEqual(r.size, "Large (Pack of 3)")
        self.assertTrue(r.has_images)
        self.assertFalse(r.has_videos)
        self.assertEqual(r.review_url, "https://www.amazon.in/gp/customer-reviews/R1TESTAAAA0001/?ASIN=B000TEST01")

    def test_newer_layout_title_without_star_text(self):
        r = self.by_id["R2TESTBBBB0002"]
        self.assertEqual(r.rating, 2.0)
        self.assertEqual(r.title, "Arrived crushed")
        self.assertEqual(r.body, "Two of the three boxes came dented. Packaging was too thin.")
        self.assertFalse(r.verified_purchase)
        self.assertEqual(r.helpful_votes, 12)
        self.assertTrue(r.has_videos)
        self.assertFalse(r.has_images)

    def test_review_from_other_country(self):
        r = self.by_id["R3TESTCCCC0003"]
        self.assertEqual(r.rating, 5.0)
        self.assertEqual(r.title, "Perfect for my closet")
        self.assertEqual(r.country, "United States")
        self.assertEqual(r.review_date, "2026-10-15")
        self.assertEqual(r.color, "Grey")
        self.assertTrue(r.verified_purchase)  # linkless badge
        self.assertEqual(r.helpful_votes, 0)

    def test_unicode_and_read_more_stripped(self):
        r = self.by_id["R4TESTDDDD0004"]
        self.assertEqual(r.title, "बहुत बढ़िया")
        self.assertEqual(r.body, "Worth ₹499. बहुत अच्छा सामान है।")
        self.assertEqual(r.helpful_votes, 1024)

    def test_minimal_review_defaults(self):
        r = self.by_id["R5TESTEEEE0005"]
        self.assertEqual((r.rating, r.helpful_votes, r.variant, r.verified_purchase), (3.0, 0, "", False))

    def test_page_state(self):
        html = load("reviews_page_sample.html")
        self.assertIsNone(parser.detect_block(html))
        self.assertTrue(parser.is_signed_in(html))
        self.assertEqual(parser.next_navigation(html, 5), "url")
        self.assertIsNone(parser.next_navigation(load("reviews_page2_sample.html"), 3))


class BlockDetectionTest(unittest.TestCase):
    def test_captcha(self):
        self.assertEqual(parser.detect_block(load("captcha_sample.html")), "captcha")
        self.assertEqual(parser.detect_block("<html></html>", "https://www.amazon.in/errors/validateCaptcha"), "captcha")

    def test_login_wall(self):
        self.assertEqual(parser.detect_block(load("signin_sample.html")), "login")
        self.assertEqual(parser.detect_block("<html></html>", "https://www.amazon.in/ap/signin?x=1"), "login")

    def test_review_text_mentioning_robot_is_not_a_captcha(self):
        html = load("reviews_page_sample.html").replace("Does the job.", "Sorry, we just need to make sure you're not a robot")
        self.assertIsNone(parser.detect_block(html))


class HelperTest(unittest.TestCase):
    def test_dates(self):
        cases = {
            "Reviewed in India on 5 October 2024": ("2024-10-05", "India"),
            "Reviewed in the United Kingdom on 1 Jan 2025": ("2025-01-01", "United Kingdom"),
            "Reviewed in the United States on March 3, 2025": ("2025-03-03", "United States"),
            "Reviewed in India on 31 February 2025": (None, "India"),
            "": (None, ""),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parser.parse_review_date(text), expected)

    def test_helpful_votes(self):
        for text, n in {"One person found this helpful": 1, "2 people found this helpful": 2,
                        "1,234 people found this helpful": 1234, "": 0, "Helpful": 0}.items():
            with self.subTest(text=text):
                self.assertEqual(parser.parse_helpful_votes(text), n)


if __name__ == "__main__":
    unittest.main()
