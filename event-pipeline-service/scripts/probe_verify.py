"""One-off probe: confirm LeadMagic /email-validate and Prospeo /email-verify
endpoint shapes before spending credits on a full batch. Prints raw JSON."""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import config

SAMPLES = ["slo@coyuchi.com", "kate.dotson@montrichardwatch.com"]

for email in SAMPLES:
    print("=" * 70)
    print("EMAIL:", email)

    print("-- LeadMagic /email-validate --")
    try:
        r = requests.post(
            "https://api.leadmagic.io/v1/email-validate",
            headers={"X-API-Key": config.LEADMAGIC_KEY, "Content-Type": "application/json"},
            json={"email": email},
            timeout=45,
        )
        print("HTTP", r.status_code)
        print(json.dumps(r.json() if r.content else {}, indent=2)[:1200])
    except Exception as e:
        print("ERROR:", type(e).__name__, e)

    print("-- Prospeo /email-verify --")
    try:
        r = requests.post(
            "https://api.prospeo.io/email-verify",
            headers={"X-KEY": config.PROSPEO_KEY, "Content-Type": "application/json"},
            json={"email": email},
            timeout=45,
        )
        print("HTTP", r.status_code)
        print(json.dumps(r.json() if r.content else {}, indent=2)[:1200])
    except Exception as e:
        print("ERROR:", type(e).__name__, e)
