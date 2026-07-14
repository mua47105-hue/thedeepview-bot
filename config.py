"""Centralized configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    # ── Source ──────────────────────────────────────────────────────
    site_base: str = "https://www.thedeepview.com"
    sitemap_url: str = "https://www.thedeepview.com/sitemap.xml"
    articles_listing_url: str = "https://www.thedeepview.com/articles"
    user_agent: str = (
        "Mozilla/5.0 (compatible; TheDeepViewBot/1.0; "
        "+https://github.com/youruser/thedeepview-bot)"
    )

    # ── Schedule (every 2 hours, on the hour) ───────────────────────
    # 24h / 2h = 12 scheduled runs per day
    scrape_cron_minute: str = "0"
    scrape_cron_hour: str = "*/2"

    # ── Rate limiting ───────────────────────────────────────────────
    request_delay_seconds: float = 1.0
    request_timeout_seconds: int = 30

    # ── Gemini ──────────────────────────────────────────────────────
    gemini_api_key: str = ""
    gemini_primary_model: str = "gemini-3.5-flash"
    gemini_fallback_model: str = "gemini-2.5-flash"
    gemini_temperature: float = 0.3
    gemini_max_output_tokens: int = 8192

    # ── Quota (free tier = 20 RPD) ──────────────────────────────────
    gemini_daily_limit: int = 20
    gemini_safety_threshold: int = 18
    gemini_batch_max_articles: int = 10

    # ── Telegram ────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_image_max_bytes: int = 5_000_000

    # ── Storage ─────────────────────────────────────────────────────
    data_dir: Path = Path("/data")
    db_path: Path = Path("/data/state.db")
    seen_path: Path = Path("/data/seen.json")

    # ── Web server ──────────────────────────────────────────────────
    web_port: int = 7860

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(os.getenv("DATA_DIR", "/data"))
        # In local dev, fall back to ./data if /data isn't writable
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / ".write_test").write_text("ok", encoding="utf-8")
            (data_dir / ".write_test").unlink()
        except (OSError, PermissionError):
            data_dir = Path("./data")
            data_dir.mkdir(parents=True, exist_ok=True)
            print(f"[config] /data not writable, using {data_dir.resolve()}")

        return cls(
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_primary_model=os.getenv("GEMINI_PRIMARY_MODEL", "gemini-3.5-flash"),
            gemini_fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            gemini_daily_limit=int(os.getenv("GEMINI_DAILY_LIMIT", "20")),
            gemini_safety_threshold=int(os.getenv("GEMINI_SAFETY_THRESHOLD", "18")),
            gemini_batch_max_articles=int(os.getenv("GEMINI_BATCH_MAX_ARTICLES", "10")),
            data_dir=data_dir,
            db_path=data_dir / "state.db",
            seen_path=data_dir / "seen.json",
            web_port=int(os.getenv("PORT", "7860")),
        )


cfg = Config.from_env()
