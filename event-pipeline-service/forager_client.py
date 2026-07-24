"""
Forager enrichment client -- used ONLY as tier-2 of the mobile waterfall (see
enrichment.py). Forager is an identity-graph lookup keyed off a LinkedIn public
identifier, not a name+domain guesser, so it is deliberately NOT part of the
email waterfall. It skips (returns the empty dict) unless FORAGER_KEY,
FORAGER_ACCOUNT_ID, and a resolvable LinkedIn handle are all present.

Auth: X-API-KEY header. Base https://api-v2.forager.ai, and the account id is
part of every path: /api/{account_id}/datastorage/...
"""
import re
import time
import requests

import config

BASE = "https://api-v2.forager.ai"

_HANDLE_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)


def linkedin_handle_from_url(url):
    """Extract the public identifier from a LinkedIn profile URL
    (linkedin.com/in/<handle>). Returns "" if it isn't a recognizable /in/ URL."""
    if not url:
        return ""
    m = _HANDLE_RE.search(url)
    return m.group(1).strip().strip("/") if m else ""


def _post(path, payload):
    url = f"{BASE}{path}"
    headers = {"X-API-KEY": config.FORAGER_KEY, "Content-Type": "application/json"}
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


def find_mobile(linkedin_url):
    """Returns {"mobile","verified","raw_status"}; the empty dict unless key,
    account id, and a LinkedIn handle are all available. Defensive about the
    response shape (list of records, or an object wrapping them) since Forager's
    phone response is documented only loosely."""
    empty = {"mobile": "", "verified": False, "raw_status": ""}
    if not config.FORAGER_KEY or not config.FORAGER_ACCOUNT_ID:
        return empty
    handle = linkedin_handle_from_url(linkedin_url)
    if not handle:
        return empty

    path = f"/api/{config.FORAGER_ACCOUNT_ID}/datastorage/person_contacts_lookup/phone_numbers/"
    resp = _post(path, {"linkedin_public_identifier": handle})
    if resp is None or resp.status_code >= 300:
        return empty
    data = resp.json() if resp.content else {}

    records = data if isinstance(data, list) else (data.get("results") or data.get("phone_numbers") or [])
    for rec in records:
        if isinstance(rec, dict):
            num = (rec.get("phone_number") or rec.get("number") or rec.get("phone") or "").strip()
            status = (rec.get("validation_status") or "").strip().lower()
        else:
            num, status = str(rec).strip(), ""
        if num:
            return {"mobile": num, "verified": True, "raw_status": status}
    return empty
