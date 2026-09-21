"""
Prospeo DISCOVERY layer -- find people at a domain from nothing but the domain.

Kept separate from prospeo_client.py on purpose: that module ENRICHES a person
we already know (it needs a name, email or LinkedIn URL). This one is a second
discovery layer behind Seamless, which is a different job.

Economics, measured 2026-09-18:
  /search-person   FREE ("free": true on the response). Returns email and
                   mobile MASKED ("revealed": false) but WITH a verification
                   status. So we can see a VERIFIED email exists before paying
                   anything -- strictly better than Seamless, where a credit is
                   spent before address quality is known.
  /enrich-person   1 credit for person+company+email, 10 with enrich_mobile.
                   No charge on no-match, none to re-enrich within 90 days.

Three API quirks that will bite anyone reusing this:
  * NO_RESULTS comes back as HTTP 400, not an empty 200. Treated here as zero
    results, never as an error.
  * company.websites.include takes ROOT domains only -- "shop.baronart.tattoo"
    is rejected with INVALID_FILTERS -- so the domain is normalized first.
  * The single /enrich-person wants `data` as an OBJECT; bulk-enrich-person
    wants a LIST. Getting it wrong returns INVALID_REQUEST with no hint.
"""
import re
import threading
import time
import requests

import config
import ratelimit

# Prospeo publishes its limits on every response:
#   x-second-rate-limit 5 | x-minute-rate-limit 180 | x-daily-rate-limit 250000
# So ~3 req/sec sustained. Measured 2026-09-21: six parallel workers pushed
# ~19/sec and 20 of 30 calls returned 429, after which the client's exponential
# backoff (1,2,4,8,16s) turned each one into a long sleep -- parallelism made
# the run SLOWER than serial. This token bucket paces every call instead, so a
# thread pool can be used without ever tripping the limit.
_SEC_LIMIT, _MIN_LIMIT = 5, 180
_lock = threading.Lock()
_hits = []


def _throttle():
    """Block until a call is allowed under both the per-second and per-minute
    limits. Cheap, exact, and shared across threads."""
    while True:
        with _lock:
            now = time.time()
            _hits[:] = [t for t in _hits if now - t < 60.0]
            in_sec = sum(1 for t in _hits if now - t < 1.0)
            if in_sec < _SEC_LIMIT and len(_hits) < _MIN_LIMIT:
                _hits.append(now)
                return
            if len(_hits) >= _MIN_LIMIT:
                wait = 60.0 - (now - _hits[0]) + 0.05
            else:
                oldest_in_sec = min(t for t in _hits if now - t < 1.0)
                wait = 1.0 - (now - oldest_in_sec) + 0.02
        time.sleep(max(wait, 0.01))

SEARCH_URL = "https://api.prospeo.io/search-person"
ENRICH_URL = "https://api.prospeo.io/enrich-person"
ACCOUNT_URL = "https://api.prospeo.io/account-information"

# Prospeo's seniority vocabulary, which differs from Seamless's.
PROSPEO_SENIORITY = ["C-Suite", "Founder/Owner", "Partner", "Vice President",
                     "Director", "Head", "Manager"]

_MULTI_TLD = {"co.uk", "com.au", "co.nz", "com.br", "co.za", "com.mx",
              "co.jp", "com.tr", "co.in", "com.sg", "co.il", "com.hk"}


def root_domain(d):
    """Reduce a host to its registrable root. Prospeo rejects subdomains."""
    d = (d or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split("?")[0]
    d = re.sub(r"^www\.", "", d)
    parts = [x for x in d.split(".") if x]
    if len(parts) <= 2:
        return ".".join(parts)
    if ".".join(parts[-2:]) in _MULTI_TLD and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _headers():
    return {"X-KEY": config.PROSPEO_KEY, "Content-Type": "application/json"}


def _post(url, body, tries=5):
    resp = None
    for a in range(tries):
        ratelimit.PROSPEO.acquire()
        try:
            resp = requests.post(url, headers=_headers(), json=body, timeout=60)
        except requests.RequestException:
            time.sleep(2 ** a)
            continue
        if resp.status_code == 429:
            time.sleep(ratelimit.retry_after_seconds(resp) + 1)
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** a)
            continue
        return resp
    return resp


def credits_remaining():
    r = _post(ACCOUNT_URL, {})
    if r is None or r.status_code != 200:
        return None
    try:
        return (r.json().get("response") or {}).get("remaining_credits")
    except ValueError:
        return None


