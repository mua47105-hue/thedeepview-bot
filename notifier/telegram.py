"""
Telegram Bot API sender — uses requests library (urllib3 backend).

httpx and urllib both fail with SSL EOF when connecting to Cloudflare Workers
from HF Spaces. The `requests` library uses urllib3 which has a different SSL
implementation that may work better.

Uses GET for all API calls (POST also fails from HF Spaces).
"""
from __future__ import annotations

import logging

import requests

from config import cfg
from utils import logger

_REQUEST_TIMEOUT = 30


def _api_url(method: str, params: dict | None = None) -> str:
    """Build a Telegram Bot API URL with GET query parameters."""
    base = cfg.telegram_api_base.rstrip("/")
    url = f"{base}/bot{cfg.telegram_bot_token}/{method}"
    if params:
        clean = {k: str(v) for k, v in params.items() if v is not None}
        from urllib.parse import urlencode
        url += "?" + urlencode(clean)
    return url


def _telegram_get(method: str, params: dict | None = None) -> dict:
    """Make a GET request to the Telegram Bot API via requests library."""
    url = _api_url(method, params)
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT, headers={"User-Agent": "TheDeepViewBot/2.0"})
        return resp.json()
    except requests.exceptions.HTTPError as e:
        logger.error(f"Telegram API {method} HTTP error: {e}")
        raise
    except Exception as e:
        logger.error(f"Telegram API {method} failed: {type(e).__name__}: {e}")
        raise


def _send_text(chat_id: str, text: str, parse_mode: str = "Markdown") -> dict:
    """Send a text message via GET (requests)."""
    try:
        return _telegram_get("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "false",
        })
    except Exception:
        if parse_mode:
            logger.warning("sendMessage failed with parse_mode, retrying as plain text")
            return _telegram_get("sendMessage", {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": "false",
            })
        raise


def _send_photo_by_url(chat_id: str, image_url: str, caption: str) -> dict:
    """Send a photo by URL via GET (requests)."""
    try:
        return _telegram_get("sendPhoto", {
            "chat_id": chat_id,
            "photo": image_url,
            "caption": caption[:1020] if caption else None,
            "parse_mode": "Markdown" if caption else None,
        })
    except Exception:
        logger.warning("sendPhoto failed with parse_mode, retrying as plain text")
        return _telegram_get("sendPhoto", {
            "chat_id": chat_id,
            "photo": image_url,
            "caption": caption[:1020] if caption else None,
        })


def _chunk_text(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) + 2 <= limit:
            current = (current + "\n\n" + paragraph).strip("\n\n")
        else:
            if current:
                chunks.append(current)
            while len(paragraph) > limit:
                chunks.append(paragraph[:limit])
                paragraph = paragraph[limit:]
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


_CATEGORY_EMOJI = {
    "model_launch": "🚀",
    "infra_upgrade": "🏗️",
    "core_logic": "🧠",
    "functional_update": "⚡",
    "research": "🔬",
    "policy": "⚖️",
    "business": "💼",
    "other": "📰",
}


def _build_caption(article: dict, category: str | None) -> str:
    title = article.get("title", "Untitled")
    author = article.get("author", "Unknown")
    published = article.get("published_at", "")
    date_str = published[:10] if published else "unknown date"
    url = article.get("url", "")
    source = article.get("source") or ""
    emoji = _CATEGORY_EMOJI.get(category or "", "📰")
    cat_label = (category or "article").replace("_", " ").title()

    source_line = f"📡 Source: {source}\n" if source else ""
    return (
        f"{emoji} *{title}*\n\n"
        f"_{author} · {date_str}_\n"
        f"{source_line}"
        f"📂 Category: {cat_label}\n"
        f"🔗 [Read original]({url})"
    )


def send_article(article: dict, summary: str | None, category: str | None = None) -> bool:
    """Send one article to Telegram. Returns True on success."""
    image_url = article.get("image_url")
    caption = _build_caption(article, category)

    try:
        if image_url:
            _send_photo_by_url(cfg.telegram_chat_id, image_url, caption)
            logger.info(f"Telegram: sent photo for {article.get('url')}")
        else:
            _send_text(cfg.telegram_chat_id, caption)
            logger.info(f"Telegram: sent header (no image) for {article.get('url')}")

        if summary:
            for chunk in _chunk_text(summary, limit=4000):
                _send_text(cfg.telegram_chat_id, chunk)
            logger.info(
                f"Telegram: sent summary ({len(summary)} chars) for {article.get('url')}"
            )
        return True
    except Exception as e:
        logger.error(f"Telegram send failed for {article.get('url')}: {e}")
        return False


def send_heartbeat(status: dict) -> bool:
    msg = (
        "🤖 *TheDeepView Bot — Heartbeat*\n\n"
        f"Last run: {status.get('last_run', 'never')}\n"
        f"New articles today: {status.get('new_today', 0)}\n"
        f"Total articles tracked: {status.get('total_tracked', 0)}\n"
        f"Gemini calls today: {status.get('gemini_calls_today', 0)}/{status.get('gemini_limit', 20)}\n"
        f"Errors today: {status.get('errors_today', 0)}"
    )
    try:
        _send_text(cfg.telegram_chat_id, msg)
        return True
    except Exception as e:
        logger.error(f"Heartbeat send failed: {e}")
        return False
