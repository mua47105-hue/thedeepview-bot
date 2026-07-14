"""Persistent state: seen URLs, article history, Gemini quota counter."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterable

from config import cfg
from utils import logger


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            url           TEXT PRIMARY KEY,
            title         TEXT,
            author        TEXT,
            published_at  TEXT,
            first_seen_at TEXT,
            last_checked  TEXT,
            summary       TEXT,
            category      TEXT,
            lastmod       TEXT,
            source        TEXT
        )
    """)
    # Migration: add `source` column to pre-existing databases (idempotent)
    try:
        conn.execute("ALTER TABLE articles ADD COLUMN source TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at      TEXT,
            finished_at     TEXT,
            discovered      INTEGER,
            new_articles    INTEGER,
            processed       INTEGER,
            telegram_sent   INTEGER,
            errors          INTEGER,
            gemini_calls    INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gemini_quota (
            date         TEXT PRIMARY KEY,
            calls_count  INTEGER DEFAULT 0,
            last_call_at TEXT
        )
    """)
    return conn


# ── seen.json (atomic snapshot) ────────────────────────────────────


def load_seen_urls() -> set[str]:
    if not cfg.seen_path.exists():
        return set()
    try:
        with open(cfg.seen_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("urls", []))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"seen.json corrupt, starting fresh: {e}")
        return set()


def save_seen_urls(urls: set[str]) -> None:
    """Atomically write the seen-URL snapshot to disk."""
    tmp = cfg.seen_path.with_suffix(".json.tmp")
    payload = {
        "urls": sorted(urls),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(urls),
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(cfg.seen_path)


def filter_new(urls: Iterable[str]) -> list[str]:
    seen = load_seen_urls()
    return [u for u in urls if u not in seen]


# ── Article records ────────────────────────────────────────────────


def record_article(
    url: str,
    title: str,
    author: str,
    published_at: str,
    summary: str,
    category: str | None = None,
    lastmod: str | None = None,
    source: str = "",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO articles
                (url, title, author, published_at, first_seen_at, last_checked,
                 summary, category, lastmod, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title=excluded.title,
                author=excluded.author,
                published_at=excluded.published_at,
                last_checked=excluded.last_checked,
                summary=excluded.summary,
                category=excluded.category,
                lastmod=excluded.lastmod,
                source=excluded.source
            """,
            (url, title, author, published_at, now, now, summary, category, lastmod, source),
        )


def record_run(
    started_at: str,
    finished_at: str,
    discovered: int,
    new_articles: int,
    processed: int,
    telegram_sent: int,
    errors: int,
    gemini_calls: int = 0,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO runs
                (started_at, finished_at, discovered, new_articles, processed,
                 telegram_sent, errors, gemini_calls)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (started_at, finished_at, discovered, new_articles, processed,
             telegram_sent, errors, gemini_calls),
        )


# ── Gemini quota tracker ──────────────────────────────────────────


def get_gemini_calls_today() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _connect() as conn:
        row = conn.execute(
            "SELECT calls_count FROM gemini_quota WHERE date = ?", (today,)
        ).fetchone()
        return row[0] if row else 0


def increment_gemini_calls(n: int = 1) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO gemini_quota (date, calls_count, last_call_at)
            VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                calls_count = gemini_quota.calls_count + excluded.calls_count,
                last_call_at = excluded.last_call_at
            """,
            (today, n, now),
        )
        row = conn.execute(
            "SELECT calls_count FROM gemini_quota WHERE date = ?", (today,)
        ).fetchone()
        return row[0] if row else 0


# ── Query helpers for the web UI ───────────────────────────────────


def get_recent_runs(limit: int = 20) -> list[dict]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_articles(limit: int = 20) -> list[dict]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT url, title, author, published_at, first_seen_at, category, source "
            "FROM articles ORDER BY first_seen_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
