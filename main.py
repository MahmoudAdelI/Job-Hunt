"""
Job Alert Automation Script
============================
Scrapes LinkedIn job search results for .NET-related positions,
filters by keyword groups, deduplicates against previously seen jobs,
and sends new matches to a Telegram bot.

Designed to run on a GitHub Actions scheduled workflow (every 2 hours).
"""

import json
import logging
import os
import re
import sys
import time
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration — edit these to customise your alerts
# ---------------------------------------------------------------------------

# LinkedIn search queries (first page only, last 24 hours)
SEARCH_QUERIES = [
    ".NET developer Egypt",
    "ASP.NET backend Egypt",
    "C# fullstack Egypt",
    "remote .NET developer",
]

# A job must match at least ONE keyword from EACH group
TECH_KEYWORDS = ["c#", ".net", "asp.net", "dotnet"]
ROLE_KEYWORDS = ["developer", "backend", "fullstack", "full-stack", "engineer", "software"]

# Deduplication settings
SEEN_JOBS_FILE = "seen_jobs.json"
RETENTION_DAYS = 30

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Realistic browser headers to reduce chance of being blocked
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "DNT": "1",
}


# ========================== URL BUILDING ===================================


def build_linkedin_url(query: str) -> str:
    """
    Build a LinkedIn job search URL for the given query.
    Filters to jobs posted in the last 24 hours (f_TPR=r86400).
    """
    encoded = quote_plus(query)
    return (
        f"https://www.linkedin.com/jobs/search/"
        f"?keywords={encoded}&f_TPR=r86400"
    )


# ========================== SCRAPING =======================================


def fetch_jobs(url: str, attempt: int = 1) -> list[dict]:
    """
    Fetch job listings from a LinkedIn search URL.

    Parses the public HTML page and extracts job cards.
    Retries once on failure with exponential backoff.

    Returns a list of dicts: {title, company, location, url}
    """
    max_attempts = 2
    try:
        logger.info("Fetching: %s (attempt %d)", url, attempt)
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("HTTP error fetching %s: %s", url, exc)
        if attempt < max_attempts:
            backoff = 2 ** attempt + random.uniform(0, 1)
            logger.info("Retrying in %.1fs ...", backoff)
            time.sleep(backoff)
            return fetch_jobs(url, attempt + 1)
        logger.error("Giving up on %s after %d attempts", url, max_attempts)
        return []

    return _parse_job_listings(response.text, url)


def _parse_job_listings(html: str, source_url: str) -> list[dict]:
    """
    Extract job listings from LinkedIn search results HTML.

    LinkedIn's public job search page renders job cards inside
    <ul class="jobs-search__results-list"> with <li> children.
    Each card contains structured data we can pull out.
    """
    soup = BeautifulSoup(html, "lxml")
    jobs: list[dict] = []

    # LinkedIn public search uses a results list with base-card items
    job_cards = soup.find_all("div", class_="base-card")

    if not job_cards:
        # Fallback: try the older markup pattern
        job_cards = soup.find_all("li", class_=re.compile(r"result-card"))

    if not job_cards:
        logger.warning("No job cards found in HTML from %s", source_url)
        return jobs

    for card in job_cards:
        try:
            job = _extract_job_from_card(card)
            if job and job.get("url"):
                jobs.append(job)
        except Exception as exc:
            logger.debug("Failed to parse a job card: %s", exc)
            continue

    logger.info("Parsed %d jobs from %s", len(jobs), source_url)
    return jobs


