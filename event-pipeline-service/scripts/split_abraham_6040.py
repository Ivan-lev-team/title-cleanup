#!/usr/bin/env python3
"""Split Abraham's book 60/40 Gavin/Abraham, protecting everything live.

Abraham always keeps (protected, never counted against the ratio target
until the ratio is filled):
  - ANY company with an open deal  ("all deals stay with Abraham")
  - Termed / churned               ("old churned abraham")
  - Working / Trial / Paid Monthly / Active / Qualified
Disqualified is held out entirely by default (dead, not real inventory).

Everything else is flexible and is allocated to land on the 60/40 target.
Two modes for how the flexible pool is cut:

  warmth     (default) Abraham is topped up to 40% with the WARMEST flexible
             accounts; Gavin gets the coldest 60%. Follows "cold goes to Gavin"
             literally, but Gavin's book ends up near-100% empty shells.
  stratified Each warmth bucket is cut 60/40, so both reps get the same MIX
             and Gavin still receives contactable accounts to start on.

  python scripts/split_abraham_6040.py <outdir> [--mode warmth|stratified]
                                       [--active-days N] [--gavin-pct 60]
                                       [--dq-to-gavin]
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
                        "last_contacted": p.get("notes_last_contacted") or ""})
        last = res[-1]["id"]
        if len(res) < 200:
            break
    return out


def company_deal_stages(ids):
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
    stages, dl = {}, sorted(all_deals)
    for i in range(0, len(dl), 100):
        chunk = dl[i:i + 100]
        r = post(BASE + "/crm/v3/objects/deals/batch/read",
                 {"properties": ["dealstage"], "inputs": [{"id": d} for d in chunk]})
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
    mode, active_days, gavin_pct = "warmth", 90, 60.0
    for i, a in enumerate(sys.argv):
        if a == "--mode" and i + 1 < len(sys.argv):
            mode = sys.argv[i + 1]
        if a == "--active-days" and i + 1 < len(sys.argv):
            active_days = int(sys.argv[i + 1])
        if a == "--gavin-pct" and i + 1 < len(sys.argv):
            gavin_pct = float(sys.argv[i + 1])
    dq_to_gavin = "--dq-to-gavin" in sys.argv
    os.makedirs(outdir, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=active_days)

    book = fetch_book()
    cds = company_deal_stages([r["hs_object_id"] for r in book])

    for r in book:
        st = cds.get(r["hs_object_id"], set())
        r["open_deal"] = any(s and s not in CLOSED for s in st)
        r["lost_only"] = bool(st) and not r["open_deal"]
        ts = parse_ts(r["last_contacted"]) if r["last_contacted"] else None
        r["days"] = (now - ts).days if ts else None
        r["recent"] = bool(ts and ts >= cutoff)
        ls = r["lifecyclestage"]

        # protected: Abraham keeps regardless of ratio
        if r["open_deal"]:
            r["side"], r["why"], r["flex"] = "A", "open deal (all deals stay)", False
        elif ls == TERMED:
            r["side"], r["why"], r["flex"] = "A", "termed/churned", False
        elif ls in KEEP_STAGE:
            r["side"], r["why"], r["flex"] = "A", KEEP_STAGE[ls], False
        elif ls == DQ and not dq_to_gavin:
            r["side"], r["why"], r["flex"] = "H", "disqualified (held)", False
        else:
            r["side"], r["why"], r["flex"] = None, None, True

        # warmth: higher = warmer
        if r["contacts"] == 0:
            r["warmth"], r["bucket"] = -20000, "empty shell"
        elif not r["last_contacted"]:
            r["warmth"], r["bucket"] = -10000, "contactable, never touched"
        else:
            r["warmth"] = -r["days"]
            r["bucket"] = ("active lead (<%dd)" % active_days) if r["recent"] \
                else ("cold/unresponsive (>%dd)" % active_days)

    held = [r for r in book if r["side"] == "H"]
    prot = [r for r in book if r["side"] == "A"]
    flex = [r for r in book if r["flex"]]
    pool = len(prot) + len(flex)
    tgt_a = int(round(pool * (100.0 - gavin_pct) / 100.0))
    need_a = max(0, tgt_a - len(prot))

    print("book %s | held(DQ) %s | ratio pool %s" % tuple(format(x, ",") for x in (len(book), len(held), pool)))
    print("target %.0f/%.0f Gavin/Abraham -> Abraham %s, Gavin %s"
          % (gavin_pct, 100 - gavin_pct, format(tgt_a, ","), format(pool - tgt_a, ",")))
    print("protected with Abraham: %s (open deal / churned / working / trial / paid / active / qualified)"
          % format(len(prot), ","))
    print("flexible pool: %s -> Abraham tops up %s, Gavin gets %s"
          % (format(len(flex), ","), format(need_a, ","), format(len(flex) - need_a, ",")))
    print("mode: %s\n" % mode)

    if mode == "ramp":
        # Abraham: every active lead first (honours "active leads stay"), then
        # tops up from the EMPTY SHELLS. That reserves the cold/unresponsive and
        # never-touched contactable accounts for Gavin so he has people to call.
        def prio(r):
            if r["bucket"].startswith("active lead"):
                return 0
            if r["bucket"] == "empty shell":
                return 1
            return 2
        flex.sort(key=lambda r: (prio(r), -r["warmth"], int(r["hs_object_id"])))
        for i, r in enumerate(flex):
            r["side"] = "A" if i < need_a else "G"
            r["why"] = r["bucket"] + (" (Abraham top-up)" if i < need_a else "")
    elif mode == "warmth":
        flex.sort(key=lambda r: (-r["warmth"], int(r["hs_object_id"])))
        for i, r in enumerate(flex):
            r["side"] = "A" if i < need_a else "G"
            r["why"] = r["bucket"] + (" (topped up to 40%)" if i < need_a else "")
    else:
        buckets = {}
        for r in flex:
            buckets.setdefault(r["bucket"], []).append(r)
        frac_a = (float(need_a) / len(flex)) if flex else 0.0
        carry = 0.0
        for b in sorted(buckets, key=lambda b: -len(buckets[b])):
            rows = sorted(buckets[b], key=lambda r: (-r["warmth"], int(r["hs_object_id"])))
            want = len(rows) * frac_a + carry
            take = int(round(want))
            carry = want - take
            for i, r in enumerate(rows):
                r["side"] = "A" if i < take else "G"
                r["why"] = r["bucket"] + (" (40% share)" if i < take else "")

    A = [r for r in book if r["side"] == "A"]
    G = [r for r in book if r["side"] == "G"]

    print("%-44s %8s %9s" % ("reason", "side", "companies"))
    reasons = {}
    for r in book:
        k = (r["side"], r["why"])
        reasons[k] = reasons.get(k, 0) + 1
    for (side, why), n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("%-44s %8s %9s" % (why, SIDE_LABEL[side], format(n, ",")))

    def stat(rows, label):
        empty = sum(1 for r in rows if r["contacts"] == 0)
        able = sum(1 for r in rows if r["contacts"] > 0)
        od = sum(1 for r in rows if r["open_deal"])
        pct = (100.0 * len(rows) / pool) if pool else 0
        print("  %-8s %6s (%4.1f%%) | %6s empty to source | %5s contactable | "
              "%3s open deals | %6s contacts"
              % (label, format(len(rows), ","), pct, format(empty, ","),
                 format(able, ","), format(od, ","),
                 format(sum(r["contacts"] for r in rows), ",")))

    print("\nRESULTING BOOKS (%% of the %s ratio pool):" % format(pool, ","))
    stat(A, "ABRAHAM")
    stat(G, "GAVIN")
    if held:
        print("  %-8s %6s  (excluded from ratio)" % ("HELD-DQ", format(len(held), ",")))

    def write(path, rows, sdr, nm):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["hs_object_id", "name", "domain", "pod", "hubspot_owner_id",
                        "sdr_owner_id", "sdr_owner_name", "why", "contacts",
                        "days_since_contact", "lifecyclestage", "open_deal", "pod_before"])
            for r in rows:
                w.writerow([r["hs_object_id"], r["name"], r["domain"], POD, KEVIN, sdr, nm,
                            r["why"], r["contacts"],
                            "" if r["days"] is None else r["days"],
                            r["lifecyclestage"], int(r["open_deal"]), r["pod_before"]])

    tag = mode
    write(os.path.join(outdir, "gavin_%s.csv" % tag), G, GAVIN, "Gavin Keo")
    write(os.path.join(outdir, "abraham_%s.csv" % tag), A, ABRAHAM, "Abraham Vanderhoff")
    if held:
        write(os.path.join(outdir, "HELD_dq.csv"), held, ABRAHAM, "Abraham Vanderhoff")
    json.dump({"mode": mode, "total": len(book), "pool": pool, "abraham": len(A),
               "gavin": len(G), "held_dq": len(held), "active_days": active_days,
               "gavin_pct_target": gavin_pct},
              open(os.path.join(outdir, "summary_%s.json" % tag), "w"), indent=1)
    print("\nwrote CSVs to %s/  (nothing pushed)" % outdir)


if __name__ == "__main__":
    main()
