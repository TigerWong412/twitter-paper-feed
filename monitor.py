#!/usr/bin/env python3
import os
import re
import logging
from pathlib import Path
from datetime import datetime

import requests
import tweepy
import gspread
from google.oauth2.service_account import Credentials
import xml.etree.ElementTree as ET

# ── CONFIG ─────────────────────────────────────────────────────────────────────
# Twitter and Google Sheets settings via environment variables or constants
TW_BEARER_TOKEN    = os.environ["TW_BEARER_TOKEN"]          # Twitter API Bearer Token
TW_USERNAME        = os.environ.get("TW_USERNAME", "nanomotorupdate")  # Twitter handle
SPREADSHEET_NAME   = "Twitter nanomotorupdate"              # Google Sheet name
SERVICE_ACCOUNT_FN = "service_account.json"                # Service account JSON filename
HISTORICAL_FILE    = Path("extracted_tweets.txt")          # Historical tweets file
SINCE_ID_FILE      = Path("since_id.txt")                  # File to track last seen tweet
START_TIME         = "2025-03-25T00:00:00Z"                 # ISO start time for live tweets
MAX_RESULTS        = 100                                     # Max tweets per API call

# ── LOGGING ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s"
)
logger = logging.getLogger(__name__)

# ── TWITTER FUNCTIONS ───────────────────────────────────────────────────────────
def fetch_historical_urls() -> list[str]:
    """
    Read URLs from the uploaded extracted_tweets.txt file.
    """
    if not HISTORICAL_FILE.exists():
        logger.warning(f"Historical file not found: {HISTORICAL_FILE}")
        return []
    content = HISTORICAL_FILE.read_text(encoding="utf-8")
    # Extract all HTTP/HTTPS URLs
    return re.findall(r'https?://\S+', content)


def fetch_new_tweets() -> list[tweepy.Tweet]:
    """
    Fetch new tweets from TW_USERNAME since START_TIME, tracking since_id to avoid duplicates.
    """
    client = tweepy.Client(bearer_token=TW_BEARER_TOKEN)
    try:
        user = client.get_user(username=TW_USERNAME).data
    except Exception as e:
        logger.error(f"Unable to fetch user '{TW_USERNAME}': {e}")
        return []

    params = {
        "tweet_fields": ["entities", "created_at"],
        "max_results": MAX_RESULTS,
        "start_time": START_TIME
    }
    # Use since_id to fetch only newer tweets
    if SINCE_ID_FILE.exists():
        try:
            params["since_id"] = int(SINCE_ID_FILE.read_text().strip())
        except ValueError:
            pass

    resp = client.get_users_tweets(id=user.id, **params)
    tweets = resp.data or []
    if tweets:
        # Save the highest ID for next time
        max_id = max(t.id for t in tweets)
        SINCE_ID_FILE.write_text(str(max_id))
    logger.info(f"Fetched {len(tweets)} new tweets since {START_TIME}")
    return tweets

# ── DOI EXTRACTION ──────────────────────────────────────────────────────────────
def extract_doi(url: str) -> str | None:
    """
    Universal DOI extraction:
    1) Regex on URL or final redirect URL
    2) <meta name="citation_doi"> fallback
    """
    # 1) Direct regex
    m = re.search(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    # 2) Follow redirects and regex
    try:
        head = requests.head(url, allow_redirects=True, timeout=10)
        m2 = re.search(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", head.url)
        if m2:
            return m2.group(1)
    except Exception:
        pass
    # 3) HTML meta tag
    try:
        html = requests.get(url, timeout=10).text
        m3 = re.search(r'<meta name="citation_doi" content="([^"]+)"', html)
        if m3:
            return m3.group(1)
    except Exception:
        pass
    return None

# ── METADATA & ABSTRACT ────────────────────────────────────────────────────────
def fetch_metadata(doi: str) -> dict:
    """
    Fetch title, authors, journal, year, volume, issue, pages, publisher from Crossref JSON.
    """
    api = f"https://api.crossref.org/works/{doi}"
    resp = requests.get(api, timeout=10)
    resp.raise_for_status()
    msg = resp.json()["message"]
    title   = msg.get("title", [""])[0]
    journal = msg.get("container-title", [""])[0]
    authors = [f"{a.get('given','')} {a.get('family','')}".strip() for a in msg.get("author", [])]
    issued  = msg.get("issued", {})
    year    = issued.get("date-parts", [[None]])[0][0]
    return {
        "title": title,
        "journal": journal,
        "authors": authors,
        "year": year,
        "volume": msg.get("volume", ""),
        "issue": msg.get("issue", ""),
        "pages": msg.get("page", ""),
        "publisher": msg.get("publisher", ""),
        "doi": doi
    }


def fetch_abstract(doi: str) -> str:
    """
    Fetch abstract from Semantic Scholar, fallback to Crossref XML if needed.
    """
    # Semantic Scholar
    ss_url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=abstract"
    try:
        r = requests.get(ss_url, timeout=10)
        if r.status_code == 200:
            abs_json = r.json()
            if (abstract := abs_json.get("abstract")):
                return abstract
    except Exception:
        pass
    # Crossref XML fallback
    xml_url = f"https://api.crossref.org/works/{doi}.xml"
    try:
        x = requests.get(xml_url, timeout=10)
        if x.status_code == 200:
            root = ET.fromstring(x.content)
            el = root.find(".//abstract")
            if el is not None:
                return ET.tostring(el, method="text", encoding="unicode").strip()
    except Exception:
        pass
    return ""

# ── GOOGLE SHEETS ───────────────────────────────────────────────────────────────
def init_sheet():
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FN,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    return client.open(SPREADSHEET_NAME).sheet1

def append_row(sheet, meta: dict, abstract: str, source: str):
    """
    Append one row of metadata + abstract + source (URL or tweet) to Google Sheet
    """
    row = [
        meta["title"],
        "; ".join(meta["authors"]),
        meta["journal"],
        meta["year"],
        meta["doi"],
        abstract,
        source
    ]
    sheet.append_row(row, value_input_option="USER_ENTERED")
    logger.info(f"Appended: {meta['doi']}")

# ── MAIN WORKFLOW ──────────────────────────────────────────────────────────────
def main():
    # 1) Historical import
    sheet = init_sheet()
    urls = fetch_historical_urls()
    seen = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        doi = extract_doi(url)
        if not doi:
            logger.info(f"No DOI in historical URL, skipping: {url}")
            continue
        try:
            meta     = fetch_metadata(doi)
            abstract = fetch_abstract(doi)
            append_row(sheet, meta, abstract, url)
        except Exception as e:
            logger.error(f"Historical processing failed for DOI {doi}: {e}")

    # 2) Live monitoring from START_TIME
    tweets = fetch_new_tweets()
    for tw in tweets:
        for u in (tw.entities or {}).get("urls", []):
            url = u.get("expanded_url")
            doi = extract_doi(url)
            if not doi:
                logger.info(f"No DOI in tweet URL, skipping: {url}")
                continue
            try:
                meta     = fetch_metadata(doi)
                abstract = fetch_abstract(doi)
                tweet_url = f"https://twitter.com/{TW_USERNAME}/status/{tw.id}"
                append_row(sheet, meta, abstract, tweet_url)
            except Exception as e:
                logger.error(f"Live processing failed for DOI {doi}: {e}")

if __name__ == "__main__":
    main()
