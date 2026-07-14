"""FastAPI app exposing /health, /wake, /status, /quota, /articles, /runs."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from config import cfg
from notifier.commands import start_command_poller
from pipeline import run_pipeline
from scraper.diff import (
    get_gemini_calls_today,
    get_recent_articles,
    get_recent_runs,
    load_seen_urls,
)

app = FastAPI(title="TheDeepView Bot", version="2.0.0")

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


@app.on_event("startup")
def _on_startup() -> None:
    # Start the interactive Telegram command poller in a background thread.
    # Works on Render (or any normal host) — connects directly to api.telegram.org.
    start_command_poller()


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})


@app.get("/", response_class=HTMLResponse)
async def root():
    """Render a clean HTML status page (shown in HF Spaces iframe)."""
    seen = load_seen_urls()
    calls_today = get_gemini_calls_today()
    quota_pct = min(100, int((calls_today / max(1, cfg.gemini_daily_limit)) * 100))
    sources_html = "".join(
        f"<li>{name} <span class='kind'>({kind})</span></li>"
        for name, kind, _, _ in cfg.sources
    )
    recent_runs = get_recent_runs(limit=5)
    runs_html = "".join(
        f"<tr><td>{r.get('started_at', '')[:19]}</td>"
        f"<td>{r.get('new_articles', 0)}</td>"
        f"<td>{r.get('telegram_sent', 0)}</td>"
        f"<td>{r.get('errors', 0)}</td>"
        f"<td>{r.get('gemini_calls', 0)}</td></tr>"
        for r in recent_runs
    ) or "<tr><td colspan='5' class='muted'>No runs yet</td></tr>"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TheDeepView Bot</title>
<style>
  :root {{
    --bg: #0f172a; --card: #1e293b; --fg: #e2e8f0;
    --muted: #94a3b8; --accent: #38bdf8; --ok: #4ade80; --warn: #fbbf24;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 2rem; font-family: -apple-system, BlinkMacSystemFont,
    'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--fg);
    line-height: 1.5;
  }}
  h1 {{ margin: 0 0 .25rem; font-size: 1.75rem; }}
  .subtitle {{ color: var(--muted); margin: 0 0 2rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; }}
  .card {{
    background: var(--card); padding: 1.25rem; border-radius: 12px;
    border: 1px solid #334155;
  }}
  .card .label {{ color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; }}
  .card .value {{ font-size: 1.75rem; font-weight: 600; margin-top: .25rem; }}
  .quota-bar {{ height: 8px; background: #334155; border-radius: 4px; overflow: hidden; margin-top: .5rem; }}
  .quota-fill {{ height: 100%; background: {('var(--warn)' if calls_today >= cfg.gemini_safety_threshold else 'var(--accent)')}; }}
  .section {{ margin-top: 2rem; }}
  .section h2 {{ font-size: 1.1rem; margin: 0 0 .75rem; }}
  ul {{ list-style: none; padding: 0; margin: 0; }}
  li {{ padding: .35rem 0; border-bottom: 1px solid #334155; }}
  .kind {{ color: var(--muted); font-size: .85rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
  th, td {{ text-align: left; padding: .5rem; border-bottom: 1px solid #334155; }}
  th {{ color: var(--muted); font-weight: 500; font-size: .8rem; text-transform: uppercase; }}
  .muted {{ color: var(--muted); }}
  .status-pill {{
    display: inline-block; padding: .15rem .6rem; border-radius: 999px;
    font-size: .75rem; background: var(--ok); color: #0f172a; font-weight: 600;
  }}
  code {{ background: #334155; padding: .1rem .35rem; border-radius: 4px; font-size: .85rem; }}
</style>
</head>
<body>
  <h1>🤖 TheDeepView Bot</h1>
  <p class="subtitle">
    Multi-source AI news → Gemini detailed summary → Telegram.
    Runs every 2 hours via APScheduler. Free-tier Hugging Face Space.
  </p>

  <div class="grid">
    <div class="card">
      <div class="label">Last Run</div>
      <div class="value">{(_last_run_at or 'never')[:19]}</div>
    </div>
    <div class="card">
      <div class="label">Articles Tracked</div>
      <div class="value">{len(seen)}</div>
    </div>
    <div class="card">
      <div class="label">Gemini Quota Today</div>
      <div class="value">{calls_today} / {cfg.gemini_daily_limit}</div>
      <div class="quota-bar"><div class="quota-fill" style="width:{quota_pct}%"></div></div>
    </div>
    <div class="card">
      <div class="label">Status</div>
      <div class="value"><span class="status-pill">{('THROTTLED' if calls_today >= cfg.gemini_safety_threshold else 'OK')}</span></div>
    </div>
  </div>

  <div class="section">
    <h2>Sources ({len(cfg.sources)})</h2>
    <ul>{sources_html}</ul>
  </div>

  <div class="section">
    <h2>Recent Runs</h2>
    <table>
      <thead><tr><th>Started (UTC)</th><th>New</th><th>Sent</th><th>Errors</th><th>Gemini Calls</th></tr></thead>
      <tbody>{runs_html}</tbody>
    </table>
  </div>

  <div class="section">
    <h2>Endpoints</h2>
    <p class="muted">
      <code>GET /health</code> · <code>POST /wake</code> · <code>GET /status</code> ·
      <code>GET /quota</code> · <code>GET /articles</code> · <code>GET /runs</code>
    </p>
    <p class="muted">
      Telegram commands: <code>/start</code> <code>/status</code> <code>/quota</code>
      <code>/latest</code> <code>/wake</code>
    </p>
  </div>
</body>
</html>
"""


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


