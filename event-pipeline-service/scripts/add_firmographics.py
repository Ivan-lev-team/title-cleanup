#!/usr/bin/env python3
"""
Add company firmographic columns to an already-enriched sheet, for a richer
HubSpot push. Reads the 'Company Domain' column, looks up each UNIQUE domain
once (Prospeo enrich-company, ~1 credit), and writes firmographics onto every
row with that domain -- qualified AND disqualified alike. Batched writes.

Usage: python3 scripts/add_firmographics.py <sheet_id> <tab_name_or_gid>
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sheets_client
import prospeo_client
import enrichment
import gspread.utils as gu

FIELDS = ["Estimated Revenue (USD)", "Revenue Range", "Employee Count", "Employee Range",
          "Industry", "HQ Location", "Company LinkedIn", "Founded"]


def main():
    sheet_id, tab = sys.argv[1], sys.argv[2]
    sh = sheets_client._gc.open_by_key(sheet_id)
    if tab.isdigit():
        ws = next((w for w in sh.worksheets() if w.id == int(tab)), sh.get_worksheet(0))
    else:
        ws = sh.worksheet(tab)
    values = ws.get_all_values()
    header = values[0]

    missing = [f for f in FIELDS if f not in header]
    if missing:
        start = len(header) + 1
        ws.update(values=[missing], range_name=gu.rowcol_to_a1(1, start), value_input_option="RAW")
        header = header + missing
    if "Company Domain" not in header:
        print("no Company Domain column -- nothing to do")
        return
    dom_idx = header.index("Company Domain")

    cache, updates = {}, []
    for i, row in enumerate(values[1:], start=2):
        row = row + [""] * (len(header) - len(row))
        dom = enrichment.clean_domain(row[dom_idx])
        if not dom:
            continue
        if dom not in cache:
            cache[dom] = prospeo_client.enrich_company_full(dom)
            print(f"{dom}: {'hit' if cache[dom] else 'miss'}", flush=True)
        fm = cache[dom]
        if not fm:
            continue
        for f in FIELDS:
            v = fm.get(f, "")
            if v != "":
                updates.append({"range": gu.rowcol_to_a1(i, header.index(f) + 1), "values": [[v]]})
        if len(updates) >= 400:
            ws.batch_update(updates, value_input_option="RAW")
            updates = []
            time.sleep(1)
    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    hits = sum(1 for v in cache.values() if v)
    print(f"DONE: {len(cache)} unique domains, {hits} enriched")


if __name__ == "__main__":
    main()
