"""
Telegram Bot API sender — GET-only with aggressive chunking.

CRITICAL FOR HF SPACES:
  - HF Spaces blocks api.telegram.org at TLS/SNI level → use Cloudflare Worker proxy
  - HF Spaces can GET to CF Worker but NOT POST → all calls use GET with query params
  - URL length through proxies/CF is limited (~2000 chars safe) → MUST chunk text
  - Markdown escaping bloats URLs → use HTML parse_mode (more compact)

  send_article flow (recommended by frontier model analysis):
    1. sendPhoto with image URL + SHORT caption (title + link, ≤900 chars)
    2. sendMessage with chunked summary (each chunk ≤1500 chars)
"""
from __future__ import annotations

import time
import urllib.parse

import httpx

from config import cfg
from utils import logger


# ── Conservative limits for GET + URL encoding through CF Worker ────────────
MAX_MSG_CHARS = 1500       # per sendMessage chunk (Telegram allows 4096, but URL length is the constraint)
MAX_CAPTION_CHARS = 900    # per sendPhoto caption (Telegram allows 1024, but URL length is the constraint)
MAX_URL_LEN = 2000         # total URL length safety limit

_HTTP_TIMEOUT = httpx.Timeout(
    connect=15.0,
    read=45.0,
    write=15.0,
    pool=15.0,
)
# CRITICAL: disable keepalive — Cloudflare Workers close connections after each
# response, and httpx's connection pool tries to reuse them → SSL EOF on 2nd request.
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
            http2=False,  # important with CF from some clouds
            headers={"User-Agent": "TheDeepViewBot/2.0"},
            follow_redirects=True,
        )
    return _client


def _reset_client() -> None:
    """Close and recreate the HTTP client to clear dead pooled connections."""
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
    _client = None


def _api_url(method: str, params: dict | None = None) -> str:
    """Build a Telegram Bot API URL with GET query parameters.

    SECURITY: never log the full URL (contains bot token).
    """
    base = cfg.telegram_api_base.rstrip("/")
    url = f"{base}/bot{cfg.telegram_bot_token}/{method}"
    if params:
        clean = {k: str(v) for k, v in params.items() if v is not None}
        url += "?" + urllib.parse.urlencode(clean, quote_via=urllib.parse.quote)
    return url


def _get_json(url: str, retries: int = 3, method_name: str = "API") -> dict:
    """Make a GET request with retry logic. Never logs the full URL (token security)."""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            if len(url) > MAX_URL_LEN:
                logger.warning(
                    f"Telegram {method_name}: URL length {len(url)} exceeds {MAX_URL_LEN} — may fail"
                )
            client = _get_client()
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
            # Telegram returns {"ok": false, "error_code": ..., "description": ...} on errors
            if isinstance(data, dict) and data.get("ok") is False:
                logger.warning(
                    f"Telegram {method_name}: API error {data.get('error_code')}: "
                    f"{data.get('description', '')[:200]}"
                )
            return data
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError,
                httpx.HTTPStatusError) as e:
            last_err = e
            logger.warning(
                f"Telegram {method_name}: attempt {attempt}/{retries} failed "
                f"({type(e).__name__}), retrying..."
            )
            _reset_client()
            time.sleep(min(2 ** attempt, 8))
        except Exception as e:
            last_err = e
            logger.warning(
                f"Telegram {method_name}: attempt {attempt}/{retries} failed "
                f"({type(e).__name__}: {str(e)[:150]}), retrying..."
            )
            _reset_client()
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Telegram {method_name} failed after {retries} attempts: {last_err}")


# ── Text chunking ───────────────────────────────────────────────────────────


def _chunk_text(text: str, limit: int = MAX_MSG_CHARS) -> list[str]:
    """Split text into chunks of at most `limit` chars, breaking at newlines/spaces."""
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Try to break at a newline first
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            # Fall back to space
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            # Hard cut
            cut = limit
        part = text[:cut].strip()
        if part:
            chunks.append(part)
        text = text[cut:].strip()
    return chunks


# ── Telegram API methods (all use GET, all chunked) ─────────────────────────


def _send_text(chat_id: str, text: str, parse_mode: str | None = "HTML") -> list[dict]:
    """Send text via GET, chunking if needed. Returns list of API responses."""
    results = []
    for part in _chunk_text(text, MAX_MSG_CHARS):
        params = {
            "chat_id": chat_id,
            "text": part,
            "disable_web_page_preview": "true",
        }
        if parse_mode:
            params["parse_mode"] = parse_mode

        url = _api_url("sendMessage", params)

        # If URL is too long, strip parse_mode and hard-trim text
        if len(url) > MAX_URL_LEN:
            params.pop("parse_mode", None)
            params["text"] = part[:1200]
            url = _api_url("sendMessage", params)

        results.append(_get_json(url, method_name="sendMessage"))
        time.sleep(0.35)  # gentle rate limit between chunks
    return results


