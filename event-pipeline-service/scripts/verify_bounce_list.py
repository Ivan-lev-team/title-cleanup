"""One-off: re-verify the Nooks "likely to bounce" recipients against
LeadMagic /email-validate and write a CSV.

Prospeo's standalone /email-verifier is removed (error_code DEPRECATED), so
LeadMagic is the only true address-verifier of the two. Usage:
    python scripts/verify_bounce_list.py <emails.txt> <out.csv>
"""
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import config

IN_PATH = sys.argv[1]
OUT_PATH = sys.argv[2]

FIELDS = [
    "email", "lm_status", "lm_message", "mx_provider", "mx_record",
    "mx_security_gateway", "company_name", "credits", "http",
]


def validate(email):
    """POST /email-validate with the retry posture used by leadmagic_client."""
    for attempt in range(5):
        try:
            r = requests.post(
                "https://api.leadmagic.io/v1/email-validate",
                headers={"X-API-Key": config.LEADMAGIC_KEY,
                         "Content-Type": "application/json"},
                json={"email": email},
                timeout=45,
            )
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "3")))
            continue
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        return r
    return None


emails = []
with open(IN_PATH, encoding="utf-8") as fh:
    for line in fh:
        e = line.strip()
        if e and e not in emails:
            emails.append(e)

print("verifying %d addresses" % len(emails))
rows = []
spent = 0.0

for i, email in enumerate(emails, 1):
    r = validate(email)
    if r is None:
        rows.append({"email": email, "lm_status": "REQUEST_FAILED", "http": ""})
        print("%3d/%d  %-45s REQUEST_FAILED" % (i, len(emails), email))
        continue
    d = r.json() if r.content else {}
    cred = d.get("credits_consumed") or 0
    try:
        spent += float(cred)
    except (TypeError, ValueError):
        pass
    row = {
        "email": email,
        "lm_status": (d.get("email_status") or "").strip() or ("HTTP_%s" % r.status_code),
        "lm_message": (d.get("message") or "").strip(),
        "mx_provider": (d.get("mx_provider") or "").strip(),
        "mx_record": (d.get("mx_record") or "").strip(),
        "mx_security_gateway": d.get("mx_security_gateway"),
        "company_name": (d.get("company_name") or "").strip(),
        "credits": cred,
        "http": r.status_code,
    }
    rows.append(row)
    print("%3d/%d  %-45s %s" % (i, len(emails), email, row["lm_status"]))

with open(OUT_PATH, "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=FIELDS)
    w.writeheader()
    for row in rows:
        w.writerow({k: row.get(k, "") for k in FIELDS})

counts = {}
for row in rows:
    counts[row.get("lm_status", "")] = counts.get(row.get("lm_status", ""), 0) + 1
print("\n=== STATUS COUNTS ===")
for k in sorted(counts, key=lambda x: -counts[x]):
    print("%-20s %d" % (k, counts[k]))
print("credits consumed: %.2f" % spent)
print("wrote", OUT_PATH)
