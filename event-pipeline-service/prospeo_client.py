"""
Step 3 of the pipeline: enrichment, only called for rows that already
survived qualify (step 1) and dedupe (step 2) -- never spend credits on a
row that would be filtered out anyway.

Schema matches the validated /hubspot-push/enrich_prospeo.py exactly
(endpoint, payload shape, verified-vs-unverified handling): bulk-enrich-person,
identifier keyed to a caller-supplied string, only_verified_email=False so we
can see unverified hits too but gate on status before ever using one, and
enrich_mobile off since mobiles cost 10 credits vs. 1 for email. The service
calls this one row at a time (poll cadence, not CSV batch), so it just sends
a single-item "batch" -- same endpoint and schema, batch size of 1.
"""
import requests

import config

PROSPEO_URL = "https://api.prospeo.io/bulk-enrich-person"


def _build_item(full_name, linkedin_url, email, company_name, domain):
    item = {"identifier": "0"}
    if full_name:
        item["full_name"] = full_name
    if linkedin_url:
        item["linkedin_url"] = linkedin_url
    if email:
        item["email"] = email
    if company_name:
        item["company_name"] = company_name
    if domain:
        item["company_website"] = domain
    return item


def _has_min_match(item):
    """Prospeo needs linkedin_url alone, email alone, or name + a company identifier."""
    if item.get("linkedin_url") or item.get("email"):
        return True
    if item.get("full_name") and (item.get("company_name") or item.get("company_website")):
        return True
    return False


def enrich_person(full_name, company_name, domain, linkedin_url="", email=""):
    """Returns a dict: {"email": str, "status": str, "linkedin_url": str}
    (all fields "" if nothing found or no key configured). Only email with
    status == "VERIFIED" should ever be auto-pushed to HubSpot; anything
    else belongs in a review-only column."""
    empty = {"email": "", "status": "", "linkedin_url": ""}
    if not config.PROSPEO_KEY:
        return empty

    item = _build_item(full_name, linkedin_url, email, company_name, domain)
    if not _has_min_match(item):
        return empty

    resp = requests.post(
        PROSPEO_URL,
        headers={"X-KEY": config.PROSPEO_KEY, "Content-Type": "application/json"},
        json={"only_verified_email": False, "enrich_mobile": False, "data": [item]},
        timeout=90,
    )
    if resp.status_code != 200:
        return empty

    matched = resp.json().get("matched", [])
    if not matched:
        return empty

    person = matched[0].get("person") or {}
    email_obj = person.get("email") or {}
    found_email = (email_obj.get("email") or "").strip()
    result = {
        "email": found_email if email_obj.get("revealed") else "",
        "status": email_obj.get("status", "") if email_obj.get("revealed") else "",
        "linkedin_url": (person.get("linkedin_url") or "").strip(),
    }
    return result


def find_mobile(full_name, company_name, domain, linkedin_url="", email=""):
    """Tier-1 of the mobile waterfall (see enrichment.py). Returns
    {"mobile": str, "verified": bool}; the empty dict when no key or no minimum
    match. Uses the same bulk-enrich-person call as the manual enrich_mobiles.py
    script -- enrich_mobile=True (mobiles cost ~10 credits per hit, vs. 1 for an
    email, which is why this only ever runs on rows with no phone yet), and the
    number is accepted only when Prospeo marks it `revealed`."""
    empty = {"mobile": "", "verified": False}
    if not config.PROSPEO_KEY:
        return empty

    item = _build_item(full_name, linkedin_url, email, company_name, domain)
    if not _has_min_match(item):
        return empty

    resp = requests.post(
        PROSPEO_URL,
        headers={"X-KEY": config.PROSPEO_KEY, "Content-Type": "application/json"},
        json={"only_verified_email": False, "enrich_mobile": True, "only_verified_mobile": False, "data": [item]},
        timeout=120,
    )
    if resp.status_code != 200:
        return empty

    matched = resp.json().get("matched", [])
    if not matched:
        return empty

    mobile_obj = (matched[0].get("person") or {}).get("mobile") or {}
    num = (mobile_obj.get("mobile_international") or mobile_obj.get("mobile") or "").strip()
    if num and mobile_obj.get("revealed"):
        return {"mobile": num, "verified": True}
    return empty


def enrich_company_revenue(domain):
    """Revenue tier: returns estimated annual revenue in USD (float) for a
    domain via /enrich-company, or None. Uses revenue_range.min (conservative;
    the low end of Prospeo's numeric band). Tested accurate on DTC brands where
    LeadMagic understates, so it's the reliable fallback in the waterfall."""
    if not config.PROSPEO_KEY or not domain:
        return None
    try:
        resp = requests.post(
            "https://api.prospeo.io/enrich-company",
            headers={"X-KEY": config.PROSPEO_KEY, "Content-Type": "application/json"},
            json={"data": {"company_website": domain}}, timeout=60,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    company = (resp.json() or {}).get("company") or {}
    lo = (company.get("revenue_range") or {}).get("min")
    return float(lo) if lo else None


def resolve_person(email="", linkedin_url=""):
    """Identity resolution via /enrich-person (single). Email standalone OR a
    LinkedIn URL standalone are both valid inputs. Returns
    {linkedin, company_name, domain, title}; empty dict on miss/no key/no input.
    (Use the single endpoint, not bulk-enrich-person, whose payload needs a
    data[] array with per-record identifiers.)"""
    empty = {"linkedin": "", "company_name": "", "domain": "", "title": ""}
    if not config.PROSPEO_KEY or not (email or linkedin_url):
        return empty
    body = {}
    if email:
        body["email"] = email
    if linkedin_url:
        body["linkedin_url"] = linkedin_url
    try:
        resp = requests.post(
            "https://api.prospeo.io/enrich-person",
            headers={"X-KEY": config.PROSPEO_KEY, "Content-Type": "application/json"},
            json=body, timeout=60,
        )
    except requests.RequestException:
        return empty
    if resp.status_code != 200:
        return empty
    d = resp.json() or {}
    if d.get("error"):
        return empty
    # single endpoint returns person/company at top level (defensive: also check
    # a "response" wrapper and a bulk-style matched[] just in case)
    root = d.get("response") if isinstance(d.get("response"), dict) else d
    if not root.get("person") and root.get("matched"):
        root = root["matched"][0] if root["matched"] else {}
    person = root.get("person") or {}
    company = root.get("company") or {}
    return {
        "linkedin": (person.get("linkedin_url") or "").strip(),
        "company_name": (company.get("name") or "").strip(),
        "domain": (company.get("domain") or company.get("website") or "").strip(),
        "title": (person.get("current_job_title") or "").strip(),
    }
