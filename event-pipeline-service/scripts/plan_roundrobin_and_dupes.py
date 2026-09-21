"""
DRY RUN ONLY. Extends the capped push plan with:
  1. Round-robin pod/AE/SDR assignment for companies that exist in HubSpot but
     have no valid sdr_owner (blank, or a stale id that 404s against the
     owners API -- found: 87811820 and 80630634 are both dead ids).
  2. Multi-association plan for contacts on domains with duplicate company
     records -- associate to ALL matching companies rather than guessing one.

Uses hubspot_client.least_loaded_pod() / least_loaded_owner_in_pod() for the
live starting counts, then simulates increments in-memory across this batch
so the 12 companies actually spread out instead of all landing on whichever
pod is least-loaded right now.
"""
import csv
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import hubspot_client as hc  # noqa: E402

INVALID_OWNER_IDS = {"87811820", "80630634"}  # confirmed 404 against /crm/v3/owners


def main():
    in_path, outdir = sys.argv[1], Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(in_path, dtype=str, keep_default_na=False)

    needs_rr = df[(df["hubspot_company_id"] != "") &
                  ((df["route_sdr_owner_id"] == "") | (df["route_sdr_owner_id"].isin(INVALID_OWNER_IDS)))]
    rr_companies = needs_rr.drop_duplicates("hubspot_company_id")[["hubspot_company_id", "company_name_source"]]
    print(f"Round-robin needed for {len(rr_companies)} companies ({len(needs_rr)} contacts)", flush=True)

    # live starting pod counts
    pod_counts = {}
    for pod in config.POD_OWNERS:
        body = {"filterGroups": [{"filters": [{"propertyName": "pod", "operator": "EQ", "value": pod}]}],
                "properties": [], "limit": 1}
        r = hc.request_with_retry("POST", f"{hc.BASE}/crm/v3/objects/companies/search", json=body)
        pod_counts[pod] = r.json().get("total", 0) if r.status_code < 300 else 0
    print("starting pod counts:", pod_counts, flush=True)

    sdr_counts = {}
    for pod, owners in config.POD_OWNERS.items():
        for owner in owners:
            body = {"filterGroups": [{"filters": [
                {"propertyName": "pod", "operator": "EQ", "value": pod},
                {"propertyName": "sdr_owner", "operator": "EQ", "value": owner},
            ]}], "properties": [], "limit": 1}
            r = hc.request_with_retry("POST", f"{hc.BASE}/crm/v3/objects/companies/search", json=body)
            sdr_counts[(pod, owner)] = r.json().get("total", 0) if r.status_code < 300 else 0

    AE_BY_POD = {  # hubspot_owner_id (AE) per pod, from the pod roster skill
        "Pod 1": "75419877", "Pod 2": "75419876", "Pod 3": "85008611", "Pod 4": "90849231",
        "Pod 5": "92545016", "Pod 6": "96662547", "Pod 7": "578616981",
    }

    assignments = []
    for _, row in rr_companies.iterrows():
        pod = min(pod_counts, key=pod_counts.get)
        pod_counts[pod] += 1
        owners = config.POD_OWNERS[pod]
        sdr = min(owners, key=lambda o: sdr_counts[(pod, o)])
        sdr_counts[(pod, sdr)] += 1
        assignments.append({
            "hubspot_company_id": row["hubspot_company_id"],
            "company_name": row["company_name_source"],
            "assigned_pod": pod,
            "assigned_hubspot_owner_id": AE_BY_POD[pod],
            "assigned_sdr_owner": sdr,
            "assigned_sdr_owner_name": hc.get_owner_name(sdr),
        })

    rr_df = pd.DataFrame(assignments)
    rr_df.to_csv(outdir / "ROUNDROBIN_PLAN.csv", index=False, quoting=csv.QUOTE_MINIMAL)
    print("\n--- Round-robin plan (12 companies) ---")
    print(rr_df.to_string(index=False))
    print("\nNew pod distribution after these 12:", pod_counts)

    # ---- duplicate-domain multi-association plan ----
    dup = df[df["company_duplicates"] != ""]
    dup_domains = sorted(dup["domain"].unique())
    print(f"\n{len(dup)} contacts on {len(dup_domains)} duplicate-domain companies", flush=True)

    dup_detail = []
    for d in dup_domains:
        body = {"filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ", "value": d}]}],
                "properties": ["name", "sdr_owner", "pod"], "limit": 10}
        r = hc.request_with_retry("POST", f"{hc.BASE}/crm/v3/objects/companies/search", json=body)
        recs = r.json().get("results", [])
        for rec in recs:
            dup_detail.append({
                "domain": d,
                "hubspot_company_id": rec["id"],
                "company_name": rec["properties"].get("name", ""),
                "sdr_owner": rec["properties"].get("sdr_owner", ""),
                "pod": rec["properties"].get("pod", ""),
                "contacts_would_associate": (dup["domain"] == d).sum(),
            })
    dup_df = pd.DataFrame(dup_detail)
    dup_df.to_csv(outdir / "DUPLICATE_DOMAIN_ASSOCIATIONS.csv", index=False, quoting=csv.QUOTE_MINIMAL)
    print(f"\nWrote {len(dup_df)} company records across {len(dup_domains)} domains "
          f"-> each contact on these domains would associate to ALL listed records for its domain.")
    print(dup_df.head(20).to_string(index=False))


if __name__ == "__main__":
    main()
