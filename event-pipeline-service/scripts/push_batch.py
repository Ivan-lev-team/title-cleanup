#!/usr/bin/env python3
"""Create net-new companies in HubSpot at scale, then fire their typeId 14
parent links. Resumable via a created-log. Dry-run unless --live.

  python scripts/push_batch.py <PUSH_LIST.csv> <export.csv> <rundir> --limit 1000 [--live]

Traceability: no new property is used. Every record this script creates carries
hs_object_source_id = 42260004 (this private app), which is distinct from the
concurrent [Admin Synced] integration (1873202). Combined with
lifecyclestage='Net New' + pod='Pod RevOps' + createdate, the batch is isolable.
"""
import csv, json, os, sys, time
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8")
csv.field_size_limit(10 ** 8)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "scripts")); os.chdir(HERE)
import requests
from dotenv import dotenv_values
from resolve_dedupe_groups import norm_domain
import import_dryrun as M
from push_test_batch import build

TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "https://api.hubapi.com"
PARENT_TYPE_ID = 14


def post(url, body, tries=6):
    for a in range(tries):
        r = requests.post(url, headers=H, json=body, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")) + 1); continue
        if r.status_code >= 500:
            time.sleep(2 ** a); continue
        return r
    return r


def existing_domains(domains):
    """Batched IN search -- 100 domains per query instead of one query each."""
    found = set()
    for i in range(0, len(domains), 100):
        chunk = domains[i:i + 100]
        body = {"filterGroups": [{"filters": [{"propertyName": "domain",
                                               "operator": "IN", "values": chunk}]}],
                "properties": ["domain"], "limit": 100}
        r = post(f"{BASE}/crm/v3/objects/companies/search", body)
        if r.status_code < 300:
            for x in r.json().get("results", []):
                d = (x["properties"].get("domain") or "").strip().lower()
                if d:
                    found.add(d)
    return found


def main():
    push_csv, export_csv, rundir = sys.argv[1:4]
    live = "--live" in sys.argv
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0

    supp = set(json.load(open(os.path.join(rundir, "groups", "all_suppress_domains.json"), encoding="utf-8")))
    links = {l["child_domain"]: l for l in
             json.load(open(os.path.join(rundir, "groups", "final_links.json"), encoding="utf-8"))}
    log_path = os.path.join(rundir, "created_companies.jsonl")
    done = set()
    if os.path.exists(log_path):
        for line in open(log_path, encoding="utf-8"):
            try:
                done.add(json.loads(line)["domain"])
            except Exception:
                pass
    print(f"already created in a previous run: {len(done):,}")

    push = {}
    for r in csv.DictReader(open(push_csv, encoding="utf-8-sig")):
        d = norm_domain(r["domain"])
        if d not in supp and d not in done:
            push[d] = r

    cands = []
    for r in csv.DictReader(open(export_csv, encoding="utf-8-sig")):
        d = norm_domain(r.get("domain"))
        pr = push.get(d)
        if not pr:
            continue
        rec, _ = build(r, pr["name"])
        cands.append({"domain": d, "rec": rec})
    cands.sort(key=lambda c: c["domain"])          # deterministic, resumable
    if limit:
        cands = cands[:limit]
    print(f"candidates this run: {len(cands):,}")

    print("checking which already exist in HubSpot...")
    exist = existing_domains([c["domain"] for c in cands])
    todo = [c for c in cands if c["domain"] not in exist]
    print(f"  already present: {len(cands)-len(todo):,}   to create: {len(todo):,}")

    if not live:
        print("\nDRY RUN (no --live). Nothing written.")
        print("sample payload:", json.dumps(todo[0]["rec"], indent=1)[:600] if todo else "n/a")
        return

    portal = requests.get(f"{BASE}/account-info/v3/details", headers=H, timeout=30)
    pid = portal.json().get("portalId") if portal.status_code < 300 else "PORTAL"
    created, failed = [], []
    t0 = time.time()
    flog = open(log_path, "a", encoding="utf-8")
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        r = post(f"{BASE}/crm/v3/objects/companies/batch/create",
                 {"inputs": [{"properties": c["rec"]} for c in chunk]})
        if r.status_code < 300:
            res = r.json().get("results", [])
            bydom = {(x["properties"].get("domain") or "").lower(): x["id"] for x in res}
            for c in chunk:
                cid = bydom.get(c["domain"])
                if cid:
                    created.append({"domain": c["domain"], "id": cid})
                    flog.write(json.dumps({"domain": c["domain"], "id": cid}) + "\n")
                else:
                    failed.append((c["domain"], "no-id-returned", ""))
        else:
            # per-record fallback so one bad row cannot kill 100
            for c in chunk:
                rr = requests.post(f"{BASE}/crm/v3/objects/companies", headers=H,
                                   json={"properties": c["rec"]}, timeout=45)
                if rr.status_code < 300:
                    cid = rr.json()["id"]
                    created.append({"domain": c["domain"], "id": cid})
                    flog.write(json.dumps({"domain": c["domain"], "id": cid}) + "\n")
                else:
                    failed.append((c["domain"], rr.status_code, rr.text[:150]))
        flog.flush()
        print(f"   {min(i+100, len(todo)):,}/{len(todo):,}  created={len(created):,} "
              f"failed={len(failed):,}  {(time.time()-t0)/60:.1f}m", flush=True)
    flog.close()

    # ---- parent links, only for the ones just created ----
    linked = fail_link = 0
    for c in created:
        l = links.get(c["domain"])
        if not l:
            continue
        r = requests.put(f"{BASE}/crm/v4/objects/companies/{c['id']}/associations/"
                         f"companies/{l['parent_id']}", headers=H,
                         json=[{"associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": PARENT_TYPE_ID}], timeout=30)
        if r.status_code < 300:
            linked += 1
        else:
            fail_link += 1
    print(f"\nRESULT: created {len(created):,} | failed {len(failed):,} | "
          f"parent links fired {linked:,} (link failures {fail_link})")
    for f in failed[:10]:
        print("   FAIL", f)
    json.dump({"created": len(created), "failed": failed, "linked": linked, "portal": pid},
              open(os.path.join(rundir, f"push_result_{len(created)}.json"), "w",
                   encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
