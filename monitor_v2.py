#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import time
import logging
from pathlib import Path
from typing import List, Tuple, Optional

import requests
import tweepy
import gspread
import xml.etree.ElementTree as ET
from google.oauth2.service_account import Credentials

# ── CONFIG ─────────────────────────────────────────────────────────────────────
TW_BEARER_TOKEN    = os.environ.get("TW_BEARER_TOKEN")          # Twitter v2 Bearer
TW_USERNAME        = "nanomotorupdate"                          # 监控的账号
SPREADSHEET_ID     = "1oYdQyh1tqPA3821PE97ru8aL8jZOe1e7vKLp2x7BSF8"  # Google Sheet ID
SERVICE_ACCOUNT_FN = "service_account.json"                     # 服务账号 JSON 文件
HISTORICAL_FILE    = Path("extracted_tweets.txt")               # 历史链接文件
SINCE_ID_FILE      = Path("since_id.txt")                       # 记录上次处理的 tweet ID
START_TIME         = "2025-10-31T00:00:00Z"                     # 初次抓取起始时间（ISO8601）
MAX_RESULTS        = 100                                         # 每次最多抓取的 tweet 数（<=100）

# 请求头（用于 DOI 解析时访问出版社站）
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}

# ── LOGGING SETUP ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s"
)
logger = logging.getLogger(__name__)

# ── GOOGLE SHEETS ──────────────────────────────────────────────────────────────
def init_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FN, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).sheet1

def append_row(sheet, meta: dict, abstract: str, source_url: str, tweet_date: str = ""):
    """
    列顺序：标题, 作者, 期刊, 年份, 出版日期, 摘要, DOI, 原始链接, 推文时间
    """
    row = [
        meta.get("title", ""),
        "; ".join(meta.get("authors", [])),
        meta.get("journal", ""),
        meta.get("year", "") or "",
        meta.get("pub_date", "") or "",
        abstract or "",
        meta.get("doi", ""),
        source_url,
        tweet_date or "",
    ]
    sheet.append_row(row, value_input_option="USER_ENTERED")
    logger.info(f"Appended DOI {meta.get('doi','')}")
    # 以第5列“出版日期”倒序排序；空表/非日期时容错
    try:
        sheet.sort((5, "desc"))
    except Exception as e:
        logger.warning(f"Sort skipped (likely empty/format): {e}")

# ── 历史链接读取 ────────────────────────────────────────────────────────────────
def fetch_historical_urls() -> List[str]:
    if not HISTORICAL_FILE.exists():
        logger.warning(f"Historical file not found: {HISTORICAL_FILE}")
        return []
    content = HISTORICAL_FILE.read_text(encoding="utf-8", errors="ignore")
    return re.findall(r'https?://\S+', content)

