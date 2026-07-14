"""Shared utilities: logging, HTML-to-text, polite sleep, HTTP client."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("thedeepview")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def polite_sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


_TAGS_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def html_to_text(html: str) -> str:
    """Strip scripts/styles/tags and normalize whitespace."""
    html = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", html, flags=re.I)
    html = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", html, flags=re.I)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</p>", "\n\n", html, flags=re.I)
    text = _TAGS_RE.sub(" ", html)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
    )
    return _WS_RE.sub(" ", text).strip()


def http_client() -> httpx.Client:
    """Pre-configured httpx client with sane defaults."""
    from config import cfg

    return httpx.Client(
        headers={
            "User-Agent": cfg.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=cfg.request_timeout_seconds,
        follow_redirects=True,
    )
