"""
Joins contacts_kept.csv (post-blocklist) against the CONSUMER/B2B/RETAILER
classification to produce the final surviving contact list plus a full audit
trail of every eliminated company and why.

Usage: python build_final_kept.py <outdir>
"""
import csv
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from screen_leadership_export import normalize  # noqa: E402


def main():
    outdir = Path(sys.argv[1])

    contacts = pd.read_csv(outdir / "contacts_kept.csv", dtype=str, keep_default_na=False)
    contacts["_key"] = contacts["Company Table Data"].map(normalize)
    contacts["_key"] = contacts["_key"].where(contacts["_key"] != "", contacts["Company Domain"].str.lower())

    consumer_class = pd.read_csv(outdir / "companies_consumer_b2b_retail.csv", dtype=str, keep_default_na=False)
    consumer_class["_key"] = consumer_class["company_name"].map(normalize)
    consumer_class["_key"] = consumer_class["_key"].where(consumer_class["_key"] != "", consumer_class["domain"].str.lower())

    keep_keys = set(consumer_class.loc[consumer_class["classification"] == "CONSUMER", "_key"])

    final_kept = contacts[contacts["_key"].isin(keep_keys)].drop(columns=["_key"])
    final_kept.to_csv(outdir / "FINAL_contacts_consumer_brands.csv", index=False, quoting=csv.QUOTE_MINIMAL)

    # audit trail: every unique company from the original blocklist screen, with final disposition
    blocklist_companies = pd.read_csv(outdir / "companies_blocked.csv", dtype=str, keep_default_na=False)
    icp_judged = pd.read_csv(outdir / "companies_icp_judged.csv", dtype=str, keep_default_na=False)

    audit_rows = []
    for _, r in blocklist_companies.iterrows():
        audit_rows.append({
            "company_name": r["company_name"], "domain": r["domain"], "contact_count": r["contact_count"],
            "disposition": "ELIMINATED", "stage": "keyword_blocklist", "reason": r["block_reason"],
        })
    for _, r in icp_judged.iterrows():
        if r["verdict"] != "PASS":
            audit_rows.append({
                "company_name": r["company_name"], "domain": r["domain"], "contact_count": r["contact_count"],
                "disposition": "ELIMINATED", "stage": f"icp_judge_{r['verdict']}", "reason": r["reason"],
            })
    for _, r in consumer_class.iterrows():
        disp = "KEPT" if r["classification"] == "CONSUMER" else "ELIMINATED"
        audit_rows.append({
            "company_name": r["company_name"], "domain": r["domain"], "contact_count": r["contact_count"],
            "disposition": disp, "stage": f"tighten_{r['classification']}", "reason": r["reason"],
        })

    audit_df = pd.DataFrame(audit_rows).sort_values(
        ["disposition", "contact_count"], ascending=[True, False]
    )
    audit_df.to_csv(outdir / "AUDIT_all_companies.csv", index=False, quoting=csv.QUOTE_MINIMAL)

    kept_companies = (audit_df["disposition"] == "KEPT").sum()
    eliminated_companies = (audit_df["disposition"] == "ELIMINATED").sum()
    eliminated_contacts = audit_df.loc[audit_df["disposition"] == "ELIMINATED", "contact_count"].astype(int).sum()

    print(f"Final kept contacts:      {len(final_kept)}")
    print(f"Final kept companies:     {kept_companies}")
    print(f"Total eliminated companies: {eliminated_companies}")
    print(f"Total eliminated contacts:  {eliminated_contacts}")


if __name__ == "__main__":
    main()
