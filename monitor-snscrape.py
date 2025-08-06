#!/usr/bin/env python3
import os
import re
import sys
import logging
from pathlib import Path
from datetime import datetime  # 新增：用于日期处理

import requests
import gspread
import xml.etree.ElementTree as ET
from google.oauth2.service_account import Credentials
import snscrape.modules.twitter as sntwitter  # 新增：snscrape 模块

# ── CONFIG ─────────────────────────────────────────────────────────────────────
# 这句不用了 TW_BEARER_TOKEN    = os.environ.get("TW_BEARER_TOKEN")          # Twitter API bearer token (optional for historical)
TW_USERNAME        = "nanomotorupdate"                          # Target account to monitor
SPREADSHEET_ID     = "1oYdQyh1tqPA3821PE97ru8aL8jZOe1e7vKLp2x7BSF8"  # Google Sheet ID
SERVICE_ACCOUNT_FN = "service_account.json"                      # Service account JSON filename
HISTORICAL_FILE    = Path("extracted_tweets.txt")                # Historical tweets file
SINCE_ID_FILE      = Path("since_id.txt")                        # Tracks last seen tweet ID
START_TIME         = "2025-04-23T00:00:00Z"                       # Only fetch tweets after this date initially
MAX_RESULTS        = 100                                          # Max tweets per API call

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

# ── LIVE TWEET FETCH (snscrape version) ─────────────────────────────────────────
def fetch_new_tweets_snscrape(since_id: int | None) -> list:
    """使用 snscrape 获取指定用户的新推文（自 since_id 之后）"""
    tweets = []
    # 遍历目标用户的推文（snscrape 会按时间倒序返回，最新的在前）
    for i, tweet in enumerate(sntwitter.TwitterUserScraper(TW_USERNAME).get_items()):
        # 如果存在 since_id，且当前推文 ID 小于等于 since_id，说明已获取所有新推文，停止循环
        if since_id and tweet.id <= since_id:
            break
        # 如果没有 since_id，使用 START_TIME 作为起始时间（转换为 datetime 类型）
        if not since_id:
            start_datetime = datetime.fromisoformat(START_TIME.replace("Z", "+00:00"))
            if tweet.date < start_datetime:
                break
        # 添加推文到列表
        tweets.append(tweet)
        # 达到最大结果数时停止
        if i >= MAX_RESULTS - 1:
            break
    # 如果有新推文，更新 since_id 为最新推文的 ID（因按倒序排列，第一个即为最新）
    if tweets:
        max_id = tweets[0].id
        SINCE_ID_FILE.write_text(str(max_id))
    logger.info(f"Fetched {len(tweets)} new tweets (via snscrape)")
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
    api_url = f"https://api.crossref.org/works/{doi}"
    r = requests.get(api_url, timeout=10)
    r.raise_for_status()
    msg = r.json()["message"]
    title   = msg.get("title", [""])[0]
    journal = msg.get("container-title", [""])[0]
    authors = [f"{a.get('given','')} {a.get('family','')}".strip() for a in msg.get("author", [])]
    pub = msg.get("published-print") or msg.get("published-online") or {}
    parts = pub.get("date-parts", [[None]])[0]
    pub_date = "-".join(str(p) for p in parts if p is not None) if parts[0] else ""
    issued = msg.get("issued", {})
    year   = issued.get("date-parts", [[None]])[0][0]
    return {"title": title, "authors": authors, "journal": journal,
            "year": year, "pub_date": pub_date, "doi": doi}


def fetch_abstract(doi: str) -> str:
    ss_url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=abstract"
    try:
        r = requests.get(ss_url, timeout=10)
        if r.status_code == 200:
            abs_txt = r.json().get("abstract")
            if abs_txt:
                return abs_txt
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
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
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
    logger.info(f"Appended DOI {meta['doi']}")
    sheet.sort((5, "desc"))

# ── PROCESS HISTORICAL ─────────────────────────────────────────────────────────
def process_historical():
    sheet = init_sheet()
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

# ── PROCESS LIVE (snscrape version) ─────────────────────────────────────────────
def process_live():
    """处理实时推文（基于 snscrape 获取的数据）"""
    sheet = init_sheet()
    # 读取上次处理的推文 ID（since_id）
    since_id = None
    if SINCE_ID_FILE.exists():
        try:
            since_id = int(SINCE_ID_FILE.read_text().strip())
        except ValueError:
            logger.warning("Invalid since_id, starting from START_TIME")
            since_id = None
    # 使用 snscrape 获取新推文
    tweets = fetch_new_tweets_snscrape(since_id)
    # 处理每条推文
    for tw in tweets:
        # snscrape 中推文的链接存储在 outlinks 属性中（扩展后的 URL）
        for source_url in (tw.outlinks or []):
            # 提取 DOI
            doi = extract_doi(source_url)
            if not doi:
                logger.info(f"Skipping live URL (no DOI): {source_url}")
                continue
            try:
                # 获取文献元数据和摘要
                meta = fetch_metadata(doi)
                abstract = fetch_abstract(doi)
                # 推文发布时间（转换为 ISO 格式字符串）
                tweet_date = tw.date.isoformat()
                # 写入 Google 表格
                append_row(sheet, meta, abstract, source_url, tweet_date)
            except Exception as e:
                logger.error(f"Live processing failed for DOI {doi}: {e}")

# ── ENTRY POINT ────────────────────────────────────────────────────────────────
def main():
    if '--historical' in sys.argv:
        process_historical()
    else:
        process_live()

if __name__ == "__main__":
    main()
