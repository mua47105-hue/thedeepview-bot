"""
Telegram Bot API sender — supports sendPhoto (with image bytes) + sendMessage.
For each article: sends photo with short caption first, then sendMessage with full summary.

On Render (or any normal host), we connect directly to api.telegram.org — no proxy needed.
The TELEGRAM_API_BASE env var is still supported for exotic setups, but defaults to the
official endpoint.
"""
from __future__ import annotations

import time

import httpx

from config import cfg
from utils import logger


# ── HTTP client configuration ────────────────────────────────────────────────
_HTTP_TIMEOUT = httpx.Timeout(
    connect=15.0,
    read=60.0,   # generous for sendPhoto with large images
    write=30.0,
    pool=15.0,
)

# Persistent client for connection reuse
_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    """Get the persistent HTTP client (creates it on first call)."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.Client(
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "TheDeepViewBot/2.0"},
        )
    return _client


def _recreate_client():
    """Close and recreate the HTTP client to clear dead pooled connections."""
    global _client
    try:
        if _client and not _client.is_closed:
            _client.close()
    except Exception:
        pass
    _client = None


def _api_url(method: str) -> str:
    """Build a Telegram Bot API URL."""
    base = cfg.telegram_api_base.rstrip("/")
    return f"{base}/bot{cfg.telegram_bot_token}/{method}"


def _request_with_retry(method: str, url: str, **kwargs) -> httpx.Response:
    """Make an HTTP request with retry logic.

    Retries up to 3 times with short backoff (1s, 3s) on connection errors.
    """
    max_attempts = 3
    backoff_seconds = [1, 3]

    for attempt in range(1, max_attempts + 1):
        try:
            client = _get_client()
            resp = client.post(url, **kwargs)
            return resp
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            if attempt == max_attempts:
                logger.error(
                    f"Telegram API {method} failed after {max_attempts} attempts: "
                    f"{type(e).__name__}: {e}"
                )
                raise
            wait = backoff_seconds[attempt - 1]
            logger.warning(
                f"Telegram API {method} attempt {attempt}/{max_attempts} failed "
                f"({type(e).__name__}: {e}), retrying in {wait}s..."
            )
            _recreate_client()
            time.sleep(wait)
        except Exception as e:
            logger.error(f"Telegram API {method} failed (unexpected error): {e}")
            raise
    raise RuntimeError("unreachable")  # type: ignore


# ── Telegram API methods ────────────────────────────────────────────────────


def _send_text(chat_id: str, text: str, parse_mode: str = "Markdown") -> dict:
    url = _api_url("sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    }
    resp = _request_with_retry("sendMessage", url, json=payload)
    if resp.status_code != 200:
        if resp.status_code == 400:
            logger.warning(f"Markdown parse failed, retrying as plain text: {resp.text[:200]}")
            payload.pop("parse_mode")
            resp = _request_with_retry("sendMessage", url, json=payload)
        resp.raise_for_status()
    return resp.json()


def _send_photo(chat_id: str, image_bytes: bytes, caption: str) -> dict:
    """Send a photo via multipart upload. Caption is capped at 1024 chars by Telegram."""
    url = _api_url("sendPhoto")
    caption = (caption or "")[:1020]
    files = {"photo": ("image.jpg", image_bytes, "image/jpeg")}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "Markdown"
    resp = _request_with_retry("sendPhoto", url, data=data, files=files)
    if resp.status_code != 200:
        if resp.status_code == 400:
            logger.warning(f"Photo send failed (400): {resp.text[:300]}")
            data.pop("parse_mode", None)
            resp = _request_with_retry("sendPhoto", url, data=data, files=files)
        resp.raise_for_status()
    return resp.json()


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


# Category emoji for visual scan in Telegram
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
    """
    Send one article to Telegram.

    If summary is None or status is SKIP, only send the photo+caption (no follow-up text).

    Returns True on success.
    """
    image_bytes = article.get("image_bytes")
    image_url = article.get("image_url")
    caption = _build_caption(article, category)

    try:
        if image_bytes:
            _send_photo(cfg.telegram_chat_id, image_bytes, caption)
            logger.info(f"Telegram: sent photo for {article.get('url')}")
        else:
            # No image — send header as text
            header = caption
            if image_url:
                header = f"🖼 [View image]({image_url})\n\n" + header
            _send_text(cfg.telegram_chat_id, header)
            logger.info(f"Telegram: sent header (no image) for {article.get('url')}")

        # Send full summary if available
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
