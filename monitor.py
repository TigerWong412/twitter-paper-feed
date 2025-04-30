#!/usr/bin/env python3
import os
import re
import logging
from pathlib import Path

import requests
import tweepy
import gspread
import xml.etree.ElementTree as ET
from google.oauth2.service_account import Credentials

# ── CONFIG ─────────────────────────────────────────────────────────────────────
TW_BEARER_TOKEN    = os.environ["TW_BEARER_TOKEN"]            # Twitter API bearer token
TW_USERNAME        = "nanomotorupdate"                        # Target account to monitor
SPREADSHEET_ID     = "1oYdQyh1tqPA3821PE97ru8aL8jZOe1e7vKLp2x7BSF8"  # Google Sheet ID
SERVICE_ACCOUNT_FN = "service_account.json"                  # Service account JSON filename
HISTORICAL_FILE    = Path("extracted_tweets.txt")            # Historical tweets file
SINCE_ID_FILE      = Path("since_id.txt")                    # Tracks last seen tweet ID
START_TIME         = "2025-03-25T00:00:00Z"                   # Only fetch tweets after this date
MAX_RESULTS        = 100                                       # Max tweets per API call

# ── LOGGING SETUP ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s"
)
logger = logging.getLogger(__name__)

# ── HISTORICAL IMPORT ─────────────────────────────────────────────────────────────
def fetch_historical_urls() -> list[str]:
    if not HISTORICAL_FILE.exists():
        logger.warning(f"Historical file not found: {HISTORICAL_FILE}")
        return []
    content = HISTORICAL_FILE.read_text(encoding="utf-8")
    return re.findall(r'https?://\S+', content)

# ── LIVE TWEET FETCH ─────────────────────────────────────────────────────────────
def fetch_new_tweets() -> list[tweepy.Tweet]:
    client = tweepy.Client(bearer_token=TW_BEARER_TOKEN)
    try:
        user = client.get_user(username=TW_USERNAME).data
    except Exception as e:
        logger.error(f"Unable to fetch user '{TW_USERNAME}': {e}")
        return []

    params = {"tweet_fields": ["entities","created_at"], "max_results": MAX_RESULTS}
    if SINCE_ID_FILE.exists():
        try:
            params["since_id"] = int(SINCE_ID_FILE.read_text().strip())
        except ValueError:
            pass
    else:
        params["start_time"] = START_TIME

    resp = client.get_users_tweets(id=user.id, **params)
    tweets = resp.data or []
    if tweets:
        max_id = max(t.id for t in tweets)
        SINCE_ID_FILE.write_text(str(max_id))
    logger.info(f"Fetched {len(tweets)} new tweets")
    return tweets

# ── DOI EXTRACTION ──────────────────────────────────────────────────────────────
def extract_doi(url: str) -> str | None:
    m = re.search(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    try:
        head = requests.head(url, allow_redirects=True, timeout=10)
        m2 = re.search(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", head.url)
        if m2:
            return m2.group(1)
    except:
        pass
    try:
        html = requests.get(url, timeout=10).text
        m3 = re.search(r'<meta name="citation_doi" content="([^"]+)"', html)
        if m3:
            return m3.group(1)
    except:
        pass
    return None

# ── METADATA & ABSTRACT ─────────────────────────────────────────────────────────
def fetch_metadata(doi: str) -> dict:
    url = f"https://api.crossref.org/works/{doi}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    msg = resp.json()["message"]
    title   = msg.get("title", [""])[0]
    journal = msg.get("container-title", [""])[0]
    authors = [f"{a.get('given','')} {a.get('family','')}".strip() for a in msg.get("author", [])]
    # Extract publication date YYYY-MM-DD
    pub = msg.get("published-print") or msg.get("published-online") or {}
    parts = pub.get("date-parts", [[None]])[0]
    pub_date = "-".join(str(p) for p in parts if p is not None) if parts[0] else ""
    issued  = msg.get("issued", {})
    year    = issued.get("date-parts", [[None]])[0][0]
    return {
        "title": title,
        "authors": authors,
        "journal": journal,
        "year": year,
        "pub_date": pub_date,
        "doi": doi
    }

def fetch_abstract(doi: str) -> str:
    ss_url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=abstract"
    try:
        r = requests.get(ss_url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if abstract := data.get("abstract"):
                return abstract
    except:
        pass
    xml_url = f"https://api.crossref.org/works/{doi}.xml"
    try:
        x = requests.get(xml_url, timeout=10)
        if x.status_code == 200:
            root = ET.fromstring(x.content)
            el = root.find(".//abstract")
            if el is not None:
                return ET.tostring(el, method="text", encoding="unicode").strip()
    except:
        pass
    return ""

# ── GOOGLE SHEETS ───────────────────────────────────────────────────────────────
def init_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FN, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).sheet1


def append_row(sheet, meta: dict, abstract: str, source_url: str, tweet_date: str = ""):
    row = [
        meta["title"],
        "; ".join(meta["authors"]),
        meta["journal"],
        meta["year"],
        meta["pub_date"],
        abstract,
        meta["doi"],
        source_url,
        tweet_date
    ]
    sheet.append_row(row, value_input_option="USER_ENTERED")
    logger.info(f"Appended {meta['doi']}")
    # Resort by publication date descending
    sheet.sort((5, "desc"))

# ── MAIN WORKFLOW ──────────────────────────────────────────────────────────────
def main():
    sheet = init_sheet()

    # Historical import (manual run)
    if '--historical' in os.sys.argv:
        urls = fetch_historical_urls()
        seen = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            doi = extract_doi(url)
            if not doi:
                logger.info(f"Skipping historical URL (no DOI): {url}")
                continue
            try:
                meta = fetch_metadata(doi)
                abstract = fetch_abstract(doi)
                append_row(sheet, meta, abstract, url)
            except Exception as e:
                logger.error(f"Historical processing failed for DOI {doi}: {e}")
        return

    # Live import (scheduled run)
    tweets = fetch_new_tweets()
    for tw in tweets:
        for u in (tw.entities or {}).get("urls", []):
            source = u.get("expanded_url")
            doi = extract_doi(source)
            if not doi:
                logger.info(f"Skipping live URL (no DOI): {source}")
                continue
            try:
                meta = fetch_metadata(doi)
                abstract = fetch_abstract(doi)
                tweet_date = tw.created_at.isoformat()
                append_row(sheet, meta, abstract, source, tweet_date)
            except Exception as e:
                logger.error(f"Live processing failed for DOI {doi}: {e}")

if __name__ == "__main__":
    main()