def _extract_job_from_card(card) -> dict | None:
    """
    Extract job details from a single LinkedIn job card element.
    Returns a dict with title, company, location, url — or None on failure.
    """
    # Title — usually in an <h3> or <a> with specific classes
    title_el = (
        card.find("h3", class_=re.compile(r"base-search-card__title"))
        or card.find("span", class_=re.compile(r"sr-only"))
        or card.find("h3")
    )
    title = title_el.get_text(strip=True) if title_el else ""

    # Company name
    company_el = (
        card.find("h4", class_=re.compile(r"base-search-card__subtitle"))
        or card.find("a", class_=re.compile(r"hidden-nested-link"))
        or card.find("h4")
    )
    company = company_el.get_text(strip=True) if company_el else "Unknown"

    # Location
    location_el = card.find("span", class_=re.compile(r"job-search-card__location"))
    location = location_el.get_text(strip=True) if location_el else ""

    # Job URL — the main <a> link on the card
    link_el = card.find("a", href=True)
    url = link_el["href"].strip() if link_el else ""

    # Clean the URL: remove tracking query params, keep the canonical path
    if url and "?" in url:
        url = url.split("?")[0]

    if not title and not url:
        return None

    return {
        "title": title,
        "company": company,
        "location": location,
        "url": url,
    }


# ========================== KEYWORD FILTERING ==============================


def _keyword_pattern(keyword: str) -> re.Pattern:
    """
    Build a word-boundary-aware regex for a keyword.
    Handles special characters like '#' and '.' in tech terms.
    """
    escaped = re.escape(keyword)
    # Use \b on the alphabetical side, but be flexible around symbols
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


def matches_keywords(job: dict) -> bool:
    """
    Check if a job matches the keyword filter criteria.

    A job passes if the combined text (title + company + location)
    contains at least one keyword from TECH_KEYWORDS
    AND at least one keyword from ROLE_KEYWORDS.

    Matching is case-insensitive and word-boundary-aware.
    """
    text = " ".join([
        job.get("title", ""),
        job.get("company", ""),
        job.get("location", ""),
    ]).lower()

    has_tech = any(_keyword_pattern(kw).search(text) for kw in TECH_KEYWORDS)
    has_role = any(_keyword_pattern(kw).search(text) for kw in ROLE_KEYWORDS)

    return has_tech and has_role


# ========================== DEDUPLICATION ==================================


def load_seen_jobs() -> list[dict]:
    """
    Load the seen-jobs list from disk.
    Returns a list of {"url": str, "seen_at": str} dicts.
    Returns [] if the file is missing or corrupt.
    """
    if not os.path.exists(SEEN_JOBS_FILE):
        return []

    try:
        with open(SEEN_JOBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Handle legacy format: plain list of URL strings
        if data and isinstance(data[0], str):
            logger.info("Migrating legacy seen_jobs format to timestamped entries")
            now = datetime.now(timezone.utc).isoformat()
            data = [{"url": url, "seen_at": now} for url in data]
        return data
    except (json.JSONDecodeError, IOError) as exc:
        logger.warning("Could not load %s: %s — starting fresh", SEEN_JOBS_FILE, exc)
        return []


def save_seen_jobs(seen: list[dict]) -> None:
    """Save the seen-jobs list to disk with readable formatting."""
    with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)
    logger.info("Saved %d entries to %s", len(seen), SEEN_JOBS_FILE)


def prune_old_entries(seen: list[dict]) -> list[dict]:
    """
    Remove entries older than RETENTION_DAYS to keep the file lean.
    Entries without a valid timestamp are kept (safe fallback).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    kept = []
    pruned_count = 0

    for entry in seen:
        try:
            seen_at = datetime.fromisoformat(entry["seen_at"])
            if seen_at >= cutoff:
                kept.append(entry)
            else:
                pruned_count += 1
        except (KeyError, ValueError):
            kept.append(entry)  # Keep entries with bad/missing timestamps

    if pruned_count:
        logger.info("Pruned %d entries older than %d days", pruned_count, RETENTION_DAYS)

    return kept


# ========================== TELEGRAM NOTIFICATIONS =========================


def get_telegram_credentials() -> tuple[str, str]:
    """
    Read Telegram credentials from environment variables.
    Exits with a clear error if either is missing.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        logger.error(
            "Missing Telegram credentials. "
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables."
        )
        sys.exit(1)

    return token, chat_id


