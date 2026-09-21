"""
Builds a Clay-sourcing seed from HubSpot list 7687 (Darko & Kristijan Companies).

Flags sub-brand / parent-domain risk so those companies are NOT auto-sourced:
Clay resolves a domain to whatever company owns it, so a sub-brand record whose
domain points at the parent (e.g. "Polly Pocket" -> mattel.com) returns the
parent corporation's executives instead of the brand's team.

Read-only against HubSpot. Usage:
    python build_list7687_seed.py <outdir>
"""
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hubspot_client as hc  # noqa: E402

LIST_ID = 7687
PROPS = ["name", "domain", "sdr_owner", "pod", "num_associated_contacts"]


def tok(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def strip_domain(d):
    """Registrable-ish root: drop scheme, www/us/shop subdomain noise, and TLD."""
    d = (d or "").lower().strip()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    parts = [p for p in d.split(".") if p]
    if not parts:
        return ""
    # drop common leading subdomains
    while len(parts) > 2 and parts[0] in ("www", "us", "uk", "shop", "store", "en"):
        parts = parts[1:]
    return parts[0] if len(parts) <= 2 else parts[-2]


def fetch_list_companies():
    ids, after = [], None
    while True:
        url = f"{hc.BASE}/crm/v3/lists/{LIST_ID}/memberships?limit=250" + (f"&after={after}" if after else "")
        d = hc.request_with_retry("GET", url).json()
        ids += [x["recordId"] for x in d.get("results", [])]
        after = (d.get("paging", {}).get("next", {}) or {}).get("after")
        if not after:
            break

    recs = []
    for batch in hc.chunked(ids, 100):
        body = {"inputs": [{"id": str(i)} for i in batch], "properties": PROPS}
        d = hc.request_with_retry("POST", f"{hc.BASE}/crm/v3/objects/companies/batch/read", json=body).json()
        recs += d.get("results", [])
    return recs


def fetch_all_records_for_domains(domains):
    """All HubSpot companies sharing each domain -> {domain: [records]}.
    Unlike find_companies_by_domains_bulk this keeps EVERY match, which is the
    whole point: duplicates are the signal we're looking for."""
    out = defaultdict(list)
    for batch in hc.chunked(sorted(set(domains)), hc.DOMAIN_BATCH_SIZE):
        body = {
            "filterGroups": [{"filters": [{"propertyName": "domain", "operator": "IN", "values": batch}]}],
            "properties": ["name", "domain", "sdr_owner"],
            "limit": 100,
        }
        after = None
        while True:
            if after:
                body["after"] = after
            d = hc.request_with_retry("POST", f"{hc.BASE}/crm/v3/objects/companies/search", json=body).json()
            for r in d.get("results", []):
                dom = (r["properties"].get("domain") or "").lower().strip()
                if dom:
                    out[dom].append(r)
            after = (d.get("paging", {}).get("next", {}) or {}).get("after")
            if not after:
                break
    return out


def main():
    outdir = Path(sys.argv[1] if len(sys.argv) > 1 else "data/list7687_seed")
    outdir.mkdir(parents=True, exist_ok=True)

    recs = fetch_list_companies()
    print(f"list {LIST_ID}: {len(recs)} companies", flush=True)

    rows = []
    for r in recs:
        p = r["properties"]
        rows.append({
            "hubspot_company_id": r["id"],
            "company_name": (p.get("name") or "").strip(),
            "domain": (p.get("domain") or "").lower().strip(),
            "sdr_owner_id": (p.get("sdr_owner") or "").strip(),
            "pod": (p.get("pod") or "").strip(),
            "existing_contacts": int(p.get("num_associated_contacts") or 0),
        })

    owner_names = {}
    for oid in {r["sdr_owner_id"] for r in rows if r["sdr_owner_id"]}:
        owner_names[oid] = hc.get_owner_name(oid) or oid
    for r in rows:
        r["sdr_owner_name"] = owner_names.get(r["sdr_owner_id"], "")

    with_dom = [r for r in rows if r["domain"]]
    blank = [r for r in rows if not r["domain"]]
    print(f"  with domain: {len(with_dom)} | blank domain: {len(blank)}", flush=True)

    # --- flag 1: domain shared by >1 record inside the list itself
    in_list = Counter(r["domain"] for r in with_dom)

    # --- flag 2: another HubSpot company shares the domain under a different name
    print("checking for parent records sharing these domains...", flush=True)
    all_recs = fetch_all_records_for_domains([r["domain"] for r in with_dom])

    for r in rows:
        d, nm = r["domain"], r["company_name"]
        if not d:
            r["dup_in_list"] = ""
            r["parent_record_exists"] = ""
            r["name_domain_mismatch"] = ""
            r["sourceable"] = "no_domain"
            continue

        r["dup_in_list"] = "Y" if in_list[d] > 1 else ""

        others = [x for x in all_recs.get(d, []) if x["id"] != r["hubspot_company_id"]]
        parent = ""
        for x in others:
            on = (x["properties"].get("name") or "").strip()
            if on and tok(on) != tok(nm) and tok(on) not in tok(nm) and tok(nm) not in tok(on):
                parent = on
                break
        r["parent_record_exists"] = parent

        root = strip_domain(d)
        n, rt = tok(nm), tok(root)
        r["name_domain_mismatch"] = "Y" if (n and rt and n not in rt and rt not in n) else ""

        flagged = r["dup_in_list"] or r["parent_record_exists"] or r["name_domain_mismatch"]
        r["sourceable"] = "review" if flagged else "yes"

    fields = ["hubspot_company_id", "company_name", "domain", "sdr_owner_id", "sdr_owner_name",
              "pod", "existing_contacts", "dup_in_list", "parent_record_exists",
              "name_domain_mismatch", "sourceable"]

    def dump(name, subset):
        with open(outdir / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in subset:
                w.writerow({k: r.get(k, "") for k in fields})

    dump("seed_all.csv", rows)
    dump("seed_sourceable.csv", [r for r in rows if r["sourceable"] == "yes"])
    dump("seed_review.csv", [r for r in rows if r["sourceable"] == "review"])
    dump("seed_no_domain.csv", blank)

    c = Counter(r["sourceable"] for r in rows)
    print("\n--- seed summary ---")
    print(f"  sourceable (clean, has domain): {c['yes']}")
    print(f"  review (sub-brand/parent risk): {c['review']}")
    print(f"  no domain (skipped):            {c['no_domain']}")
    print(f"  TOTAL:                          {sum(c.values())}")
    print("\n  flag counts among domain-bearing:")
    print(f"    dup_in_list:          {sum(1 for r in with_dom if r['dup_in_list'])}")
    print(f"    parent_record_exists: {sum(1 for r in with_dom if r['parent_record_exists'])}")
    print(f"    name_domain_mismatch: {sum(1 for r in with_dom if r['name_domain_mismatch'])}")


if __name__ == "__main__":
    main()
