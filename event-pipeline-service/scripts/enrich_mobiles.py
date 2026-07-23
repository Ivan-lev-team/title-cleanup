#!/usr/bin/env python3
"""
One-off maintenance job (NOT run by the polling service): backfills mobile
numbers for contacts already pushed under a given event drill-down value
that are missing a phone. Ported from the validated /hubspot-push version --
same Prospeo bulk-enrich-person call, just reusing this service's
config.py/hubspot_client.py instead of its own env handling.

Mobiles cost 10 Prospeo credits each (only when found), which is why this is
a separate, manually-run script rather than part of the live per-row
pipeline -- the live pipeline only ever spends the 1-credit email lookup.

Usage:
    cd event-pipeline-service
    python3 scripts/enrich_mobiles.py "Social Commerce Summit 2026 NYC"
"""
import os
import sys
import time
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
import hubspot_client

PROSPEO_URL = "https://api.prospeo.io/bulk-enrich-person"
DRILLDOWN_PROP = "how_did_you_hear_about_us___drill_down"


def val(r, p):
    return (r["properties"].get(p) or "").strip()


def collect_candidates(event_name):
    rows, after = [], None
    while True:
        body = {
            "filterGroups": [{"filters": [{"propertyName": DRILLDOWN_PROP, "operator": "EQ", "value": event_name}]}],
            "properties": ["email", "phone", "mobilephone", "hs_linkedin_url", "firstname", "lastname"],
            "limit": 100,
        }
        if after:
            body["after"] = after
        resp = hubspot_client.request_with_retry(
            "POST", f"{hubspot_client.BASE}/crm/v3/objects/contacts/search", json=body
        )
        d = resp.json()
        rows += d.get("results", [])
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    cands = []
    for r in rows:
        if val(r, "phone") or val(r, "mobilephone"):
            continue  # already has a phone
        if not (val(r, "email") or val(r, "hs_linkedin_url")):
            continue  # no identifier to match on
        cands.append(r)
    return cands


def chunk(lst, n=50):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def main():
    if len(sys.argv) < 2:
        sys.exit('Usage: python3 scripts/enrich_mobiles.py "Event Name"')
    if not config.PROSPEO_KEY:
        sys.exit("Set PROSPEO_KEY in .env first.")
    event_name = sys.argv[1]
    pros_headers = {"X-KEY": config.PROSPEO_KEY, "Content-Type": "application/json"}

    cands = collect_candidates(event_name)
    print(f"{len(cands)} contacts missing a phone with a usable identifier")
    if not cands:
        sys.exit("Nothing to enrich.")

    results = {}
    total_cost = 0
    for bi, batch in enumerate(chunk(cands, 50)):
        data = []
        for r in batch:
            item = {"identifier": r["id"]}
            if val(r, "hs_linkedin_url"):
                item["linkedin_url"] = val(r, "hs_linkedin_url")
            if val(r, "email"):
                item["email"] = val(r, "email")
            fn, ln = val(r, "firstname"), val(r, "lastname")
            if fn or ln:
                item["full_name"] = (fn + " " + ln).strip()
            data.append(item)
        payload = {"only_verified_email": False, "enrich_mobile": True, "only_verified_mobile": False, "data": data}
        resp = requests.post(PROSPEO_URL, headers=pros_headers, json=payload, timeout=120)
        if resp.status_code >= 300:
            print(f"  batch {bi}: Prospeo error {resp.status_code} {resp.text[:300]}")
            continue
        d = resp.json()
        total_cost += d.get("total_cost", 0)
        matched = d.get("matched", [])
        found = 0
        for m in matched:
            cid = m["identifier"]
            mob = (m.get("person") or {}).get("mobile") or {}
            num = (mob.get("mobile_international") or mob.get("mobile") or "").strip()
            if num and mob.get("revealed"):
                results[cid] = num
                found += 1
        print(f"  batch {bi}: {len(batch)} sent, {len(matched)} matched, {found} with mobile, cost so far {total_cost}")
        time.sleep(1)

    updated = 0
    for cid, num in results.items():
        try:
            hubspot_client.update_contact(cid, {"mobilephone": num, "phone": num})
            updated += 1
        except RuntimeError as e:
            print(f"  update {cid} failed: {e}")

    print("\n--- MOBILE ENRICHMENT SUMMARY ---")
    print(f"Candidates queried:          {len(cands)}")
    print(f"Mobiles found:               {len(results)}")
    print(f"Contacts updated in HubSpot: {updated}")
    print(f"Total Prospeo credits spent: {total_cost}")


if __name__ == "__main__":
    main()
