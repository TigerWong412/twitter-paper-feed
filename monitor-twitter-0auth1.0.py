#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, logging
from pathlib import Path
from datetime import datetime
import xml.etree.ElementTree as ET

import requests
import tweepy
import gspread
from google.oauth2.service_account import Credentials

# ── CONFIG ─────────────────────────────────────────────────────────────────────
TW_USERNAME        = "nanomotorupdate"  # 监控的账号（不是你的账号名）
SPREADSHEET_ID     = "1oYdQyh1tqPA3821PE97ru8aL8jZOe1e7vKLp2x7BSF8"  # Google Sheet ID
SERVICE_ACCOUNT_FN = "service_account.json"
HISTORICAL_FILE    = Path("extracted_tweets.txt")
SINCE_ID_FILE      = Path("since_id.txt")
MAX_RESULTS        = 200  # v1.1 单次最多 200

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)

# ── Google Sheets ──────────────────────────────────────────────────────────────
def init_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FN, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).sheet1

def append_row(sheet, meta: dict, abstract: str, source_url: str, tweet_date: str = ""):
    # 列顺序：标题, 作者, 期刊, 年份, 出版日期, 摘要, DOI, 原始链接, 推文时间
    row = [
        meta.get("title",""),
        "; ".join(meta.get("authors", [])),
        meta.get("journal",""),
        meta.get("year","") or "",
        meta.get("pub_date","") or "",
        abstract or "",
        meta.get("doi",""),
        source_url,
        tweet_date or "",
    ]
    sheet.append_row(row, value_input_option="USER_ENTERED")
    # 以出版日期（第5列）倒序排序
    try:
        sheet.sort((5, "desc"))
    except Exception as e:
        logger.warning(f"排序失败（可能是空表或日期格式问题）：{e}")

# ── 读取历史文本里的 URL ───────────────────────────────────────────────────────
def fetch_historical_urls() -> list[str]:
    if not HISTORICAL_FILE.exists():
        logger.warning(f"未找到历史文件：{HISTORICAL_FILE}")
        return []
    content = HISTORICAL_FILE.read_text(encoding="utf-8", errors="ignore")
    return re.findall(r'https?://\S+', content)

