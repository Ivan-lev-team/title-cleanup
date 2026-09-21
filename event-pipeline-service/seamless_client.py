"""
Seamless.ai Public API client -- contact DISCOVERY plus email+mobile research.

Unlike the other providers in this repo, Seamless finds PEOPLE at a domain, not
just attributes for a person we already know. So the flow is two-phase and the
phases have very different economics:

    search_contacts()    FREE   -- the title filter. Runs first, always.
    research_contacts()  PAID   -- 1 credit per submitted contact, win or lose.

That asymmetry is the whole reason the title filter belongs at search time.
Filtering after research means paying for contacts we then throw away.

Auth: bare "Token: <key>" header. Base https://api.seamless.ai/api/client/v1.
Returns the empty result when SEAMLESS_API_KEY is blank, same as every other
client here, so Seamless drops out just by leaving the key unset.

Rate limit is 60 req/min PER ENDPOINT, ORG-WIDE -- not per key, not per user.
So a parallel runner can starve other users of the org. Keep concurrency low.

A 422 means insufficient credits or missing license and carries
`productCategory` / `additionalCreditsNeeded`. That is raised as
SeamlessCreditError rather than swallowed, because silently returning "no
contacts found" when the real cause is an empty credit pool would corrupt
every coverage number in the pilot audit.
"""
import time
import requests

import config
import ratelimit

BASE = "https://api.seamless.ai/api/client/v1"

# Terminal poll states. Anything not here means "still working".
DONE = "done"
TERMINAL = {
    "done", "error", "missing", "duplicate", "not found",
    "contact-already-researched", "No license or credits available",
}

# Only a personal mobile counts. A "main" number is a switchboard and must
# never be handed to an SDR as if it were a direct line.
MOBILE_DATATYPE = "mobile"


class SeamlessCreditError(RuntimeError):
    """422 -- out of credits or missing license. Carries the API's own detail."""

    def __init__(self, message, product_category="", additional_credits_needed=""):
        super().__init__(message)
        self.product_category = product_category
        self.additional_credits_needed = additional_credits_needed


def _headers():
    return {"Token": config.SEAMLESS_API_KEY, "Content-Type": "application/json"}


def _request(method, path, tries=6, **kwargs):
    """Retry posture matching the other clients: Retry-After on 429, backoff on
    5xx and connection errors. Raises SeamlessCreditError on 422 immediately --
    retrying an out-of-credits call just burns wall time."""
    url = f"{BASE}{path}"
    resp = None
    for attempt in range(tries):
        ratelimit.SEAMLESS.acquire()
        try:
            resp = requests.request(method, url, headers=_headers(), timeout=60, **kwargs)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 422:
            detail = {}
            try:
                detail = resp.json() if resp.content else {}
            except ValueError:
                pass
            raise SeamlessCreditError(
                f"422 insufficient credits or license: {resp.text[:200]}",
                product_category=str(detail.get("productCategory") or ""),
                additional_credits_needed=str(detail.get("additionalCreditsNeeded") or ""),
            )
        if resp.status_code == 429:
            time.sleep(ratelimit.retry_after_seconds(resp) + 1)
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        return resp
    return resp


def credit_headers(resp):
    """Pull the credit/rate-limit telemetry Seamless puts on every response.
    Call this before and after a run to see which credit pool actually moved --
    the split between the 'universal' and 'intent' pools is unconfirmed."""
    if resp is None:
        return {}
    h = resp.headers
    return {
        "rate_limit": h.get("X-RateLimit-Limit", ""),
        "rate_remaining": h.get("X-RateLimit-Remaining", ""),
        "rate_reset": h.get("X-RateLimit-Reset", ""),
        "credits": h.get("X-PublicAPI-Credits", ""),
    }


