#!/usr/bin/env python3
"""Split Abraham's book by ENGAGEMENT, not by count.

Abraham keeps anything live/in-motion:
  - any open deal (non-closed stage)
  - lifecyclestage Working / Trial / Paid Monthly / Active / Qualified
  - active leads: contacted within ACTIVE_DAYS
Gavin gets the cold residue:
  - zero-contact shells (his job is to source contacts into them)
  - contactable but never contacted
  - contacted longer ago than ACTIVE_DAYS with no open deal (cold/unresponsive)
  - closed-lost-only accounts
Termed (churned) and Disqualified are held out for an explicit call.

  python scripts/split_abraham_by_engagement.py <outdir> [--active-days N]
                                                [--termed-to-gavin] [--dq-to-gavin]
"""
import csv, json, os, sys, time, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)
import requests
from dotenv import dotenv_values

TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
H = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
BASE = "https://api.hubapi.com"

ABRAHAM, GAVIN, KEVIN, POD = "87755705", "98868558", "75419876", "Pod 2"
PROPS = ["name", "domain", "pod", "hubspot_owner_id", "lifecyclestage",
         "num_associated_contacts", "notes_last_contacted", "num_associated_deals"]

KEEP_STAGE = {"252225308": "working", "53309303": "trial", "53292289": "paid monthly",
              "1421690282": "active", "252141832": "qualified"}
TERMED, DQ = "customer", "53282366"
CLOSED = {"closedlost", "closedwon"}
SIDE_LABEL = {"A": "ABRAHAM", "G": "GAVIN", "H": "HELD"}


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


def fetch_book():
    out, last = [], "0"
    while True:
        body = {"filterGroups": [{"filters": [
                    {"propertyName": "sdr_owner", "operator": "EQ", "value": ABRAHAM},
                    {"propertyName": "hs_object_id", "operator": "GT", "value": last}]}],
                "properties": PROPS,
                "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
                "limit": 200}
        r = post(BASE + "/crm/v3/objects/companies/search", body)
        if r.status_code >= 300:
            print("SEARCH FAIL", r.status_code, r.text[:300])
            sys.exit(1)
        res = r.json().get("results", [])
        if not res:
            break
        for x in res:
            p = x["properties"]
            out.append({"hs_object_id": x["id"],
                        "name": (p.get("name") or "").replace("\n", " ").strip(),
                        "domain": p.get("domain") or "",
                        "pod_before": p.get("pod") or "",
                        "lifecyclestage": p.get("lifecyclestage") or "",
                        "contacts": int(p.get("num_associated_contacts") or 0),
                        "deals": int(p.get("num_associated_deals") or 0),
                        "last_contacted": p.get("notes_last_contacted") or ""})
        last = res[-1]["id"]
        if len(res) < 200:
            break
    return out


def company_deal_stages(ids):
    """company_id -> set of dealstages, via v4 assoc batch read + deal batch read."""
    comp_deals, all_deals = {}, set()
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        r = post(BASE + "/crm/v4/associations/companies/deals/batch/read",
                 {"inputs": [{"id": c} for c in chunk]})
        if r.status_code >= 300:
            print("ASSOC FAIL", r.status_code, r.text[:200])
            sys.exit(1)
        for res in r.json().get("results", []):
            d = [str(t["toObjectId"]) for t in res.get("to", [])]
            if d:
                comp_deals[str(res["from"]["id"])] = d
                all_deals |= set(d)
        print("  assoc %s/%s" % (min(i + 100, len(ids)), len(ids)), flush=True)
    stages, dl = {}, sorted(all_deals)
    for i in range(0, len(dl), 100):
        chunk = dl[i:i + 100]
        r = post(BASE + "/crm/v3/objects/deals/batch/read",
                 {"properties": ["dealstage", "dealname"],
                  "inputs": [{"id": d} for d in chunk]})
        if r.status_code >= 300:
            print("DEAL FAIL", r.status_code, r.text[:200])
            sys.exit(1)
        for x in r.json().get("results", []):
            stages[x["id"]] = x["properties"].get("dealstage") or ""
    return dict((c, set(stages.get(d, "") for d in ds)) for c, ds in comp_deals.items())


def parse_ts(v):
    try:
        if v.isdigit():
            return dt.datetime.fromtimestamp(int(v) / 1000, dt.timezone.utc)
        return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception:
        return None


