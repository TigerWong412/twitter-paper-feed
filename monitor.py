#!/usr/bin/env python3
import os
import re
import json
import logging
from pathlib import Path

import requests
import tweepy
import gspread
from google.oauth2.service_account import Credentials

# ── CONFIG ─────────────────────────────────────────────────────────────────────

# Environment vars:
TW_BEARER_TOKEN    = os.environ["TW_BEARER_TOKEN"]
SPREADSHEET_NAME   = os.environ.get("Twitter nanomotorupdate", "My Research Feed")
SERVICE_ACCOUNT_FN = "service_account.json"

# How many tweets to fetch at once (up to 100)
MAX_RESULTS = 10

# Where to keep track of the last‐seen tweet
SINCE_ID_FILE = Path("since_id.txt")

# Crossref & Semantic Scholar endpoints
CROSSREF_API = "https://api.crossref.org/works/{doi}"
SSCHOLAR_API = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=abstract"

# ── LOGGING SETUP ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s"
)
logger = logging.getLogger(__name__)

# ── TWITTER FUNCTIONS ───────────────────────────────────────────────────────────

def get_last_seen_id() -> int | None:
    if SINCE_ID_FILE.exists():
        return int(SINCE_ID_FILE.read_text().strip())
    return None

def save_last_seen_id(since_id: int):
    SINCE_ID_FILE.write_text(str(since_id))

def fetch_new_tweets(username: str) -> list[tweepy.Tweet]:
    client = tweepy.Client(bearer_token=TW_BEARER_TOKEN)
    user = client.get_user(username=username).data
    params = {
        "tweet_fields": ["entities","created_at"],
        "max_results": MAX_RESULTS
    }
    last_id = get_last_seen_id()
    if last_id:
        params["since_id"] = last_id

    resp = client.get_users_tweets(id=user.id, **params)
    tweets = resp.data or []
    if tweets:
        # Tweets are in reverse‐chronological order; save the newest ID
        save_last_seen_id(tweets[0].id)
    logger.info(f"Fetched {len(tweets)} new tweet(s)")
    return tweets

# ── DOI / URL EXTRACTION ────────────────────────────────────────────────────────

def extract_first_url(tweet: tweepy.Tweet) -> str | None:
    urls = tweet.entities and tweet.entities.get("urls", [])
    if urls:
        return urls[0].get("expanded_url")
    return None

def extract_doi_from_url(url: str) -> str | None:
    m = re.search(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", url)
    return m.group(1) if m else None

def resolve_publisher_url(url: str) -> str | None:
    try:
        r = requests.head(url, allow_redirects=True, timeout=10)
        return extract_doi_from_url(r.url)
    except Exception as e:
        logger.warning(f"Failed to resolve DOI from {url}: {e}")
    return None

# ── METADATA FETCHING ───────────────────────────────────────────────────────────

def fetch_metadata_from_crossref(doi: str) -> dict:
    url = CROSSREF_API.format(doi=doi)
    r = requests.get(url, timeout=10).json()["message"]
    title   = r.get("title", [""])[0]
    journal = r.get("container-title", [""])[0]
    authors = [
        f"{a.get('given','')} {a.get('family','')}".strip()
        for a in r.get("author", [])
    ]
    # published-print first, fallback to published-online
    date = r.get("published-print") or r.get("published-online") or {}
    year = date.get("date-parts", [[None]])[0][0]
    return {
        "title":   title,
        "journal": journal,
        "authors": authors,
        "year":    year,
        "doi":     doi
    }

def fetch_abstract(doi: str) -> str:
    url = SSCHOLAR_API.format(doi=doi)
    try:
        r = requests.get(url, timeout=10).json()
        return r.get("abstract", "")
    except Exception as e:
        logger.warning(f"Failed to fetch abstract for {doi}: {e}")
    return ""

# ── GOOGLE SHEETS ────────────────────────────────────────────────────────────────

def init_sheet() -> gspread.models.Spreadsheet:
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FN,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    return gc.open(SPREADSHEET_NAME).sheet1

def append_paper_row(sheet, meta: dict, abstract: str, tweet_url: str):
    row = [
        meta["title"],
        "; ".join(meta["authors"]),
        meta["journal"],
        meta["year"],
        meta["doi"],
        abstract,
        tweet_url
    ]
    sheet.append_row(row, value_input_option="USER_ENTERED")
    logger.info(f"Appended row for DOI {meta['doi']}")

# ── MAIN PROCESS ────────────────────────────────────────────────────────────────

def process_new_papers(username: str):
    sheet = init_sheet()
    tweets = fetch_new_tweets(username=username)
    for tw in tweets:
        try:
            link = extract_first_url(tw)
            if not link:
                continue

            doi = extract_doi_from_url(link) or resolve_publisher_url(link)
            if not doi:
                logger.info(f"No DOI found in {link}, skipping.")
                continue

            meta = fetch_metadata_from_crossref(doi)
            abstract = fetch_abstract(doi)
            tweet_url = f"https://twitter.com/{username}/status/{tw.id}"

            append_paper_row(sheet, meta, abstract, tweet_url)

        except Exception as e:
            logger.error(f"Error processing tweet {tw.id}: {e}", exc_info=True)

if __name__ == "__main__":
    TW_USERNAME = os.environ.get("nanomotorupdate", "<twitter_handle_here>")
    if TW_USERNAME.startswith("<"):
        logger.error("Please set TW_USERNAME env var to the Twitter handle (without @).")
        exit(1)

    process_new_papers(TW_USERNAME)
