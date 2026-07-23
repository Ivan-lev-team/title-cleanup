"""
Reads new rows from the Google Sheet (mirroring the Contact Import /
Company Import tabs from the Levanta Event Import Template) and writes
results back into status columns so non-technical users see outcomes
without leaving the spreadsheet.

Expects the sheet to have a "Pipeline Status" column -- add it once,
manually, as the last column on each tab. Blank/missing = not yet
processed. Anything else = already handled, skipped on future polls.
"""
import gspread
from google.oauth2.service_account import Credentials

import config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

STATUS_COL = "Pipeline Status"
RESULT_COLS = [
    STATUS_COL,
    "ICP Verdict",
    "Already in HubSpot?",
    "HubSpot Contact ID",
    "HubSpot Company ID",
    "Notes",
]

_creds = Credentials.from_service_account_file(config.GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES)
_gc = gspread.authorize(_creds)


def _ensure_result_columns(worksheet, header):
    """Adds any missing result columns to the sheet, once, so writes never
    fail on a column that doesn't exist yet."""
    missing = [c for c in RESULT_COLS if c not in header]
    if not missing:
        return header
    start_col = len(header) + 1
    worksheet.update(
        f"{gspread.utils.rowcol_to_a1(1, start_col)}:{gspread.utils.rowcol_to_a1(1, start_col + len(missing) - 1)}",
        [missing],
    )
    return header + missing


def get_unprocessed_rows(sheet_name):
    """Returns a list of (row_number, row_dict) for every row that hasn't
    been marked with a Pipeline Status yet. row_number is 1-indexed and
    matches the real sheet row, for writing results back later."""
    sh = _gc.open_by_key(config.GOOGLE_SHEET_ID)
    ws = sh.worksheet(sheet_name)
    all_values = ws.get_all_values()
    if not all_values:
        return ws, []

    header = all_values[0]
    header = _ensure_result_columns(ws, header)
    status_idx = header.index(STATUS_COL)

    unprocessed = []
    for i, row in enumerate(all_values[1:], start=2):  # sheet row 2 = first data row
        row = row + [""] * (len(header) - len(row))  # pad short rows
        if row[status_idx].strip():
            continue  # already processed
        row_dict = dict(zip(header, row))
        # skip genuinely empty rows and the template's example/instruction row
        if not row_dict.get("First Name", "").strip() and not row_dict.get("Company Name", "").strip():
            continue
        unprocessed.append((i, row_dict, header))
    return ws, unprocessed


def write_result(worksheet, row_number, header, results):
    """results: dict subset of RESULT_COLS -> value."""
    for col_name, value in results.items():
        if col_name not in header:
            continue
        col_idx = header.index(col_name) + 1
        worksheet.update_cell(row_number, col_idx, value)


def get_company_import_by_domain():
    """Reads the whole Company Import tab once per poll cycle and returns
    {domain_lower: row_dict}, so pipeline.py can enrich a brand-new company
    with the richer fields that only live on that tab (Industry, Company
    Type, Employees, Revenue, LinkedIn Company Page, Company Owner,
    Amazon Storefront URL, ...) instead of just name+domain from the
    Contact Import row. Company Domain is the required dedupe/association
    key on that tab, exactly as the template's instructions say."""
    sh = _gc.open_by_key(config.GOOGLE_SHEET_ID)
    ws = sh.worksheet(config.COMPANY_SHEET_NAME)
    all_values = ws.get_all_values()
    if not all_values:
        return {}

    header = all_values[0]
    by_domain = {}
    for row in all_values[1:]:
        row = row + [""] * (len(header) - len(row))
        row_dict = dict(zip(header, row))
        domain = row_dict.get("Company Domain", "").strip().lower()
        if not domain:
            continue  # required field on this tab -- skip anything without it, per template
        by_domain[domain] = row_dict
    return by_domain
