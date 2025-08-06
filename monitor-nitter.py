#!/usr/bin/env python3
import os
import re
import sys
import logging
import time
from requests.exceptions import HTTPError
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup           # 用于 Nitter HTML 解析
import gspread
import xml.etree.ElementTree as ET
from google.oauth2.service_account import Credentials

NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.snopyta.org",
    "https://nitter.kavin.rocks",
    "https://nitter.tedomum.net",
    "https://nitter.pussthecat.org",
    "https://nitter.invak.id",
    "https://nitter.42l.fr",
]
# ── CONFIG ─────────────────────────────────────────────────────────────────────
TW_USERNAME        = "nanomotorupdate"
SPREADSHEET_ID     = "1oYdQyh1tqPA3821PE97ru8aL8jZOe1e7vKLp2x7BSF8"
SERVICE_ACCOUNT_FN = "service_account.json"
SINCE_ID_FILE      = Path("since_id.txt")
START_TIME         = "2025-04-23T00:00:00Z"
MAX_RESULTS        = 100

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

# ── LIVE TWEET FETCH (Nitter 版) ────────────────────────────────────────────────
def fetch_new_tweets_nitter(since_id: int | None) -> list[dict]:
    """
    通过 Nitter 抓取指定用户的新推文（自 since_id 之后），
    并输出调试日志：HTTP 状态和解析到的节点数。
    返回格式：[{ 'id': int, 'date': datetime, 'outlinks': [urls] }, …]
    """
    tweets = []
    resp = None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/115.0 Safari/537.36"
        )
    }

    # 依次尝试各镜像，直到拿到有效页面
    for base in NITTER_INSTANCES:
        time.sleep(3)  # 确保请求不超过 1 次／秒
        url = f"{base}/{TW_USERNAME}"
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            logger.info(f"请求 {url} 返回状态码 {resp.status_code}")
            snippet = resp.text[:500].replace('\n', ' ')
            logger.info(f"页面 HTML 片段: {snippet!r}")
            resp.raise_for_status()

            break
        except HTTPError as e:
            logger.warning(f"{base} HTTP 错误：{e}")
        except Exception as e:
            logger.error(f"{base} 请求失败：{e}")

    if not resp:
        logger.error("所有 Nitter 实例均不可用，跳过本次抓取")
        return tweets

    soup = BeautifulSoup(resp.text, "lxml")
    # 兼容老版和新版 Nitter 容器
    items = soup.select("article.tweet, div.timeline-item")
    logger.info(f"Parsed 推文节点数量: {len(items)}")

    count = 0
    for item in items:
        link = item.select_one("a.tweet-link")
        if not link or "/status/" not in link["href"]:
            continue
        tid = int(link["href"].split("/")[-1])
        if since_id and tid <= since_id:
            break

        # 解析发布时间
        time_tag = item.select_one("span.tweet-date time")
        tweet_date = None
        if time_tag and time_tag.has_attr("datetime"):
            tweet_date = datetime.fromisoformat(
                time_tag["datetime"].replace("Z", "+00:00")
            )

        # 解析正文中的外部链接
        outlinks = []
        for a in item.select("div.tweet-content a"):
            href = a.get("href", "")
            if href.startswith("http"):
                outlinks.append(href)

        tweets.append({"id": tid, "date": tweet_date, "outlinks": outlinks})
        count += 1
        if count >= MAX_RESULTS:
            break

    # 更新 since_id.txt
    if tweets:
        SINCE_ID_FILE.write_text(str(tweets[0]["id"]))
    logger.info(f"Fetched {len(tweets)} new tweets (via Nitter)")
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

# ── PROCESS LIVE ────────────────────────────────────────────────────────────────
def process_live():
    sheet = init_sheet()

    # 读取 since_id
    since_id = None
    if SINCE_ID_FILE.exists():
        try:
            since_id = int(SINCE_ID_FILE.read_text().strip())
        except ValueError:
            since_id = None

    # 抓取新推文
    tweets = fetch_new_tweets_nitter(since_id)

    for tw in tweets:
        for source_url in tw["outlinks"]:
            doi = extract_doi(source_url)
            if not doi:
                logger.info(f"Skipping URL (no DOI): {source_url}")
                continue
            try:
                meta     = fetch_metadata(doi)
                abstract = fetch_abstract(doi)
                tweet_date = tw["date"].isoformat() if tw["date"] else ""
                append_row(sheet, meta, abstract, source_url, tweet_date)
            except Exception as e:
                logger.error(f"Live processing failed for DOI {doi}: {e}")

# ── ENTRY POINT ────────────────────────────────────────────────────────────────
def main():
    if "--historical" in sys.argv:
        process_historical()
    else:
        process_live()

if __name__ == "__main__":
    main()
