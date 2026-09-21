#!/usr/bin/env python3
"""Split Abraham Vanderhoff's company book 50/50 with new SDR Gavin Keo.

Pulls every company where sdr_owner = Abraham, stratifies by (has contacts,
ever contacted) so each half gets an identical mix of sourcing work vs warm
accounts, then writes push-ready CSVs for push_pod7_dryrun_update.py.

  python scripts/split_abraham_to_gavin.py <outdir> [--include-live] [--include-dq]

Holds back by default (written to their own CSVs, not pushed):
  live/customer stage  Trial, Paid Monthly, Active, Termed
  Disqualified
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

ABRAHAM = "87755705"
GAVIN   = "98868558"
KEVIN   = "75419876"   # Pod 2 AE, company owner of record
POD     = "Pod 2"

PROPS = ["name", "domain", "pod", "sdr_owner", "hubspot_owner_id",
         "lifecyclestage", "num_associated_contacts", "notes_last_contacted"]

# lifecyclestage internal values
LIVE = {"53309303": "Trial", "53292289": "Paid Monthly",
        "1421690282": "Active", "customer": "Termed"}
DQ   = {"53282366": "Disqualified"}


def post(url, body, tries=6):
    for a in range(tries):
        r = requests.post(url, headers=H, json=body, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")) + 1); continue
        if r.status_code >= 500:
            time.sleep(2 ** a); continue
        return r
    return r


def fetch_book():
    """Keyset-paginate every company with sdr_owner = Abraham."""
    out, last = [], "0"
    while True:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "sdr_owner",    "operator": "EQ", "value": ABRAHAM},
                {"propertyName": "hs_object_id", "operator": "GT", "value": last},
            ]}],
            "properties": PROPS,
            "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
            "limit": 200,
        }
        r = post(f"{BASE}/crm/v3/objects/companies/search", body)
        if r.status_code >= 300:
            print("SEARCH FAIL", r.status_code, r.text[:300]); sys.exit(1)
        res = r.json().get("results", [])
        if not res:
            break
        for x in res:
            p = x["properties"]
            out.append({
                "hs_object_id": x["id"],
                "name": (p.get("name") or "").replace("\n", " ").strip(),
                "domain": p.get("domain") or "",
                "pod_before": p.get("pod") or "",
                "owner_before": p.get("hubspot_owner_id") or "",
                "lifecyclestage": p.get("lifecyclestage") or "",
                "contacts": int(p.get("num_associated_contacts") or 0),
                "last_contacted": p.get("notes_last_contacted") or "",
            })
        last = res[-1]["id"]
        print(f"  fetched {len(out):,}", flush=True)
        if len(res) < 200:
            break
    return out


def bucket(r):
    has = r["contacts"] > 0
    touched = bool(r["last_contacted"])
    if not has and not touched:  return "A_empty_untouched"
    if not has and touched:      return "B_empty_touched"
    if has and not touched:      return "C_workable_fresh"
    return "D_workable_worked"


def write_csv(path, rows, sdr_id, sdr_name):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["hs_object_id", "name", "domain", "pod", "hubspot_owner_id",
                    "sdr_owner_id", "sdr_owner_name", "bucket", "contacts",
                    "lifecyclestage", "pod_before"])
        for r in rows:
            w.writerow([r["hs_object_id"], r["name"], r["domain"], POD, KEVIN,
                        sdr_id, sdr_name, r["bucket"], r["contacts"],
                        r["lifecyclestage"], r["pod_before"]])


def main():
    outdir = sys.argv[1]
    inc_live = "--include-live" in sys.argv
    inc_dq   = "--include-dq" in sys.argv
    os.makedirs(outdir, exist_ok=True)

    print("fetching Abraham's book...")
    book = fetch_book()
    print(f"\ntotal companies on sdr_owner=Abraham: {len(book):,}\n")

    for r in book:
        r["bucket"] = bucket(r)

    live = [r for r in book if r["lifecyclestage"] in LIVE]
    dq   = [r for r in book if r["lifecyclestage"] in DQ]
    held = set()
    if not inc_live: held |= {r["hs_object_id"] for r in live}
    if not inc_dq:   held |= {r["hs_object_id"] for r in dq}
    splittable = [r for r in book if r["hs_object_id"] not in held]

    print("HELD BACK (own CSVs, not in the split):")
    print(f"  live/customer stage : {len(live):,}  {'INCLUDED' if inc_live else 'held'}")
    for k, v in LIVE.items():
        n = sum(1 for r in live if r["lifecyclestage"] == k)
        if n: print(f"      {v:<14} {n:,}")
    print(f"  disqualified        : {len(dq):,}  {'INCLUDED' if inc_dq else 'held'}")
    print(f"\nsplittable: {len(splittable):,}\n")

    # stratified alternate within each bucket -> identical mix in both halves
    gavin, retained = [], []
    print(f"{'bucket':<20} {'total':>7} {'gavin':>7} {'abraham':>8}")
    for b in ("A_empty_untouched", "B_empty_touched", "C_workable_fresh", "D_workable_worked"):
        rows = sorted([r for r in splittable if r["bucket"] == b],
                      key=lambda r: int(r["hs_object_id"]))
        g = rows[0::2]; a = rows[1::2]
        gavin += g; retained += a
        if rows:
            print(f"{b:<20} {len(rows):>7,} {len(g):>7,} {len(a):>8,}")
    print(f"{'TOTAL':<20} {len(splittable):>7,} {len(gavin):>7,} {len(retained):>8,}")

    mis = sum(1 for r in splittable if r["pod_before"] != POD)
    print(f"\npod value corrected to '{POD}' on {mis:,} of the {len(splittable):,} "
          f"splittable rows (were blank or another pod)")

    def stat(rows, label):
        c = sum(1 for r in rows if r["contacts"] > 0)
        fresh = sum(1 for r in rows if r["contacts"] > 0 and not r["last_contacted"])
        empty = sum(1 for r in rows if r["contacts"] == 0)
        tot_contacts = sum(r["contacts"] for r in rows)
        print(f"  {label:<9} {len(rows):>6,} companies | {empty:>6,} empty to source "
              f"| {c:>5,} contactable | {fresh:>4,} fresh workable | {tot_contacts:>6,} contacts")

    print("\nRESULTING BOOKS:")
    stat(gavin, "GAVIN")
    stat(retained, "ABRAHAM")

    write_csv(os.path.join(outdir, "gavin_half.csv"), gavin, GAVIN, "Gavin Keo")
    write_csv(os.path.join(outdir, "abraham_half.csv"), retained, ABRAHAM, "Abraham Vanderhoff")
    if not inc_live:
        write_csv(os.path.join(outdir, "HELD_live_customer.csv"), live, ABRAHAM, "Abraham Vanderhoff")
    if not inc_dq:
        write_csv(os.path.join(outdir, "HELD_disqualified.csv"), dq, ABRAHAM, "Abraham Vanderhoff")

    json.dump({"total": len(book), "splittable": len(splittable),
               "gavin": len(gavin), "abraham": len(retained),
               "held_live": len(live), "held_dq": len(dq)},
              open(os.path.join(outdir, "split_summary.json"), "w"), indent=1)
    print(f"\nwrote CSVs to {outdir}/")
    print("next: python scripts/push_pod7_dryrun_update.py "
          f"{outdir}/gavin_half.csv {outdir}/run_gavin   (add --live to write)")


if __name__ == "__main__":
    main()
