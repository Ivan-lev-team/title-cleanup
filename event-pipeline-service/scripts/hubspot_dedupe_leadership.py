"""
Checks contacts against HubSpot by LinkedIn URL (hs_linkedin_url /
linkedin_personal_url) and by email, and splits into net-new vs
already-in-HubSpot. Same batched-search pattern as hubspot_linkedin_dedupe.py,
generalized to take CLI args instead of a hardcoded path.

Usage: python hubspot_dedupe_leadership.py <input.csv> <outdir>
Input CSV must have "LinkedIn Profile" and/or "Email" columns.
"""
import csv
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
TOKEN = dotenv_values(str(ENV_PATH))["HUBSPOT_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
URL = "https://api.hubapi.com/crm/v3/objects/contacts/search"
BATCH = 100


def handle(url):
    m = re.search(r"linkedin\.com/in/([a-z0-9\-_%\.]+)", (url or "").strip().lower())
    return m.group(1).rstrip("/") if m else ""


def search_by_linkedin(handles):
    matched = {}
    batches = [handles[i:i + BATCH] for i in range(0, len(handles), BATCH)]
    for bi, b in enumerate(batches):
        hs_vals = [f"https://linkedin.com/in/{h}" for h in b]
        li_vals = [f"https://www.linkedin.com/in/{h}" for h in b]
        payload = {
            "filterGroups": [
                {"filters": [{"propertyName": "hs_linkedin_url", "operator": "IN", "values": hs_vals}]},
                {"filters": [{"propertyName": "linkedin_personal_url", "operator": "IN", "values": li_vals}]},
            ],
            "properties": ["hs_linkedin_url", "linkedin_personal_url", "email"],
            "limit": 100,
        }
        after = None
        while True:
            if after:
                payload["after"] = after
            for attempt in range(5):
                r = requests.post(URL, headers=H, json=payload, timeout=60)
                if r.status_code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
                break
            data = r.json()
            for c in data.get("results", []):
                p = c.get("properties", {})
                h = handle(p.get("hs_linkedin_url")) or handle(p.get("linkedin_personal_url"))
                if h and h not in matched:
                    matched[h] = c["id"]
            after = (data.get("paging", {}).get("next", {}) or {}).get("after")
            if not after:
                break
            time.sleep(0.25)
        print(f"  linkedin batch {bi+1}/{len(batches)}: running {len(matched)} matched", flush=True)
        time.sleep(0.3)
    return matched


def search_by_email(emails):
    matched = {}
    batches = [emails[i:i + BATCH] for i in range(0, len(emails), BATCH)]
    for bi, b in enumerate(batches):
        payload = {
            "filterGroups": [{"filters": [{"propertyName": "email", "operator": "IN", "values": b}]}],
            "properties": ["email"],
            "limit": 100,
        }
        after = None
        while True:
            if after:
                payload["after"] = after
            for attempt in range(5):
                r = requests.post(URL, headers=H, json=payload, timeout=60)
                if r.status_code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
                break
            data = r.json()
            for c in data.get("results", []):
                em = (c.get("properties", {}).get("email") or "").lower()
                if em and em not in matched:
                    matched[em] = c["id"]
            after = (data.get("paging", {}).get("next", {}) or {}).get("after")
            if not after:
                break
            time.sleep(0.25)
        print(f"  email batch {bi+1}/{len(batches)}: running {len(matched)} matched", flush=True)
        time.sleep(0.3)
    return matched


def main():
    if len(sys.argv) != 3:
        print("Usage: python hubspot_dedupe_leadership.py <input.csv> <outdir>")
        sys.exit(1)
    in_path, outdir = sys.argv[1], Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)

    rows = list(csv.DictReader(open(in_path, encoding="utf-8-sig")))
    print(f"{len(rows)} contacts loaded", flush=True)

    li_col = "LinkedIn Profile" if rows and "LinkedIn Profile" in rows[0] else None
    email_col = "Email" if rows and "Email" in rows[0] else None

    handle_map = {}
    if li_col:
        for i, r in enumerate(rows):
            h = handle(r.get(li_col))
            if h:
                handle_map.setdefault(h, []).append(i)
        handles = sorted(handle_map)
        print(f"{len(handles)} unique LinkedIn handles", flush=True)
        li_found = search_by_linkedin(handles) if handles else {}
    else:
        li_found = {}
        print("No LinkedIn Profile column found -- skipping LinkedIn match", flush=True)

    email_map = {}
    if email_col:
        for i, r in enumerate(rows):
            e = (r.get(email_col) or "").strip().lower()
            if e:
                email_map.setdefault(e, []).append(i)
        emails = sorted(email_map)
        print(f"{len(emails)} unique emails", flush=True)
        email_found = search_by_email(emails) if emails else {}
    else:
        email_found = {}
        print("No Email column found -- skipping email match", flush=True)

    in_hubspot_idx = set()
    for i, r in enumerate(rows):
        h = handle(r.get(li_col)) if li_col else ""
        e = (r.get(email_col) or "").strip().lower() if email_col else ""
        hs_id = li_found.get(h) or email_found.get(e)
        if hs_id:
            r["In HubSpot?"] = "Yes"
            r["HubSpot Contact Id"] = hs_id
            in_hubspot_idx.add(i)
        else:
            r["In HubSpot?"] = "No"
            r["HubSpot Contact Id"] = ""

    fields = list(rows[0].keys()) if rows else []

    net_new = [r for i, r in enumerate(rows) if i not in in_hubspot_idx]
    already = [r for i, r in enumerate(rows) if i in in_hubspot_idx]

    with open(outdir / "HUBSPOT_net_new_contacts.csv", "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        for r in net_new:
            wr.writerow(r)

    with open(outdir / "HUBSPOT_already_exists_contacts.csv", "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        for r in already:
            wr.writerow(r)

    print(f"\nDONE: {len(already)}/{len(rows)} already in HubSpot | {len(net_new)} net-new")


if __name__ == "__main__":
    main()
