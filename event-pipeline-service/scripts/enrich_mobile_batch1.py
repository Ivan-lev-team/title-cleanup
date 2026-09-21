"""
Runs enrichment.find_mobile (Prospeo -> Forager -> LeadMagic waterfall) over
the priority mobile batch, threaded.

Usage: python enrich_mobile_batch1.py <input.csv> <outdir> <output_filename.csv> [--workers N]
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
    result = enrichment.find_mobile(
        full_name=row.get("Full Name", ""),
        first_name=row.get("First Name", ""),
        last_name=row.get("Last Name", ""),
        company_name=row.get("Company Table Data", ""),
        domain=row.get("Company Domain", ""),
        linkedin_url=row.get("LinkedIn Profile", ""),
        email="",
    )
    return row, result.get("mobile", ""), result.get("provider", "")


def main():
    if len(sys.argv) < 4:
        print("Usage: python enrich_mobile_batch1.py <input.csv> <outdir> <output_filename.csv> [--workers N]")
        sys.exit(1)
    in_path, outdir, out_filename = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
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
            row, mobile, provider = fut.result()
            row["Mobile Phone Number"] = mobile
            row["Mobile Source"] = provider
            results.append(row)
            done += 1
            if provider:
                provider_counts[provider] += 1
            if done % 50 == 0 or done == len(rows):
                print(f"{done}/{len(rows)} processed, {sum(provider_counts.values())} mobiles found "
                      f"({dict(provider_counts)})", flush=True)

    out_df = pd.DataFrame(results)
    out_df.to_csv(outdir / out_filename, index=False, quoting=csv.QUOTE_MINIMAL)

    found = sum(1 for r in results if r["Mobile Phone Number"])
    print(f"\nDONE: {found}/{len(results)} mobiles found ({found/len(results)*100:.1f}%)")
    print("By provider:", dict(provider_counts))


if __name__ == "__main__":
    main()
