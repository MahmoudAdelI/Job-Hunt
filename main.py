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

# LinkedIn search queries for Egypt
SEARCH_QUERIES = [
    {"keywords": ".NET", "location": "Egypt"},
    {"keywords": "C#", "location": "Egypt"},
    {"keywords": "ASP.NET", "location": "Egypt"},
    {"keywords": "Software Engineer .NET", "location": "Egypt"},
    {"keywords": "Full Stack .NET", "location": "Egypt"},
    {"keywords": "Backend .NET", "location": "Egypt"},
    {"keywords": "C# developer", "location": "Cairo, Egypt"},
]

# Expanded Tech Keywords
TECH_KEYWORDS = [
    "c#", ".net", "asp.net", "dotnet", "dot net",
    ".net core", "ef core", "entity framework",
    "blazor", "web api", "microservices", "aspnet",
]

# Expanded Role Keywords
ROLE_KEYWORDS = [
    "developer", "backend", "back-end", "fullstack", "full-stack",
    "engineer", "software", "architect", "lead", "senior",
    "junior", "mid", "consultant", "programmer", "coder",
]

# Egypt Location Allowlist — ONLY keep jobs that match Egypt
EGYPT_LOCATIONS = [
    "egypt", "cairo", "giza", "maadi", "smart village",
    "october", "6th of october", "6 october", "nasr city",
    "heliopolis", "mansoura", "new cairo", "sheikh zayed",
]

# DuckDuckGo search queries for LinkedIn posts (using site:linkedin.com/posts)
POST_SEARCH_QUERIES = [
    'site:linkedin.com/posts ".NET" developer Egypt hiring',
    'site:linkedin.com/posts "C#" developer Cairo hiring',
    'site:linkedin.com/posts dotnet Egypt hiring',
    'site:linkedin.com/posts "ASP.NET" Egypt hiring',
]

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


def build_linkedin_url(query: dict) -> str:
    """
    Build a LinkedIn job search URL for the given query dict.
    Filters to jobs posted in the last 24 hours (f_TPR=r86400).
    Includes geoId=102007122 for Egypt location searches.
    """
    keywords = quote_plus(query.get("keywords", ""))
    location = quote_plus(query.get("location", ""))
    url = (
        f"https://www.linkedin.com/jobs/search/"
        f"?keywords={keywords}&location={location}&geoId=102007122&f_TPR=r86400"
    )
    if "f_WT" in query:
        url += f"&f_WT={query['f_WT']}"
    return url


# ========================== JOB LISTING SCRAPING ===========================


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


# ========================== LINKEDIN POST SCRAPING (via DuckDuckGo) ========


def build_ddg_search_url(query: str) -> str:
    """
    Build a DuckDuckGo HTML search URL for finding LinkedIn posts.
    """
    encoded = quote_plus(query)
    return f"https://html.duckduckgo.com/html/?q={encoded}"


