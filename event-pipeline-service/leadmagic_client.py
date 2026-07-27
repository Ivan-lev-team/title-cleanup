"""
LeadMagic enrichment client -- tier-1 of the email waterfall and tier-3 of
the mobile waterfall (see enrichment.py). Mirrors prospeo_client's shape:
each finder returns a normalized dict and skips (returns the empty dict) when
LEADMAGIC_KEY is blank, so LeadMagic drops out of the waterfall just by
leaving the key unset.

Auth: X-API-Key header (key format lm_live_...). Base https://api.leadmagic.io/v1.
Billing is pay-per-hit -- a miss (no email found) is free, which is why trying
LeadMagic before Prospeo in the email waterfall costs nothing on a miss.
"""
import time
import requests

import config

BASE = "https://api.leadmagic.io/v1"

# LeadMagic email statuses safe to auto-use. Strict policy: only "valid".
# The accept-all grey zone ("valid_catch_all"/"catch_all"), "unknown", and
# "invalid" are never auto-pushed -- they're surfaced in Notes for review.
_VERIFIED_EMAIL_STATUSES = {"valid"}


def _post(path, payload):
    """POST with the same retry posture as hubspot_client.request_with_retry:
    honor Retry-After on 429, exponential backoff on 5xx, up to 6 attempts.
    Returns the Response, or None if every attempt failed to connect."""
    url = f"{BASE}{path}"
    headers = {"X-API-Key": config.LEADMAGIC_KEY, "Content-Type": "application/json"}
    resp = None
    for attempt in range(6):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", "2")))
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        return resp
    return resp


def find_email(first_name, last_name, company_name, domain):
    """Returns {"email","verified","raw_status","linkedin_url"}; the empty dict
    when no key, insufficient inputs, or nothing found. `verified` is True only
    for a strictly-valid email (see _VERIFIED_EMAIL_STATUSES)."""
    empty = {"email": "", "verified": False, "raw_status": "", "linkedin_url": ""}
    if not config.LEADMAGIC_KEY:
        return empty
    if not (first_name or last_name) or not (domain or company_name):
        return empty

    payload = {}
    if first_name:
        payload["first_name"] = first_name
    if last_name:
        payload["last_name"] = last_name
    if domain:
        payload["domain"] = domain
    elif company_name:
        payload["company_name"] = company_name

    resp = _post("/people/email-finder", payload)
    if resp is None or resp.status_code >= 300:
        return empty
    data = resp.json() if resp.content else {}
    email = (data.get("email") or "").strip()
    status = (data.get("status") or "").strip().lower()
    return {
        "email": email,
        "verified": bool(email) and status in _VERIFIED_EMAIL_STATUSES,
        "raw_status": status,
        "linkedin_url": "",
    }


def find_mobile(profile_url, work_email=""):
    """Returns {"mobile","verified","raw_status"}; the empty dict when no key or
    no LinkedIn profile_url (LeadMagic's mobile-finder keys off the profile)."""
    empty = {"mobile": "", "verified": False, "raw_status": ""}
    if not config.LEADMAGIC_KEY or not profile_url:
        return empty
    payload = {"profile_url": profile_url}
    if work_email:
        payload["work_email"] = work_email
    resp = _post("/people/mobile-finder", payload)
    if resp is None or resp.status_code >= 300:
        return empty
    data = resp.json() if resp.content else {}
    mobile = (data.get("mobile_number") or "").strip()
    return {"mobile": mobile, "verified": bool(mobile), "raw_status": (data.get("status") or "").strip().lower()}


import re as _re

_BAND_RE = _re.compile(r"\$?([\d.]+)\s*([KMB])", _re.IGNORECASE)


def company_revenue(domain):
    """Revenue tier: returns estimated annual revenue in USD (float) for a
    domain via /companies/company-search, or None. LeadMagic returns a
    HEADCOUNT-DERIVED band in `revenue_formatted` (e.g. "$10M to <$50M"); the
    numeric `revenue` is a band-encoding sentinel, not a real figure. We parse
    the band's LOWER bound and only return it when it's >= $1M -- a "<$1M" or
    absent band is treated as no-data (return None -> fall through to the next
    provider), because LeadMagic systematically understates lean DTC brands."""
    if not config.LEADMAGIC_KEY or not domain:
        return None
    resp = _post("/companies/company-search", {"company_domain": domain})
    if resp is None or resp.status_code >= 300:
        return None
    fmt = ((resp.json() if resp.content else {}).get("revenue_formatted") or "").strip()
    if not fmt or fmt.startswith("<"):  # "<$1M" == LeadMagic's low/unknown -> miss
        return None
    m = _BAND_RE.search(fmt)  # lower bound of the band, e.g. "$10M to <$50M" -> 10M
    if not m:
        return None
    dollars = float(m.group(1)) * {"K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()]
    return dollars if dollars >= 1_000_000 else None
