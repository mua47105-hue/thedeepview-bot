"""Entry point: starts uvicorn (web server) + APScheduler (in-process cron).

CRITICAL FOR HF SPACES:
  uvicorn MUST bind port 7860 within ~5 minutes or HF marks the Space as
  crashed and restart-loops it. This module starts uvicorn FIRST, then does
  all heavy work (state restore, Gemini self-check, initial pipeline run)
  in a background thread AFTER the port is bound.

  Every step in the background thread is wrapped in try/except so a failure
  in any one step does NOT crash the thread or the main process.
"""
from __future__ import annotations

import threading
import time

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

from app import app, _run_in_background
from config import cfg
from pipeline import run_pipeline
from state_sync import download_state
from utils import logger


def start_scheduler():
    """Start APScheduler with the every-2h cron job."""
    try:
        sched = BackgroundScheduler(timezone="UTC")
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
    except Exception as e:
        logger.error(f"Failed to start scheduler (non-fatal): {e}")
        return None


def _safe_step(name: str, fn, *args, **kwargs):
    """Run a step, log any error, never raise. Returns the result or None."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.error(f"[startup] {name} FAILED (non-fatal, continuing): {e}")
        return None


def _deferred_startup():
    """Background thread that does heavy startup work AFTER uvicorn binds port.

    Every step is wrapped in _safe_step so a failure in any one does NOT
    crash the thread or the main process. The app stays alive even if
    everything here fails.
    """
    # Step 1: Restore state from HF Hub
    _safe_step("download_state", download_state)

    # Step 2: Gemini self-check (logs which models the API key can use)
    def _gemini_self_check():
        from summarizer.gemini import startup_self_check
        startup_self_check()
    _safe_step("gemini_self_check", _gemini_self_check)

    # Step 3: Wait 60s so HF marks the Space as RUNNING before we do heavy work
    logger.info("[startup] waiting 60s before first pipeline run...")
    time.sleep(60)

    # Step 4: Trigger the first pipeline run
    logger.info("[startup] triggering initial pipeline run...")
    _safe_step("initial_pipeline_run", _run_in_background)


def main():
    # Log startup info (don't exit on missing secrets — let the dashboard show the error)
    missing = []
    if not cfg.gemini_api_key:
        missing.append("GEMINI_API_KEY")
    if not cfg.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not cfg.telegram_chat_id:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        logger.error(
            f"Missing required env vars: {missing}. "
            "Set them in HF Space Secrets. Dashboard will still load."
        )
    else:
        logger.info("All required env vars present.")

    # Start APScheduler (registers cron, no immediate run)
    sched = start_scheduler()

    # Launch deferred startup in background thread.
    # CRITICAL: this must be a daemon thread so it doesn't block shutdown.
    threading.Thread(target=_deferred_startup, daemon=True).start()

    # CRITICAL: uvicorn.run() MUST be the last thing main() does.
    # It binds port 7860 within milliseconds, which is what HF's health
    # check needs to see. Everything else runs in the background thread.
    logger.info(f"Starting uvicorn on 0.0.0.0:{cfg.web_port}...")
    try:
        uvicorn.run(app, host="0.0.0.0", port=cfg.web_port, log_level="info")
    except Exception as e:
        logger.error(f"uvicorn failed to start: {e}")
        raise
    finally:
        if sched:
            sched.shutdown(wait=False)


if __name__ == "__main__":
    main()
