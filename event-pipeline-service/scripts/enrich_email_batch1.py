"""
Runs enrichment.find_email (LeadMagic -> Prospeo waterfall) over Batch 1,
threaded. Only strictly-VERIFIED emails go in the main Email column;
unverified hits go to a Notes-style column, never auto-used.

Usage: python enrich_email_batch1.py <input.csv> <outdir> [--workers N]
"""
import csv
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import enrichment  # noqa: E402


def enrich_one(row):
    result = enrichment.find_email(
        first_name=row.get("First Name", ""),
        last_name=row.get("Last Name", ""),
        full_name=row.get("Full Name", ""),
        company_name=row.get("Company Table Data", ""),
        domain=row.get("Company Domain", ""),
        linkedin_url=row.get("LinkedIn Profile", ""),
    )
    return row, result


def main():
    if len(sys.argv) < 3:
        print("Usage: python enrich_email_batch1.py <input.csv> <outdir> [--workers N]")
        sys.exit(1)
    in_path, outdir = sys.argv[1], Path(sys.argv[2])
    workers = 6
    if "--workers" in sys.argv:
        workers = int(sys.argv[sys.argv.index("--workers") + 1])
    outdir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(in_path, dtype=str, keep_default_na=False)
    rows = df.to_dict("records")

    results = []
    done = 0
    provider_counts = Counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(enrich_one, r): r for r in rows}
        for fut in as_completed(futures):
            row, res = fut.result()
            row["Email"] = res.get("email", "")
            row["Email Source"] = res.get("provider", "")
            row["Unverified Email"] = res.get("unverified_email", "")
            row["Unverified Email Note"] = res.get("unverified_note", "")
            if not row.get("LinkedIn Profile") and res.get("linkedin_url"):
                row["LinkedIn Profile"] = res["linkedin_url"]
            results.append(row)
            done += 1
            if res.get("provider"):
                provider_counts[res["provider"]] += 1
            if done % 50 == 0 or done == len(rows):
                print(f"{done}/{len(rows)} processed, {sum(provider_counts.values())} verified emails found "
                      f"({dict(provider_counts)})", flush=True)

    out_df = pd.DataFrame(results)
    out_df.to_csv(outdir / "BATCH1_mobile_email_enriched.csv", index=False, quoting=csv.QUOTE_MINIMAL)

    found = sum(1 for r in results if r["Email"])
    unverified = sum(1 for r in results if not r["Email"] and r["Unverified Email"])
    print(f"\nDONE: {found}/{len(results)} verified emails found ({found/len(results)*100:.1f}%)")
    print(f"Unverified-only hits (not auto-used): {unverified}")
    print("By provider:", dict(provider_counts))


if __name__ == "__main__":
    main()
