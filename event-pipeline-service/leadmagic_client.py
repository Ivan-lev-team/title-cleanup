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
import ratelimit

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
        ratelimit.LEADMAGIC.acquire()
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429:
            time.sleep(ratelimit.retry_after_seconds(resp) + 1)
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
    data = ratelimit.safe_json(resp)
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
    data = ratelimit.safe_json(resp)
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


def company_firmographics(domain):
    """Full firmographics from /companies/company-search (1 credit on hit, free
    on miss). Returns any of: revenue_usd (band lower bound, float), employee_count,
    industry, linkedin_url, founded_year, city, state, country. {} on miss/no key.
    Reuses the single company-search call, so this doubles as the revenue source."""
    if not config.LEADMAGIC_KEY or not domain:
        return {}
    resp = _post("/companies/company-search", {"company_domain": domain})
    if resp is None or resp.status_code >= 300:
        return {}
    d = resp.json() if resp.content else {}
    if not isinstance(d, dict) or d.get("message") == "Company not found":
        return {}
    out = {}
    fmt = (d.get("revenue_formatted") or "").strip()
    if fmt and not fmt.startswith("<"):
        m = _BAND_RE.search(fmt)  # band lower bound, e.g. "$10M to <$50M" -> 10M
        if m:
            out["revenue_usd"] = float(m.group(1)) * {"K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()]
    ec = d.get("employeeCount") or (d.get("employeeCountRange") or {}).get("start")
    if ec:
        out["employee_count"] = ec
    if (d.get("industry") or "").strip():
        out["industry"] = d["industry"].strip()
    if (d.get("linkedin_url") or "").strip():
        out["linkedin_url"] = d["linkedin_url"].strip()
    fy = d.get("founded_year") or (d.get("foundedOn") or {}).get("year")
    if fy:
        out["founded_year"] = str(fy)
    locs = d.get("locations") or []
    if locs and isinstance(locs[0], dict):
        l = locs[0]
        if l.get("city"):
            out["city"] = l["city"]
        if l.get("geographicArea"):
            out["state"] = l["geographicArea"]
        if l.get("country"):
            out["country"] = l["country"]
    return out


def email_to_profile(personal_email="", work_email=""):
    """Reverse email -> LinkedIn profile_url via /people/b2b-profile. Returns ""
    on miss / no key / no email. (10 credits on a hit, 0 on miss.)"""
    if not config.LEADMAGIC_KEY or not (personal_email or work_email):
        return ""
    body = {}
    if work_email:
        body["work_email"] = work_email
    if personal_email:
        body["personal_email"] = personal_email
    resp = _post("/people/b2b-profile", body)
    if resp is None or resp.status_code >= 300:
        return ""
    return ((resp.json() if resp.content else {}).get("profile_url") or "").strip()


def profile_to_company(profile_url):
    """LinkedIn profile_url -> {company_name, domain, title} via
    /people/profile-search. Empty dict on miss/no key. (1 credit, free on miss.)"""
    empty = {"company_name": "", "domain": "", "title": ""}
    if not config.LEADMAGIC_KEY or not profile_url:
        return empty
    resp = _post("/people/profile-search", {"profile_url": profile_url})
    if resp is None or resp.status_code >= 300:
        return empty
    d = resp.json() if resp.content else {}
    return {
        "company_name": (d.get("company_name") or "").strip(),
        "domain": (d.get("company_website") or "").strip(),
        "title": (d.get("professional_title") or "").strip(),
    }


def validate_email(email):
    """Validate an existing address via POST /v1/email-validate.

    This is a VERIFIER, not a finder -- it takes an address we already have and
    says whether it is deliverable. Added 2026-09-18 as the second pass behind
    DeBounce, for one specific reason:

      DeBounce returns "risky" (code 4) for EVERY address on an accept-all
      domain and cannot resolve further -- a real mailbox and a fabricated one
      come back identical. LeadMagic can discriminate there. Verified against
      a known-good address on a domain already proven catch-all:
          ivan@levanta.io           -> DeBounce risky, LeadMagic "valid"
          zzq-fake-8821@levanta.io  -> DeBounce risky, LeadMagic "invalid"
      On resolvable domains the two agree (marc@researchanddesign.com valid/
      valid, marc.conaway@ invalid/invalid).

    So the chain is DeBounce first (independent, not also a finder), then
    LeadMagic only on risky/unknown, where it adds information rather than a
    second opinion on something already settled.

    Costs ~0.25 credits per call. Returns the empty result when the key is
    blank, so the tier drops out rather than raising.
    """
    empty = {"status": "", "raw_status": "", "verified": False, "provider": "LeadMagic",
             "mx_provider": "", "credits": "", "error": ""}
    if not config.LEADMAGIC_KEY:
        out = dict(empty); out["error"] = "LEADMAGIC_KEY not set"; return out
    email = (email or "").strip()
    if not email:
        return dict(empty)

    resp = _post("/email-validate", {"email": email})
    out = dict(empty)
    if resp is None:
        out["error"] = "connection failed after retries"; return out
    if resp.status_code >= 300:
        out["error"] = "http %s: %s" % (resp.status_code, resp.text[:120]); return out
    try:
        data = ratelimit.safe_json(resp)
    except ValueError:
        out["error"] = "non-json: %s" % resp.text[:120]; return out

    raw = (data.get("email_status") or data.get("status") or "").strip().lower()
    # Map LeadMagic's vocabulary onto the same three buckets debounce_client uses.
    if raw in _VERIFIED_EMAIL_STATUSES:
        status = "valid"
    elif raw in ("valid_catch_all", "catch_all", "catch-all", "accept_all"):
        status = "risky"
    elif raw in ("unknown", ""):
        status = "unknown"
    else:
        status = "invalid"
    out.update({"status": status, "raw_status": raw,
                "verified": status == "valid",
                "mx_provider": str(data.get("mx_provider") or ""),
                "credits": str(data.get("credits_consumed") or "")})
    return out