# ── DOI 提取 ───────────────────────────────────────────────────────────────────
def extract_doi(url: str) -> str | None:
    # 1) 直接匹配
    m = re.search(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    # 2) 跟随跳转
    try:
        head = requests.head(url, allow_redirects=True, timeout=10)
        m2 = re.search(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", head.url)
        if m2:
            return m2.group(1)
    except Exception:
        pass
    # 3) HTML meta
    try:
        html = requests.get(url, timeout=12).text
        m3 = re.search(r'<meta[^>]+name=["\']citation_doi["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if m3:
            return m3.group(1).strip()
    except Exception:
        pass
    return None

# ── 元数据与摘要 ───────────────────────────────────────────────────────────────
def fetch_metadata(doi: str) -> dict:
    api_url = f"https://api.crossref.org/works/{doi}"
    r = requests.get(api_url, timeout=12)
    r.raise_for_status()
    msg = r.json()["message"]

    title   = (msg.get("title") or [""])[0]
    journal = (msg.get("container-title") or [""])[0]
    authors = [f"{a.get('given','')} {a.get('family','')}".strip()
               for a in msg.get("author", [])]

    # 出版日期
    pub = msg.get("published-print") or msg.get("published-online") or {}
    parts = (pub.get("date-parts") or [[None]])[0]
    pub_date = "-".join(str(p) for p in parts if p is not None) if parts and parts[0] else ""

    # 年份
    issued = msg.get("issued", {})
    year   = (issued.get("date-parts") or [[None]])[0][0]

    return {"title": title, "authors": authors, "journal": journal,
            "year": year, "pub_date": pub_date, "doi": doi}

def fetch_abstract(doi: str) -> str:
    # 1) Semantic Scholar（更常有摘要）
    try:
        ss = requests.get(
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=abstract",
            timeout=12,
        )
        if ss.status_code == 200:
            abs_txt = ss.json().get("abstract")
            if abs_txt:
                return abs_txt
    except Exception:
        pass
    # 2) Crossref XML fallback
    try:
        x = requests.get(f"https://api.crossref.org/works/{doi}.xml", timeout=12)
        if x.status_code == 200:
            root = ET.fromstring(x.content)
            el = root.find(".//abstract")
            if el is not None:
                return ET.tostring(el, method="text", encoding="unicode").strip()
    except Exception:
        pass
    return ""

# ── v1.1 拉取最新推文 ───────────────────────────────────────────────────────────
def fetch_new_tweets_v1(since_id: int | None):
    """用 v1.1 用户上下文获取时间线（不受“每月100条”限制）"""
    auth = tweepy.OAuth1UserHandler(
        os.environ["TW_API_KEY"],
        os.environ["TW_API_SECRET"],
        os.environ["TW_ACCESS_TOKEN"],
        os.environ["TW_ACCESS_SECRET"],
    )
    api = tweepy.API(auth, wait_on_rate_limit=True)

    params = {
        "screen_name": TW_USERNAME,
        "count": min(MAX_RESULTS, 200),
        "tweet_mode": "extended",   # 拿 full_text
        "include_rts": True,        # 视需求可 False
        "exclude_replies": False,
    }
    if since_id:
        params["since_id"] = since_id

    try:
        tweets = api.user_timeline(**params)  # List[tweepy.models.Status]
    except Exception as e:
        logger.error(f"v1.1 拉取失败：{e}", exc_info=True)
        return []

    # 更新 since_id 为最新
    if tweets:
        SINCE_ID_FILE.write_text(str(tweets[0].id))
    logger.info(f"Fetched {len(tweets)} new tweets (via v1.1 API)")
    return tweets

# ── 处理历史导入 ────────────────────────────────────────────────────────────────
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
            logger.info(f"跳过（无 DOI）：{url}")
            continue
        try:
            meta = fetch_metadata(doi)
            abstract = fetch_abstract(doi)
            append_row(sheet, meta, abstract, url, "")
            logger.info(f"历史导入成功：{doi}")
        except Exception as e:
            logger.error(f"历史导入失败 {doi}: {e}")

# ── 处理 Live 导入 ─────────────────────────────────────────────────────────────
def process_live():
    sheet = init_sheet()
    since_id = None
    if SINCE_ID_FILE.exists():
        try:
            since_id = int(SINCE_ID_FILE.read_text().strip())
        except Exception:
            since_id = None

    tweets = fetch_new_tweets_v1(since_id)

    for tw in tweets:
        # 若是转推，正文/实体在 retweeted_status 中
        status = getattr(tw, "retweeted_status", tw)

        # URL 实体：优先 unwound_url，其次 expanded_url
        url_entities = (status.entities or {}).get("urls", [])
        urls = []
        for ent in url_entities:
            urls.append(ent.get("unwound_url") or ent.get("expanded_url") or ent.get("url"))

        for source_url in filter(None, urls):
            doi = extract_doi(source_url)
            if not doi:
                logger.info(f"跳过（无 DOI）：{source_url}")
                continue
            try:
                meta     = fetch_metadata(doi)
                abstract = fetch_abstract(doi)
                tweet_dt = tw.created_at.isoformat() if getattr(tw, "created_at", None) else ""
                append_row(sheet, meta, abstract, source_url, tweet_dt)
                logger.info(f"已写入：{doi}")
            except Exception as e:
                logger.error(f"写入失败 {doi}: {e}")

# ── 入口 ────────────────────────────────────────────────────────────────────────
def main():
    mode = "live"
    for arg in sys.argv[1:]:
        if arg in ("--live", "--historical"):
            mode = arg.lstrip("-")
            break

    if mode == "historical":
        process_historical()
    else:
        process_live()

if __name__ == "__main__":
    main()