def search_person(domain, job_titles=None, seniority=None, page=1, match_mode="CONTAINS"):
    """FREE. Find people at a company domain.

    Returns {"people": [...], "total": int, "pages": int, "error": str}. Each
    person carries the masked email/mobile plus its verification status, so the
    caller can pre-screen on status == "VERIFIED" before paying to reveal.

    Prospeo having nobody at the domain is zero results, not an error.
    """
    empty = {"people": [], "total": 0, "pages": 0, "error": ""}
    if not config.PROSPEO_KEY:
        out = dict(empty); out["error"] = "PROSPEO_KEY not set"; return out
    dom = root_domain(domain)
    if not dom or "." not in dom:
        out = dict(empty); out["error"] = "unusable domain %r" % domain; return out

    filters = {"company": {"websites": {"include": [dom]}}}
    if job_titles:
        filters["person_job_title"] = {"include": list(job_titles),
                                       "match_mode": match_mode}
    if seniority:
        filters["person_seniority"] = {"include": list(seniority)}

    r = _post(SEARCH_URL, {"filters": filters, "page": int(page)})
    if r is None:
        out = dict(empty); out["error"] = "connection failed"; return out
    try:
        j = r.json() if r.content else {}
    except ValueError:
        out = dict(empty); out["error"] = "non-json"; return out

    code = str(j.get("error_code") or "")
    if code == "NO_RESULTS":
        return dict(empty)
    if r.status_code != 200 or j.get("error"):
        out = dict(empty)
        out["error"] = code or ("http %s" % r.status_code)
        return out

    pag = j.get("pagination") or {}
    people = []
    for row in j.get("results") or []:
        per = row.get("person") or {}
        comp = row.get("company") or {}
        em = per.get("email") if isinstance(per.get("email"), dict) else {}
        mo = per.get("mobile") if isinstance(per.get("mobile"), dict) else {}
        people.append({
            "person_id": per.get("person_id") or "",
            "first_name": (per.get("first_name") or "").strip(),
            "last_name": (per.get("last_name") or "").strip(),
            "full_name": (per.get("full_name") or "").strip(),
            "job_title": (per.get("current_job_title") or "").strip(),
            "linkedin_url": (per.get("linkedin_url") or "").strip(),
            "email_status": (em.get("status") or "").strip().upper(),
            "email_masked": em.get("email") or "",
            "email_verification_method": em.get("verification_method") or "",
            "mobile_status": (mo.get("status") or "").strip().upper(),
            "mobile_masked": mo.get("mobile") or "",
            "company_name": (comp.get("name") or "").strip(),
            "company_domain": (comp.get("domain") or comp.get("website") or "").strip(),
            "employee_count": comp.get("employee_count"),
            "revenue_range": comp.get("revenue_range_printed")
                             or comp.get("revenue_range") or "",
        })
    return {"people": people,
            "total": pag.get("total_count") or len(people),
            "pages": pag.get("total_page") or 1, "error": ""}


def reveal_person(person_id, enrich_mobile=False):
    """PAID. Reveal the real email (1 credit), optionally the mobile (10)."""
    empty = {"email": "", "email_status": "", "mobile": "", "mobile_status": "",
             "free_enrichment": None, "error": ""}
    if not config.PROSPEO_KEY or not person_id:
        return dict(empty)
    body = {"enrich_mobile": bool(enrich_mobile), "only_verified_email": False,
            "data": {"person_id": person_id}}
    r = _post(ENRICH_URL, body)
    if r is None:
        out = dict(empty); out["error"] = "connection failed"; return out
    try:
        j = r.json() if r.content else {}
    except ValueError:
        out = dict(empty); out["error"] = "non-json"; return out
    if r.status_code != 200 or j.get("error"):
        out = dict(empty)
        out["error"] = str(j.get("error_code") or ("http %s" % r.status_code))
        return out
    root = j.get("response") if isinstance(j.get("response"), dict) else j
    per = (root or {}).get("person") or {}
    em = per.get("email") if isinstance(per.get("email"), dict) else {}
    mo = per.get("mobile") if isinstance(per.get("mobile"), dict) else {}
    return {"email": (em.get("email") or "").strip(),
            "email_status": (em.get("status") or "").strip().upper(),
            "mobile": (mo.get("mobile") or "").strip(),
            "mobile_status": (mo.get("status") or "").strip().upper(),
            "free_enrichment": j.get("free_enrichment"), "error": ""}