# ── DOI 提取 ───────────────────────────────────────────────────────────────────
def extract_doi(url: str) -> Optional[str]:
    # 1) 直接从 URL 匹配
    m = re.search(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    # 2) 跟随重定向（获取最终 URL）
    try:
        head = requests.head(url, allow_redirects=True, timeout=12, headers=REQUEST_HEADERS)
        m2 = re.search(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", head.url)
        if m2:
            return m2.group(1)
    except Exception:
        pass
    # 3) 解析 HTML Meta
    try:
        html = requests.get(url, timeout=15, headers=REQUEST_HEADERS).text
        m3 = re.search(r'<meta[^>]+name=["\']citation_doi["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if m3:
            return m3.group(1).strip()
    except Exception:
        pass
    return None

# ── 文献信息与摘要 ─────────────────────────────────────────────────────────────
def fetch_metadata(doi: str) -> dict:
    api_url = f"https://api.crossref.org/works/{doi}"
    r = requests.get(api_url, timeout=15)
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
    # 1) Semantic Scholar
    try:
        ss = requests.get(
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=abstract",
            timeout=15,
        )
        if ss.status_code == 200:
            abs_txt = ss.json().get("abstract")
            if abs_txt:
                return abs_txt
    except Exception:
        pass
    # 2) Crossref XML 兜底
    try:
        x = requests.get(f"https://api.crossref.org/works/{doi}.xml", timeout=15)
        if x.status_code == 200:
            root = ET.fromstring(x.content)
            el = root.find(".//abstract")
            if el is not None:
                return ET.tostring(el, method="text", encoding="unicode").strip()
    except Exception:
        pass
    return ""

# ── v2: 拉取最新推文（含转推/引用的原帖展开）─────────────────────────────────────
def fetch_new_tweets(since_id: Optional[int]) -> List[Tuple[tweepy.Tweet, tweepy.Tweet]]:
    """
    返回 [(原始tweet, 解析后tweet)]：
    - 原始tweet：用于拿 created_at（tweet 的发布时间）
    - 解析后tweet：若为转推/引用，则换成原帖；否则为自己
    """
    client = tweepy.Client(bearer_token=TW_BEARER_TOKEN)
    try:
        user = client.get_user(username=TW_USERNAME).data
    except Exception as e:
        logger.error(f"Unable to fetch user '{TW_USERNAME}': {e}")
        return []

    params = {
        "tweet_fields": ["entities", "created_at", "referenced_tweets"],
        "max_results": min(MAX_RESULTS, 100),
        "expansions": ["referenced_tweets.id"],
    }
    if since_id:
        params["since_id"] = since_id
    else:
        params["start_time"] = START_TIME

    all_tweets: List[tweepy.Tweet] = []
    resp = client.get_users_tweets(id=user.id, **params)
    if resp and resp.data:
        all_tweets.extend(resp.data)

    # 如需翻页，请取消注释（注意配额消耗）
    next_token = resp.meta.get("next_token") if (resp and resp.meta) else None
    while next_token and len(all_tweets) < MAX_RESULTS:
         resp = client.get_users_tweets(id=user.id, pagination_token=next_token, **params)
         if not resp or not resp.data:
             break
         all_tweets.extend(resp.data)
         next_token = resp.meta.get("next_token") if (resp and resp.meta) else None

    if all_tweets:
        SINCE_ID_FILE.write_text(str(max(t.id for t in all_tweets)))

    # includes 中的原帖映射
    includes_map = {}
    if resp and resp.includes and "tweets" in resp.includes:
        for t in resp.includes["tweets"]:
            includes_map[str(t.id)] = t

    result: List[Tuple[tweepy.Tweet, tweepy.Tweet]] = []
    for t in (all_tweets or []):
        resolved = t
        if getattr(t, "referenced_tweets", None):
            for ref in t.referenced_tweets:
                if ref.type in ("retweeted", "quoted", "replied_to"):
                    rt = includes_map.get(ref.id)
                    if rt:
                        resolved = rt
                        break
        result.append((t, resolved))

    logger.info(f"Fetched {len(result)} new tweets")
    return result

# ── 历史导入 ───────────────────────────────────────────────────────────────────
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
            append_row(sheet, meta, abstract, url, "")
        except Exception as e:
            logger.error(f"Historical processing failed for DOI {doi}: {e}")

# ── 实时导入 ───────────────────────────────────────────────────────────────────
def process_live():
    if not TW_BEARER_TOKEN:
        logger.error("TW_BEARER_TOKEN must be set for live imports")
        sys.exit(1)

    sheet = init_sheet()

    since_id: Optional[int] = None
    if SINCE_ID_FILE.exists():
        try:
            since_id = int(SINCE_ID_FILE.read_text().strip())
        except Exception:
            since_id = None

    tweet_pairs = fetch_new_tweets(since_id)  # [(tw, resolved_status)]

    for tw, status in tweet_pairs:
        # 从解析后的 tweet 的 entities 里拿 URL（优先 unwound_url）
        urls = []
        ents = getattr(status, "entities", None) or {}
        for u in ents.get("urls", []):
            src = u.get("unwound_url") or u.get("expanded_url") or u.get("url")
            if src:
                urls.append(src)

        for source in urls:
            doi = extract_doi(source)
            if not doi:
                logger.info(f"Skipping live URL (no DOI): {source}")
                continue
            try:
                meta = fetch_metadata(doi)
                abstract = fetch_abstract(doi)
                tweet_date = tw.created_at.isoformat() if getattr(tw, "created_at", None) else ""
                append_row(sheet, meta, abstract, source, tweet_date)
            except Exception as e:
                logger.error(f"Live processing failed for DOI {doi}: {e}")

        # ✅ 每处理一条 tweet，间隔 1 秒
        time.sleep(1)

# ── 入口 ────────────────────────────────────────────────────────────────────────
def main():
    if '--historical' in sys.argv:
        process_historical()
    else:
        process_live()

if __name__ == "__main__":
    main()
