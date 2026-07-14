"""
Interactive Telegram bot commands via long-polling.

Uses urllib (Python stdlib) instead of httpx to avoid TLS session caching
issues with Cloudflare Workers from HF Spaces. Every request is a fresh
TCP+TLS connection — no pooling, no session reuse, no SSL errors.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

from config import cfg
from scraper.diff import get_gemini_calls_today, get_recent_articles, get_recent_runs
from utils import logger

_TELEGRAM_LONG_POLL_SECONDS = 15

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
    """Send a text message via GET (urllib)."""
    base = cfg.telegram_api_base.rstrip("/")
    params = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    url = f"{base}/bot{cfg.telegram_bot_token}/sendMessage?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TheDeepViewBot/2.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 400 and parse_mode:
            # Markdown parse failure — retry as plain text
            params.pop("parse_mode", None)
            url = f"{base}/bot{cfg.telegram_bot_token}/sendMessage?" + urllib.parse.urlencode(params)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "TheDeepViewBot/2.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp.read()
            except Exception:
                pass
        else:
            logger.warning(f"command reply HTTP {e.code}: {e.reason}")
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
    """Run getUpdates in a loop using urllib (not httpx).

    Each request is a fresh urllib.request.urlopen() call — no connection
    pooling, no TLS session caching. This eliminates the SSL handshake
    timeout / SSL EOF errors that httpx was causing on the 2nd+ request.
    """
    base = cfg.telegram_api_base.rstrip("/")
    url_base = f"{base}/bot{cfg.telegram_bot_token}/getUpdates"
    offset = 0
    consecutive_errors = 0
    logger.info(
        f"Telegram command poller started (15s long-poll, base={cfg.telegram_api_base}, urllib)"
    )

    while True:
        try:
            params = {
                "timeout": _TELEGRAM_LONG_POLL_SECONDS,
                "offset": offset,
                "allowed_updates": "message",
            }
            url = url_base + "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={"User-Agent": "TheDeepViewBot/2.0"})
            # Timeout = long-poll (15s) + buffer (20s) = 35s
            with urllib.request.urlopen(req, timeout=35) as resp:
                data = json.loads(resp.read().decode("utf-8"))

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

        except Exception as e:
            consecutive_errors += 1
            logger.warning(
                f"long-poll error (#{consecutive_errors}): {type(e).__name__}: {e} — "
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
