#!/usr/bin/env python3
"""
Merge title verdicts + company verdicts + HubSpot dedup into one final CSV.

Usage:
    python3 merge_final.py \
        --main <original contacts csv> \
        --titles <titles_pass1.csv, output of classify_titles.py> \
        --company-verdicts <json file: [{"domain":..., "verdict": "PASS"/"EXCLUDE", "reason":...}, ...]> \
        --hubspot <hubspot export csv> \
        --out <final combined csv>
"""

import argparse
import csv
import json
import re


def norm_linkedin(url):
    if not url:
        return None
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.rstrip("/")
    # strip query/fragment junk some exports append (e.g. "#:~:text=...")
    u = u.split("#")[0].split("?")[0]
    return u or None


def norm_domain(domain):
    if not domain:
        return None
    d = domain.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.rstrip("/")
    return d or None


def norm_name(first, last):
    n = f"{(first or '').strip()} {(last or '').strip()}".strip().lower()
    n = re.sub(r"[^a-z0-9 ]", "", n)
    n = re.sub(r"\s+", " ", n)
    return n or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--main", required=True)
    ap.add_argument("--titles", required=True)
    ap.add_argument("--company-verdicts", required=True)
    ap.add_argument("--hubspot", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    # --- Load title verdicts (aligned by row order with --main) ---
    with open(args.titles, newline="", encoding="utf-8") as f:
        title_rows = list(csv.DictReader(f))

    # --- Load company verdicts, keyed by normalized domain ---
    with open(args.company_verdicts, encoding="utf-8") as f:
        company_list = json.load(f)
    company_by_domain = {}
    for c in company_list:
        d = norm_domain(c.get("domain", ""))
        if d:
            company_by_domain[d] = (c.get("verdict", "EXCLUDE"), c.get("reason", ""))

    # --- Load HubSpot worked-contacts export, build exclusion sets ---
    with open(args.hubspot, newline="", encoding="utf-8") as f:
        hs_rows = list(csv.DictReader(f))

    hs_linkedins = set()
    hs_name_domain = set()
    for row in hs_rows:
        li = norm_linkedin(row.get("Linkedin Personal Url", ""))
        if li:
            hs_linkedins.add(li)
        nm = norm_name(row.get("Firstname", ""), row.get("Lastname", ""))
        dom = norm_domain(row.get("Domain", "") or row.get("Website", ""))
        if nm and dom:
            hs_name_domain.add((nm, dom))

    # --- Load main file, merge everything ---
    with open(args.main, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        main_fieldnames = reader.fieldnames
        main_rows = list(reader)

    assert len(main_rows) == len(title_rows), (
        f"Row count mismatch: main={len(main_rows)} titles={len(title_rows)}"
    )

    seen_dedup_keys = set()
    out_fieldnames = list(main_fieldnames) + [
        "Title Verdict",
        "Title Reason",
        "Company Verdict",
        "Company Reason",
        "HubSpot Match",
        "Internal Duplicate",
        "Final Verdict",
    ]

    with open(args.out, "w", newline="", encoding="utf-8") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=out_fieldnames)
        writer.writeheader()

        for main_row, title_row in zip(main_rows, title_rows):
            title_verdict = title_row.get("verdict", "FAIL")
            title_reason = title_row.get("reason", "")

            domain = norm_domain(main_row.get("Company Domain", ""))
            if domain and domain in company_by_domain:
                company_verdict, company_reason = company_by_domain[domain]
            else:
                company_verdict, company_reason = "EXCLUDE", "No company domain / not classified"

            li = norm_linkedin(main_row.get("LinkedIn Profile", ""))
            nm = norm_name(main_row.get("First Name", ""), main_row.get("Last Name", ""))

            hubspot_match = False
            if li and li in hs_linkedins:
                hubspot_match = True
            elif nm and domain and (nm, domain) in hs_name_domain:
                hubspot_match = True

            # de-dupe within this file itself
            dedup_key = li or (nm, domain)
            is_dup = dedup_key in seen_dedup_keys if dedup_key else False
            if dedup_key:
                seen_dedup_keys.add(dedup_key)

            if hubspot_match:
                final_verdict = "EXCLUDE"
            elif is_dup:
                final_verdict = "EXCLUDE"
            elif title_verdict == "PASS" and company_verdict == "PASS":
                final_verdict = "PASS"
            else:
                final_verdict = "EXCLUDE"

            out_row = dict(main_row)
            out_row["Title Verdict"] = title_verdict
            out_row["Title Reason"] = title_reason
            out_row["Company Verdict"] = company_verdict
            out_row["Company Reason"] = company_reason
            out_row["HubSpot Match"] = "YES" if hubspot_match else ""
            out_row["Internal Duplicate"] = "YES" if is_dup else ""
            out_row["Final Verdict"] = final_verdict
            writer.writerow(out_row)

    print(f"Wrote {len(main_rows)} rows -> {args.out}")


if __name__ == "__main__":
    main()
