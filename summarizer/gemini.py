"""
Gemini 3.5 Flash wrapper — batched summarization in ONE API call.

Strategy:
- All new articles from a single pipeline run go into ONE prompt
- Gemini classifies each (model_launch / infra_upgrade / core_logic /
  functional_update / research / policy / business / other)
- For model launches: extracts company, model name, benchmarks,
  abilities, pricing, availability
- For other categories: produces a focused summary
- Pure opinion/culture pieces are skipped via [SKIPPED] marker
- One call = one quota unit regardless of article count
"""
from __future__ import annotations

import re
from typing import Any

import google.generativeai as genai
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import cfg
from utils import logger


_BATCH_PROMPT_TEMPLATE = """You are an expert AI industry analyst writing a daily intelligence briefing for a technical reader who needs deep, structured detail on AI model launches and infrastructure upgrades.

Below are {n} news articles from The Deep View (a daily AI publication). For EACH article, classify it and produce a structured summary.

CLASSIFICATION CATEGORIES (pick the best fit):
- model_launch       : A new AI model is being released (e.g., GPT-5.6, Claude Sonnet 5, Gemini 3.5)
- infra_upgrade      : Infrastructure announcement (data centers, chips, networking, power, capacity)
- core_logic         : Algorithmic/architectural change (new training method, reasoning approach, RL technique)
- functional_update  : Feature rollout in an existing product (new tool, integration, capability expansion)
- research           : Research paper or scientific breakthrough
- policy             : Regulation, governance, safety, government action
- business           : Funding, IPOs, M&A, partnerships, market dynamics
- other              : Anything else

OUTPUT FORMAT — use these EXACT delimiters. Do NOT add any text before the first "=== ARTICLE 1 ===" marker or after the last article. For each article, output exactly:

=== ARTICLE {idx} ===
CATEGORY: <one of the categories above>
STATUS: <SUMMARIZE | SKIP>
{structured_content}
=== END ARTICLE {idx} ===

RULES FOR STATUS:
- SKIP if the article is pure opinion, culture, lifestyle, or has no technical/business substance
- SUMMARIZE for everything else

RULES FOR structured_content (depends on category):

If CATEGORY = model_launch AND STATUS = SUMMARIZE, output these sections:
### TL;DR
One sentence capturing the most important takeaway.

### Model Details
- Model name: <full name with variant (e.g., GPT-5.6 Sol)>
- Company: <company name>
- Variants: <list variants if any, with positioning (flagship/balanced/fast)>
- Context window: <token count if mentioned, else "not disclosed">
- Modalities: <text/vision/audio/video/code>

### Benchmarks
- <Benchmark name>: <score> (vs <competitor>: <score> if mentioned)
- One bullet per benchmark mentioned in the article

### Key Abilities
- 3-5 bullet points describing what the model can do that's new or improved

### Pricing & Availability
- Pricing: <per 1M tokens or subscription, if mentioned>
- Availability: <API, chatbot, enterprise, waitlist, etc.>
- Licensing: <open/closed, if mentioned>

### Why It Matters
2-3 sentences on industry impact, who is affected, what changes.

If CATEGORY in (infra_upgrade, core_logic, functional_update) AND STATUS = SUMMARIZE:
### TL;DR
One sentence.
### What Changed
3-5 bullet points, concrete and technical.
### Impact
2-3 sentences on affected products, users, industry direction.

If CATEGORY in (research, policy, business, other) AND STATUS = SUMMARIZE:
### TL;DR
One sentence.
### Key Points
3-5 bullet points.
### Why It Matters
2-3 sentences.

If STATUS = SKIP:
<no other content — just the STATUS line>

---
ARTICLES TO SUMMARIZE:

{articles_block}
"""


def _format_article_block(idx: int, article: dict) -> str:
    return (
        f"=== ARTICLE {idx} ===\n"
        f"TITLE: {article['title']}\n"
        f"AUTHOR: {article['author'] or 'Unknown'}\n"
        f"PUBLISHED: {article['published_at'] or 'unknown'}\n"
        f"URL: {article['url']}\n\n"
        f"TEXT:\n{article['body_text'][:12000]}\n"
    )


def build_batch_prompt(articles: list[dict]) -> str:
    """Build the batched summarization prompt. Exposed for testing."""
    articles_block = "\n\n".join(
        _format_article_block(i + 1, a) for i, a in enumerate(articles)
    )
    # Use replace() instead of .format() because the template contains many literal
    # braces (e.g., <full name with variant (e.g., GPT-5.6 Sol)>) that would break format().
    prompt = _BATCH_PROMPT_TEMPLATE.replace("{n}", str(len(articles)))
    prompt = prompt.replace("{articles_block}", articles_block)
    return prompt