@app.get("/debug")
async def debug():
    """Diagnostic endpoint showing config, Gemini status, Telegram status.
    Use this to debug without needing container logs."""
    import httpx

    # Check Gemini connectivity (list_models is free, doesn't count against quota)
    gemini_status = {"configured": bool(cfg.gemini_api_key), "models": [], "error": None}
    if cfg.gemini_api_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=cfg.gemini_api_key)
            models = list(genai.list_models())
            all_models = [
                m.name.replace("models/", "")
                for m in models
                if "generateContent" in (getattr(m, "supported_generation_methods", []) or [])
            ]
            # Check primary availability against the FULL list, not the truncated one
            gemini_status["primary_model"] = cfg.gemini_primary_model
            gemini_status["primary_available"] = cfg.gemini_primary_model in all_models
            gemini_status["total_models"] = len(all_models)
            # Return all models (not truncated) so the user can see what's available
            gemini_status["models"] = all_models
        except Exception as e:
            gemini_status["error"] = str(e)[:300]

    # Check Telegram connectivity using urllib (not httpx — httpx has TLS issues)
    telegram_status = {
        "configured": bool(cfg.telegram_bot_token),
        "api_base": cfg.telegram_api_base,
        "using_proxy": cfg.telegram_api_base != "https://api.telegram.org",
        "bot_info": None,
        "error": None,
    }
    if cfg.telegram_bot_token:
        try:
            base = cfg.telegram_api_base.rstrip("/")
            with httpx.Client(timeout=httpx.Timeout(connect=10, read=20, write=10, pool=10)) as client:
                resp = client.get(f"{base}/bot{cfg.telegram_bot_token}/getMe")
            data = resp.json()
            if data.get("ok"):
                telegram_status["bot_info"] = {
                    "username": data["result"]["username"],
                    "first_name": data["result"]["first_name"],
                    "id": data["result"]["id"],
                }
            else:
                telegram_status["error"] = str(data)[:300]
        except Exception as e:
            err_msg = str(e)[:300]
            telegram_status["error"] = err_msg

    return JSONResponse({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "gemini_primary_model": cfg.gemini_primary_model,
            "gemini_fallback_model": cfg.gemini_fallback_model,
            "gemini_extra_fallbacks": list(cfg.gemini_extra_fallbacks),
            "gemini_batch_max_articles": cfg.gemini_batch_max_articles,
            "sources_count": len(cfg.sources),
            "sources": [{"name": n, "kind": k} for n, k, _, _ in cfg.sources],
            "data_dir": str(cfg.data_dir),
            "web_port": cfg.web_port,
        },
        "env_vars": {
            "GEMINI_API_KEY": "set" if cfg.gemini_api_key else "MISSING",
            "TELEGRAM_BOT_TOKEN": "set" if cfg.telegram_bot_token else "MISSING",
            "TELEGRAM_CHAT_ID": "set" if cfg.telegram_chat_id else "MISSING",
            "TELEGRAM_API_BASE": cfg.telegram_api_base,
            "HF_TOKEN": "set" if cfg.hf_token else "not set",
            "HF_STATE_REPO": cfg.hf_state_repo or "not set",
        },
        "gemini": gemini_status,
        "telegram": telegram_status,
        "last_run": {
            "at": _last_run_at,
            "summary": _last_run_summary,
        },
        "quota": {
            "calls_today": get_gemini_calls_today(),
            "daily_limit": cfg.gemini_daily_limit,
        },
    })


@app.post("/test-telegram")
async def test_telegram():
    """Send a test message to Telegram to verify connectivity.
    Uses the same notifier module as the pipeline, so it tests the full
    chain: proxy → Telegram API → your chat."""
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        return JSONResponse({
            "status": "error",
            "message": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set",
        }, status_code=400)

    try:
        from notifier.telegram import _send_text
        _send_text(
            cfg.telegram_chat_id,
            "🧪 Test message from TheDeepView Bot\n\n"
            "If you see this, Telegram sending works through the proxy!\n"
            f"Proxy: {cfg.telegram_api_base}",
        )
        return JSONResponse({
            "status": "ok",
            "message": "Test message sent successfully",
            "proxy": cfg.telegram_api_base,
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "message": str(e)[:500],
            "proxy": cfg.telegram_api_base,
        }, status_code=500)
