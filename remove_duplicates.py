#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
A utility script to remove duplicate rows from the Google Sheet 
based on a unique key (DOI).

This script reads all data, identifies duplicates based on the 
DOI column (Column G), and batch-deletes them, keeping only 
the first occurrence of each DOI.
"""

import logging
import gspread
from gspread.exceptions import APIError  # <-- IMPORT THIS
from google.oauth2.service_account import Credentials

# ── CONFIG ─────────────────────────────────────────────────────────────────────
# !! These values must match your main script !!
SPREADSHEET_ID = "1oYdQyh1tqPA3821PE97ru8aL8jZOe1e7vKLp2x7BSF8"
SERVICE_ACCOUNT_FN = "service_account.json"

# Define which column to check for duplicates.
# Based on your script's `append_row` function:
# [Title, Authors, Journal, Year, Pub Date, Abstract, DOI, Source URL, Tweet Date]
# Column G (DOI) is at index 6 (0-indexed)
UNIQUE_KEY_COLUMN_INDEX = 6

# ── LOGGING SETUP ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s"
)
logger = logging.getLogger(__name__)

# ── GOOGLE SHEETS ──────────────────────────────────────────────────────────────

def init_sheet():
    """Initializes and returns the Google Sheet client."""
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
        return None

def deduplicate_sheet(sheet):
    """
    Fetches all data, finds duplicates based on the UNIQUE_KEY_COLUMN_INDEX,
    and batch-deletes the duplicate rows.
    """
    try:
        logger.info("Fetching all data from sheet...")
        all_data = sheet.get_all_values()
    except Exception as e:
        logger.error(f"Could not fetch data from sheet: {e}")
        return

    if not all_data or len(all_data) < 2:
        logger.info("Sheet is empty or has only a header row. No duplicates to remove.")
        return

    # Data rows start from the second row (index 1)
    header = all_data[0]
    rows = all_data[1:]
    
    seen_keys = set()
    rows_to_delete = []  # Stores 1-based GSheet row numbers

    logger.info(f"Processing {len(rows)} data rows to find duplicates...")

    # Iterate from top to bottom
    for i, row in enumerate(rows):
        # GSheet row number is 1-indexed, and we skipped the header
        # So, the data row at index `i` is at sheet row `i + 2`
        sheet_row_num = i + 2
        
        try:
            # Get the key (DOI) and strip whitespace
            key = row[UNIQUE_KEY_COLUMN_INDEX].strip()
        except IndexError:
            # This handles rows that might be shorter than expected
            logger.warning(f"Skipping row {sheet_row_num}: row is malformed or shorter than expected.")
            continue

        # Skip rows where the key is empty
        if not key:
            continue

        if key in seen_keys:
            # This is a duplicate, mark its sheet row number for deletion
            rows_to_delete.append(sheet_row_num)
        else:
            # This is the first time we've seen this key.
            seen_keys.add(key)
    
    # --- Perform batch deletion ---
    if not rows_to_delete:
        logger.info("No duplicates found.")
        return

    logger.info(f"Found {len(rows_to_delete)} duplicate row(s). Preparing batch delete...")

    # We must build the batch delete request by deleting from the
    # bottom up to avoid shifting row indices.
    requests = []
    for row_num in sorted(rows_to_delete, reverse=True):
        requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId": sheet.id,
                    "dimension": "ROWS",
                    "startIndex": row_num - 1,  # API is 0-indexed (row 2 is index 1)
                    "endIndex": row_num
                }
            }
        })
    
    try:
        # --- START OF FIX ---
        
        # We must wrap our list of requests in a dictionary with a "requests" key.
        body = {"requests": requests}
        
        # Call batch_update on the *spreadsheet* object, not the *sheet* object.
        # This sends the raw request without gspread trying to modify it.
        sheet.spreadsheet.batch_update(body)
        
        logger.info(f"Successfully deleted {len(requests)} duplicate rows.")
    
    # Catch the specific APIError for better debugging
    except APIError as e:
        logger.error(f"Google API Error: Failed to batch delete rows: {e.response.json()}")
    except Exception as e:
        # Catch any other unexpected errors
        logger.error(f"Failed to batch delete rows (General Error): {e}")
        
    # --- END OF FIX ---

# ── MAIN EXECUTION ─────────────────────────────────────────────────────────────

def main():
    logger.info("Starting deduplication process...")
    sheet = init_sheet()
    if sheet:
        deduplicate_sheet(sheet)
    logger.info("Process finished.")

if __name__ == "__main__":
    main()
