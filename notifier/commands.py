"""
Interactive Telegram bot commands via long-polling.

Uses GET for ALL Telegram API calls (POST fails to Cloudflare Workers from
HF Spaces). The long-poll loop uses GET getUpdates which works reliably.
"""
from __future__ import annotations

import threading
import time
import urllib.parse

import httpx

from config import cfg
from scraper.diff import get_gemini_calls_today, get_recent_articles, get_recent_runs
from utils import logger

_TELEGRAM_LONG_POLL_SECONDS = 30
_HTTP_TIMEOUT = httpx.Timeout(
    connect=15.0,
    read=45.0,   # buffer above 30s long-poll
    write=10.0,
    pool=10.0,
)
# Disable keepalive — Cloudflare Workers close connections after each response
_HTTP_LIMITS = httpx.Limits(
    max_keepalive_connections=0,
    max_connections=10,
    keepalive_expiry=0.0,
)

HELP_TEXT = (
    "🤖 *TheDeepView Bot — Commands*\n\n"
    "/start — show this help\n"
    "/help  — show this help\n"
    "/status — last run summary + recent runs\n"
    "/quota  — Gemini API quota used today\n"
    "/latest — last 10 articles tracked\n"
    "/wake   — trigger an immediate pipeline run\n"
)


def _send_text(chat_id: str, text: str, parse_mode: str = "Markdown") -> None:
    """Send a text message via GET request (POST fails to Cloudflare Workers from HF Spaces)."""
    base = cfg.telegram_api_base.rstrip("/")
    params = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    url = f"{base}/bot{cfg.telegram_bot_token}/sendMessage?" + urllib.parse.urlencode(params)
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, limits=_HTTP_LIMITS) as client:
            resp = client.get(url)
        if resp.status_code == 400 and parse_mode:
            # Markdown parse failure — retry as plain text
            params.pop("parse_mode", None)
            url = f"{base}/bot{cfg.telegram_bot_token}/sendMessage?" + urllib.parse.urlencode(params)
            with httpx.Client(timeout=_HTTP_TIMEOUT, limits=_HTTP_LIMITS) as client:
                client.get(url)
    except Exception as e:
        logger.warning(f"command reply failed: {e}")


def _format_status() -> str:
    runs = get_recent_runs(limit=5)
    if not runs:
        return "No pipeline runs recorded yet."
    lines = ["🤖 *Recent Pipeline Runs*\n"]
    for r in runs:
        lines.append(
            f"• `{r.get('started_at', '')[:19]}` — "
            f"new={r.get('new_articles', 0)}, "
            f"sent={r.get('telegram_sent', 0)}, "
            f"errors={r.get('errors', 0)}, "
            f"gemini_calls={r.get('gemini_calls', 0)}"
        )
    return "\n".join(lines)


def _format_quota() -> str:
    today = get_gemini_calls_today()
    remaining = max(0, cfg.gemini_daily_limit - today)
    return (
        f"📊 *Gemini Quota Today*\n\n"
        f"Used: {today} / {cfg.gemini_daily_limit}\n"
        f"Remaining: {remaining}\n"
        f"Safety threshold: {cfg.gemini_safety_threshold}\n"
        f"Status: {'⚠️ THROTTLED' if today >= cfg.gemini_safety_threshold else '✅ OK'}"
    )


def _format_latest() -> str:
    articles = get_recent_articles(limit=10)
    if not articles:
        return "No articles tracked yet."
    lines = ["📰 *Latest 10 Articles*\n"]
    for a in articles:
        title = (a.get("title") or "Untitled")[:80]
        src = a.get("source") or "?"
        cat = (a.get("category") or "—").replace("_", " ")
        first_seen = (a.get("first_seen_at") or "")[:10]
        lines.append(f"• [{src}] *{title}* ({cat}, {first_seen})")
    return "\n".join(lines)


def _handle_update(update: dict) -> None:
    """Route one Telegram update to the appropriate handler."""
    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not text or not chat_id:
        return

    if cfg.telegram_chat_id and chat_id != cfg.telegram_chat_id:
        logger.info(f"Ignoring command from unauthorized chat_id={chat_id}: {text!r}")
        return

    cmd = text.split()[0].lower().split("@")[0]
    if cmd in ("/start", "/help"):
        _send_text(chat_id, HELP_TEXT)
    elif cmd == "/status":
        _send_text(chat_id, _format_status())
    elif cmd == "/quota":
        _send_text(chat_id, _format_quota())
    elif cmd == "/latest":
        _send_text(chat_id, _format_latest())
    elif cmd == "/wake":
        _send_text(chat_id, "⏰ Triggering a pipeline run now…")
        from app import _run_in_background
        threading.Thread(target=_run_in_background, daemon=True).start()


def _long_poll_loop() -> None:
    """Run getUpdates in a loop using GET (POST fails, GET works)."""
    base_url = f"{cfg.telegram_api_base.rstrip('/')}/bot{cfg.telegram_bot_token}/getUpdates"
    offset = 0
    consecutive_errors = 0
    logger.info(
        f"Telegram command poller started (30s long-poll, base={cfg.telegram_api_base})"
    )

    client = httpx.Client(timeout=_HTTP_TIMEOUT, limits=_HTTP_LIMITS)

    while True:
        try:
            params = {
                "timeout": _TELEGRAM_LONG_POLL_SECONDS,
                "offset": offset,
                "allowed_updates": "message",
            }
            url = base_url + "?" + urllib.parse.urlencode(params)
            resp = client.get(url)
            data = resp.json()
            if not data.get("ok"):
                logger.warning(f"getUpdates not ok: {data}")
                consecutive_errors += 1
                _backoff_sleep(consecutive_errors)
                continue

            updates = data.get("result") or []
            for upd in updates:
                offset = max(offset, (upd.get("update_id") or 0) + 1)
                try:
                    _handle_update(upd)
                except Exception as e:
                    logger.warning(f"command handler error: {e}")

            consecutive_errors = 0

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            consecutive_errors += 1
            logger.warning(
                f"long-poll connection error (#{consecutive_errors}): {e} — "
                f"recreating client, backing off {_backoff_seconds(consecutive_errors)}s"
            )
            try:
                client.close()
            except Exception:
                pass
            client = httpx.Client(timeout=_HTTP_TIMEOUT, limits=_HTTP_LIMITS)
            _backoff_sleep(consecutive_errors)

        except Exception as e:
            consecutive_errors += 1
            logger.warning(
                f"long-poll loop error (#{consecutive_errors}): {e} — "
                f"backing off {_backoff_seconds(consecutive_errors)}s"
            )
            _backoff_sleep(consecutive_errors)


def _backoff_seconds(consecutive_errors: int) -> float:
    return min(60.0, 5.0 * (2 ** max(0, consecutive_errors - 1)))


def _backoff_sleep(consecutive_errors: int) -> None:
    time.sleep(_backoff_seconds(consecutive_errors))


_started = False
_thread: threading.Thread | None = None


def start_command_poller() -> None:
    """Start the long-poll loop in a background daemon thread (idempotent)."""
    global _started, _thread
    if _started:
        return
    if not cfg.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN not set; command poller disabled")
        return
    _thread = threading.Thread(target=_long_poll_loop, daemon=True, name="tg-cmd-poller")
    _thread.start()
    _started = True
    logger.info("Telegram command poller thread launched")
