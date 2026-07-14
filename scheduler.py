"""Entry point: starts APScheduler (in-process cron) + uvicorn (web server)."""
from __future__ import annotations

import threading

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

from app import app, _run_in_background
from config import cfg
from pipeline import run_pipeline
from utils import logger


def start_scheduler():
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

    # Run once on startup so first scrape happens immediately after deploy
    logger.info("Running initial pipeline on startup...")
    threading.Thread(target=_run_in_background, daemon=True).start()

    return sched


def main():
    # Validate required env vars
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
            "Set them in HF Space Secrets before launching. "
            "Pipeline will no-op until they are set."
        )

    sched = start_scheduler()

    # Run uvicorn in the main thread (blocks)
    uvicorn.run(app, host="0.0.0.0", port=cfg.web_port, log_level="info")

    # On shutdown
    sched.shutdown(wait=False)


if __name__ == "__main__":
    main()
