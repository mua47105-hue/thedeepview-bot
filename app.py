"""FastAPI app exposing /health, /wake, /status, /quota, /articles, /runs."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config import cfg
from pipeline import run_pipeline
from scraper.diff import (
    get_gemini_calls_today,
    get_recent_articles,
    get_recent_runs,
    load_seen_urls,
)

app = FastAPI(title="TheDeepView Bot", version="1.0.0")

_last_run_summary: dict | None = None
_last_run_at: str | None = None
_run_lock = threading.Lock()


def _run_in_background():
    global _last_run_summary, _last_run_at
    try:
        _last_run_summary = run_pipeline()
        _last_run_at = datetime.now(timezone.utc).isoformat()
    except Exception as e:
        _last_run_summary = {"error": str(e)}
        _last_run_at = datetime.now(timezone.utc).isoformat()


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})


@app.get("/")
async def root():
    seen = load_seen_urls()
    return JSONResponse({
        "service": "TheDeepView → Gemini 3.5 Flash → Telegram Bot",
        "last_run_at": _last_run_at,
        "last_run_summary": _last_run_summary,
        "total_articles_tracked": len(seen),
        "gemini_calls_today": get_gemini_calls_today(),
        "gemini_daily_limit": cfg.gemini_daily_limit,
        "endpoints": ["/health", "/wake", "/status", "/quota", "/articles", "/runs"],
    })


@app.post("/wake")
async def wake():
    """Trigger an immediate pipeline run (used by external cron/uptime monitors)."""
    if _run_lock.locked():
        return JSONResponse(
            {"status": "already_running", "last_run_at": _last_run_at},
            status_code=409,
        )
    if not _run_lock.acquire(blocking=False):
        return JSONResponse(
            {"status": "already_running", "last_run_at": _last_run_at},
            status_code=409,
        )
    try:
        threading.Thread(target=_run_in_background, daemon=True).start()
    finally:
        _run_lock.release()
    return JSONResponse({"status": "triggered", "ts": datetime.now(timezone.utc).isoformat()})


@app.get("/status")
async def status():
    return JSONResponse({
        "last_run_at": _last_run_at,
        "last_run_summary": _last_run_summary,
        "recent_runs": get_recent_runs(limit=10),
    })


@app.get("/quota")
async def quota():
    today = get_gemini_calls_today()
    return JSONResponse({
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "calls_today": today,
        "daily_limit": cfg.gemini_daily_limit,
        "safety_threshold": cfg.gemini_safety_threshold,
        "remaining": max(0, cfg.gemini_daily_limit - today),
        "status": "ok" if today < cfg.gemini_safety_threshold else "throttled",
    })


@app.get("/articles")
async def articles():
    return JSONResponse(get_recent_articles(limit=50))


@app.get("/runs")
async def runs():
    return JSONResponse(get_recent_runs(limit=50))