def _send_photo_by_url(chat_id: str, image_url: str, caption: str,
                       parse_mode: str | None = "HTML") -> dict:
    """Send a photo by URL via GET. Caption is kept SHORT (≤900 chars)."""
    cap = (caption or "").strip()
    if len(cap) > MAX_CAPTION_CHARS:
        cap = cap[: MAX_CAPTION_CHARS - 1] + "…"

    params = {
        "chat_id": chat_id,
        "photo": image_url,
        "caption": cap,
    }
    if parse_mode:
        params["parse_mode"] = parse_mode

    url = _api_url("sendPhoto", params)
    if len(url) > MAX_URL_LEN:
        # Strip parse_mode and trim caption further
        params.pop("parse_mode", None)
        params["caption"] = cap[:600]
        url = _api_url("sendPhoto", params)

    return _get_json(url, method_name="sendPhoto")


# ── Category emoji for visual scan in Telegram ──────────────────────────────
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


def _build_short_caption(article: dict, category: str | None) -> str:
    """Build a SHORT caption for sendPhoto (HTML format, ≤900 chars).

    Only includes: emoji + bold title + link. Long summary goes in follow-up
    sendMessage calls.
    """
    title = article.get("title", "Untitled")
    url = article.get("url", "")
    emoji = _CATEGORY_EMOJI.get(category or "", "📰")

    # HTML format (more compact than Markdown for URL encoding)
    if url:
        return f"{emoji} <b>{title}</b>\n\n🔗 <a href=\"{url}\">Read original</a>"
    return f"{emoji} <b>{title}</b>"


def _build_full_header(article: dict, category: str | None) -> str:
    """Build the full header (used when no image, or as first summary chunk)."""
    title = article.get("title", "Untitled")
    author = article.get("author", "Unknown")
    published = article.get("published_at", "")
    date_str = published[:10] if published else "unknown date"
    url = article.get("url", "")
    source = article.get("source") or ""
    emoji = _CATEGORY_EMOJI.get(category or "", "📰")
    cat_label = (category or "article").replace("_", " ").title()

    source_line = f"📡 Source: {source}\n" if source else ""
    header = (
        f"{emoji} <b>{title}</b>\n\n"
        f"<i>{author} · {date_str}</i>\n"
        f"{source_line}"
        f"📂 Category: {cat_label}\n"
    )
    if url:
        header += f'🔗 <a href="{url}">Read original</a>'
    return header


def send_article(article: dict, summary: str | None, category: str | None = None) -> bool:
    """
    Send one article to Telegram using the recommended flow:
      1. sendPhoto with image URL + SHORT caption (title + link)
      2. sendMessage with full header + chunked summary

    If no image, sends header as text first, then summary chunks.

    Returns True on success.
    """
    image_url = article.get("image_url")
    short_caption = _build_short_caption(article, category)
    full_header = _build_full_header(article, category)

    try:
        # Step 1: Send photo with short caption (or header text if no image)
        if image_url:
            try:
                _send_photo_by_url(cfg.telegram_chat_id, image_url, short_caption, parse_mode="HTML")
                logger.info(f"Telegram: sent photo for {article.get('url')}")
            except Exception as e:
                logger.warning(f"Telegram: sendPhoto failed, falling back to text-only: {e}")
                _send_text(cfg.telegram_chat_id, full_header, parse_mode="HTML")
        else:
            _send_text(cfg.telegram_chat_id, full_header, parse_mode="HTML")
            logger.info(f"Telegram: sent header (no image) for {article.get('url')}")

        # Step 2: Send full summary in chunks (if available)
        if summary:
            # Prepend header to first chunk if no image was sent
            body = summary
            if not image_url:
                # Header already sent as text above, just send summary
                pass
            else:
                # Photo sent with short caption; send full header + summary as text
                body = full_header + "\n\n" + summary

            for chunk in _chunk_text(body, MAX_MSG_CHARS):
                _send_text(cfg.telegram_chat_id, chunk, parse_mode="HTML")
            logger.info(
                f"Telegram: sent summary ({len(summary)} chars) for {article.get('url')}"
            )
        return True

    except Exception as e:
        logger.error(f"Telegram send failed for {article.get('url')}: {e}")
        return False


def send_heartbeat(status: dict) -> bool:
    msg = (
        "🤖 <b>TheDeepView Bot — Heartbeat</b>\n\n"
        f"Last run: {status.get('last_run', 'never')}\n"
        f"New articles today: {status.get('new_today', 0)}\n"
        f"Total articles tracked: {status.get('total_tracked', 0)}\n"
        f"Gemini calls today: {status.get('gemini_calls_today', 0)}/{status.get('gemini_limit', 20)}\n"
        f"Errors today: {status.get('errors_today', 0)}"
    )
    try:
        _send_text(cfg.telegram_chat_id, msg, parse_mode="HTML")
        return True
    except Exception as e:
        logger.error(f"Heartbeat send failed: {e}")
        return False
