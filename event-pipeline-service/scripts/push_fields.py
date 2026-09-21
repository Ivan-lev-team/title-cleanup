#!/usr/bin/env python3
"""Batch-PATCH only the NAMED fields from a push CSV, leaving every other
property untouched. Same batching/retry/resume behaviour as
push_pod7_dryrun_update.py, but lets you fix pod without touching ownership.

  python scripts/push_fields.py <csv> <rundir> --fields pod[,hubspot_owner_id]
                               [--only-if-pod-differs] [--owner-is <id>] [--live]

  --only-if-pod-differs  keep only rows whose pod_before != target pod
  --owner-is <id>        keep only rows whose CURRENT live owner == <id>
"""
import csv, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)
import requests
from dotenv import dotenv_values

TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
H = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
BASE = "https://api.hubapi.com"
CSVCOL = {"pod": "pod", "hubspot_owner_id": "hubspot_owner_id", "sdr_owner": "sdr_owner_id"}


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


def arg(flag, default=None):
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main():
    csv_path, rundir = sys.argv[1], sys.argv[2]
    live = "--live" in sys.argv
    fields = [f.strip() for f in (arg("--fields") or "").split(",") if f.strip()]
    if not fields or any(f not in CSVCOL for f in fields):
        print("--fields must be a comma list from: %s" % ", ".join(CSVCOL)); sys.exit(2)
    owner_is = arg("--owner-is")
    only_pod_diff = "--only-if-pod-differs" in sys.argv

    os.makedirs(rundir, exist_ok=True)
    log_path = os.path.join(rundir, "updated_companies.jsonl")
    done = set()
    if os.path.exists(log_path):
        for line in open(log_path, encoding="utf-8"):
            try: done.add(json.loads(line)["id"])
            except Exception: pass

    rows = [r for r in csv.DictReader(open(csv_path, encoding="utf-8"))
            if r["hs_object_id"] not in done]
    if only_pod_diff:
        rows = [r for r in rows if (r.get("pod_before") or "") != r["pod"]]
    if owner_is:
        keep, ids = [], [r["hs_object_id"] for r in rows]
        cur = {}
        for i in range(0, len(ids), 100):
            rr = post(BASE + "/crm/v3/objects/companies/batch/read",
                      {"properties": ["hubspot_owner_id"],
                       "inputs": [{"id": x} for x in ids[i:i + 100]]})
            for x in rr.json().get("results", []):
                cur[x["id"]] = x["properties"].get("hubspot_owner_id") or ""
        keep = [r for r in rows if cur.get(r["hs_object_id"]) == owner_is]
        print("owner filter: %s of %s rows currently owned by %s"
              % (format(len(keep), ","), format(len(rows), ","), owner_is))
        rows = keep

    print("fields written : %s" % ", ".join(fields))
    print("already done   : %s" % format(len(done), ","))
    print("todo           : %s" % format(len(rows), ","))
    if not rows:
        print("nothing to do."); return
    if not live:
        print("\nDRY RUN (no --live). Nothing written. First 3:")
        for r in rows[:3]:
            vals = ", ".join("%s=%s" % (f, r[CSVCOL[f]]) for f in fields)
            print("   %s %-28s %s" % (r["hs_object_id"], r["name"][:28], vals))
        return

    updated, failed = [], []
    flog = open(log_path, "a", encoding="utf-8")
    t0 = time.time()
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        body = {"inputs": [{"id": r["hs_object_id"],
                            "properties": dict((f, r[CSVCOL[f]]) for f in fields)}
                           for r in chunk]}
        r = post(BASE + "/crm/v3/objects/companies/batch/update", body)
        if r.status_code < 300:
            for c in chunk:
                updated.append(c["hs_object_id"])
                flog.write(json.dumps({"id": c["hs_object_id"]}) + "\n")
        else:
            for c in chunk:
                rr = requests.patch(BASE + "/crm/v3/objects/companies/" + c["hs_object_id"],
                                    headers=H,
                                    json={"properties": dict((f, c[CSVCOL[f]]) for f in fields)},
                                    timeout=45)
                if rr.status_code < 300:
                    updated.append(c["hs_object_id"])
                    flog.write(json.dumps({"id": c["hs_object_id"]}) + "\n")
                else:
                    failed.append((c["hs_object_id"], rr.status_code, rr.text[:150]))
        flog.flush()
        print("   %s/%s updated=%s failed=%s  %.1fm"
              % (format(min(i + 100, len(rows)), ","), format(len(rows), ","),
                 format(len(updated), ","), len(failed), (time.time() - t0) / 60), flush=True)
    flog.close()
    print("\nRESULT: updated %s | failed %s" % (format(len(updated), ","), len(failed)))
    for f in failed[:10]:
        print("   FAIL", f)
    json.dump({"fields": fields, "updated": len(updated), "failed": failed},
              open(os.path.join(rundir, "result_%d.json" % len(updated)), "w"), indent=1)


if __name__ == "__main__":
    main()
