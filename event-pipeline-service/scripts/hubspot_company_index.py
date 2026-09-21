#!/usr/bin/env python3
"""Page every HubSpot company to a local JSONL cache.

Uses GET /crm/v3/objects/companies (the paging list endpoint). The Search API
caps at 10,000 results per query, so it cannot be used for a full pull.

  python scripts/hubspot_company_index.py <out.jsonl>

Read-only. Resumable: re-running continues from the last saved `after` cursor.
"""
import json, os, sys, time
sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

import requests
from dotenv import dotenv_values

TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
BASE = "https://api.hubapi.com"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
PROPS = ["name", "domain", "website", "pod", "sdr_owner",
         "num_associated_contacts", "lifecyclestage", "hs_object_id"]


def get(url, params, tries=6):
    for a in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=45)
        except requests.RequestException:
            time.sleep(2 ** a)
            continue
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")) + 1)
            continue
        if r.status_code >= 500:
            time.sleep(2 ** a)
            continue
        return r
    return None


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "hubspot_companies.jsonl"
    cur_path = out_path + ".cursor"
    after = None
    mode = "w"
    if os.path.exists(out_path) and os.path.exists(cur_path):
        after = open(cur_path, encoding="utf-8").read().strip() or None
        if after:
            mode = "a"
            print(f"resuming from cursor {after}")
    n = 0
    t0 = time.time()
    with open(out_path, mode, encoding="utf-8") as f:
        while True:
            params = {"limit": 100, "properties": ",".join(PROPS)}
            if after:
                params["after"] = after
            r = get(f"{BASE}/crm/v3/objects/companies", params)
            if r is None or r.status_code >= 300:
                print(f"STOP: {getattr(r,'status_code',None)} {getattr(r,'text','')[:200]}")
                break
            data = r.json()
            for rec in data.get("results", []):
                p = rec.get("properties", {}) or {}
                f.write(json.dumps({
                    "id": rec.get("id"),
                    "name": p.get("name") or "",
                    "domain": p.get("domain") or "",
                    "website": p.get("website") or "",
                    "pod": p.get("pod") or "",
                    "sdr_owner": p.get("sdr_owner") or "",
                    "contacts": p.get("num_associated_contacts") or "0",
                    "lifecyclestage": p.get("lifecyclestage") or "",
                }, ensure_ascii=False) + "\n")
                n += 1
            after = (data.get("paging", {}).get("next", {}) or {}).get("after")
            open(cur_path, "w", encoding="utf-8").write(after or "")
            if n % 5000 < 100:
                f.flush()
                print(f"  {n:,} companies  {(time.time()-t0)/60:.1f}m", flush=True)
            if not after:
                break
    print(f"DONE {n:,} companies in {(time.time()-t0)/60:.1f} min -> {out_path}")


if __name__ == "__main__":
    main()
