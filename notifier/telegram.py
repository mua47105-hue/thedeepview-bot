"""
Telegram Bot API sender — uses GET requests for all API calls.

CRITICAL FOR HF SPACES:
  Hugging Face Spaces blocks api.telegram.org at the TLS/SNI level.
  We route through a Cloudflare Worker proxy (TELEGRAM_API_BASE env var).
  
  However, HF Spaces can only do GET requests to Cloudflare Workers —
  POST requests fail with SSL EOF. So we convert ALL Telegram API calls
  to GET with query parameters. The Telegram Bot API supports GET for
  all methods.

  For sendPhoto: instead of uploading image bytes (which requires POST
  multipart), we pass the image URL directly. Telegram downloads the
  image from the URL itself.
"""
from __future__ import annotations

import time
import urllib.parse

import httpx

from config import cfg
from utils import logger


# ── HTTP client configuration ────────────────────────────────────────────────
_HTTP_TIMEOUT = httpx.Timeout(
    connect=15.0,
    read=30.0,
    write=15.0,
    pool=15.0,
)
_HTTP_LIMITS = httpx.Limits(
    max_keepalive_connections=0,
    max_connections=10,
    keepalive_expiry=0.0,
)

_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    """Get the persistent HTTP client (creates it on first call)."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.Client(
            timeout=_HTTP_TIMEOUT,
            limits=_HTTP_LIMITS,
            headers={"User-Agent": "TheDeepViewBot/2.0"},
        )
    return _client


def _api_url(method: str, params: dict | None = None) -> str:
    """Build a Telegram Bot API URL with GET query parameters.

    Uses GET for ALL API calls because HF Spaces can't POST to Cloudflare
    Workers (SSL EOF). The Telegram Bot API supports GET for everything.
    """
    base = cfg.telegram_api_base.rstrip("/")
    url = f"{base}/bot{cfg.telegram_bot_token}/{method}"
    if params:
        # Filter out None values and convert all to strings
        clean = {k: str(v) for k, v in params.items() if v is not None}
        url += "?" + urllib.parse.urlencode(clean)
    return url


def _get_with_retry(method: str, url: str) -> dict:
    """Make a GET request with retry logic.

    Retries up to 3 times with short backoff (1s, 3s) on connection errors.
    """
    max_attempts = 3
    backoff_seconds = [1, 3]

    for attempt in range(1, max_attempts + 1):
        try:
            client = _get_client()
            resp = client.get(url)
            return resp.json()
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
    return {}  # unreachable but keeps type checker happy


def _recreate_client():
    """Close and recreate the HTTP client to clear dead pooled connections."""
    global _client
    try:
        if _client and not _client.is_closed:
            _client.close()
    except Exception:
        pass
    _client = None


# ── Telegram API methods (all use GET) ──────────────────────────────────────


def _send_text(chat_id: str, text: str, parse_mode: str = "Markdown") -> dict:
    """Send a text message via GET request."""
    url = _api_url("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": "false",
    })
    try:
        result = _get_with_retry("sendMessage", url)
    except Exception:
        # If Markdown fails, retry as plain text
        if parse_mode:
            logger.warning("sendMessage failed with parse_mode, retrying as plain text")
            url = _api_url("sendMessage", {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": "false",
            })
            result = _get_with_retry("sendMessage", url)
        else:
            raise
    return result


def _send_photo_by_url(chat_id: str, image_url: str, caption: str) -> dict:
    """Send a photo by passing its URL to Telegram.

    Telegram downloads the image directly from the URL — we don't need to
    upload bytes. This works with GET requests (no multipart upload needed).
    """
    url = _api_url("sendPhoto", {
        "chat_id": chat_id,
        "photo": image_url,
        "caption": caption[:1020] if caption else None,
        "parse_mode": "Markdown" if caption else None,
    })
    try:
        return _get_with_retry("sendPhoto", url)
    except Exception:
        # Retry without parse_mode
        logger.warning("sendPhoto failed with parse_mode, retrying as plain text")
        url = _api_url("sendPhoto", {
            "chat_id": chat_id,
            "photo": image_url,
            "caption": caption[:1020] if caption else None,
        })
        return _get_with_retry("sendPhoto", url)


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

    Uses sendPhoto with the image URL (GET) instead of uploading bytes (POST).
    This is required because HF Spaces can't POST to Cloudflare Workers.

    Returns True on success.
    """
    image_url = article.get("image_url")
    caption = _build_caption(article, category)

    try:
        if image_url:
            # Send photo by URL — Telegram downloads it directly
            _send_photo_by_url(cfg.telegram_chat_id, image_url, caption)
            logger.info(f"Telegram: sent photo for {article.get('url')}")
        else:
            # No image — send header as text
            _send_text(cfg.telegram_chat_id, caption)
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
