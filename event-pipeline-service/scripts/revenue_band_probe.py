#!/usr/bin/env python3
"""
Where is the contact-data coverage cliff, by company revenue?

The 100-company run found 57 of 100 companies had nobody in EITHER Seamless or
Prospeo. That sample was $981k-$3.1M revenue / 6-125 employees, and the
"Prioritization by Title" sheet matched 0 contacts on it. This probe tests
whether coverage and ICP-title match rate improve with company size, BEFORE
committing research credits to a 3-4k run.

Method, deliberately cheap:
  * sample N companies per revenue band from the Pod RevOps zero-contact pool,
    reusing the same hygiene rules as the pilot picker
  * ONE unfiltered Seamless search per band (domains batch up to 100), paged
  * apply icp_titles.title_passes LOCALLY to the returned titles -- free, and
    it yields both raw availability and ICP-qualified availability from the
    same call

No research. No HubSpot writes.

  python scripts/revenue_band_probe.py [--per-band 25] [--seed 4242]
"""
import csv, json, os, sys, collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)

import requests
from dotenv import dotenv_values

import config, icp_titles, seamless_client, enrichment
sys.path.insert(0, os.path.join(HERE, "scripts"))
from fix_list_domains import NON_BRAND_HOSTS

HUBSPOT_TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
HS = {"Authorization": "Bearer " + HUBSPOT_TOKEN, "Content-Type": "application/json"}

# Non-overlapping bands. The first pass used $3M-10M AND $5M-10M, which
# overlap, and the subset scored half as well as its superset -- impossible as
# a real effect and the tell that n=25 was far too small (95% CI ~ +/-20pts).
BANDS = [
    ("$1M-3M   (pilot baseline)", 1_000_000, 3_000_000),
    ("$3M-5M",                    3_000_000, 5_000_000),
    ("$5M-10M",                   5_000_000, 10_000_000),
    ("$10M-50M",                 10_000_000, 50_000_000),
    ("$50M-500M",                50_000_000, 500_000_000),
]


def arg(flag, d=None):
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return d


def pick(lo, hi, n, seed):
    import random
    out, last, seen = [], "0", set()
    while len(out) < n * 12:
        body = {"filterGroups": [{"filters": [
                    {"propertyName": "pod", "operator": "EQ", "value": "Pod RevOps"},
                    {"propertyName": "num_associated_contacts", "operator": "EQ", "value": "0"},
                    {"propertyName": "domain", "operator": "HAS_PROPERTY"},
                    {"propertyName": "annualrevenue", "operator": "GTE", "value": str(lo)},
                    {"propertyName": "annualrevenue", "operator": "LT", "value": str(hi)},
                    {"propertyName": "hs_object_id", "operator": "GT", "value": last}]}],
                "properties": ["name", "domain", "annualrevenue", "numberofemployees"],
                "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
                "limit": 100}
        r = requests.post("https://api.hubapi.com/crm/v3/objects/companies/search",
                          headers=HS, json=body, timeout=60)
        if r.status_code >= 300:
            print("  hubspot search failed", r.status_code, r.text[:140]); break
        res = r.json().get("results", [])
        if not res:
            break
        for x in res:
            p = x["properties"]
            d = enrichment.clean_domain(p.get("domain") or "")
            if not d or "." not in d or d in seen:
                continue
            if any(d == h or d.endswith("." + h) for h in NON_BRAND_HOSTS):
                continue
            seen.add(d)
            out.append({"domain": d, "name": (p.get("name") or "").strip(),
                        "rev": p.get("annualrevenue") or "",
                        "emp": p.get("numberofemployees") or ""})
        last = res[-1]["id"]
    return random.Random(seed).sample(out, min(n, len(out))) if out else []


def probe_per_domain(domains):
    """One unfiltered search PER DOMAIN, deduped by searchResultId.

    Was originally one batched call in a 3-iteration loop, but that loop never
    advanced a page token, so it re-ran the same query and triple-counted every
    contact (78 "ICP contacts" from 25 companies). Per-domain is the honest
    measurement: it attributes contacts to the right company and cannot
    double-count, at the cost of one search call per company.
    """
    out, seen = {}, set()
    for d in domains:
        res = seamless_client.search_contacts_unfiltered([d], limit=25)
        rows = []
        for g in res.get("results") or []:
            sid = g.get("searchResultId")
            if sid and sid in seen:
                continue
            if sid:
                seen.add(sid)
            rows.append(g)
        out[d] = rows
    return out


