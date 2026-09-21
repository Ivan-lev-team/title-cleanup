"""
Read-only: looks up Batch 1's unique companies in HubSpot and reports how they
break down by the company-level `sdr_owner` property (e.g. Darko vs Kristijan).

Writes nothing to HubSpot. Reuses hubspot_client.find_companies_by_domains_bulk,
which already requests ["name","domain","pod","sdr_owner"] and carries the
over-match guard + per-domain fallback.

Usage: python batch1_sdr_owner_split.py <enriched.csv> <outdir>
"""
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hubspot_client  # noqa: E402


def main():
    if len(sys.argv) != 3:
        print("Usage: python batch1_sdr_owner_split.py <enriched.csv> <outdir>")
        sys.exit(1)
    in_path, outdir = sys.argv[1], Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(in_path, dtype=str, keep_default_na=False)
    companies = (
        df.groupby("Company Table Data")
        .agg(domain=("Company Domain", "first"), contacts=("Full Name", "size"))
        .reset_index()
        .rename(columns={"Company Table Data": "company_name"})
    )
    companies["domain"] = companies["domain"].str.strip().str.lower()
    print(f"{len(companies)} unique companies "
          f"({(companies['domain'] != '').sum()} with a domain, "
          f"{(companies['domain'] == '').sum()} without)", flush=True)

    domains = [d for d in companies["domain"] if d]
    print("Querying HubSpot by domain...", flush=True)
    found = hubspot_client.find_companies_by_domains_bulk(domains)
    print(f"{len(found)} of {len(domains)} domains matched a HubSpot company", flush=True)

    # resolve sdr_owner ids -> names
    owner_ids = set()
    for rec in found.values():
        v = (rec["properties"].get("sdr_owner") or "").strip()
        if v:
            owner_ids.add(v)

    owner_names = {}
    for oid in owner_ids:
        try:
            name = hubspot_client.get_owner_name(oid)
        except Exception:
            name = ""
        owner_names[oid] = name or f"(owner {oid})"

    rows = []
    for _, c in companies.iterrows():
        rec = found.get(c["domain"]) if c["domain"] else None
        if rec is None:
            status = "no_domain" if not c["domain"] else "not_in_hubspot"
            sdr_id, sdr_name, pod, hs_id = "", "", "", ""
        else:
            p = rec["properties"]
            sdr_id = (p.get("sdr_owner") or "").strip()
            sdr_name = owner_names.get(sdr_id, "") if sdr_id else ""
            pod = (p.get("pod") or "").strip()
            hs_id = rec["id"]
            status = "in_hubspot"
        rows.append({
            "company_name": c["company_name"],
            "domain": c["domain"],
            "contacts": c["contacts"],
            "status": status,
            "hubspot_company_id": hs_id,
            "sdr_owner_id": sdr_id,
            "sdr_owner_name": sdr_name,
            "pod": pod,
        })

    out = pd.DataFrame(rows).sort_values("contacts", ascending=False)
    out.to_csv(outdir / "BATCH1_companies_sdr_owner.csv", index=False, quoting=csv.QUOTE_MINIMAL)

    # ---- aggregate report ----
    def bucket(r):
        if r["status"] == "no_domain":
            return "No domain (not queryable)"
        if r["status"] == "not_in_hubspot":
            return "Not in HubSpot"
        if not r["sdr_owner_id"]:
            return "In HubSpot, no SDR Owner set"
        return r["sdr_owner_name"]

    out["bucket"] = out.apply(bucket, axis=1)
    agg = out.groupby("bucket").agg(companies=("company_name", "size"),
                                    contacts=("contacts", "sum")).reset_index()
    agg = agg.sort_values("contacts", ascending=False)

    print("\n--- Batch 1 companies by SDR Owner ---")
    print(agg.to_string(index=False))
    print(f"\nTOTAL: {agg['companies'].sum()} companies / {agg['contacts'].sum()} contacts")


if __name__ == "__main__":
    main()
