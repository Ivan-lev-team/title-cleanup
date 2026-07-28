#!/usr/bin/env python3
"""
Ad-hoc list enrichment: point at ANY Google Sheet + tab and run the ENRICH_ONLY
flow (pipeline.enrich_row) over every unprocessed row, writing results back in
BATCHED updates (so a big list doesn't blow past Google's write rate limit).

Usage:
    python3 scripts/enrich_sheet.py <sheet_id> "<tab name>" [--mobile]

Email-only by default (mobile is the expensive tier); pass --mobile to include
mobile enrichment for Qualified prospects. Makes NO HubSpot writes -- HubSpot is
read-only here (dedup / customer / open-deal / existing revenue).
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
import sheets_client
import pipeline


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    sheet_id, tab = sys.argv[1], sys.argv[2]
    if "--mobile" not in sys.argv:
        config.ENRICH_MOBILE = False  # email-only unless explicitly asked

    gc = sheets_client._gc
    sh = gc.open_by_key(sheet_id)
    if tab.isdigit():  # allow passing a gid instead of the (often messy) tab name
        ws = next((w for w in sh.worksheets() if w.id == int(tab)), sh.get_worksheet(0))
    else:
        ws = sh.worksheet(tab)
    values = ws.get_all_values()
    header = sheets_client._ensure_result_columns(ws, values[0])
    status_idx = header.index(sheets_client.STATUS_COL)

    import gspread.utils as gu
    updates = []          # batched {range, values}
    processed = 0
    for i, row in enumerate(values[1:], start=2):
        row = row + [""] * (len(header) - len(row))
        if row[status_idx].strip():
            continue  # already done
        rd = dict(zip(header, row))
        if not (rd.get("First Name", "").strip() or rd.get("Company Name", "").strip()
                or rd.get("Email", "").strip()):
            continue  # blank row
        try:
            res = pipeline.enrich_row(rd)
        except Exception as e:
            res = {"Pipeline Status": "Error", "Notes": f"{type(e).__name__}: {e}"}
        for col_name, val in res.items():
            if col_name in header:
                a1 = gu.rowcol_to_a1(i, header.index(col_name) + 1)
                updates.append({"range": a1, "values": [[val]]})
        processed += 1
        print(f"row {i}: {res.get('Pipeline Status')} | {res.get('ICP Verdict') or ''} "
              f"| {res.get('Qualification') or ''} | {res.get('Company Domain', '')}", flush=True)
        # flush writes in chunks so progress is visible and payloads stay small
        if len(updates) >= 400:
            ws.batch_update(updates, value_input_option="RAW")
            updates = []
            time.sleep(1)
    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    print(f"DONE: processed {processed} rows")


if __name__ == "__main__":
    main()
