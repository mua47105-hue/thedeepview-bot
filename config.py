"""Centralized configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# ── Source registry ──────────────────────────────────────────────────────────
# Each source has:
#   name        : human-readable label (used in Telegram captions + logs)
#   kind        : "sitemap" | "rss"
#   feed_url    : URL to fetch (sitemap.xml or RSS feed)
#   url_pattern : substring that an article URL must contain to be accepted
#                 (filters out landing pages, tag pages, etc.)
#
# To disable a source, comment it out. To add a new source, append a dict.
# Order = priority order (when capping articles per run, earlier sources win).
DEFAULT_SOURCES: list[dict] = [
    {
        "name": "TheDeepView",
        "kind": "sitemap",
        "feed_url": "https://www.thedeepview.com/sitemap.xml",
        "url_pattern": "/articles/",
    },
    {
        "name": "TechCrunch AI",
        "kind": "rss",
        "feed_url": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "url_pattern": "techcrunch.com",
    },
    {
        "name": "VentureBeat AI",
        "kind": "rss",
        "feed_url": "https://venturebeat.com/category/ai/feed/",
        "url_pattern": "venturebeat.com",
    },
    {
        "name": "MIT Tech Review AI",
        "kind": "rss",
        "feed_url": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
        "url_pattern": "technologyreview.com",
    },
    {
        "name": "OpenAI Blog",
        "kind": "rss",
        "feed_url": "https://openai.com/blog/rss.xml",
        "url_pattern": "openai.com",
    },
    {
        "name": "Google AI Blog",
        "kind": "rss",
        "feed_url": "https://blog.google/technology/ai/rss/",
        "url_pattern": "blog.google",
    },
    {
        "name": "Hugging Face Blog",
        "kind": "rss",
        "feed_url": "https://huggingface.co/blog/feed.xml",
        "url_pattern": "huggingface.co",
    },
    {
        "name": "The Verge AI",
        "kind": "rss",
        "feed_url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "url_pattern": "theverge.com",
    },
]


@dataclass(frozen=True)
class Config:
    # ── HTTP ────────────────────────────────────────────────────────
    user_agent: str = (
        "Mozilla/5.0 (compatible; TheDeepViewBot/1.0; "
        "+https://github.com/mua47105-hue/thedeepview-bot)"
    )
    request_delay_seconds: float = 1.0
    request_timeout_seconds: int = 30

    # ── Schedule (every 2 hours, on the hour) ───────────────────────
    # 24h / 2h = 12 scheduled runs per day
    scrape_cron_minute: str = "0"
    scrape_cron_hour: str = "*/2"

    # ── Sources ─────────────────────────────────────────────────────
    # Serialised as a JSON string in env var SOURCES_JSON (optional override).
    sources: tuple = field(default_factory=lambda: tuple(
        (s["name"], s["kind"], s["feed_url"], s["url_pattern"])
        for s in DEFAULT_SOURCES
    ))

    # ── Gemini ──────────────────────────────────────────────────────
    gemini_api_key: str = ""
    # gemini-2.5-flash is the current free-tier model as of 2026.
    # gemini-3.5-flash is kept as a fallback for forward compatibility.
    gemini_primary_model: str = "gemini-2.5-flash"
    gemini_fallback_model: str = "gemini-2.0-flash"
    gemini_temperature: float = 0.4
    # 16384 tokens ≈ 12K words of output — enough for 15 detailed summaries in one batch
    gemini_max_output_tokens: int = 16384

    # ── Quota (free tier = 20 RPD) ──────────────────────────────────
    gemini_daily_limit: int = 20
    # Stop calling Gemini when we hit 18, leaving 2 as buffer for manual triggers
    gemini_safety_threshold: int = 18
    # Cap articles per run to stay within Gemini input token budget
    # (~4 chars/token, 1M token input limit → 250K chars total → ~16K chars/article)
    gemini_batch_max_articles: int = 15

    # ── Telegram ────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_image_max_bytes: int = 5_000_000

    # ── Storage ─────────────────────────────────────────────────────
    data_dir: Path = Path("/data")
    db_path: Path = Path("/data/state.db")
    seen_path: Path = Path("/data/seen.json")

    # ── Hugging Face Hub persistent state sync (optional) ───────────
    # Free-tier HF Spaces have ephemeral storage — /data is wiped on restart.
    # If HF_TOKEN + HF_STATE_REPO are set, the bot uploads state.db + seen.json
    # to a private HF dataset repo at the end of each run, and downloads them
    # back on startup. This survives Space restarts.
    hf_token: str = ""
    hf_state_repo: str = ""  # e.g. "mua47105-hue/thedeepview-bot-state"

    # ── Web server ──────────────────────────────────────────────────
    web_port: int = 7860

    @classmethod
    def from_env(cls) -> "Config":
        import json

        data_dir = Path(os.getenv("DATA_DIR", "/data"))
        # In local dev or ephemeral HF Spaces, fall back to ./data if /data isn't writable
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / ".write_test").write_text("ok", encoding="utf-8")
            (data_dir / ".write_test").unlink()
        except (OSError, PermissionError):
            data_dir = Path("./data")
            data_dir.mkdir(parents=True, exist_ok=True)
            print(f"[config] /data not writable, using {data_dir.resolve()}")

        # Allow SOURCES_JSON env var to override the default source list.
        sources_tuple = tuple(
            (s["name"], s["kind"], s["feed_url"], s["url_pattern"])
            for s in DEFAULT_SOURCES
        )
        sources_json = os.getenv("SOURCES_JSON", "").strip()
        if sources_json:
            try:
                custom = json.loads(sources_json)
                sources_tuple = tuple(
                    (s["name"], s["kind"], s["feed_url"], s["url_pattern"])
                    for s in custom
                )
                print(f"[config] Loaded {len(sources_tuple)} custom sources from SOURCES_JSON")
            except (json.JSONDecodeError, KeyError) as e:
                print(f"[config] SOURCES_JSON parse failed ({e}), using defaults")

        return cls(
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_primary_model=os.getenv("GEMINI_PRIMARY_MODEL", "gemini-2.5-flash"),
            gemini_fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.0-flash"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            gemini_daily_limit=int(os.getenv("GEMINI_DAILY_LIMIT", "20")),
            gemini_safety_threshold=int(os.getenv("GEMINI_SAFETY_THRESHOLD", "18")),
            gemini_batch_max_articles=int(os.getenv("GEMINI_BATCH_MAX_ARTICLES", "15")),
            sources=sources_tuple,
            data_dir=data_dir,
            db_path=data_dir / "state.db",
            seen_path=data_dir / "seen.json",
            hf_token=os.getenv("HF_TOKEN", ""),
            hf_state_repo=os.getenv("HF_STATE_REPO", ""),
            web_port=int(os.getenv("PORT", "7860")),
        )


cfg = Config.from_env()