def search_contacts(domain, job_titles, seniority=None, department=None,
                    limit=5, titles_exact_match=False, next_token=""):
    """FREE. Find title-matched contacts at one company domain.

    Returns {"results": [...], "next_token": str, "credits": {...}}, where each
    result carries the `searchResultId` that research_contacts() consumes.
    Returns empty when the key is unset or job_titles is empty -- an unfiltered
    search would hand back whoever Seamless happens to rank first, which is
    exactly what the title gate exists to prevent.
    """
    empty = {"results": [], "next_token": "", "credits": {}}
    if not config.SEAMLESS_API_KEY:
        return empty
    if not domain or not job_titles:
        return empty

    payload = {
        "companyDomain": [domain] if isinstance(domain, str) else list(domain),
        "jobTitle": list(job_titles),
        "titlesExactMatch": bool(titles_exact_match),
        "limit": int(limit),
    }
    if seniority:
        payload["seniority"] = list(seniority)
    if department:
        payload["department"] = list(department)
    if next_token:
        payload["nextToken"] = next_token

    resp = _request("POST", "/search/contacts", json=payload)
    if resp is None or resp.status_code >= 300:
        return empty
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        return empty
    return {
        "results": data.get("data") or [],
        "next_token": ((data.get("supplementalData") or {}).get("nextToken") or ""),
        "credits": credit_headers(resp),
    }


def research_contacts(search_result_ids):
    """PAID -- 1 credit per submitted id, whether or not anything is found.

    Max 100 ids per call. skipDeduplicationCheck is deliberately left at its
    default (false) so Seamless's own dedup applies.

    Returns {"request_ids": [...], "credits": {...}}.
    """
    empty = {"request_ids": [], "credits": {}}
    if not config.SEAMLESS_API_KEY or not search_result_ids:
        return empty
    ids = [i for i in search_result_ids if i][:100]
    if not ids:
        return empty

    resp = _request("POST", "/contacts/research", json={"searchResultIds": ids})
    if resp is None or resp.status_code >= 300:
        return empty
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        return empty
    return {"request_ids": data.get("requestIds") or [], "credits": credit_headers(resp)}


def research_by_identity(contacts):
    """PAID -- 1 credit per contact. Resolve a person we ALREADY know into their
    email and mobile, without having discovered them through Seamless search.

    This is the cheap way to use Seamless. Its /search/contacts is billed at
    roughly 1 credit per 10 results plus a floor per call -- 830 of 971 credits
    in a measured 200-company run -- so using it for discovery costs about 15
    credits per verified contact. Resolving a known identity costs exactly 1,
    and returns the same email AND mobile payload. So another provider can do
    the (free) discovery while Seamless still supplies, and is still measured
    on, every contact's email and mobile.

    Verified 2026-09-19: returned an address for 21 of 22 people Seamless had
    failed to surface from its own domain search.

    `contacts` is a list of dicts, each with ONE of:
        {"liProfileUrl": ...}                  strongest key
        {"contactName": ..., "domain": ...}
        {"email": ...}
    Max 100 per call. Returns {"request_ids": [...], "credits": {...}} -- poll
    with wait_for_research() exactly as for search-driven research.
    """
    empty = {"request_ids": [], "credits": {}}
    if not config.SEAMLESS_API_KEY or not contacts:
        return empty
    payload = [c for c in contacts if c][:100]
    if not payload:
        return empty
    resp = _request("POST", "/contacts/research", json={"contacts": payload})
    if resp is None or resp.status_code >= 300:
        return empty
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        return empty
    return {"request_ids": data.get("requestIds") or [], "credits": credit_headers(resp)}


def poll_research(request_ids):
    """Poll one batch of research request ids. Returns {"results": [...],
    "credits": {...}} -- raw, unnormalized, one entry per request id."""
    empty = {"results": [], "credits": {}}
    if not config.SEAMLESS_API_KEY or not request_ids:
        return empty
    resp = _request("GET", "/contacts/research/poll",
                    params={"requestIds": ",".join(request_ids)})
    if resp is None or resp.status_code >= 300:
        return empty
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        return empty
    results = data.get("data") if isinstance(data.get("data"), list) else data.get("results")
    return {"results": results or [], "credits": credit_headers(resp)}


def wait_for_research(request_ids, timeout=300, interval=5):
    """Poll until every id reaches a terminal state or `timeout` elapses.
    Returns {request_id: result_entry}. Ids still unresolved at timeout are
    returned with status "timeout" rather than dropped, so the audit can count
    them honestly instead of mistaking a slow lookup for a miss."""
    pending = {rid for rid in request_ids if rid}
    out = {}
    deadline = time.time() + timeout
    while pending and time.time() < deadline:
        batch = list(pending)[:100]
        got = poll_research(batch).get("results") or []
        for entry in got:
            rid = str(entry.get("requestId") or entry.get("id") or "")
            status = str(entry.get("status") or "")
            if rid and status in TERMINAL:
                out[rid] = entry
                pending.discard(rid)
        if pending:
            time.sleep(interval)
    for rid in pending:
        out[rid] = {"requestId": rid, "status": "timeout"}
    return out


