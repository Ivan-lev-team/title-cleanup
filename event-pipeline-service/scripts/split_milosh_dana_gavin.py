#!/usr/bin/env python3
"""Split Milosh Mihajlovikj's book: most to Dana (Pod 1), a 10-20% slice to
Gavin (Pod 2).

Protected -> Dana always (never eligible for Gavin):
  any open deal, Trial, Paid Monthly, Active, Revenue Active, Working,
  Qualified, Termed. Live revenue and in-flight pipeline do not move to a
  brand-new rep in another pod.
Eligible for Gavin: Lead / Net New with no open deal. Drawn CONTACTABLE-FIRST
so the slice is callable inventory, not empty shells.

Gavin's slice is cross-pod: pod Pod 1 -> Pod 2 and AE Katie -> Kevin.

  python scripts/split_milosh_dana_gavin.py <outdir> [--pct 10] [--no-owner-change]
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

MILOSH = "87811820"
DANA, GAVIN = "98870321", "98868558"
KATIE, KEVIN = "75419877", "75419876"
PROPS = ["name", "domain", "pod", "hubspot_owner_id", "lifecyclestage",
         "num_associated_contacts", "notes_last_contacted"]
KEEP = {"252225308": "working", "53309303": "trial", "53292289": "paid monthly",
        "1421690282": "active", "252141832": "qualified", "customer": "termed"}
CLOSED = {"closedlost", "closedwon", "133173801", "133173802", "1240352796",
          "230160747", "230160748"}


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


def fetch_book():
    out, last = [], "0"
    while True:
        body = {"filterGroups": [{"filters": [
                    {"propertyName": "sdr_owner", "operator": "EQ", "value": MILOSH},
                    {"propertyName": "hs_object_id", "operator": "GT", "value": last}]}],
                "properties": PROPS,
                "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
                "limit": 200}
        r = post(BASE + "/crm/v3/objects/companies/search", body)
        if r.status_code >= 300:
            print("SEARCH FAIL", r.status_code, r.text[:300]); sys.exit(1)
        res = r.json().get("results", [])
        if not res:
            break
        for x in res:
            p = x["properties"]
            out.append({"hs_object_id": x["id"],
                        "name": (p.get("name") or "").replace("\n", " ").strip(),
                        "domain": p.get("domain") or "",
                        "pod_before": p.get("pod") or "",
                        "owner_before": p.get("hubspot_owner_id") or "",
                        "lifecyclestage": p.get("lifecyclestage") or "",
                        "contacts": int(p.get("num_associated_contacts") or 0),
                        "last_contacted": p.get("notes_last_contacted") or ""})
        last = res[-1]["id"]
        if len(res) < 200:
            break
    return out


def open_deal_ids(ids):
    comp_deals, all_deals = {}, set()
    for i in range(0, len(ids), 100):
        r = post(BASE + "/crm/v4/associations/companies/deals/batch/read",
                 {"inputs": [{"id": c} for c in ids[i:i + 100]]})
        if r.status_code >= 300:
            print("ASSOC FAIL", r.status_code, r.text[:200]); sys.exit(1)
        for res in r.json().get("results", []):
            d = [str(t["toObjectId"]) for t in res.get("to", [])]
            if d:
                comp_deals[str(res["from"]["id"])] = d
                all_deals |= set(d)
    stages, dl = {}, sorted(all_deals)
    for i in range(0, len(dl), 100):
        r = post(BASE + "/crm/v3/objects/deals/batch/read",
                 {"properties": ["dealstage"], "inputs": [{"id": d} for d in dl[i:i + 100]]})
        for x in r.json().get("results", []):
            stages[x["id"]] = x["properties"].get("dealstage") or ""
    out = set()
    for c, ds in comp_deals.items():
        if any(stages.get(d, "") and stages.get(d, "") not in CLOSED for d in ds):
            out.add(c)
    return out, comp_deals


def arg(flag, d=None):
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return d


def main():
    outdir = sys.argv[1]
    pct = float(arg("--pct", "10"))
    owner_change = "--no-owner-change" not in sys.argv
    os.makedirs(outdir, exist_ok=True)

    book = fetch_book()
    ids = [r["hs_object_id"] for r in book]
    od, comp_deals = open_deal_ids(ids)
    print("Milosh book: %s companies | %s with any deal | %s with an OPEN deal\n"
          % (format(len(book), ","), format(len(comp_deals), ","), format(len(od), ",")))

    for r in book:
        r["open_deal"] = r["hs_object_id"] in od
        ls = r["lifecyclestage"]
        if r["open_deal"]:
            r["protect"], r["why_p"] = True, "open deal"
        elif ls in KEEP:
            r["protect"], r["why_p"] = True, KEEP[ls]
        else:
            r["protect"], r["why_p"] = False, ""

    prot = [r for r in book if r["protect"]]
    elig = [r for r in book if not r["protect"]]

    tgt_contacts = arg("--target-contacts")
    if tgt_contacts:
        tgt_contacts = int(tgt_contacts)
        lo = int(arg("--min-per-co", "1"))
        hi = int(arg("--max-per-co", "19"))
        band = [r for r in elig if lo <= r["contacts"] <= hi]
        rest = [r for r in elig if not (lo <= r["contacts"] <= hi)]
        # densest first inside the band, so the target is hit with fewer accounts
        band.sort(key=lambda r: (-r["contacts"], int(r["hs_object_id"])))
        run, chosen = 0, set()
        for r in band:
            if run >= tgt_contacts:
                break
            chosen.add(r["hs_object_id"])
            run += r["contacts"]
        for r in elig:
            r["side"] = "G" if r["hs_object_id"] in chosen else "D"
        print("contact-target mode: %s contacts, accounts with %d-%d contacts only"
              % (format(tgt_contacts, ","), lo, hi))
        print("  band had %s accounts / %s contacts; excluded %s accounts outside the band"
              % (format(len(band), ","), format(sum(r["contacts"] for r in band), ","),
                 format(len(rest), ",")))
        print("  selected %s accounts carrying %s contacts"
              % (format(len(chosen), ","), format(run, ",")))
    else:
        target = int(round(len(book) * pct / 100.0))
        take = min(target, len(elig))
        # contactable first, most contacts first, so Gavin gets callable accounts
        elig.sort(key=lambda r: (-(1 if r["contacts"] > 0 else 0), -r["contacts"],
                                 int(r["hs_object_id"])))
        for i, r in enumerate(elig):
            r["side"] = "G" if i < take else "D"
    for r in prot:
        r["side"] = "D"

    G = [r for r in book if r["side"] == "G"]
    D = [r for r in book if r["side"] == "D"]

    if not tgt_contacts:
        print("target %.0f%% of %s = %s  (eligible pool %s, taking %s)"
              % (pct, format(len(book), ","), format(target, ","),
                 format(len(elig), ","), format(take, ",")))
    print("protected to Dana: %s" % format(len(prot), ","))
    pc = {}
    for r in prot:
        pc[r["why_p"]] = pc.get(r["why_p"], 0) + 1
    for k, v in sorted(pc.items(), key=lambda kv: -kv[1]):
        print("    %-16s %5s" % (k, format(v, ",")))

    def stat(rows, label, pod, sdr):
        empty = sum(1 for r in rows if r["contacts"] == 0)
        able = sum(1 for r in rows if r["contacts"] > 0)
        fresh = sum(1 for r in rows if r["contacts"] > 0 and not r["last_contacted"])
        print("  %-6s %6s companies | %5s contactable | %5s empty | %4s fresh | "
              "%3s open deals | %6s contacts  -> pod %s, sdr %s"
              % (label, format(len(rows), ","), format(able, ","), format(empty, ","),
                 format(fresh, ","), sum(1 for r in rows if r["open_deal"]),
                 format(sum(r["contacts"] for r in rows), ","), pod, sdr))

    print("\nRESULT:")
    stat(G, "GAVIN", "Pod 2", "Gavin")
    stat(D, "DANA", "Pod 1", "Dana")

    pod_ch = sum(1 for r in G if r["pod_before"] != "Pod 2")
    own_ch = sum(1 for r in G if r["owner_before"] != KEVIN)
    print("\nGavin slice is CROSS-POD: pod changes on %s of %s rows; AE owner would "
          "change on %s rows (owner_change=%s)"
          % (format(pod_ch, ","), format(len(G), ","), format(own_ch, ","), owner_change))
    ob = {}
    for r in G:
        ob[r["owner_before"] or "(blank)"] = ob.get(r["owner_before"] or "(blank)", 0) + 1
    print("  current AE owners in Gavin's slice:")
    for k, v in sorted(ob.items(), key=lambda kv: -kv[1])[:6]:
        print("     %-14s %5s" % (k, format(v, ",")))

    def write(path, rows, pod, sdr, nm, ae):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["hs_object_id", "name", "domain", "pod", "hubspot_owner_id",
                        "sdr_owner_id", "sdr_owner_name", "why", "contacts",
                        "lifecyclestage", "open_deal", "pod_before", "owner_before"])
            for r in rows:
                w.writerow([r["hs_object_id"], r["name"], r["domain"], pod,
                            ae if ae else r["owner_before"], sdr, nm,
                            r["why_p"] or ("contactable" if r["contacts"] else "empty shell"),
                            r["contacts"], r["lifecyclestage"], int(r["open_deal"]),
                            r["pod_before"], r["owner_before"]])

    write(os.path.join(outdir, "gavin_from_milosh_%dpct.csv" % int(pct)), G,
          "Pod 2", GAVIN, "Gavin Keo", KEVIN if owner_change else "")
    write(os.path.join(outdir, "dana_from_milosh_%dpct.csv" % int(pct)), D,
          "Pod 1", DANA, "Dana Nicoletti", "")
    json.dump({"pct": pct, "total": len(book), "gavin": len(G), "dana": len(D),
               "protected": len(prot)},
              open(os.path.join(outdir, "summary_%dpct.json" % int(pct)), "w"), indent=1)
    print("\nwrote CSVs to %s/  (nothing pushed)" % outdir)


if __name__ == "__main__":
    main()