def parse_batch_response(response_text: str, expected_count: int) -> list[dict]:
    """
    Parse Gemini's batched response into per-article structured results.

    Returns a list of length `expected_count`. Each item is a dict with keys:
        category: str | None
        status:   "SUMMARIZE" | "SKIP" | None
        summary:  str | None  (None if SKIP or parse failure)
    """
    # Split on "=== ARTICLE N ===" markers
    # Regex captures: the article number, then everything until the next marker or END
    pattern = re.compile(
        r"===\s*ARTICLE\s*(\d+)\s*===\s*\n(.*?)(?===\s*(?:ARTICLE\s*\d+|END))",
        re.DOTALL,
    )
    results_by_idx: dict[int, dict] = {}
    for m in pattern.finditer(response_text):
        try:
            idx = int(m.group(1))
        except ValueError:
            continue
        body = m.group(2).strip()
        # Trim trailing "=== END ARTICLE N ===" if present
        body = re.sub(r"===\s*END\s*ARTICLE\s*\d+\s*===\s*$", "", body).strip()

        # Extract CATEGORY and STATUS lines
        cat_match = re.search(r"^CATEGORY:\s*(.+?)$", body, re.MULTILINE)
        status_match = re.search(r"^STATUS:\s*(.+?)$", body, re.MULTILINE)
        category = cat_match.group(1).strip() if cat_match else None
        status = status_match.group(1).strip().upper() if status_match else "SUMMARIZE"

        # If SKIP, no summary
        if status == "SKIP":
            results_by_idx[idx] = {
                "category": category,
                "status": "SKIP",
                "summary": None,
            }
            continue

        # The summary is the body with the CATEGORY and STATUS lines removed
        summary = re.sub(r"^CATEGORY:\s*.+?\n", "", body, flags=re.MULTILINE)
        summary = re.sub(r"^STATUS:\s*.+?\n", "", summary, flags=re.MULTILINE).strip()

        if len(summary) < 50:
            # Too short, probably a parse failure
            results_by_idx[idx] = {
                "category": category,
                "status": status,
                "summary": None,
            }
        else:
            results_by_idx[idx] = {
                "category": category,
                "status": status,
                "summary": summary,
            }

    # Return in order 1..expected_count, with None for any missing
    return [
        results_by_idx.get(i, {"category": None, "status": None, "summary": None})
        for i in range(1, expected_count + 1)
    ]


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    reraise=True,
)
def _call_gemini(model, prompt: str) -> str:
    response = model.generate_content(prompt)
    try:
        return response.text
    except ValueError:
        if response.candidates:
            return "".join(
                part.text
                for c in response.candidates
                for part in (c.content.parts or [])
            )
        return ""


def summarize_batch(articles: list[dict]) -> list[dict]:
    """
    Summarize N articles in ONE Gemini API call.
    Returns a list of dicts (same length as `articles`):
        {category, status, summary}
    On total failure, returns list of None-dicts so caller can handle gracefully.
    """
    if not articles:
        return []

    prompt = build_batch_prompt(articles)

    # Token budget sanity check (~4 chars/token, leave 200K for output)
    estimated_input_tokens = len(prompt) // 4
    if estimated_input_tokens > 800_000:
        logger.warning(
            f"Batch prompt is ~{estimated_input_tokens} tokens; "
            "consider reducing gemini_batch_max_articles"
        )

    for model_name in (cfg.gemini_primary_model, cfg.gemini_fallback_model):
        try:
            genai.configure(api_key=cfg.gemini_api_key)
            model = genai.GenerativeModel(
                model_name=model_name,
                generation_config={
                    "temperature": cfg.gemini_temperature,
                    "max_output_tokens": cfg.gemini_max_output_tokens,
                    "top_p": 0.95,
                },
            )
            logger.info(
                f"Calling Gemini model={model_name} for batch of {len(articles)} articles "
                f"(~{estimated_input_tokens} input tokens)"
            )
            response_text = _call_gemini(model, prompt)
            if not response_text or len(response_text) < 100:
                logger.warning(f"Empty response from {model_name}")
                continue

            results = parse_batch_response(response_text, expected_count=len(articles))
            successful = sum(1 for r in results if r.get("summary") or r.get("status") == "SKIP")
            logger.info(
                f"Gemini batch returned {successful}/{len(articles)} valid results"
            )

            # Accept if at least half parsed successfully
            if successful >= max(1, len(articles) // 2):
                return results

            logger.warning(
                f"Parsing yielded only {successful}/{len(articles)} results; trying fallback"
            )
        except Exception as e:
            logger.warning(f"Gemini batch call failed on {model_name}: {e}")
            continue

    # Total failure — return empty results so pipeline can record articles as failed
    logger.error("All Gemini models failed for batch")
    return [
        {"category": None, "status": None, "summary": None}
        for _ in articles
    ]
