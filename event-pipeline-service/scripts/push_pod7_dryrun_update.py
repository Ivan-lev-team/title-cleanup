#!/usr/bin/env python3
"""Batch-PATCH pod/hubspot_owner_id/sdr_owner onto the Pod 7 dry-run slice.

Follows the same pattern as scripts/push_batch.py: token from .env,
100-record batches against the real HubSpot batch API, retry on 429/5xx,
resumable via a written log. Dry-run unless --live.

  python scripts/push_pod7_dryrun_update.py <csv> <rundir> [--live] [--skip-ids id1,id2,...]
"""
import csv, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)
import requests
from dotenv import dotenv_values

TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "https://api.hubapi.com"


def post(url, body, tries=6):
    for a in range(tries):
        r = requests.post(url, headers=H, json=body, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")) + 1); continue
        if r.status_code >= 500:
            time.sleep(2 ** a); continue
        return r
    return r


def main():
    csv_path, rundir = sys.argv[1], sys.argv[2]
    live = "--live" in sys.argv
    skip_ids = set()
    for i, a in enumerate(sys.argv):
        if a == "--skip-ids" and i + 1 < len(sys.argv):
            skip_ids = set(sys.argv[i + 1].split(","))

    os.makedirs(rundir, exist_ok=True)
    log_path = os.path.join(rundir, "updated_companies.jsonl")
    done = set()
    if os.path.exists(log_path):
        for line in open(log_path, encoding="utf-8"):
            try:
                done.add(json.loads(line)["id"])
            except Exception:
                pass
    print(f"already updated in a previous run of this script: {len(done):,}")

    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    todo = [r for r in rows if r["hs_object_id"] not in done and r["hs_object_id"] not in skip_ids]
    print(f"total rows: {len(rows):,}  skip(already pushed manually): {len(skip_ids):,}  "
          f"skip(already done by this script): {len(done):,}  todo: {len(todo):,}")

    if not live:
        print("\nDRY RUN (no --live). Nothing written.")
        sample = todo[:3]
        for r in sample:
            print(" ", r["hs_object_id"], r["name"], "->", r["sdr_owner_name"])
        return

    updated, failed = [], []
    t0 = time.time()
    flog = open(log_path, "a", encoding="utf-8")
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        body = {"inputs": [
            {"id": r["hs_object_id"], "properties": {
                "pod": r["pod"],
                "hubspot_owner_id": r["hubspot_owner_id"],
                "sdr_owner": r["sdr_owner_id"],
            }} for r in chunk
        ]}
        r = post(f"{BASE}/crm/v3/objects/companies/batch/update", body)
        if r.status_code < 300:
            for c in chunk:
                updated.append(c["hs_object_id"])
                flog.write(json.dumps({"id": c["hs_object_id"]}) + "\n")
        else:
            # per-record fallback so one bad row cannot kill the batch
            for c in chunk:
                rr = requests.patch(
                    f"{BASE}/crm/v3/objects/companies/{c['hs_object_id']}", headers=H,
                    json={"properties": {
                        "pod": c["pod"], "hubspot_owner_id": c["hubspot_owner_id"],
                        "sdr_owner": c["sdr_owner_id"],
                    }}, timeout=45)
                if rr.status_code < 300:
                    updated.append(c["hs_object_id"])
                    flog.write(json.dumps({"id": c["hs_object_id"]}) + "\n")
                else:
                    failed.append((c["hs_object_id"], rr.status_code, rr.text[:150]))
        flog.flush()
        print(f"   {min(i+100, len(todo)):,}/{len(todo):,}  updated={len(updated):,} "
              f"failed={len(failed):,}  {(time.time()-t0)/60:.1f}m", flush=True)
    flog.close()

    print(f"\nRESULT: updated {len(updated):,} | failed {len(failed):,}")
    for f in failed[:10]:
        print("   FAIL", f)
    json.dump({"updated": len(updated), "failed": failed},
              open(os.path.join(rundir, f"update_result_{len(updated)}.json"), "w",
                   encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
