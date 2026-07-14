"""
Interactive Telegram bot commands via long-polling.

Runs in a background daemon thread alongside the FastAPI server.
Responds to: /start, /status, /quota, /latest, /wake, /help
"""
from __future__ import annotations

import threading
import time

import httpx

from config import cfg
from scraper.diff import get_gemini_calls_today, get_recent_articles, get_recent_runs
from utils import logger

TELEGRAM_API = "https://api.telegram.org"

# Telegram long-poll can legitimately take up to 50 seconds (we ask for 30s
# timeout in the getUpdates request). HF Spaces → api.telegram.org can have
# slow SSL handshakes, so we use a split timeout: short connect (10s) but
# long read (90s) so the long-poll has time to return.
_HTTP_TIMEOUT = httpx.Timeout(
    connect=15.0,   # SSL handshake + TCP connect
    read=90.0,      # long-poll can take up to 50s; give buffer
    write=10.0,
    pool=10.0,
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
    url = f"{TELEGRAM_API}/bot{cfg.telegram_bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(url, json=payload)
        if resp.status_code == 400:
            # Markdown parse failure — retry as plain text
            payload.pop("parse_mode", None)
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                client.post(url, json=payload)
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

    # Only respond to the configured chat_id (security: don't reply to random users)
    if cfg.telegram_chat_id and chat_id != cfg.telegram_chat_id:
        logger.info(f"Ignoring command from unauthorized chat_id={chat_id}: {text!r}")
        return

    cmd = text.split()[0].lower().split("@")[0]  # strip bot username suffix
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
        # Import here to avoid circular import at module load
        from app import _run_in_background
        threading.Thread(target=_run_in_background, daemon=True).start()
    else:
        # Silently ignore unknown commands (avoid spamming on every message)
        pass


def _long_poll_loop() -> None:
    """Run getUpdates in a loop. Exits cleanly on thread shutdown.

    Uses a 50-second Telegram-side long-poll timeout, with split httpx
    timeouts (15s connect / 90s read) to handle HF Space → Telegram SSL
    handshake variability. On errors, backs off exponentially up to 60s
    so we don't spam the logs.
    """
    base_url = f"{TELEGRAM_API}/bot{cfg.telegram_bot_token}/getUpdates"
    offset = 0  # 0 = first call returns all pending updates; subsequent calls use offset
    consecutive_errors = 0
    logger.info("Telegram command poller started")

    while True:
        try:
            # Telegram-side long-poll: 50 seconds. Combined with our 90s read
            # timeout, this gives plenty of buffer for slow SSL handshakes.
            params = {"timeout": 50, "offset": offset, "allowed_updates": ["message"]}
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                resp = client.get(base_url, params=params)
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

            # Reset error counter on any successful getUpdates call
            consecutive_errors = 0
        except httpx.TimeoutException as e:
            # SSL handshake or read timeout — common on HF free tier, usually transient
            consecutive_errors += 1
            logger.warning(
                f"long-poll timeout (#{consecutive_errors}): {e} — "
                f"backing off {_backoff_seconds(consecutive_errors)}s"
            )
            _backoff_sleep(consecutive_errors)
        except Exception as e:
            consecutive_errors += 1
            logger.warning(
                f"long-poll loop error (#{consecutive_errors}): {e} — "
                f"backing off {_backoff_seconds(consecutive_errors)}s"
            )
            _backoff_sleep(consecutive_errors)


def _backoff_seconds(consecutive_errors: int) -> float:
    """Exponential backoff: 5s, 10s, 20s, 40s, capped at 60s."""
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
