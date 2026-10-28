"""SQLite storage (source of truth) and CSV exports."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .parser import COLUMNS, Review

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    asin              TEXT NOT NULL,
    review_id         TEXT NOT NULL,
    rating            REAL,
    title             TEXT,
    body              TEXT,
    review_date       TEXT,
    reviewer_name     TEXT,
    verified_purchase INTEGER,
    helpful_votes     INTEGER,
    variant           TEXT,
    size              TEXT,
    color             TEXT,
    has_images        INTEGER,
    has_videos        INTEGER,
    country           TEXT,
    review_url        TEXT,
    scraped_at        TEXT,
    first_seen_at     TEXT NOT NULL,
    UNIQUE (asin, review_id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_asin_date ON reviews (asin, review_date);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT NOT NULL,
    asin          TEXT NOT NULL,
    status        TEXT NOT NULL,
    pages_scraped INTEGER,
    reviews_seen  INTEGER,
    new_reviews   INTEGER,
    stop_reason   TEXT,
    errors        TEXT,
    finished_at   TEXT
);
"""

BOOL_COLUMNS = ("verified_purchase", "has_images", "has_videos")


class ReviewStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> ReviewStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def known_ids(self, asin: str) -> set[str]:
        rows = self.conn.execute("SELECT review_id FROM reviews WHERE asin = ?", (asin,))
        return {r[0] for r in rows}

    def save(self, reviews: list[Review]) -> list[Review]:
        """Insert new reviews; refresh mutable fields (votes, edits) of known ones. Returns the new ones."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        new: list[Review] = []
        with self.conn:
            for r in reviews:
                row = r.to_dict()
                cur = self.conn.execute(
                    f"INSERT OR IGNORE INTO reviews ({', '.join(COLUMNS)}, first_seen_at) "
                    f"VALUES ({', '.join('?' * len(COLUMNS))}, ?)",
                    [row[c] for c in COLUMNS] + [now],
                )
                if cur.rowcount:
                    new.append(r)
                else:
                    self.conn.execute(
                        "UPDATE reviews SET helpful_votes = ?, title = ?, body = ?, scraped_at = ? "
                        "WHERE asin = ? AND review_id = ?",
                        (r.helpful_votes, r.title, r.body, r.scraped_at, r.asin, r.review_id),
                    )
        return new

    def record_run(self, run_id: str, stats: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, stats["asin"], stats["status"], stats["pages_scraped"],
                    stats["reviews_seen"], stats["new_reviews"], stats["stop_reason"],
                    "; ".join(stats["errors"]),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )

    def export_all(self, csv_path: str | Path) -> int:
        df = pd.read_sql_query(
            "SELECT * FROM reviews ORDER BY asin, review_date DESC, first_seen_at DESC", self.conn
        )
        for col in BOOL_COLUMNS:
            df[col] = df[col].astype(bool)
        _write_csv(df, csv_path)
        return len(df)


def export_reviews(reviews: list[Review], csv_path: str | Path) -> int:
    df = pd.DataFrame([r.to_dict() for r in reviews], columns=COLUMNS)
    _write_csv(df, csv_path)
    return len(df)


def _write_csv(df: pd.DataFrame, csv_path: str | Path) -> None:
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel shows ₹ and non-Latin review text correctly.
    df.to_csv(path, index=False, encoding="utf-8-sig")
