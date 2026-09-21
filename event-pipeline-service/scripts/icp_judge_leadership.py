"""
Runs qualify.company_icp_judge over the unique companies that survived the
free keyword blocklist screen (screen_leadership_export.py), threaded.

Usage: python icp_judge_leadership.py <companies_kept.csv> <outdir> [--workers N]
"""
import csv
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qualify import company_icp_judge  # noqa: E402


def judge_one(row):
    verdict, reason = company_icp_judge(row["company_name"], row.get("domain", ""))
    return row["company_name"], row.get("domain", ""), row["contact_count"], verdict, reason


def main():
    if len(sys.argv) < 3:
        print("Usage: python icp_judge_leadership.py <companies_kept.csv> <outdir> [--workers N]")
        sys.exit(1)
    in_path, outdir = sys.argv[1], Path(sys.argv[2])
    workers = 8
    if "--workers" in sys.argv:
        workers = int(sys.argv[sys.argv.index("--workers") + 1])
    outdir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(in_path, dtype=str, keep_default_na=False)
    rows = df.to_dict("records")
    for r in rows:
        r["contact_count"] = int(r["contact_count"])

    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(judge_one, r): r for r in rows}
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            if done % 50 == 0 or done == len(rows):
                print(f"{done}/{len(rows)} judged", flush=True)

    out_df = pd.DataFrame(results, columns=["company_name", "domain", "contact_count", "verdict", "reason"])
    out_df = out_df.sort_values("contact_count", ascending=False)
    out_df.to_csv(outdir / "companies_icp_judged.csv", index=False, quoting=csv.QUOTE_MINIMAL)

    counts = Counter(out_df["verdict"])
    total_contacts_by_verdict = out_df.groupby("verdict")["contact_count"].sum()
    print("\nVerdict counts (companies):", dict(counts))
    print("Verdict counts (contacts):", total_contacts_by_verdict.to_dict())


if __name__ == "__main__":
    main()