def fetch_linkedin_posts(query: str, attempt: int = 1) -> list[dict]:
    """
    Search DuckDuckGo HTML for LinkedIn posts matching the query using POST form submit.
    Extracts post URLs, author names, and text snippets from results.

    Returns a list of dicts: {author, snippet, url}
    """
    url = "https://html.duckduckgo.com/html/"
    max_attempts = 2
    ddg_headers = {
        **HEADERS,
        "Origin": "https://html.duckduckgo.com",
        "Referer": "https://html.duckduckgo.com/",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        logger.info("Fetching posts via DuckDuckGo POST: %s (attempt %d)", query, attempt)
        response = requests.post(url, data={"q": query, "b": ""}, headers=ddg_headers, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("HTTP error fetching DuckDuckGo results: %s", exc)
        if attempt < max_attempts:
            backoff = 2 ** attempt + random.uniform(0, 1)
            logger.info("Retrying in %.1fs ...", backoff)
            time.sleep(backoff)
            return fetch_linkedin_posts(query, attempt + 1)
        logger.error("Giving up on DuckDuckGo query after %d attempts", max_attempts)
        return []

    return _parse_ddg_results(response.text, query)


def _parse_ddg_results(html: str, query: str) -> list[dict]:
    """
    Extract LinkedIn post links and snippets from DuckDuckGo HTML search results.
    Only keeps results that link to linkedin.com/posts/, /feed/update/, or /pulse/.
    """
    soup = BeautifulSoup(html, "lxml")
    posts: list[dict] = []

    # DuckDuckGo HTML search results use <a> tags with class result__a or result__url
    for link in soup.find_all("a", href=True):
        href = link["href"]
        clean_url = _extract_clean_linkedin_url(href)
        if not clean_url:
            continue

        title_text = link.get_text(strip=True)

        # Look for snippet in parent result container
        parent = link.find_parent(["div", "td", "tr"])
        snippet_el = (
            parent.find("a", class_=re.compile(r"snippet"))
            or parent.find("div", class_=re.compile(r"snippet"))
            or parent.find("td", class_=re.compile(r"snippet"))
        ) if parent else None

        snippet = snippet_el.get_text(strip=True) if snippet_el else title_text

        author = _extract_author_from_url(clean_url)
        if not author and title_text:
            author = title_text.split(" - ")[0].split(" on ")[0].strip()

        posts.append({
            "author": author or "Unknown",
            "snippet": snippet[:200] if snippet else "",
            "url": clean_url,
        })

    # Deduplicate by URL within this batch
    seen = set()
    unique_posts = []
    for p in posts:
        if p["url"] not in seen:
            seen.add(p["url"])
            unique_posts.append(p)

    logger.info("Found %d LinkedIn posts via DuckDuckGo for query: %s", len(unique_posts), query)
    return unique_posts


def _extract_clean_linkedin_url(href: str) -> str | None:
    """
    Extract and clean a LinkedIn post URL from a result link.
    Handles DuckDuckGo redirects (/l/?uddg=...) and Google redirects (/url?q=...).
    Returns None if the URL is not a LinkedIn post/feed update/article.
    """
    from urllib.parse import urlparse, parse_qs, unquote

    # DuckDuckGo redirect format: /l/?uddg=https%3A%2F%2Fwww.linkedin.com%2Fposts%2F...
    if "uddg=" in href:
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        if "uddg" in params:
            href = params["uddg"][0]

    # Google redirect format
    if "/url?" in href:
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        if "q" in params:
            href = params["q"][0]

    href = unquote(href)

    # Keep LinkedIn posts, feed updates, or pulse articles
    is_post_link = (
        "linkedin.com/posts/" in href
        or "linkedin.com/feed/update/" in href
        or "linkedin.com/pulse/" in href
    )
    if not is_post_link:
        return None

    # Strip tracking query params
    if "?" in href:
        href = href.split("?")[0]

    return href


def _extract_author_from_url(url: str) -> str:
    """
    Extract the author name from a LinkedIn post URL.
    URLs follow the pattern: linkedin.com/posts/author-name_rest-of-slug
    """
    try:
        slug = url.split("/posts/")[1]
        author_slug = slug.split("_")[0].split("-activity")[0]
        return author_slug.replace("-", " ").title()
    except (IndexError, AttributeError):
        return "Unknown"


def post_matches_tech_keywords(post: dict) -> bool:
    """
    Check if a LinkedIn post mentions at least one tech keyword.
    """
    text = " ".join([
        post.get("author", ""),
        post.get("snippet", ""),
    ]).lower()

    return any(_keyword_pattern(kw).search(text) for kw in TECH_KEYWORDS)


# ========================== KEYWORD FILTERING ==============================


def _keyword_pattern(keyword: str) -> re.Pattern:
    """
    Build a word-boundary-aware regex for a keyword.
    Handles special characters like '#' and '.' in tech terms.
    """
    escaped = re.escape(keyword)
    # Use \b on the alphabetical side, but be flexible around symbols
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


def fetch_job_description(job_url: str) -> tuple[str, str]:
    """
    Fetch full job description and snippet using LinkedIn's public guest API.
    URL format: https://www.linkedin.com/jobs/view/{job_id} -> API: /jobs-guest/jobs/api/jobPosting/{job_id}

    Returns tuple of (description_text, snippet_preview).
    """
    job_id_match = re.search(r"/view/(?:[^\-/]+-)?(\d+)", job_url) or re.search(r"(\d{8,})", job_url)
    if not job_id_match:
        return "", ""

    job_id = job_id_match.group(1)
    api_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

    try:
        resp = requests.get(api_url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "lxml")
            desc_el = (
                soup.find("div", class_=re.compile(r"show-more-less-html__markup"))
                or soup.find("section", class_=re.compile(r"description"))
                or soup
            )
            text = desc_el.get_text(separator=" ", strip=True) if desc_el else ""
            snippet = text[:200] if text else ""
            return text, snippet
    except Exception as exc:
        logger.debug("Failed to fetch description for job %s: %s", job_id, exc)

    return "", ""


def matches_keywords(job: dict) -> tuple[bool, list[str], list[str]]:
    """
    Check if a job matches keyword filter criteria.

    Returns tuple: (is_match, matched_tech_keywords, matched_role_keywords).
    """
    text = " ".join([
        job.get("title", ""),
        job.get("company", ""),
        job.get("location", ""),
        job.get("description", ""),
    ]).lower()

    matched_tech = [
        kw for kw in TECH_KEYWORDS
        if _keyword_pattern(kw).search(text)
    ]
    matched_roles = [
        kw for kw in ROLE_KEYWORDS
        if _keyword_pattern(kw).search(text)
    ]

    is_match = bool(matched_tech) and bool(matched_roles)
    return is_match, matched_tech, matched_roles


def matches_location(job: dict) -> bool:
    """
    Check if a job is located in Egypt or is a remote position.

    1. Accepts jobs explicitly in Egyptian locations.
    2. Rejects jobs hosted on non-Egypt country domain subdomains (sa.linkedin.com, ae.linkedin.com, etc.).
    3. Rejects jobs in foreign countries, cities, or states (Saudi, UAE, USA, UK, India, etc.).
    4. Accepts remote/worldwide jobs that do not belong to an excluded foreign location.
    """
    title = job.get("title", "").lower()
    location = job.get("location", "").lower()
def matches_location(job: dict) -> bool:
    """
    EGYPT ONLY ALLOWLIST:

    Only accepts a job if it is explicitly located in an Egyptian city/region
    (EGYPT_LOCATIONS) or hosted on eg.linkedin.com domain.
    """
    title = job.get("title", "").lower()
    location = job.get("location", "").lower()
    url = job.get("url", "").lower()
    full_text = f"{title} {location}"

    is_egypt_city = any(loc in full_text for loc in EGYPT_LOCATIONS)
    is_egypt_domain = "eg.linkedin.com" in url

    return is_egypt_city or is_egypt_domain


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
    Includes matched keywords and description snippet if available.
    """
    parts = [
        "🆕 <b>New .NET Job Alert!</b>",
        "",
        f"💼 <b>{_escape_html(job['title'])}</b>",
        f"🏢 {_escape_html(job['company'])}",
    ]

    if job.get("location"):
        parts.append(f"📍 {_escape_html(job['location'])}")

    matched_tech = job.get("matched_tech", [])
    matched_roles = job.get("matched_roles", [])
    if matched_tech or matched_roles:
        matched_str = ", ".join([kw.upper() for kw in (matched_tech[:2] + matched_roles[:2])])
        parts.append(f"🏷️ <b>Matched:</b> {_escape_html(matched_str)}")

    snippet = job.get("snippet", "")
    if snippet:
        # Keep snippet concise for Telegram
        clean_snippet = snippet[:180] + ("..." if len(snippet) > 180 else "")
        parts.append(f"📝 <i>{_escape_html(clean_snippet)}</i>")

    parts.append(f"🔗 <a href=\"{job['url']}\">View Job</a>")

    return "\n".join(parts)


def format_post_message(post: dict) -> str:
    """
    Format a LinkedIn post into a Telegram-friendly message.
    Uses a distinct style to differentiate from formal job listings.
    """
    snippet = post.get("snippet", "")
    # Truncate long snippets to keep messages readable
    if len(snippet) > 150:
        snippet = snippet[:147] + "..."

    parts = [
        "📢 <b>New .NET Job Post Found!</b>",
        "",
        f"👤 <b>{_escape_html(post['author'])}</b>",
    ]

    if snippet:
        parts.append(f"📝 {_escape_html(snippet)}")

    parts.append(f"🔗 <a href=\"{post['url']}\">View Post</a>")

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
    Supports a single chat ID / channel handle or a comma-separated list of targets
    (e.g., '12345678, @DotNetEgyptJobs').
    Returns True if sent to at least one target, False otherwise.
    """
    targets = [cid.strip() for cid in chat_id.split(",") if cid.strip()]
    if not targets:
        logger.error("No valid chat IDs provided in TELEGRAM_CHAT_ID")
        return False

    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    any_success = False

    for target in targets:
        payload = {
            "chat_id": target,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }

        try:
            resp = requests.post(api_url, json=payload, timeout=15)
            if resp.status_code == 200:
                any_success = True
            else:
                logger.error(
                    "Telegram API error %d for target %s: %s",
                    resp.status_code, target, resp.text[:200]
                )
        except requests.RequestException as exc:
            logger.error("Failed to send Telegram message to %s: %s", target, exc)

    return any_success


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

    # --- Step 3: Fetch, filter, deduplicate JOB LISTINGS ---
    total_fetched = 0
    total_passed_filter = 0
    new_jobs: list[dict] = []

    for query in SEARCH_QUERIES:
        url = build_linkedin_url(query)
        jobs = fetch_jobs(url)
        total_fetched += len(jobs)

        for job in jobs:
            if not matches_location(job):
                continue

            if job["url"] in seen_urls:
                continue

            # Fetch full description for unseen location-matching jobs
            desc, snippet = fetch_job_description(job["url"])
            job["description"] = desc
            job["snippet"] = snippet

            is_match, matched_tech, matched_roles = matches_keywords(job)
            if not is_match:
                logger.debug("Job failed keyword filter: %s", job["title"])
                continue

            job["matched_tech"] = matched_tech
            job["matched_roles"] = matched_roles
            total_passed_filter += 1
            new_jobs.append(job)
            seen_urls.add(job["url"])

        # Random delay between queries to be polite
        delay = random.uniform(1, 3)
        logger.info("Waiting %.1fs before next query...", delay)
        time.sleep(delay)

    # --- Step 3b: Fetch, filter, deduplicate LINKEDIN POSTS ---
    total_posts_fetched = 0
    total_posts_passed = 0
    new_posts: list[dict] = []

    for query in POST_SEARCH_QUERIES:
        posts = fetch_linkedin_posts(query)
        total_posts_fetched += len(posts)

        for post in posts:
            if not post_matches_tech_keywords(post):
                continue
            total_posts_passed += 1

            if post["url"] in seen_urls:
                continue

            new_posts.append(post)
            seen_urls.add(post["url"])

        # Random delay between DuckDuckGo queries to avoid rate limits
        delay = random.uniform(2, 4)
        logger.info("Waiting %.1fs before next DuckDuckGo query...", delay)
        time.sleep(delay)

    # --- Step 4: Send notifications ---
    sent_count = 0

    # Send job listing alerts
    for i, job in enumerate(new_jobs):
        message = format_job_message(job)
        if send_telegram_message(message, token, chat_id):
            sent_count += 1
        else:
            logger.warning("Failed to send alert for: %s", job["title"])
        # 1-second delay between messages to respect Telegram rate limits
        if i < len(new_jobs) - 1 or new_posts:
            time.sleep(1)

    # Send post alerts
    for i, post in enumerate(new_posts):
        message = format_post_message(post)
        if send_telegram_message(message, token, chat_id):
            sent_count += 1
        else:
            logger.warning("Failed to send alert for post by: %s", post["author"])
        if i < len(new_posts) - 1:
            time.sleep(1)

    # --- Step 5: Persist seen jobs ---
    now = datetime.now(timezone.utc).isoformat()
    for job in new_jobs:
        seen_jobs.append({"url": job["url"], "seen_at": now})
    for post in new_posts:
        seen_jobs.append({"url": post["url"], "seen_at": now})
    save_seen_jobs(seen_jobs)

    # --- Step 6: Summary ---
    total_new = len(new_jobs) + len(new_posts)
    logger.info("=" * 50)
    logger.info("RUN SUMMARY")
    logger.info("=" * 50)
    logger.info("Job queries executed:  %d", len(SEARCH_QUERIES))
    logger.info("Jobs fetched:          %d", total_fetched)
    logger.info("Jobs passed filter:    %d", total_passed_filter)
    logger.info("New job listings:      %d", len(new_jobs))
    logger.info("Post queries executed: %d", len(POST_SEARCH_QUERIES))
    logger.info("Posts fetched:         %d", total_posts_fetched)
    logger.info("Posts passed filter:   %d", total_posts_passed)
    logger.info("New posts:             %d", len(new_posts))
    logger.info("Telegram alerts sent:  %d", sent_count)
    logger.info("Seen entries in file:  %d", len(seen_jobs))
    logger.info("=" * 50)

    if total_new == 0:
        logger.info("No new matching jobs or posts found this run. Nothing to do.")


if __name__ == "__main__":
    main()
