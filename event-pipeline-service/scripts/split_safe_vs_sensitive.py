#!/usr/bin/env python3
"""Split a fixes CSV into SAFE vs SENSITIVE writes.

SAFE      pod-only corrections, plus owner changes where the CURRENT owner is
          Abraham himself (moving to the Pod 2 AE of record is intended).
SENSITIVE owner changes that would pull a company off a DIFFERENT AE. Held for
          explicit approval because it can disrupt another rep's live account.

  python scripts/split_safe_vs_sensitive.py <in.csv> <safe.csv> <sensitive.csv>
"""
import csv, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)
import requests
from dotenv import dotenv_values

TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
H = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
BASE = "https://api.hubapi.com"
ABRAHAM, KEVIN = "87755705", "75419876"


def post(url, body, tries=6):
    r = None
    for a in range(tries):
        r = requests.post(url, headers=H, json=body, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")) + 1); continue
        if r.status_code >= 500:
            time.sleep(2 ** a); continue
        return r
    return r


def owner_name_map(ids):
    r = requests.get(BASE + "/crm/v3/owners?limit=500", headers=H, timeout=45)
    m = {}
    if r.status_code < 300:
        for o in r.json().get("results", []):
            nm = ((o.get("firstName") or "") + " " + (o.get("lastName") or "")).strip()
            m[str(o.get("id"))] = nm or (o.get("email") or str(o.get("id")))
    return m


def main():
    src, safe_p, sens_p = sys.argv[1], sys.argv[2], sys.argv[3]
    rows = list(csv.DictReader(open(src, encoding="utf-8")))
    ids = [r["hs_object_id"] for r in rows]
    live = {}
    for i in range(0, len(ids), 100):
        r = post(BASE + "/crm/v3/objects/companies/batch/read",
                 {"properties": ["pod", "hubspot_owner_id", "sdr_owner"],
                  "inputs": [{"id": x} for x in ids[i:i + 100]]})
        if r.status_code >= 300:
            print("READ FAIL", r.status_code, r.text[:200]); sys.exit(1)
        for x in r.json().get("results", []):
            live[x["id"]] = x["properties"]

    names = owner_name_map(ids)
    safe, sens = [], []
    by_owner = {}
    for r in rows:
        cur = live.get(r["hs_object_id"], {})
        cur_owner = cur.get("hubspot_owner_id") or ""
        owner_changes = cur_owner != KEVIN
        if owner_changes and cur_owner and cur_owner != ABRAHAM:
            r["_cur_owner"] = cur_owner
            sens.append(r)
            k = names.get(cur_owner, cur_owner)
            by_owner.setdefault(k, {"n": 0, "deals": 0})
            by_owner[k]["n"] += 1
            by_owner[k]["deals"] += int(r["open_deal"])
        else:
            safe.append(r)

    print("input %s -> SAFE %s | SENSITIVE %s\n"
          % tuple(format(len(x), ",") for x in (rows, safe, sens)))
    print("SENSITIVE: would move these companies off another AE and onto Kevin Rasted")
    print("%-26s %8s %12s" % ("current owner (AE)", "accounts", "w/ open deal"))
    for k, v in sorted(by_owner.items(), key=lambda kv: -kv[1]["n"]):
        print("%-26s %8s %12s" % (k, v["n"], v["deals"]))
    print("\nSENSITIVE open-deal accounts: %s of %s"
          % (sum(int(r["open_deal"]) for r in sens), len(sens)))

    for path, data in ((safe_p, safe), (sens_p, sens)):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys(), extrasaction="ignore")
            w.writeheader(); w.writerows(data)
    print("\nwrote %s (%s rows) and %s (%s rows)"
          % (safe_p, format(len(safe), ","), sens_p, format(len(sens), ",")))


if __name__ == "__main__":
    main()
