#!/usr/bin/env python3
"""Repoint the 463 Amazon-URL collision records at their real brand domain.

For each: move the existing amazon.com value into amazon_storefront_url, then
write the brand domain into `domain`. This OVERWRITES `domain` (it currently
holds amazon.com), so it is deliberately outside the enrich-only rule.

Safety rules kept:
  - amazon_storefront_url is only written when currently BLANK (44,708 records
    already use that field; we never clobber an existing storefront URL).
  - a record is skipped if its `domain` is no longer an Amazon value at write
    time (someone else fixed it first).

  python scripts/fix_amazon_collisions.py <collisions.json> [--live] [--limit N]
"""
import json, os, re, sys, time
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "scripts")); os.chdir(HERE)

import requests
from dotenv import dotenv_values
TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "https://api.hubapi.com"

AMZ = re.compile(r"amazon\.|amzn\.to|sellercentral", re.I)
SHOW = ["name", "domain", "amazon_storefront_url", "website", "pod", "sdr_owner",
        "num_associated_contacts", "lifecyclestage", "city", "state", "country"]


def batch_read(ids, props):
    out = {}
    for i in range(0, len(ids), 100):
        chunk = [{"id": str(x)} for x in ids[i:i + 100]]
        for a in range(5):
            r = requests.post(f"{BASE}/crm/v3/objects/companies/batch/read", headers=H,
                              json={"inputs": chunk, "properties": props}, timeout=45)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "2")) + 1); continue
            break
        if r.status_code < 300:
            for rec in r.json().get("results", []):
                out[rec["id"]] = rec.get("properties", {}) or {}
    return out


def build(rows, cur):
    """-> (inputs, skips)"""
    inputs, skips = [], Counter()
    for c in rows:
        p = cur.get(c["hs_id"])
        if p is None:
            skips["record_missing"] += 1; continue
        dom = (p.get("domain") or "").strip()
        if not AMZ.search(dom):
            skips["domain_no_longer_amazon"] += 1; continue
        props = {"domain": c["push_domain"]}
        if not (p.get("amazon_storefront_url") or "").strip():
            props["amazon_storefront_url"] = dom
        else:
            skips["storefront_url_already_set_kept"] += 1
        inputs.append({"id": c["hs_id"], "properties": props})
    return inputs, skips


def main():
    path = sys.argv[1]
    live = "--live" in sys.argv
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    rows = json.load(open(path, encoding="utf-8"))["confident"]
    if limit:
        rows = rows[:limit]
    print(f"collision records to fix: {len(rows):,}")
    cur = batch_read([c["hs_id"] for c in rows], SHOW)
    inputs, skips = build(rows, cur)
    print(f"will update: {len(inputs):,}   skips: {dict(skips)}")

    if not live:
        print("\n=== DRY RUN, full before/after ===")
        for c in rows[:5]:
            p = cur.get(c["hs_id"], {})
            inp = next((i for i in inputs if i["id"] == c["hs_id"]), None)
            if not inp:
                continue
            after = dict(p); after.update(inp["properties"])
            print(f"\nHubSpot id {c['hs_id']}   push match: {c['push_name']}")
            print(f"  {'property':26} {'BEFORE':32} {'AFTER':32}")
            for f in SHOW:
                b, a = p.get(f), after.get(f)
                mark = "  <== CHANGED" if str(b) != str(a) else ""
                print(f"  {f:26} {str(b)[:30]:32} {str(a)[:30]:32}{mark}")
            changed = [f for f in set(list(p) + list(after)) if str(p.get(f)) != str(after.get(f))]
            print(f"  fields changed: {sorted(changed)}")
        print("\nNOTHING WRITTEN (no --live).")
        return

    print(f"\nWRITING {len(inputs):,} records...")
    ok, fails = 0, []
    for i in range(0, len(inputs), 100):
        chunk = inputs[i:i + 100]
        for a in range(5):
            r = requests.post(f"{BASE}/crm/v3/objects/companies/batch/update", headers=H,
                              json={"inputs": chunk}, timeout=60)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "2")) + 1); continue
            if r.status_code >= 500:
                time.sleep(2 ** a); continue
            break
        if r.status_code < 300:
            ok += len(r.json().get("results", []))
        else:
            for one in chunk:
                rr = requests.patch(f"{BASE}/crm/v3/objects/companies/{one['id']}", headers=H,
                                    json={"properties": one["properties"]}, timeout=45)
                if rr.status_code < 300:
                    ok += 1
                else:
                    fails.append((one["id"], rr.status_code, rr.text[:150]))
        print(f"   ...{min(i+100, len(inputs)):,}/{len(inputs):,}", flush=True)
    print(f"\nRESULT: {ok:,} updated, {len(fails):,} failed")
    for f in fails[:10]:
        print("   FAIL", f)
    json.dump({"ok": ok, "fails": fails, "ids": [i["id"] for i in inputs]},
              open(os.path.join(os.path.dirname(path), "result_amazon_fix.json"),
                   "w", encoding="utf-8"))


if __name__ == "__main__":
    main()
