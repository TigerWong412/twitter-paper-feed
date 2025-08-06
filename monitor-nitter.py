#!/usr/bin/env python3
import os
import re
import sys
import logging
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup           # 用于 Nitter HTML 解析
import gspread
import xml.etree.ElementTree as ET
from google.oauth2.service_account import Credentials

# ── CONFIG ─────────────────────────────────────────────────────────────────────
TW_USERNAME        = "nanomotorupdate"
SPREADSHEET_ID     = "1oYdQyh1tqPA3821PE97ru8aL8jZOe1e7vKLp2x7BSF8"
SERVICE_ACCOUNT_FN = "service_account.json"
SINCE_ID_FILE      = Path("since_id.txt")
START_TIME         = "2025-04-23T00:00:00Z"
MAX_RESULTS        = 100

# … 省略历史和元数据部分，保持不变 …

# ── LIVE TWEET FETCH (Nitter 版) ────────────────────────────────────────────────
def fetch_new_tweets_nitter(since_id: int | None) -> list[dict]:
    """
    通过 Nitter 抓取页面 HTML，解析出最近的推文：
      - id: 推文 ID（int）
      - date: 发布日期（datetime）
      - outlinks: 推文中的外部链接列表
    """
    url = f"https://nitter.net/{TW_USERNAME}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    tweets = []
    for item in soup.select("div.timeline-item"):
        a = item.select_one("a.tco-link")
        if not a or "status" not in a["href"]:
            continue
        tid = int(a["href"].rstrip("/").split("/")[-1])
        if since_id and tid <= since_id:
            break

        # 解析时间
        time_tag = item.select_one("span.tweet-date time")
        date = None
        if time_tag and time_tag.has_attr("datetime"):
            date = datetime.fromisoformat(time_tag["datetime"].replace("Z", "+00:00"))

        # 解析外部链接
        outlinks = [lnk["href"] for lnk in item.select("div.tweet-content a.tco-link") if lnk.get("href") and lnk["href"].startswith("http")]

        tweets.append({"id": tid, "date": date, "outlinks": outlinks})
        if len(tweets) >= MAX_RESULTS:
            break

    # 更新 since_id
    if tweets:
        SINCE_ID_FILE.write_text(str(tweets[0]["id"]))
    logger.info(f"Fetched {len(tweets)} new tweets (via Nitter)")
    return tweets

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
