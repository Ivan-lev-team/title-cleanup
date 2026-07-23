"""
Step 3 of the pipeline: enrichment, only called for rows that already
survived qualify (step 1) and dedupe (step 2) -- never spend credits on a
row that would be filtered out anyway.

Only fills in a missing EMAIL right now (that's the concrete gap we hit on
the Social Commerce Summit list -- 91 name-only rows with no email and no
LinkedIn). If Prospeo's hit-rate turns out too low for a given list, this is
the seam where a second provider could be added later.
"""
import requests

import config

PROSPEO_URL = "https://api.prospeo.io/email-finder"


def find_email(first_name, last_name, domain):
    """Returns (email, confidence) or (None, None) if not found or no key set."""
    if not config.PROSPEO_API_KEY or not domain:
        return None, None

    resp = requests.post(
        PROSPEO_URL,
        headers={"X-KEY": config.PROSPEO_API_KEY, "Content-Type": "application/json"},
        json={"first_name": first_name, "last_name": last_name, "company": domain},
        timeout=30,
    )
    if resp.status_code != 200:
        return None, None

    data = resp.json()
    email_info = data.get("response", {}).get("email")
    if not email_info:
        return None, None

    email = email_info.get("email")
    verified = email_info.get("verification", {}).get("status") == "VALID"
    return (email, "verified" if verified else "unverified") if email else (None, None)
