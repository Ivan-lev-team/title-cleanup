"""
Re-attempts leadmagic_client.find_email (LeadMagic tier only, not the full
LeadMagic->Prospeo waterfall) on a set of contacts that already have a
mobile number but no email yet. Confirms/re-checks the LeadMagic tier
specifically rather than re-running the whole email waterfall.

Usage: python retry_leadmagic_email.py <input.csv> <outdir> [--workers N]
"""
import csv
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import leadmagic_client  # noqa: E402


def retry_one(row):
    res = leadmagic_client.find_email(
        first_name=row.get("First Name", ""),
        last_name=row.get("Last Name", ""),
        company_name=row.get("Company Table Data", ""),
        domain=row.get("Company Domain", ""),
    )
    return row, res


def main():
    if len(sys.argv) < 3:
        print("Usage: python retry_leadmagic_email.py <input.csv> <outdir> [--workers N]")
        sys.exit(1)
    in_path, outdir = sys.argv[1], Path(sys.argv[2])
    workers = 6
    if "--workers" in sys.argv:
        workers = int(sys.argv[sys.argv.index("--workers") + 1])
    outdir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(in_path, dtype=str, keep_default_na=False)
    has_mobile = df["Mobile Phone Number"].str.strip() != ""
    has_email = df["Email"].str.strip() != ""
    target = df[has_mobile & ~has_email].copy()
    print(f"{len(target)} contacts to retry (mobile present, email blank)", flush=True)

    rows = target.to_dict("records")
    results = []
    done = 0
    status_counts = Counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(retry_one, r): r for r in rows}
        for fut in as_completed(futures):
            row, res = fut.result()
            row["Retry Email"] = res.get("email", "")
            row["Retry Email Verified"] = res.get("verified", False)
            row["Retry Email Raw Status"] = res.get("raw_status", "")
            results.append(row)
            done += 1
            status_counts["verified" if res.get("verified") else ("found_unverified" if res.get("email") else "none")] += 1
            if done % 50 == 0 or done == len(rows):
                print(f"{done}/{len(rows)} retried, {dict(status_counts)}", flush=True)

    out_df = pd.DataFrame(results)
    out_df.to_csv(outdir / "BATCH1_leadmagic_retry.csv", index=False, quoting=csv.QUOTE_MINIMAL)

    verified = sum(1 for r in results if r["Retry Email Verified"])
    unverified = sum(1 for r in results if r["Retry Email"] and not r["Retry Email Verified"])
    none_found = len(results) - verified - unverified
    print(f"\nDONE: {verified} verified / {unverified} unverified-only / {none_found} none found (of {len(results)})")


if __name__ == "__main__":
    main()
