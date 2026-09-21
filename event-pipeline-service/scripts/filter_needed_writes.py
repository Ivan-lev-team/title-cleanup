#!/usr/bin/env python3
"""Filter a push CSV down to only the rows whose live HubSpot values differ
from what the CSV would write. Avoids no-op writes on already-correct records.

  python scripts/filter_needed_writes.py <in.csv> <out.csv>
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
FIELDS = ("pod", "hubspot_owner_id", "sdr_owner")


def post(url, body, tries=6):
    r = None
    for a in range(tries):
        r = requests.post(url, headers=H, json=body, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")) + 1)
            continue
        if r.status_code >= 500:
            time.sleep(2 ** a)
            continue
        return r
    return r


def main():
    src, dst = sys.argv[1], sys.argv[2]
    rows = list(csv.DictReader(open(src, encoding="utf-8")))
    ids = [r["hs_object_id"] for r in rows]
    live = {}
    for i in range(0, len(ids), 100):
        r = post(BASE + "/crm/v3/objects/companies/batch/read",
                 {"properties": list(FIELDS), "inputs": [{"id": x} for x in ids[i:i + 100]]})
        if r.status_code >= 300:
            print("READ FAIL", r.status_code, r.text[:200])
            sys.exit(1)
        for x in r.json().get("results", []):
            live[x["id"]] = x["properties"]

    need, noop, missing, diffs = [], 0, 0, {}
    for r in rows:
        cur = live.get(r["hs_object_id"])
        if cur is None:
            missing += 1
            continue
        want = {"pod": r["pod"], "hubspot_owner_id": r["hubspot_owner_id"],
                "sdr_owner": r["sdr_owner_id"]}
        changed = [f for f in FIELDS if (cur.get(f) or "") != want[f]]
        if changed:
            need.append(r)
            for f in changed:
                k = "%s: %r -> %r" % (f, cur.get(f) or "", want[f])
                diffs[k] = diffs.get(k, 0) + 1
        else:
            noop += 1

    print("input rows        : %s" % format(len(rows), ","))
    print("already correct   : %s  (would be no-op writes, dropped)" % format(noop, ","))
    print("not found in CRM  : %s" % format(missing, ","))
    print("need a write      : %s\n" % format(len(need), ","))
    print("changes that will be made (field: before -> after, count):")
    for k, v in sorted(diffs.items(), key=lambda kv: -kv[1]):
        print("   %-58s %5s" % (k, format(v, ",")))

    with open(dst, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(need)
    print("\nwrote %s with %s rows" % (dst, format(len(need), ",")))


if __name__ == "__main__":
    main()
