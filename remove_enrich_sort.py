#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Utility script to perform three sequential tasks:
1. Remove duplicates based on DOI (Column G).
2. Enrich missing metadata (Title, Abstract, etc.).
3. Enforce date format (YYYY-MM-DD) and sort the sheet.
"""

import os
import re
import sys
import logging
from typing import List, Optional
import requests
import gspread
import xml.etree.ElementTree as ET
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

# ── CONFIG ─────────────────────────────────────────────────────────────────────
# !! These values must match your main script !!
SPREADSHEET_ID = "1oYdQyh1tqPA3821PE97ru8aL8jZOe1e7vKLp2x7BSF8"
SERVICE_ACCOUNT_FN = "service_account.json"
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Define which columns contain the required identifiers
DOI_COLUMN_INDEX = 6   # G (0-indexed)
URL_COLUMN_INDEX = 7   # H (0-indexed)

# ── LOGGING SETUP ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s"
)
logger = logging.getLogger(__name__)

# ── SHARED FUNCTIONS ───────────────────────────────────────────────────────────

def init_sheet():
    """Initializes and returns the Google Sheet client and the Sheet1 object."""
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FN, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        logger.info(f"Successfully connected to Google Sheet: {sheet.title}")
        return sheet
    except Exception as e:
        logger.error(f"Failed to initialize Google Sheet: {e}")
        logger.error("Please ensure 'service_account.json' is present and has access to the sheet.")
        sys.exit(1)

def extract_doi(url: str) -> Optional[str]:
    """Tries to extract DOI from URL, or by following redirects/parsing metadata."""
    m = re.search(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    
    try:
        head = requests.head(url, allow_redirects=True, timeout=12, headers=REQUEST_HEADERS)
        m2 = re.search(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", head.url)
        if m2:
            return m2.group(1)
    except Exception:
        pass
        
    try:
        html = requests.get(url, timeout=15, headers=REQUEST_HEADERS).text
        m3 = re.search(r'<meta[^>]+name=["\']citation_doi["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if m3:
            return m3.group(1).strip()
    except Exception:
        pass
        
    return None

def fetch_metadata(doi: str) -> dict:
    """Fetches CrossRef metadata for a given DOI."""
    api_url = f"https://api.crossref.org/works/{doi}"
    r = requests.get(api_url, timeout=15)
    r.raise_for_status()
    msg = r.json()["message"]

    title   = (msg.get("title") or [""])[0]
    journal = (msg.get("container-title") or [""])[0]
    authors = [f"{a.get('given','')} {a.get('family','')}".strip()
               for a in msg.get("author", [])]

    # Published date (priority: print > online)
    pub = msg.get("published-print") or msg.get("published-online") or {}
    parts = (pub.get("date-parts") or [[None]])[0]
    pub_date = "-".join(str(p) for p in parts if p is not None) if parts and parts[0] else ""

    # Year
    issued = msg.get("issued", {})
    year   = (issued.get("date-parts") or [[None]])[0][0]

    return {"title": title, "authors": authors, "journal": journal,
            "year": year, "pub_date": pub_date, "doi": doi}

def fetch_abstract(doi: str) -> str:
    """Fetches abstract text (Semantic Scholar primary, Crossref XML secondary)."""
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

def format_date(pub_date_str: str) -> str:
    """
    Ensures date is in YYYY-MM-DD format for consistent sorting.
    Pads missing month/day with '01'.
    """
    if not pub_date_str:
        return ""
    
    parts = pub_date_str.split('-')
    year = parts[0]
    
    if len(parts) == 1:
        # Only year (e.g., "2024") -> "2024-01-01"
        return f"{year}-01-01"
    elif len(parts) == 2:
        # Year-month (e.g., "2024-10") -> "2024-10-01"
        month = parts[1].zfill(2)
        return f"{year}-{month}-01"
    elif len(parts) >= 3:
        # Full date (e.g., "2024-10-30") - format parts for consistency
        try:
            month = parts[1].zfill(2)
            day = parts[2].zfill(2)
            return f"{year}-{month}-{day}"
        except IndexError:
            return pub_date_str # Return original if splitting fails oddly
    return pub_date_str

# ── DEDUPLICATION LOGIC ────────────────────────────────────────────────────────

def deduplicate_rows(sheet, all_data: List[List[str]]) -> bool:
    """
    Identifies and removes duplicate rows based on DOI (Column G).
    
    Args:
        sheet: gspread Worksheet object.
        all_data: List of lists containing all cell values (including header).
        
    Returns:
        True if rows were deleted, False otherwise.
    """
    if len(all_data) < 2:
        return False

    rows = all_data[1:]
    seen_keys = set()
    rows_to_delete = []  # Stores 1-based GSheet row numbers

    logger.info(f"Deduplication: Processing {len(rows)} data rows to find duplicates...")

    for i, row in enumerate(rows):
        sheet_row_num = i + 2  # 1-based sheet row number (skips header)
        
        try:
            key = row[DOI_COLUMN_INDEX].strip()
        except IndexError:
            logger.warning(f"Skipping row {sheet_row_num}: row is malformed.")
            continue

        if not key:
            continue

        if key in seen_keys:
            # This is a duplicate, mark its sheet row number for deletion
            rows_to_delete.append(sheet_row_num)
        else:
            # This is the first time we've seen this key.
            seen_keys.add(key)
    
    if not rows_to_delete:
        logger.info("Deduplication: No duplicates found.")
        return False

    logger.info(f"Deduplication: Found {len(rows_to_delete)} duplicate row(s). Preparing batch delete...")

    requests = []
    # Sort in reverse order to delete from the bottom up (prevents index shifting)
    for row_num in sorted(rows_to_delete, reverse=True):
        requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId": sheet.id,
                    "dimension": "ROWS",
                    "startIndex": row_num - 1,  # API is 0-indexed
                    "endIndex": row_num
                }
            }
        })
    
    try:
        body = {"requests": requests}
        # Use the spreadsheet object for raw batch API requests (fixes 'range' KeyError)
        sheet.spreadsheet.batch_update(body)
        logger.info(f"Deduplication: Successfully deleted {len(requests)} duplicate rows.")
        return True
    except APIError as e:
        logger.error(f"Deduplication: Google API Error: Failed to batch delete rows: {e.response.json()}")
    except Exception as e:
        logger.error(f"Deduplication: Failed to batch delete rows (General Error): {e}")
        return False

# ── MAIN LOGIC ─────────────────────────────────────────────────────────────────

def enrich_and_sort_sheet(sheet):
    """
    Orchestrates the deduplication, enrichment, date formatting, and sorting.
    """
    try:
        logger.info("Fetching all data from sheet for initial processing...")
        all_data = sheet.get_all_values()
    except Exception as e:
        logger.error(f"Could not fetch data from sheet: {e}")
        return

    # --- 1. Deduplication Step ---
    if len(all_data) >= 2:
        if deduplicate_rows(sheet, all_data):
            # If deletion happened, re-fetch data before enriching
            logger.info("Duplicates removed. Re-fetching fresh data for enrichment...")
            all_data = sheet.get_all_values()
    
    if len(all_data) < 2:
        logger.info("Sheet is empty or only contains a header. Stopping.")
        return

    # Data starts from the second row (index 1)
    header = all_data[0]
    
    rows_to_update = []
    
    logger.info(f"Processing {len(all_data) - 1} data rows for enrichment and date cleanup...")

    # --- 2. Enrichment and Date Formatting ---
    for i, row in enumerate(all_data[1:]):
        sheet_row_num = i + 2
        
        # Check if key fields (Title, Abstract, Pub Date) are blank
        needs_enrichment = not row[0].strip() or not row[5].strip() or not row[4].strip() 

        # --- Date Formatting Check (Always run this) ---
        original_pub_date = row[4].strip() if len(row) > 4 else ""
        formatted_date = format_date(original_pub_date)
        
        # Check if the date needs cleaning or formatting
        date_changed = False
        if formatted_date and formatted_date != original_pub_date:
            row[4] = formatted_date
            date_changed = True
            
        if date_changed or needs_enrichment:
            
            doi = row[DOI_COLUMN_INDEX].strip() if len(row) > DOI_COLUMN_INDEX else ""
            url = row[URL_COLUMN_INDEX].strip() if len(row) > URL_COLUMN_INDEX else ""

            # Try to get DOI from URL if missing
            if not doi and url:
                doi = extract_doi(url)
                if doi and len(row) > DOI_COLUMN_INDEX:
                    row[DOI_COLUMN_INDEX] = doi # Update DOI column if found

            if doi:
                try:
                    meta = fetch_metadata(doi)
                    abstract = fetch_abstract(doi)
                    
                    # Fill missing fields
                    if not row[0].strip(): row[0] = meta.get("title", row[0])
                    if not row[1].strip() and meta.get("authors"): row[1] = "; ".join(meta["authors"])
                    if not row[2].strip(): row[2] = meta.get("journal", row[2])
                    if not row[3].strip(): row[3] = meta.get("year", row[3]) or ""

                    # Re-run date formatting on the newly fetched date, if applicable
                    new_pub_date = meta.get("pub_date", "")
                    if new_pub_date:
                        row[4] = format_date(new_pub_date)
                    
                    if not row[5].strip() and abstract: row[5] = abstract
                    
                    rows_to_update.append(row)
                    logger.info(f"Row {sheet_row_num} enriched successfully (DOI: {doi})")

                except Exception as e:
                    logger.warning(f"Could not enrich row {sheet_row_num} (DOI: {doi}): {e}")
                    
    # --- 3. Batch Update Data ---
    if rows_to_update:
        # Since we modified the rows in the all_data list, we write back the 
        # entire modified data range (excluding header).
        range_to_update = f"A2:I{len(all_data)}" 
        data_to_write = all_data[1:]

        logger.info(f"Writing {len(data_to_write)} total data rows back to sheet (including updates)...")
        try:
            # Update the entire range A2:I(last_row)
            sheet.update(range_to_update, data_to_write, value_input_option="USER_ENTERED")
            logger.info("Batch enrichment update complete.")
        except Exception as e:
            logger.error(f"Failed to write batch update to sheet: {e}")
            
    else:
        logger.info("No data enrichment or date formatting was needed.")


    # --- 4. Final Sort by Column E (Pub Date) Ascending ---
    logger.info("Starting final sort by Column E (Pub Date) in ascending order.")
    
    try:
        # Sort by 5th column (E, Pub Date) in ascending order ("asc"). 
        # Ascending order means older dates are on top, newer dates (latest articles) are at the bottom.
        sheet.sort((5, "asc"))
        
        logger.info("Sheet sorted successfully: latest articles are at the bottom.")
    except APIError as e:
        logger.error(f"Google API Error: Failed to sort sheet: {e.response.json()}")
    except Exception as e:
        logger.error(f"Failed to sort sheet (General Error): {e}")


# ── MAIN EXECUTION ─────────────────────────────────────────────────────────────

def main():
    logger.info("Starting combined utility process (Deduplicate, Enrich, Sort)...")
    sheet = init_sheet()
    if sheet:
        enrich_and_sort_sheet(sheet)
    logger.info("Process finished.")

if __name__ == "__main__":
    main()
