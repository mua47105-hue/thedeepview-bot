"""
Orchestrates the full scrape → diff → fetch → batch-summarize → notify pipeline.

Key design: ONE Gemini API call per run, regardless of how many new articles.
This respects the 20 RPD hard limit on Gemini 3.5 Flash free tier.
"""
from __future__ import annotations

from datetime import datetime, timezone

from config import cfg
from scraper.article import fetch_article
from scraper.diff import (
    filter_new,
    get_gemini_calls_today,
    increment_gemini_calls,
    load_seen_urls,
    record_article,
    record_run,
    save_seen_urls,
)
from scraper.discovery import discover_all
from summarizer.gemini import summarize_batch
from notifier.telegram import send_article
from state_sync import upload_state
from utils import logger, polite_sleep


def run_pipeline(max_articles_per_run: int | None = None) -> dict:
    """Run one full scrape cycle. Returns a summary dict."""
    started_at = datetime.now(timezone.utc).isoformat()
    if max_articles_per_run is None:
        max_articles_per_run = cfg.gemini_batch_max_articles

    logger.info(f"=== Pipeline run started at {started_at} ===")

    # ── Phase 0: Discovery ──────────────────────────────────────────
    discovered = discover_all()
    if not discovered:
        logger.warning("Discovery returned 0 articles; aborting run")
        record_run(
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            discovered=0, new_articles=0, processed=0,
            telegram_sent=0, errors=0, gemini_calls=0,
        )
        upload_state()  # sync the empty-run record to HF Hub
        return {"discovered": 0, "new": 0, "processed": 0, "sent": 0, "errors": 0, "gemini_calls": 0}

    discovered_urls = [r.url for r in discovered]
    new_urls = filter_new(discovered_urls)
    logger.info(f"Discovered={len(discovered_urls)} | New={len(new_urls)}")

    if not new_urls:
        logger.info("No new articles — exiting quietly (zero Gemini calls)")
        record_run(
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            discovered=len(discovered_urls), new_articles=0, processed=0,
            telegram_sent=0, errors=0, gemini_calls=0,
        )
        upload_state()  # sync the no-op run record to HF Hub
        return {"discovered": len(discovered_urls), "new": 0, "processed": 0,
                "sent": 0, "errors": 0, "gemini_calls": 0}

    # ── Cap per-run processing ──────────────────────────────────────
    to_process = new_urls[:max_articles_per_run]
    if len(new_urls) > max_articles_per_run:
        logger.warning(
            f"Throttling: {len(new_urls)} new articles found, "
            f"processing only first {max_articles_per_run} this run. "
            f"Remaining will be picked up on the next run."
        )

    # ── Phase 1: Fetch all new articles ─────────────────────────────
    fetched_articles = []
    fetch_errors = 0
    for ref in [r for r in discovered if r.url in to_process]:
        polite_sleep(cfg.request_delay_seconds)
        article = fetch_article(ref.url, fetch_image=True, source=ref.source)
        if not article:
            fetch_errors += 1
            continue
        fetched_articles.append(article)

    if not fetched_articles:
        logger.error("No articles fetched successfully; aborting Gemini call")
        record_run(
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            discovered=len(discovered_urls), new_articles=len(new_urls),
            processed=0, telegram_sent=0, errors=fetch_errors, gemini_calls=0,
        )
        # Still mark these as seen so we don't keep retrying broken URLs
        seen = load_seen_urls()
        seen.update(to_process)
        save_seen_urls(seen)
        upload_state()  # sync seen-URL update + run record to HF Hub
        return {"discovered": len(discovered_urls), "new": len(new_urls),
                "processed": 0, "sent": 0, "errors": fetch_errors, "gemini_calls": 0}

    # ── Phase 2: Quota check + ONE batched Gemini call ─────────────
    gemini_calls_used = 0
    calls_today = get_gemini_calls_today()
    if calls_today >= cfg.gemini_safety_threshold:
        logger.warning(
            f"Gemini daily safety threshold reached ({calls_today}/{cfg.gemini_daily_limit}); "
            f"skipping batch. Articles will be retried next run."
        )
        results = [
            {"category": None, "status": None, "summary": None}
            for _ in fetched_articles
        ]
    else:
        article_dicts = [
            {
                "title": a.title,
                "author": a.author,
                "published_at": a.published_at,
                "url": a.url,
                "source": a.source,
                "body_text": a.body_text,
            }
            for a in fetched_articles
        ]
        results = summarize_batch(article_dicts)
        gemini_calls_used = 1
        increment_gemini_calls(n=1)

    # ── Phase 3: Send each article to Telegram ─────────────────────
    sent = 0
    send_errors = 0
    for article, result in zip(fetched_articles, results):
        category = result.get("category")
        status = result.get("status")
        summary = result.get("summary")

        # Record in DB regardless (so we don't reprocess)
        record_article(
            url=article.url,
            title=article.title,
            author=article.author,
            published_at=article.published_at,
            summary=summary or "(no summary)",
            category=category,
            source=article.source,
        )

        # If Gemini said SKIP, send only the photo+caption (no summary text)
        if status == "SKIP":
            logger.info(f"Article SKIP-ped by Gemini: {article.url}")
            if send_article(
                {"title": article.title, "author": article.author,
                 "published_at": article.published_at, "url": article.url,
                 "image_url": article.image_url, "image_bytes": article.image_bytes,
                 "source": article.source},
                summary=None,
                category=category,
            ):
                sent += 1
            else:
                send_errors += 1
            continue

        if not summary:
            send_errors += 1
            continue

        article_dict = {
            "title": article.title,
            "author": article.author,
            "published_at": article.published_at,
            "url": article.url,
            "image_url": article.image_url,
            "image_bytes": article.image_bytes,
            "source": article.source,
        }
        if send_article(article_dict, summary, category=category):
            sent += 1
        else:
            send_errors += 1

    # ── Final state save ────────────────────────────────────────────
    seen = load_seen_urls()
    seen.update(to_process)
    save_seen_urls(seen)

    finished_at = datetime.now(timezone.utc).isoformat()
    total_errors = fetch_errors + send_errors
    record_run(
        started_at=started_at,
        finished_at=finished_at,
        discovered=len(discovered_urls),
        new_articles=len(new_urls),
        processed=len(fetched_articles),
        telegram_sent=sent,
        errors=total_errors,
        gemini_calls=gemini_calls_used,
    )

    summary = {
        "started_at": started_at,
        "finished_at": finished_at,
        "discovered": len(discovered_urls),
        "new": len(new_urls),
        "processed": len(fetched_articles),
        "sent": sent,
        "errors": total_errors,
        "gemini_calls": gemini_calls_used,
        "gemini_calls_today": get_gemini_calls_today(),
    }
    logger.info(f"=== Pipeline run finished: {summary} ===")
    upload_state()  # sync state.db + seen.json to HF Hub (survives Space restart)
    return summary
