"""Fetch a single article page and extract structured metadata + body text + image bytes."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from config import cfg
from utils import html_to_text, http_client, logger


@dataclass
class Article:
    url: str
    title: str
    description: str
    author: str
    published_at: str
    modified_at: str | None
    image_url: str | None
    image_bytes: bytes | None
    image_content_type: str | None
    body_text: str
    raw_html: str
    source: str = ""


_JSONLD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>([\s\S]*?)</script>',
    re.I,
)


def _extract_jsonld(html: str) -> dict | None:
    matches = _JSONLD_RE.findall(html)
    for raw in matches:
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "NewsArticle":
                return item
    return None


def _extract_meta(html: str, prop: str) -> str | None:
    m = re.search(
        rf'<meta\s+(?:property|name)=["\']({re.escape(prop)})["\'][^>]*content=["\']([^"\']+)["\']',
        html,
        re.I,
    )
    if not m:
        m = re.search(
            rf'<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']({re.escape(prop)})["\']',
            html,
            re.I,
        )
        if m:
            return m.group(1)
        return None
    return m.group(2)


def _download_image(image_url: str) -> tuple[bytes | None, str | None]:
    """Download hero image. Returns (bytes, content_type) or (None, None) on failure."""
    if not image_url:
        return None, None
    try:
        with http_client() as client:
            img_resp = client.get(
                image_url,
                headers={"Accept": "image/png, image/jpeg, image/webp, image/*;q=0.8"},
            )
            img_resp.raise_for_status()
        ctype = img_resp.headers.get("content-type", "").split(";")[0].strip().lower()
        # Telegram sendPhoto supports: jpg, png, webp (NOT svg, NOT gif-as-photo)
        if ctype not in ("image/jpeg", "image/jpg", "image/png", "image/webp"):
            logger.warning(f"Unsupported image content-type '{ctype}' for {image_url}")
            return None, None
        # Telegram photo limit is 10 MB; we cap at 5 MB for safety
        if len(img_resp.content) > cfg.telegram_image_max_bytes:
            logger.warning(
                f"Image too large ({len(img_resp.content)} bytes) for {image_url}"
            )
            return None, None
        if len(img_resp.content) == 0:
            return None, None
        return img_resp.content, ctype
    except Exception as e:
        logger.warning(f"Image download failed for {image_url}: {e}")
        return None, None


def fetch_article(url: str, fetch_image: bool = True, source: str = "") -> Article | None:
    """Fetch one article, extract metadata, return structured Article or None on failure."""
    try:
        with http_client() as client:
            resp = client.get(url)
            resp.raise_for_status()
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return None

    html = resp.text
    jsonld = _extract_jsonld(html) or {}

    title = (
        jsonld.get("headline")
        or _extract_meta(html, "og:title")
        or _extract_meta(html, "twitter:title")
        or "Untitled"
    )
    description = (
        jsonld.get("description")
        or _extract_meta(html, "og:description")
        or _extract_meta(html, "description")
        or ""
    )
    author = ""
    if isinstance(jsonld.get("author"), list) and jsonld["author"]:
        author = jsonld["author"][0].get("name", "")
    elif isinstance(jsonld.get("author"), dict):
        author = jsonld["author"].get("name", "")
    if not author:
        author = _extract_meta(html, "article:author") or "Unknown"

    published_at = (
        jsonld.get("datePublished")
        or _extract_meta(html, "article:published_time")
        or ""
    )
    modified_at = jsonld.get("dateModified") or _extract_meta(html, "article:modified_time")

    image_url = None
    img = jsonld.get("image")
    if isinstance(img, list) and img:
        image_url = img[0]
    elif isinstance(img, str):
        image_url = img
    if not image_url:
        image_url = _extract_meta(html, "og:image")

    body_text = html_to_text(html)
    # Trim boilerplate at the start (nav, sign-in buttons, etc.)
    if title and title in body_text:
        body_text = body_text[body_text.index(title):]
    # Cap input to ~12K chars to keep prompt size reasonable when batching 15 articles
    body_text = body_text[:12000]

    image_bytes, image_content_type = (None, None)
    if fetch_image and image_url:
        image_bytes, image_content_type = _download_image(image_url)

    return Article(
        url=url,
        title=title.strip(),
        description=description.strip(),
        author=author.strip(),
        published_at=published_at,
        modified_at=modified_at,
        image_url=image_url,
        image_bytes=image_bytes,
        image_content_type=image_content_type,
        body_text=body_text,
        raw_html=html,
        source=source,
    )
