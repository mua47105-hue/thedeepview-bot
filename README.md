---
title: TheDeepView Bot
emoji: 🤖
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
tags:
  - ai-news
  - telegram-bot
  - gemini
  - scraper
  - huggingface-spaces
---

# TheDeepView Bot

A free-tier, fully automated AI news briefing bot for Telegram.

**Pipeline:** Multiple AI news sources → scrape → ONE batched Gemini call (detailed structured summaries) → Telegram (with scraped article images, never AI-generated).

Runs every 2 hours automatically. Designed to fit within the Gemini free-tier quota of **20 requests per day** by batching all new articles into a single Gemini call per run.

---

## What it does

1. **Scrapes** 8 AI news sources every 2 hours (sitemap + RSS feeds).
2. **Diffs** against a persistent seen-URL store (SQLite + JSON snapshot).
3. **Fetches** each new article's full body and hero image.
4. **Batches** all new articles into ONE Gemini prompt → ONE API call → detailed structured summaries (400-600 words per article, classified into 8 categories).
5. **Sends** to your Telegram chat: `sendPhoto` with the scraped hero image + caption, then `sendMessage` with the full detailed summary (chunked if > 4000 chars).
6. **Tracks** Gemini quota in SQLite, throttles at 18/20 calls to leave buffer.
7. **Exposes** a FastAPI server on port 7860 with a status dashboard, plus `/wake` for external cron triggers.
8. **Responds** to Telegram commands: `/start`, `/status`, `/quota`, `/latest`, `/wake`, `/help`.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Hugging Face Space (Docker, free CPU tier, port 7860)        │
│                                                                │
│  ┌────────────┐   ┌──────────────┐   ┌──────────────────────┐ │
│  │ APScheduler│──>│  pipeline.py │──>│ summarizer/gemini.py │ │
│  │  cron */2h │   │ (orchestrator)│   │ (ONE batched call)   │ │
│  └────────────┘   └──────┬───────┘   └──────────┬───────────┘ │
│       ▲                   │                      │             │
│       │                   ▼                      ▼             │
│  ┌────┴─────┐   ┌──────────────────┐   ┌──────────────────┐  │
│  │ FastAPI  │   │ scraper/         │   │ notifier/        │  │
│  │ /wake    │   │  discovery.py    │   │  telegram.py     │  │
│  │ /status  │   │  article.py      │   │  commands.py     │  │
│  └──────────┘   └──────────────────┘   └──────────────────┘  │
│       ▲                                                          │
│       │ external ping (cron-job.org, every 5-15 min)            │
└───────┼──────────────────────────────────────────────────────────┘
        │
        ▼
   keeps the free Space awake
```

### Quota math

- Gemini free tier: **20 requests/day**
- Schedule: every 2 hours = **12 runs/day**
- Per run: **1** Gemini call (regardless of how many new articles)
- Worst case: 12 calls/day → leaves **8 calls/day** of buffer for manual `/wake` triggers
- Safety throttle kicks in at 18/20 to preserve buffer

---

## Sources (8 by default)

| Source | Kind | URL |
|---|---|---|
| TheDeepView | sitemap | https://www.thedeepview.com/sitemap.xml |
| TechCrunch AI | RSS | https://techcrunch.com/category/artificial-intelligence/feed/ |
| VentureBeat AI | RSS | https://venturebeat.com/category/ai/feed/ |
| MIT Tech Review AI | RSS | https://www.technologyreview.com/topic/artificial-intelligence/feed |
| OpenAI Blog | RSS | https://openai.com/blog/rss.xml |
| Google AI Blog | RSS | https://blog.google/technology/ai/rss/ |
| Hugging Face Blog | RSS | https://huggingface.co/blog/feed.xml |
| The Verge AI | RSS | https://www.theverge.com/rss/ai-artificial-intelligence/index.xml |

All feeds verified as reachable. To customize, set the `SOURCES_JSON` env var (JSON array, same shape as `DEFAULT_SOURCES` in `config.py`), or edit `DEFAULT_SOURCES` directly.

---

## Categories (Gemini classifies each article into one)

| Category | Emoji | Meaning |
|---|---|---|
| `model_launch` | 🚀 | New AI model release |
| `infra_upgrade` | 🏗️ | Data centers, chips, networking, power |
| `core_logic` | 🧠 | New training method, reasoning approach, RL technique |
| `functional_update` | ⚡ | Feature rollout in existing product |
| `research` | 🔬 | Research paper or scientific breakthrough |
| `policy` | ⚖️ | Regulation, governance, safety, government action |
| `business` | 💼 | Funding, IPOs, M&A, partnerships |
| `other` | 📰 | Anything else substantive |

Pure opinion/culture/lifestyle pieces are marked `SKIP` and only the photo+caption is sent (no summary).

---

## Setup — deploy to Hugging Face Spaces (free)

### Step 1: Create the Telegram bot

1. Open Telegram, message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, follow the prompts, copy the **bot token**.
3. Send `/setprivacy` → Disable (so the bot sees commands in groups, if you use a group).
4. Open your new bot, send `/start`.
5. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser to find your `chat_id` (look for `"chat":{"id":...}`).

### Step 2: Get a Gemini API key

1. Visit https://aistudio.google.com/apikey
2. Create a key (free tier: 20 RPD, no credit card needed).
3. Copy the key.

### Step 3: Create the Hugging Face Space

1. Go to https://huggingface.co/new-space
2. **Owner:** your account. **Name:** `thedeepview-bot` (or whatever).
3. **SDK:** Docker. **Visibility:** Private (recommended).
4. **Hardware:** CPU basic (free).
5. Create.

### Step 4: Push this code to the Space

Either:
- Clone the Space repo (`git clone https://huggingface.co/spaces/<you>/thedeepview-bot`), copy these files in, commit, push.
- Or: push to GitHub and connect the Space to a GitHub repo (HF supports this in Settings → "GitHub sync").

