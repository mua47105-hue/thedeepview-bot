"""Discover article URLs from multiple sources (RSS + sitemap)."""
from __future__ import annotations

import re
from dataclasses import dataclass

import feedparser

from config import cfg
from utils import http_client, logger


@dataclass
class ArticleRef:
    url: str
    lastmod: str | None = None
    source: str = ""  # which source it came from (for logging / Telegram caption)


# ── Sitemap parser (XML) ─────────────────────────────────────────────────────


def _discover_from_sitemap(feed_url: str, url_pattern: str, source_name: str) -> list[ArticleRef]:
    """Fetch sitemap.xml and return all URLs matching `url_pattern`."""
    with http_client() as client:
        resp = client.get(feed_url)
        resp.raise_for_status()
    body = resp.text

    # Try standard <url><loc>...</loc>[<lastmod>...</lastmod>]</url> pairs first
    url_blocks = re.findall(
        r"<url>\s*(?:<loc>([^<]+)</loc>\s*(?:<lastmod>([^<]+)</lastmod>)?)",
        body,
    )
    if url_blocks:
        refs = [
            ArticleRef(url=u, lastmod=lm if lm else None, source=source_name)
            for u, lm in url_blocks
            if url_pattern in u
        ]
        return refs

    # Fallback: parse href links (some sites wrap sitemap.xml in HTML)
    urls = re.findall(r'href="(https?://[^"]+)"', body)
    return [
        ArticleRef(url=u, source=source_name)
        for u in urls
        if url_pattern in u
    ]


# ── RSS / Atom parser ────────────────────────────────────────────────────────


def _discover_from_rss(feed_url: str, url_pattern: str, source_name: str) -> list[ArticleRef]:
    """Fetch RSS/Atom feed and return all entry URLs matching `url_pattern`.

    Uses feedparser which handles RSS 2.0, RSS 1.0, Atom, and edge cases.
    We fetch the raw bytes ourselves (so we control UA + timeout via httpx),
    then pass the body to feedparser.
    """
    with http_client() as client:
        resp = client.get(feed_url)
        resp.raise_for_status()

    # feedparser can parse from bytes; we don't need to decode ourselves
    parsed = feedparser.parse(resp.content)

    refs: list[ArticleRef] = []
    for entry in parsed.entries:
        # `link` is the canonical URL for RSS entries
        link = getattr(entry, "link", "") or ""
        if not link or url_pattern not in link:
            continue
        # published_parsed -> ISO string if available
        published = ""
        if hasattr(entry, "published") and entry.published:
            published = entry.published
        elif hasattr(entry, "updated") and entry.updated:
            published = entry.updated
        refs.append(ArticleRef(url=link, lastmod=published or None, source=source_name))

    return refs


# ── Top-level orchestrator ───────────────────────────────────────────────────


def discover_from_source(name: str, kind: str, feed_url: str, url_pattern: str) -> list[ArticleRef]:
    """Try one source. Returns [] on failure (already logged)."""
    try:
        if kind == "sitemap":
            refs = _discover_from_sitemap(feed_url, url_pattern, name)
        elif kind == "rss":
            refs = _discover_from_rss(feed_url, url_pattern, name)
        else:
            logger.warning(f"Unknown source kind '{kind}' for {name}; skipping")
            return []
        logger.info(f"[{name}] discovered {len(refs)} article URLs")
        return refs
    except Exception as e:
        logger.warning(f"[{name}] discovery failed: {e}")
        return []


def discover_all() -> list[ArticleRef]:
    """Iterate over all configured sources and return a deduplicated, combined list."""
    all_refs: list[ArticleRef] = []
    seen_urls: set[str] = set()
    for name, kind, feed_url, url_pattern in cfg.sources:
        refs = discover_from_source(name, kind, feed_url, url_pattern)
        for r in refs:
            if r.url not in seen_urls:
                seen_urls.add(r.url)
                all_refs.append(r)
    logger.info(
        f"Total: {len(all_refs)} unique article URLs across {len(cfg.sources)} sources"
    )
    return all_refs