def main():
    outdir = sys.argv[1]
    active_days = 90
    for i, a in enumerate(sys.argv):
        if a == "--active-days" and i + 1 < len(sys.argv):
            active_days = int(sys.argv[i + 1])
    termed_to_gavin = "--termed-to-gavin" in sys.argv
    dq_to_gavin = "--dq-to-gavin" in sys.argv
    os.makedirs(outdir, exist_ok=True)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=active_days)

    print("fetching book...")
    book = fetch_book()
    print("total on sdr_owner=Abraham: %s" % format(len(book), ","))
    print("reading deal associations...")
    cds = company_deal_stages([r["hs_object_id"] for r in book])
    print("companies with >=1 deal: %s\n" % format(len(cds), ","))

    for r in book:
        st = cds.get(r["hs_object_id"], set())
        r["open_deal"] = any(s and s not in CLOSED for s in st)
        r["lost_only"] = bool(st) and not r["open_deal"]
        ts = parse_ts(r["last_contacted"]) if r["last_contacted"] else None
        r["recent"] = bool(ts and ts >= cutoff)

        ls = r["lifecyclestage"]
        if ls == DQ:
            r["side"], r["why"] = ("G" if dq_to_gavin else "H"), "disqualified"
        elif ls == TERMED:
            r["side"], r["why"] = ("G" if termed_to_gavin else "H"), "termed/churned"
        elif r["open_deal"]:
            r["side"], r["why"] = "A", "open deal"
        elif ls in KEEP_STAGE:
            r["side"], r["why"] = "A", KEEP_STAGE[ls]
        elif r["recent"]:
            r["side"], r["why"] = "A", "active lead (touched <%dd)" % active_days
        elif r["contacts"] == 0:
            r["side"], r["why"] = "G", "empty shell, needs sourcing"
        elif not r["last_contacted"]:
            r["side"], r["why"] = "G", "contactable, never touched"
        elif r["lost_only"]:
            r["side"], r["why"] = "G", "closed-lost only"
        else:
            r["side"], r["why"] = "G", "cold/unresponsive (>%dd)" % active_days

    A = [r for r in book if r["side"] == "A"]
    G = [r for r in book if r["side"] == "G"]
    Hh = [r for r in book if r["side"] == "H"]

    print("ACTIVE CUTOFF: contacted within %d days\n" % active_days)
    print("%-40s %8s %10s" % ("reason", "side", "companies"))
    reasons = {}
    for r in book:
        k = (r["side"], r["why"])
        reasons[k] = reasons.get(k, 0) + 1
    for (side, why), n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("%-40s %8s %10s" % (why, SIDE_LABEL[side], format(n, ",")))

    def stat(rows, label):
        empty = sum(1 for r in rows if r["contacts"] == 0)
        able = sum(1 for r in rows if r["contacts"] > 0)
        fresh = sum(1 for r in rows if r["contacts"] > 0 and not r["last_contacted"])
        od = sum(1 for r in rows if r["open_deal"])
        print("  %-8s %6s companies | %6s empty to source | %5s contactable | "
              "%4s never touched | %3s open deals | %6s contacts"
              % (label, format(len(rows), ","), format(empty, ","), format(able, ","),
                 format(fresh, ","), format(od, ","),
                 format(sum(r["contacts"] for r in rows), ",")))

    print("\nRESULTING BOOKS:")
    stat(A, "ABRAHAM")
    stat(G, "GAVIN")
    stat(Hh, "HELD")

    def write(path, rows, sdr, nm):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["hs_object_id", "name", "domain", "pod", "hubspot_owner_id",
                        "sdr_owner_id", "sdr_owner_name", "why", "contacts",
                        "lifecyclestage", "open_deal", "pod_before"])
            for r in rows:
                w.writerow([r["hs_object_id"], r["name"], r["domain"], POD, KEVIN, sdr, nm,
                            r["why"], r["contacts"], r["lifecyclestage"],
                            int(r["open_deal"]), r["pod_before"]])

    write(os.path.join(outdir, "gavin_cold.csv"), G, GAVIN, "Gavin Keo")
    write(os.path.join(outdir, "abraham_active.csv"), A, ABRAHAM, "Abraham Vanderhoff")
    write(os.path.join(outdir, "HELD_termed_dq.csv"), Hh, ABRAHAM, "Abraham Vanderhoff")
    json.dump({"total": len(book), "abraham": len(A), "gavin": len(G), "held": len(Hh),
               "active_days": active_days},
              open(os.path.join(outdir, "summary.json"), "w"), indent=1)
    print("\nwrote CSVs to %s/" % outdir)


if __name__ == "__main__":
    main()
