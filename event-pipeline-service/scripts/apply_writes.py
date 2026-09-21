#!/usr/bin/env python3
"""Apply the approved HubSpot mutations. LIVE writes.

  python scripts/apply_writes.py <groupsdir> a|b [--live]

Stage a: 453 parent enrichments (enrich-only, never overwrite)
Stage b: 2,368 domain backfills (only onto a null domain)

Both stages RE-READ each record immediately before writing and drop any field
that is no longer blank, so the enrich-only guarantee holds even if the record
changed since the dry-run. Without --live it prints what it would do and exits.
"""
import json, os, sys, time
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "scripts")); os.chdir(HERE)

import requests
from dotenv import dotenv_values
TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "https://api.hubapi.com"
from dryrun_groups_abd import READ_PROPS, batch_read, blank


def batch_update(inputs):
    """POST batch/update in chunks of 100. Returns (ok, failures)."""
    ok, fails = 0, []
    for i in range(0, len(inputs), 100):
        chunk = inputs[i:i + 100]
        for attempt in range(5):
            r = requests.post(f"{BASE}/crm/v3/objects/companies/batch/update", headers=H,
                              json={"inputs": chunk}, timeout=60)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "2")) + 1); continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt); continue
            break
        if r.status_code < 300:
            ok += len(r.json().get("results", []))
        else:
            # fall back per-record so one bad row cannot kill 100
            for one in chunk:
                rr = requests.patch(f"{BASE}/crm/v3/objects/companies/{one['id']}",
                                    headers=H, json={"properties": one["properties"]}, timeout=45)
                if rr.status_code < 300:
                    ok += 1
                else:
                    fails.append((one["id"], rr.status_code, rr.text[:160]))
        print(f"   ...{min(i+100, len(inputs)):,}/{len(inputs):,}", flush=True)
    return ok, fails


def main():
    G, stage = sys.argv[1], sys.argv[2]
    live = "--live" in sys.argv

    if stage == "a":
        planned = json.load(open(os.path.join(G, "final_parent_enrich.json"), encoding="utf-8"))
        ids = sorted(planned)
        print(f"STAGE A: {len(ids):,} parents planned, "
              f"{sum(len(v) for v in planned.values()):,} fields")
        cur = batch_read(ids)
        inputs, dropped = [], Counter()
        for pid, adds in planned.items():
            props = cur.get(pid)
            if props is None:
                dropped["record_missing"] += 1; continue
            safe = {}
            for f, v in adds.items():
                if blank(props.get(f)):
                    safe[f] = v
                else:
                    dropped["now_populated:" + f] += 1     # refuse to overwrite
            if safe:
                inputs.append({"id": pid, "properties": safe})
        print(f"  after re-check: {len(inputs):,} companies, "
              f"{sum(len(i['properties']) for i in inputs):,} fields")
        if dropped:
            print(f"  dropped to protect existing values: {dict(dropped)}")
    else:
        D = json.load(open(os.path.join(G, "planD_writes.json"), encoding="utf-8"))
        ids = [d["hs_id"] for d in D]
        print(f"STAGE B: {len(D):,} domain backfills planned")
        cur = batch_read(ids)
        inputs, dropped = [], Counter()
        for d in D:
            props = cur.get(d["hs_id"])
            if props is None:
                dropped["record_missing"] += 1; continue
            if not blank(props.get("domain")):
                dropped["domain_now_set"] += 1; continue   # refuse to overwrite
            inputs.append({"id": d["hs_id"], "properties": {"domain": d["domain"]}})
        print(f"  after re-check: {len(inputs):,} records")
        if dropped:
            print(f"  dropped: {dict(dropped)}")

    if not live:
        print("\nDRY RUN (no --live). Nothing written.")
        return
    print(f"\nWRITING {len(inputs):,} records...", flush=True)
    t0 = time.time()
    ok, fails = batch_update(inputs)
    print(f"\nRESULT: {ok:,} updated OK, {len(fails):,} failed in {(time.time()-t0)/60:.1f} min")
    for f in fails[:15]:
        print(f"   FAIL id={f[0]} status={f[1]} {f[2]}")
    json.dump({"ok": ok, "fails": fails},
              open(os.path.join(G, f"result_stage_{stage}.json"), "w", encoding="utf-8"))


if __name__ == "__main__":
    main()