def format_job_message(job: dict) -> str:
    """
    Format a job listing into a Telegram-friendly message.
    Uses emoji for visual appeal.
    """
    parts = [
        "🆕 <b>New .NET Job Alert!</b>",
        "",
        f"💼 <b>{_escape_html(job['title'])}</b>",
        f"🏢 {_escape_html(job['company'])}",
    ]

    if job.get("location"):
        parts.append(f"📍 {_escape_html(job['location'])}")

    parts.append(f"🔗 <a href=\"{job['url']}\">View Job</a>")

    return "\n".join(parts)


def _escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def send_telegram_message(text: str, token: str, chat_id: str) -> bool:
    """
    Send a message via the Telegram Bot API.
    Returns True on success, False on failure.
    """
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        resp = requests.post(api_url, json=payload, timeout=15)
        if resp.status_code == 200:
            return True
        else:
            logger.error(
                "Telegram API error %d: %s", resp.status_code, resp.text[:200]
            )
            return False
    except requests.RequestException as exc:
        logger.error("Failed to send Telegram message: %s", exc)
        return False


# ========================== MAIN ORCHESTRATION =============================


def main() -> None:
    """
    Main entry point — orchestrates the full pipeline:
    1. Validate Telegram credentials
    2. Load seen jobs & prune old entries
    3. For each query: fetch → filter → deduplicate
    4. Send Telegram notifications for new matches
    5. Save updated seen-jobs list
    6. Print summary
    """
    # --- Step 1: Validate credentials early ---
    token, chat_id = get_telegram_credentials()
    logger.info("Telegram credentials loaded successfully")

    # --- Step 2: Load & prune seen jobs ---
    seen_jobs = load_seen_jobs()
    seen_jobs = prune_old_entries(seen_jobs)
    seen_urls = {entry["url"] for entry in seen_jobs}

    # --- Step 3: Fetch, filter, deduplicate ---
    total_fetched = 0
    total_passed_filter = 0
    new_jobs: list[dict] = []

    for query in SEARCH_QUERIES:
        url = build_linkedin_url(query)
        jobs = fetch_jobs(url)
        total_fetched += len(jobs)

        for job in jobs:
            if not matches_keywords(job):
                continue
            total_passed_filter += 1

            if job["url"] in seen_urls:
                continue

            new_jobs.append(job)
            seen_urls.add(job["url"])

        # Random delay between queries to be polite
        delay = random.uniform(1, 3)
        logger.info("Waiting %.1fs before next query...", delay)
        time.sleep(delay)

    # --- Step 4: Send notifications ---
    sent_count = 0
    for i, job in enumerate(new_jobs):
        message = format_job_message(job)
        if send_telegram_message(message, token, chat_id):
            sent_count += 1
        else:
            logger.warning("Failed to send alert for: %s", job["title"])

        # 1-second delay between messages to respect Telegram rate limits
        if i < len(new_jobs) - 1:
            time.sleep(1)

    # --- Step 5: Persist seen jobs ---
    now = datetime.now(timezone.utc).isoformat()
    for job in new_jobs:
        seen_jobs.append({"url": job["url"], "seen_at": now})
    save_seen_jobs(seen_jobs)

    # --- Step 6: Summary ---
    logger.info("=" * 50)
    logger.info("RUN SUMMARY")
    logger.info("=" * 50)
    logger.info("Queries executed:     %d", len(SEARCH_QUERIES))
    logger.info("Total jobs fetched:   %d", total_fetched)
    logger.info("Passed keyword filter: %d", total_passed_filter)
    logger.info("New (unseen) jobs:    %d", len(new_jobs))
    logger.info("Telegram alerts sent: %d", sent_count)
    logger.info("Seen jobs in file:    %d", len(seen_jobs))
    logger.info("=" * 50)

    if not new_jobs:
        logger.info("No new matching jobs found this run. Nothing to do.")


if __name__ == "__main__":
    main()