### Step 5: Set Secrets

In the Space → **Settings** → **Repository secrets** → add:

| Secret name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from step 1 |
| `TELEGRAM_CHAT_ID` | Your chat ID from step 1 |
| `GEMINI_API_KEY` | Your Gemini API key from step 2 |

### Step 6: Keep the Space awake (free-tier limitation)

Hugging Face **free CPU Spaces sleep after 48 hours of no HTTP traffic**. APScheduler only runs while the Space is awake. To keep it awake, set up a free external cron:

1. Sign up at https://cron-job.org (free, no credit card).
2. Create a job:
   - **URL:** `https://<your-space-name>.hf.space/health`
   - **Schedule:** every 5 minutes (or every 15 min — both work)
   - **Method:** GET
3. Save.

This keeps the Space perpetually awake so APScheduler fires every 2 hours reliably.

Alternative: use [UptimeRobot](https://uptimerobot.com) (free, 5-minute pings).

---

## Telegram commands (interactive)

Once running, you can message the bot directly:

| Command | Action |
|---|---|
| `/start` | Show help |
| `/help` | Show help |
| `/status` | Last 5 pipeline runs |
| `/quota` | Gemini API usage today |
| `/latest` | Last 10 articles tracked |
| `/wake` | Trigger an immediate pipeline run |

The bot only responds to the configured `TELEGRAM_CHAT_ID` for security.

---

## Environment variables

### Required

| Var | Example | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `123456:ABC-DEF...` | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | `123456789` | Your Telegram chat ID |
| `GEMINI_API_KEY` | `AIza...` | Gemini API key from aistudio.google.com |

### Optional

| Var | Default | Description |
|---|---|---|
| `GEMINI_PRIMARY_MODEL` | `gemini-2.5-flash` | Primary Gemini model |
| `GEMINI_FALLBACK_MODEL` | `gemini-2.0-flash` | Fallback if primary fails |
| `GEMINI_DAILY_LIMIT` | `20` | Gemini daily request limit |
| `GEMINI_SAFETY_THRESHOLD` | `18` | Stop calling at this count |
| `GEMINI_BATCH_MAX_ARTICLES` | `15` | Max articles per batched call |
| `SOURCES_JSON` | (none) | Override default source list (JSON array) |
| `DATA_DIR` | `/data` | State storage dir (HF persistent storage or `./data` fallback) |
| `PORT` | `7860` | HTTP port (HF Spaces requires 7860) |

See [`.env.example`](.env.example) for a copy-paste template.

---

## Free-tier limitations & mitigations

| Limitation | Mitigation |
|---|---|
| HF free Spaces sleep after 48h of inactivity | External cron pings `/health` every 5 min (cron-job.org) |
| HF free Spaces have **no persistent storage** — `/data` is ephemeral | Code falls back to `./data`; on Space restart the seen-URL store resets and some articles may be re-sent. To get true persistence, upgrade to HF Pro (paid) for persistent storage, or wire `DATA_DIR` to an external service. |
| Gemini free tier = 20 RPD | One batched call per run, throttled at 18/20, so worst case 12 calls/day leaves 8 of buffer |
| Telegram `sendPhoto` 10 MB limit | Code caps downloaded images at 5 MB |
| Telegram `sendMessage` 4096 char limit | Code chunks longer summaries into multiple messages |
| Scraping may hit rate limits | 1-second polite sleep between article fetches |

---

## Run locally

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
export GEMINI_API_KEY=...

python scheduler.py
# → starts uvicorn on http://0.0.0.0:7860
# → starts APScheduler (every 2h cron)
# → starts Telegram long-poll thread
# → runs an initial pipeline on startup
```

Visit `http://localhost:7860` for the status dashboard.

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | HTML status dashboard (HF Spaces iframe) |
| GET | `/health` | `{"status":"ok"}` — for uptime monitors |
| POST | `/wake` | Trigger an immediate pipeline run |
| GET | `/status` | Last run summary + recent runs (JSON) |
| GET | `/quota` | Gemini quota for today (JSON) |
| GET | `/articles` | Last 50 tracked articles (JSON) |
| GET | `/runs` | Last 50 pipeline runs (JSON) |

---

## File layout

```
.
├── app.py                 # FastAPI app + HTML dashboard + /wake endpoint
├── scheduler.py           # Entry point: APScheduler (every 2h) + uvicorn
├── pipeline.py            # Orchestrates scrape → fetch → batch-summarize → notify
├── config.py              # All config + source registry (env-var driven)
├── utils.py               # Logger, httpx client, HTML-to-text, polite sleep
├── requirements.txt
├── Dockerfile             # HF Spaces-compatible Docker image
├── .env.example           # Copy to .env and fill in
├── scraper/
│   ├── discovery.py       # Multi-source discovery (RSS + sitemap)
│   ├── article.py         # Single-article fetcher (JSON-LD + og:image + body)
│   └── diff.py            # SQLite state: seen URLs, articles, runs, quota
├── summarizer/
│   └── gemini.py          # Batched Gemini prompt (ONE call per run)
└── notifier/
    ├── telegram.py        # sendPhoto + sendMessage (with chunking)
    └── commands.py        # Long-poll thread for /status, /quota, /latest, /wake
```

---

## License

MIT — see source headers. No warranty. Don't blame me if Gemini changes its quota.
