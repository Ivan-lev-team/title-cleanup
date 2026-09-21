import csv
import sys
import time

sys.path.insert(0, ".")
import enrichment  # noqa: E402

SRC = r"C:\Users\Ivan\AppData\Local\Temp\claude\C--Users-Ivan\9edd606f-998c-4975-bd0b-e8cdc81bcf41\scratchpad\merged_contact_import.csv"
OUT = r"C:\Users\Ivan\AppData\Local\Temp\claude\C--Users-Ivan\9edd606f-998c-4975-bd0b-e8cdc81bcf41\scratchpad\no_email_enrich_results.csv"

TARGET_TAGS = {"", "Pattern Accelerate 26"}

with open(SRC, newline="", encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))

todo = [
    r for r in rows
    if r.get("Email", "").strip() == ""
    and r.get("Lead source drill down", "").strip() in TARGET_TAGS
]
out_fields = [
    "First Name", "Last Name", "Company Name", "Company Domain", "Lead source drill down",
    "resolved_email", "provider", "resolved_linkedin", "unverified_email", "unverified_note",
]

# Resume support: if OUT already has rows (from a prior run that got killed),
# skip that many from the front of todo instead of re-spending API credits.
import os
already_done = 0
file_exists = os.path.exists(OUT)
if file_exists:
    with open(OUT, newline="", encoding="utf-8") as existing:
        already_done = sum(1 for _ in existing) - 1  # minus header
    already_done = max(already_done, 0)

todo = todo[already_done:]
print(f"Total rows to attempt: {len(todo) + already_done} (resuming after {already_done} already done)", flush=True)

mode = "a" if file_exists and already_done > 0 else "w"
with open(OUT, mode, newline="", encoding="utf-8") as out_f:
    writer = csv.DictWriter(out_f, fieldnames=out_fields)
    if mode == "w":
        writer.writeheader()
    found = 0
    for i, r in enumerate(todo, 1):
        first = r.get("First Name", "").strip()
        last = r.get("Last Name", "").strip()
        full_name = f"{first} {last}".strip()
        company = r.get("Company Name", "").strip()
        domain = r.get("Company Domain", "").strip()
        linkedin = r.get("LinkedIn URL", "").strip()
        try:
            res = enrichment.find_email(first, last, full_name, company, domain, linkedin_url=linkedin)
        except Exception as e:
            res = {"email": "", "provider": "", "linkedin_url": linkedin,
                   "unverified_email": "", "unverified_note": f"ERROR: {e}"}
        if res.get("email"):
            found += 1
        writer.writerow({
            "First Name": first,
            "Last Name": last,
            "Company Name": company,
            "Company Domain": domain,
            "Lead source drill down": r.get("Lead source drill down", ""),
            "resolved_email": res.get("email", ""),
            "provider": res.get("provider", ""),
            "resolved_linkedin": res.get("linkedin_url", ""),
            "unverified_email": res.get("unverified_email", ""),
            "unverified_note": res.get("unverified_note", ""),
        })
        out_f.flush()
        if i % 25 == 0:
            print(f"{already_done + i}/{already_done + len(todo)} processed, "
                  f"{found} verified emails found this run so far", flush=True)

print(f"DONE. {already_done + len(todo)} total rows now in {OUT}; "
      f"{found} verified emails found this run.", flush=True)