def normalize_contact(entry):
    """Flatten one poll result into the row shape the pilot runner writes.

    Keeps ALL THREE emails and BOTH phones, not just the primary, because the
    audit compares Seamless's own EmailAI verdict against DeBounce's and needs
    the alternates to do it.

    Mobile rule: only a phone whose DataType == "mobile" is taken. If phone1 is
    a switchboard ("main") and phone2 is a mobile, phone2 wins. If neither is a
    mobile, mobile stays empty and mobile_datatype records what was on offer.
    """
    status = str(entry.get("status") or "")
    c = entry.get("contact") or {}
    row = {
        "seamless_status": status,
        "request_id": str(entry.get("requestId") or entry.get("id") or ""),
        "first_name": (c.get("firstName") or c.get("first_name") or "").strip(),
        "last_name": (c.get("lastName") or c.get("last_name") or "").strip(),
        "job_title": (c.get("title") or c.get("jobTitle") or "").strip(),
        "linkedin_url": (c.get("linkedInUrl") or c.get("linkedin") or "").strip(),
        "company_name": (c.get("companyName") or "").strip(),
        "company_domain": (c.get("companyDomain") or c.get("domain") or "").strip(),
        "email": "",
        "email_seamless_validity": "",
        "email_seamless_confidence": "",
        "email_source": "",
        "mobile": "",
        "mobile_datatype": "",
        "mobile_confidence": "",
        "mobile_source": "",
    }

    emails = []
    for n in (1, 2, 3):
        addr = (c.get(f"email{n}") or "").strip()
        validity = (c.get(f"email{n}EmailAI") or c.get(f"email{n}_EmailAI")
                    or c.get(f"emailAI{n}") or "")
        conf = (c.get(f"email{n}Confidence") or c.get(f"email{n}TotalAI") or "")
        row[f"email{n}"] = addr
        row[f"email{n}_validity"] = str(validity).strip().lower()
        row[f"email{n}_confidence"] = str(conf)
        if addr:
            emails.append((addr, str(validity).strip().lower(), str(conf)))
    if emails:
        row["email"], row["email_seamless_validity"], row["email_seamless_confidence"] = emails[0]
        row["email_source"] = "Seamless"

    offered = []
    for n in (1, 2):
        num = (c.get(f"contactPhone{n}") or "").strip()
        dtype = str(c.get(f"contactPhone{n}DataType") or c.get(f"phone{n}DataType") or "").strip().lower()
        conf = str(c.get(f"contactPhone{n}TotalAI") or c.get(f"phone{n}TotalAI") or "")
        row[f"phone{n}"] = num
        row[f"phone{n}_datatype"] = dtype
        row[f"phone{n}_confidence"] = conf
        if num:
            offered.append((num, dtype, conf))
    mobile = next((o for o in offered if o[1] == MOBILE_DATATYPE), None)
    if mobile:
        row["mobile"], row["mobile_datatype"], row["mobile_confidence"] = mobile
        row["mobile_source"] = "Seamless"
    elif offered:
        # A switchboard was on offer but no personal mobile. Record that fact --
        # it is a distinct audit bucket from "no number at all".
        row["mobile_datatype"] = offered[0][1] or "unknown"
    return row


def search_contacts_unfiltered(domains, limit=100):
    """FREE. Company-level probe with NO title filter.

    Exists for one reason: Seamless omits `employeeCount` from responses when
    jobTitle filters are applied (verified 2026-09-18 -- unfiltered returns 30
    for researchanddesign.com, the filtered call returns blank). The
    conditional-include headcount gate in icp_titles cannot run without it, so
    this fetches company attributes separately. Still costs no credits.
    """
    empty = {"results": [], "credits": {}}
    if not config.SEAMLESS_API_KEY or not domains:
        return empty
    payload = {
        "companyDomain": [domains] if isinstance(domains, str) else list(domains),
        "limit": int(limit),
    }
    resp = _request("POST", "/search/contacts", json=payload)
    if resp is None or resp.status_code >= 300:
        return empty
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        return empty
    return {"results": data.get("data") or [], "credits": credit_headers(resp)}
