"""Discover article URLs from sitemap.xml or /articles listing page."""
from __future__ import annotations

import re
from dataclasses import dataclass

from config import cfg
from utils import http_client, logger


@dataclass
class ArticleRef:
    url: str
    lastmod: str | None = None


def discover_from_sitemap() -> list[ArticleRef]:
    """Fetch sitemap.xml and return all article URLs with lastmod timestamps."""
    with http_client() as client:
        resp = client.get(cfg.sitemap_url)
        resp.raise_for_status()
    body = resp.text

    # Next.js serves sitemap.xml as HTML-wrapped; handle both raw XML and HTML variants.
    # Try <loc>...</loc> pairs first (standard sitemap.xml)
    url_blocks = re.findall(r"<url>\s*(?:<loc>([^<]+)</loc>\s*(?:<lastmod>([^<]+)</lastmod>)?)", body)
    if url_blocks:
        refs = [
            ArticleRef(url=u, lastmod=lm if lm else None)
            for u, lm in url_blocks
            if "/articles/" in u
        ]
    else:
        # Fallback: parse href links in the HTML wrapper
        urls = re.findall(r'href="(https?://[^"]+/articles/[^"]+)"', body)
        refs = [ArticleRef(url=u) for u in urls]

    logger.info(f"Discovered {len(refs)} article URLs from sitemap")
    return refs


def discover_from_listing() -> list[ArticleRef]:
    """Fallback: scrape /articles listing page for article hrefs."""
    with http_client() as client:
        resp = client.get(cfg.articles_listing_url)
        resp.raise_for_status()
    body = resp.text

    slugs = set(re.findall(r'href="/articles/([a-z0-9\-]+)"', body))
    refs = [ArticleRef(url=f"{cfg.site_base}/articles/{s}") for s in slugs]
    logger.info(f"Discovered {len(refs)} article URLs from listing page (fallback)")
    return refs


def discover_all() -> list[ArticleRef]:
    """Try sitemap first, fall back to listing page on failure."""
    try:
        refs = discover_from_sitemap()
        if refs:
            return refs
        logger.warning("Sitemap returned 0 articles; falling back to listing")
    except Exception as e:
        logger.warning(f"Sitemap discovery failed: {e}")
    try:
        return discover_from_listing()
    except Exception as e:
        logger.error(f"Listing discovery also failed: {e}")
        return []