def main():
    per = int(arg("--per-band", "25"))
    seed = int(arg("--seed", "4242"))
    outdir = arg("--outdir", "runs/revenue_band_probe_20260918")
    os.makedirs(outdir, exist_ok=True)
    print("REVENUE BAND COVERAGE PROBE  (%d companies/band, seed %d)\n" % (per, seed))

    summary, detail = [], []
    for label, lo, hi in BANDS:
        comps = pick(lo, hi, per, seed)
        if not comps:
            print("%-28s no companies available in pool" % label)
            continue
        by_dom = {c["domain"]: c for c in comps}
        raw = probe_per_domain([c["domain"] for c in comps])
        contacts = [g for gs in raw.values() for g in gs]

        # Strict attribution: a contact counts for the company we queried only
        # if the returned domain agrees (or it reports no domain at all).
        # Anything else is leakage and is counted, not credited.
        per_dom = collections.defaultdict(list)
        leaked = 0
        for d, gs in raw.items():
            for g in gs:
                cands = [x for x in
                         [(g.get("domain") or "").lower()] +
                         [(y or "").lower() for y in (g.get("domains") or [])] if x]
                if (d in cands) or (not cands):
                    per_dom[d].append(g)
                else:
                    leaked += 1

        icp_dom = collections.defaultdict(list)
        for d, gs in per_dom.items():
            for g in gs:
                ok, _ = icp_titles.title_passes(g.get("title") or "",
                                                employee_count=g.get("employeeCount"))
                if ok:
                    icp_dom[d].append(g)

        emps = [int(g["employeeCount"]) for gs in per_dom.values() for g in gs
                if str(g.get("employeeCount") or "").strip().isdigit()]
        med_emp = sorted(emps)[len(emps) // 2] if emps else None
        row = {
            "band": label, "companies": len(comps),
            "leaked": leaked,
            "contacts_returned": len(contacts),
            "with_any_contact": len(per_dom),
            "with_icp_contact": len(icp_dom),
            "icp_contacts": sum(len(v) for v in icp_dom.values()),
            "median_employees": med_emp,
        }
        row["pct_any"] = round(100.0 * row["with_any_contact"] / row["companies"], 1)
        row["pct_icp"] = round(100.0 * row["with_icp_contact"] / row["companies"], 1)
        row["icp_per_company"] = round(row["icp_contacts"] / row["companies"], 2)
        summary.append(row)

        print("%-28s companies=%-3d  any_contact=%-3d (%4.1f%%)  ICP_contact=%-3d (%4.1f%%)  "
              "ICP_contacts=%-3d (%.2f/co)  leak=%-3d med_emp=%s"
              % (label, row["companies"], row["with_any_contact"], row["pct_any"],
                 row["with_icp_contact"], row["pct_icp"], row["icp_contacts"],
                 row["icp_per_company"], leaked, med_emp))

        for d, gs in sorted(icp_dom.items()):
            for g in gs[:3]:
                detail.append({"band": label, "domain": d,
                               "company": by_dom[d]["name"], "revenue": by_dom[d]["rev"],
                               "name": g.get("name", ""), "title": g.get("title", ""),
                               "seniority": g.get("seniority", ""),
                               "employees": g.get("employeeCount", "")})

    with open(os.path.join(outdir, "band_summary.csv"), "w", newline="",
              encoding="utf-8") as f:
        if summary:
            w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
            w.writeheader(); w.writerows(summary)
    with open(os.path.join(outdir, "band_icp_examples.csv"), "w", newline="",
              encoding="utf-8") as f:
        if detail:
            w = csv.DictWriter(f, fieldnames=list(detail[0].keys()))
            w.writeheader(); w.writerows(detail)
    print("\nwrote %s/band_summary.csv and band_icp_examples.csv" % outdir)
    print("NO research calls, NO HubSpot writes.")


if __name__ == "__main__":
    main()
