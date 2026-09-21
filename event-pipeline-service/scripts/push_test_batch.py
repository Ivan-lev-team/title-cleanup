#!/usr/bin/env python3
"""Create a SMALL test batch of companies live, then fire their typeId 14
parent links. Defaults to dry-run; --live actually writes.

  python scripts/push_test_batch.py <PUSH_LIST.csv> <export.csv> <rundir> [--live]
"""
import csv, json, os, re, sys, time
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8")
csv.field_size_limit(10 ** 8)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "scripts")); os.chdir(HERE)
import requests
from dotenv import dotenv_values
from resolve_dedupe_groups import norm_domain
import import_dryrun as M           # reuse the EXACT mapping that was dry-run approved

TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "https://api.hubapi.com"
PARENT_TYPE_ID = 14                 # "Parent Company", child -> parent


def build(rec_src, cleaned_name):
    """Assemble one company payload using the approved mapping."""
    r = rec_src
    yearly = M.money(r.get("estimated_yearly_sales"))
    rec = {}
    for src, tgt in M.MAP.items():
        if not tgt or src.endswith("_SRC"):
            continue
        prop, fn = tgt
        try:
            v = fn(r) if fn else None
        except Exception:
            v = None
        if src == "title":
            v = cleaned_name
        if v not in (None, ""):
            rec[prop] = v
    rec = M.stamp(rec, yearly, r)
    return {k: v for k, v in rec.items() if v not in (None, "")}, yearly


def main():
    push_csv, export_csv, rundir = sys.argv[1:4]
    live = "--live" in sys.argv
    supp = set(json.load(open(os.path.join(rundir, "groups", "all_suppress_domains.json"),
                              encoding="utf-8")))
    links = {l["child_domain"]: l for l in
             json.load(open(os.path.join(rundir, "groups", "final_links.json"), encoding="utf-8"))}
    push = {}
    for r in csv.DictReader(open(push_csv, encoding="utf-8-sig")):
        d = norm_domain(r["domain"])
        if d not in supp:
            push[d] = r

    # gather candidates with their assembled payloads
    cands = []
    for r in csv.DictReader(open(export_csv, encoding="utf-8-sig")):
        d = norm_domain(r.get("domain"))
        pr = push.get(d)
        if not pr:
            continue
        rec, yearly = build(r, pr["name"])
        cands.append({"domain": d, "rec": rec, "yearly": yearly or 0,
                      "nfields": len(rec), "has_link": d in links,
                      "bucket": pr.get("bucket", "")})
    print(f"candidate pool: {len(cands):,}")

    def pick(pred, key, used, n=1):
        sel = sorted([c for c in cands if pred(c) and c["domain"] not in used],
                     key=key, reverse=True)[:n]
        for c in sel:
            used.add(c["domain"])
        return sel

    used = set()
    chosen = []
    # 1 with a real parent link AND the most fields -> tests the link mechanism
    chosen += pick(lambda c: c["has_link"], lambda c: c["nfields"], used, 1)
    # 1 Enterprise
    chosen += pick(lambda c: c["yearly"] >= 1e7, lambda c: c["nfields"], used, 1)
    # 2 Gold
    chosen += pick(lambda c: 1e6 <= c["yearly"] < 1e7, lambda c: c["nfields"], used, 2)
    # 1 Launch
    chosen += pick(lambda c: c["yearly"] < 1e6, lambda c: c["nfields"], used, 1)
    chosen = chosen[:5]

    print("\n=== SELECTED 5 ===")
    for c in chosen:
        print(f"  {c['domain'][:34]:34} seg={c['rec'].get('account_segment'):11} "
              f"fields={c['nfields']:2} link={'YES -> '+links[c['domain']]['parent_name'][:24] if c['has_link'] else 'no'}")

    # never create a company that already exists on this domain
    print("\n=== PRE-CREATE EXISTENCE CHECK ===")
    for c in chosen:
        body = {"filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ",
                                               "value": c["domain"]}]}],
                "properties": ["name"], "limit": 1}
        r = requests.post(f"{BASE}/crm/v3/objects/companies/search", headers=H, json=body, timeout=30)
        hits = r.json().get("results", []) if r.status_code < 300 else []
        c["exists"] = bool(hits)
        print(f"  {c['domain'][:34]:34} already in HubSpot: {'YES - WILL SKIP' if hits else 'no'}")

    todo = [c for c in chosen if not c["exists"]]
    if not live:
        print("\n=== PAYLOADS (dry run) ===")
        for c in todo:
            print(f"\n--- {c['domain']} ---")
            for k in sorted(c["rec"]):
                print(f"   {k:28} = {str(c['rec'][k])[:58]}")
        print("\nNOTHING WRITTEN (no --live).")
        return

    print(f"\n=== CREATING {len(todo)} COMPANIES ===")
    portal = requests.get(f"{BASE}/account-info/v3/details", headers=H, timeout=30)
    pid = portal.json().get("portalId") if portal.status_code < 300 else "PORTAL"
    created, failed = [], []
    for c in todo:
        r = requests.post(f"{BASE}/crm/v3/objects/companies", headers=H,
                          json={"properties": c["rec"]}, timeout=45)
        if r.status_code < 300:
            cid = r.json()["id"]
            c["id"] = cid
            created.append(c)
            print(f"  CREATED {cid}  {c['domain']}")
        else:
            failed.append((c["domain"], r.status_code, r.text[:200]))
            print(f"  FAILED  {c['domain']}: {r.status_code} {r.text[:160]}")

    print(f"\n=== FIRING typeId {PARENT_TYPE_ID} PARENT LINKS ===")
    linked = []
    for c in created:
        l = links.get(c["domain"])
        if not l:
            print(f"  {c['domain'][:34]:34} no parent link defined - skipped")
            continue
        url = (f"{BASE}/crm/v4/objects/companies/{c['id']}/associations/"
               f"companies/{l['parent_id']}")
        r = requests.put(url, headers=H, json=[{"associationCategory": "HUBSPOT_DEFINED",
                                                "associationTypeId": PARENT_TYPE_ID}], timeout=30)
        ok = r.status_code < 300
        linked.append((c["domain"], l["parent_id"], l["parent_name"], ok, r.status_code))
        print(f"  {c['domain'][:30]:30} -> parent {l['parent_id']} "
              f"'{l['parent_name'][:24]}'  {'OK' if ok else 'FAIL '+str(r.status_code)+' '+r.text[:120]}")

    print("\n=== RESULT ===")
    print(f"  created: {len(created)}   failed: {len(failed)}   links fired: {len(linked)}")
    print("\n  HubSpot record links:")
    for c in created:
        print(f"   {c['domain'][:34]:34} id={c['id']}")
        print(f"      https://app.hubspot.com/contacts/{pid}/company/{c['id']}")
    json.dump({"created": [{"domain": c["domain"], "id": c["id"]} for c in created],
               "links": linked, "failed": failed, "portal": pid},
              open(os.path.join(rundir, "test_batch_result.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
