"""Entry point: starts uvicorn (web server) + APScheduler (in-process cron).

Startup order (CRITICAL for HF Spaces):
  1. main() starts uvicorn immediately — binds port 7860 within milliseconds
  2. FastAPI's @app.on_event("startup") fires AFTER the port is bound:
     a. start_command_poller()  — Telegram long-poll thread
     b. download_state()         — restore state.db + seen.json from HF Hub (background)
     c. initial pipeline run     — delayed 60s so HF health check passes first
  3. APScheduler cron ticks every 2 hours

This order ensures HF's health check sees a responding port quickly, so the
Space transitions from APP_STARTING → RUNNING without timing out.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

from app import app, _run_in_background
from config import cfg
from pipeline import run_pipeline
from state_sync import download_state
from utils import logger


def start_scheduler():
    """Start APScheduler with the every-2h cron job. Does NOT trigger an
    immediate pipeline run — that's handled separately in main()."""
    sched = BackgroundScheduler(timezone="UTC")

    # Cron: every 2 hours, on the hour (00:00, 02:00, 04:00, ...)
    sched.add_job(
        run_pipeline,
        trigger="cron",
        minute=cfg.scrape_cron_minute,
        hour=cfg.scrape_cron_hour,
        id="scrape_every_2h",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    sched.start()
    logger.info(
        f"Scheduler started: cron minute={cfg.scrape_cron_minute} hour={cfg.scrape_cron_hour} "
        f"(every 2 hours = 12 runs/day)"
    )
    return sched


def _deferred_startup():
    """Background thread that does the heavy startup work AFTER uvicorn
    has bound the port and HF's health check has passed.

    Runs in this order:
      1. download_state()           — restore HF Hub state (can be slow, but doesn't block port)
      2. gemini.startup_self_check() — log which Gemini models the API key can use (no quota cost)
      3. sleep 60s                   — give HF time to mark Space as RUNNING
      4. run_pipeline()              — first scrape cycle
    """
    try:
        logger.info("[startup] restoring state from HF Hub (if configured)...")
        download_state()
    except Exception as e:
        logger.warning(f"[startup] download_state failed (non-fatal): {e}")

    # Log which Gemini models are available — helps debug "model not available" errors
    # without waiting for a full pipeline run. Does NOT count against quota.
    try:
        from summarizer.gemini import startup_self_check
        startup_self_check()
    except Exception as e:
        logger.warning(f"[startup] gemini self-check failed (non-fatal): {e}")

    # Give HF's health check time to mark the Space as RUNNING before we
    # start hammering the network with the first pipeline run.
    logger.info("[startup] waiting 60s before first pipeline run (lets HF health check pass)...")
    time.sleep(60)

    logger.info("[startup] triggering initial pipeline run...")
    _run_in_background()


def main():
    # Validate required env vars — log but don't exit (HF would show APP_ERROR
    # if we exit; better to let the app start so the user can see the dashboard
    # and the error message in /status)
    missing = []
    if not cfg.gemini_api_key:
        missing.append("GEMINI_API_KEY")
    if not cfg.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not cfg.telegram_chat_id:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        logger.error(
            f"Missing required environment variables: {missing}. "
            "Set them in HF Space Secrets. Pipeline will no-op until they are set, "
            "but the web dashboard will still load so you can verify the deployment."
        )

    # Start APScheduler (registers the every-2h cron job, no immediate run)
    sched = start_scheduler()

    # Launch the deferred startup thread — runs download_state + initial
    # pipeline AFTER uvicorn binds the port. This is the key fix: uvicorn
    # starts listening within milliseconds, HF's health check passes,
    # Space transitions to RUNNING, THEN we do the heavy work.
    threading.Thread(target=_deferred_startup, daemon=True).start()

    # Run uvicorn in the main thread (blocks until shutdown)
    # This must be the LAST thing main() does — it blocks.
    logger.info(f"Starting uvicorn on 0.0.0.0:{cfg.web_port}...")
    uvicorn.run(app, host="0.0.0.0", port=cfg.web_port, log_level="info")

    # On shutdown
    sched.shutdown(wait=False)


if __name__ == "__main__":
    main()
