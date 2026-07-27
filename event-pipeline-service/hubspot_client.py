"""
All HubSpot API interaction. Core request/batch functions (chunked,
request_with_retry, batch_update, batch_associate_default, normalize_phone)
are carried over unchanged from push_event_to_hubspot.py -- the script that
was actually dry-run validated against live HubSpot data on the Social
Commerce Summit 2026 NYC list (225 rows, 98 companies created, 21 updated,
193 contacts created, 32 updated, 222 associations, zero errors).

Two things layered on top of that proven logic for this service:
  1. Per-row processing uses single exact-match (EQ) lookups, not bulk IN
     batches -- since rows arrive one at a time off the Sheet poll, there's
     no need for IN-batching at all, which sidesteps the domain-IN
     over-matching bug entirely (found live during the CLI hardening
     session: one bad value in a 84-domain IN batch matched 10,000+
     companies and blew past HubSpot's search pagination cap). The bulk
     helpers below keep that same fix (small batches + a sanity-check
     fallback to per-domain EQ) for any future bulk-reconciliation job that
     processes many rows in one sweep.
  2. Round-robin is live-balanced off current HubSpot company counts per
     Pod, not a fixed cycle position -- no local state to lose or desync.
"""
import time
import requests

import config

HEADERS = {"Authorization": f"Bearer {config.HUBSPOT_TOKEN}", "Content-Type": "application/json"}
BASE = config.HUBSPOT_BASE

DOMAIN_BATCH_SIZE = 25
OVERMATCH_RATIO = 4  # a batch returning more than N x its own size is untrustworthy


def chunked(lst, n=100):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def request_with_retry(method, url, **kwargs):
    resp = None
    for attempt in range(6):
        resp = requests.request(method, url, headers=HEADERS, timeout=30, **kwargs)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "2"))
            time.sleep(wait)
            continue
        # HubSpot's search API intermittently throws spurious 400/5xx; these
        # are safe to retry on read-only calls and must not be treated as final.
        if resp.status_code == 400 or resp.status_code >= 500:
            wait = 2 ** attempt
            time.sleep(wait)
            continue
        return resp
    return resp


def normalize_phone(p):
    p = (p or "").strip()
    if not p:
        return None
    return p if p.startswith("+") else "+" + p


# ---------------------------------------------------------------- lookups --

def find_contact_by_email(email):
    if not email:
        return None
    body = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "jobtitle", "phone", "hs_linkedin_url", "hs_lead_status", "lifecyclestage",
                       "how_did_you_hear_about_us_", "contact_type", "gold___ent__qualification"],
        "limit": 1,
    }
    resp = request_with_retry("POST", f"{BASE}/crm/v3/objects/contacts/search", json=body)
    if resp.status_code >= 300:
        raise RuntimeError(f"contact search failed: {resp.status_code} {resp.text[:300]}")
    results = resp.json().get("results", [])
    return results[0] if results else None


def find_company_by_domain(domain):
    if not domain:
        return None
    body = {
        "filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]}],
        "properties": ["name", "domain", "pod", "sdr_owner", "lifecyclestage", "estimated_annual_revenue"],
        "limit": 1,
    }
    resp = request_with_retry("POST", f"{BASE}/crm/v3/objects/companies/search", json=body)
    if resp.status_code >= 300:
        raise RuntimeError(f"company search failed: {resp.status_code} {resp.text[:300]}")
    results = resp.json().get("results", [])
    return results[0] if results else None


def company_has_open_deal(company_id):
    """True if the company has at least one OPEN (not closed-won/closed-lost)
    associated deal -- used to skip companies Sales is already actively working,
    so an event attendee doesn't get re-tagged over a live opportunity. Returns
    False (i.e. don't skip) on any lookup failure, so a transient API hiccup
    never silently drops a lead. Requires the token's crm.objects.deals.read
    scope; without it the deals call 403s and this returns False."""
    if not company_id:
        return False
    resp = request_with_retry(
        "GET", f"{BASE}/crm/v4/objects/companies/{company_id}/associations/deals", params={"limit": 100}
    )
    if resp.status_code >= 300:
        return False
    deal_ids = [r.get("toObjectId") for r in resp.json().get("results", []) if r.get("toObjectId")]
    if not deal_ids:
        return False
    body = {"inputs": [{"id": str(d)} for d in deal_ids], "properties": ["dealstage", "hs_is_closed"]}
    resp = request_with_retry("POST", f"{BASE}/crm/v3/objects/deals/batch/read", json=body)
    if resp.status_code >= 300:
        return False
    for d in resp.json().get("results", []):
        # hs_is_closed is a calculated bool returned as the string "true"/"false";
        # anything other than "true" (open, or unset) counts as an open deal.
        if (d.get("properties", {}).get("hs_is_closed") or "").strip().lower() != "true":
            return True
    return False


def find_owner_id_by_email(email):
    """The template's 'Contact Owner' / 'Company Owner' columns are HubSpot
    user emails, not numeric IDs -- resolve here before setting hubspot_owner_id."""
    if not email:
        return None
    resp = request_with_retry("GET", f"{BASE}/crm/v3/owners", params={"email": email})
    if resp.status_code >= 300:
        return None
    results = resp.json().get("results", [])
    return results[0]["id"] if results else None


def find_companies_by_domains_bulk(domains):
    """Batch lookup for bulk reconciliation jobs (not used in normal one-row
    polling). Returns {domain: record}. Same over-match guard validated
    during the manual Social Commerce Summit push."""
    found = {}
    for batch in chunked(sorted(set(d for d in domains if d)), DOMAIN_BATCH_SIZE):
        body = {
            "filterGroups": [{"filters": [{"propertyName": "domain", "operator": "IN", "values": batch}]}],
            "properties": ["name", "domain", "pod", "sdr_owner"],
            "limit": 100,
        }
        resp = request_with_retry("POST", f"{BASE}/crm/v3/objects/companies/search", json=body)
        data = resp.json() if resp.status_code < 300 else {"results": [], "total": 10**9}

        if data.get("total", 0) > len(batch) * OVERMATCH_RATIO:
            for d in batch:
                rec = find_company_by_domain(d)
                if rec:
                    found[d] = rec
            continue

        for r in data.get("results", []):
            d = (r["properties"].get("domain") or "").strip().lower()
            if d:
                found[d] = r
    return found


# --------------------------------------------------------- round robin ----

def least_loaded_pod():
    """Live round-robin: count existing companies per pod right now, pick
    whichever has the fewest. Pod RevOps is never a candidate."""
    counts = {}
    for pod in config.POD_OWNERS:
        body = {
            "filterGroups": [{"filters": [{"propertyName": "pod", "operator": "EQ", "value": pod}]}],
            "properties": [],
            "limit": 1,
        }
        resp = request_with_retry("POST", f"{BASE}/crm/v3/objects/companies/search", json=body)
        counts[pod] = resp.json().get("total", 0) if resp.status_code < 300 else 0
    return min(counts, key=counts.get)


def least_loaded_partnership_owner():
    """Same live-balance approach as least_loaded_owner_in_pod, but for
    agency companies -- these have no "pod" set, so we balance directly off
    each partnership owner's current company count instead of filtering by pod."""
    counts = {}
    for owner in config.PARTNERSHIP_OWNERS:
        body = {
            "filterGroups": [{"filters": [{"propertyName": "sdr_owner", "operator": "EQ", "value": owner}]}],
            "properties": [],
            "limit": 1,
        }
        resp = request_with_retry("POST", f"{BASE}/crm/v3/objects/companies/search", json=body)
        counts[owner] = resp.json().get("total", 0) if resp.status_code < 300 else 0
    return min(counts, key=counts.get)


def least_loaded_owner_in_pod(pod):
    owners = config.POD_OWNERS[pod]
    if len(owners) == 1:
        return owners[0]
    counts = {}
    for owner in owners:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "pod", "operator": "EQ", "value": pod},
                {"propertyName": "sdr_owner", "operator": "EQ", "value": owner},
            ]}],
            "properties": [],
            "limit": 1,
        }
        resp = request_with_retry("POST", f"{BASE}/crm/v3/objects/companies/search", json=body)
        counts[owner] = resp.json().get("total", 0) if resp.status_code < 300 else 0
    return min(counts, key=counts.get)


# --------------------------------------------------------- create/update --
#
# Every write below checks config.DRY_RUN first. In dry-run mode, reads/
# searches (everything above this line) still hit live HubSpot -- only
# creates/updates/associations are skipped, so a dry run exercises the real
# qualify -> dedupe -> enrich -> round-robin logic against real data without
# writing anything.

_dry_run_counter = 0


def _dry_run_id(kind):
    global _dry_run_counter
    _dry_run_counter += 1
    return f"DRY-RUN-{kind}-{_dry_run_counter}"


def create_company(properties):
    if config.DRY_RUN:
        fake_id = _dry_run_id("COMPANY")
        print(f"  [DRY RUN] would create company {fake_id}: {properties}")
        return fake_id
    resp = request_with_retry("POST", f"{BASE}/crm/v3/objects/companies", json={"properties": properties})
    if resp.status_code >= 300:
        raise RuntimeError(f"company create failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()["id"]


def update_company(company_id, properties):
    if not properties:
        return
    if config.DRY_RUN:
        print(f"  [DRY RUN] would update company {company_id}: {properties}")
        return
    resp = request_with_retry(
        "PATCH", f"{BASE}/crm/v3/objects/companies/{company_id}", json={"properties": properties}
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"company update failed: {resp.status_code} {resp.text[:500]}")


def create_contact(properties):
    if config.DRY_RUN:
        fake_id = _dry_run_id("CONTACT")
        print(f"  [DRY RUN] would create contact {fake_id}: {properties}")
        return fake_id
    resp = request_with_retry("POST", f"{BASE}/crm/v3/objects/contacts", json={"properties": properties})
    if resp.status_code >= 300:
        raise RuntimeError(f"contact create failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()["id"]


def update_contact(contact_id, properties):
    if not properties:
        return
    if config.DRY_RUN:
        print(f"  [DRY RUN] would update contact {contact_id}: {properties}")
        return
    resp = request_with_retry(
        "PATCH", f"{BASE}/crm/v3/objects/contacts/{contact_id}", json={"properties": properties}
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"contact update failed: {resp.status_code} {resp.text[:500]}")


def batch_update(object_type, inputs):
    """Carried over from push_event_to_hubspot.py for any future bulk job."""
    for batch in chunked(inputs, 100):
        resp = request_with_retry(
            "POST", f"{BASE}/crm/v3/objects/{object_type}/batch/update", json={"inputs": batch}
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"{object_type} batch update failed: {resp.status_code} {resp.text[:500]}")


def associate_contact_to_company(contact_id, company_id):
    """Uses the same v4 default-association batch endpoint validated in the
    manual push (batch of 1 here since this service processes one row at a
    time, but it's the identical, proven call)."""
    if config.DRY_RUN:
        print(f"  [DRY RUN] would associate contact {contact_id} <-> company {company_id}")
        return
    body = {"inputs": [{"from": {"id": str(contact_id)}, "to": {"id": str(company_id)}}]}
    resp = request_with_retry(
        "POST", f"{BASE}/crm/v4/associations/contacts/companies/batch/associate/default", json=body
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"association failed: {resp.status_code} {resp.text[:500]}")


def batch_associate_default(from_type, to_type, pairs):
    """Bulk version, carried over from push_event_to_hubspot.py."""
    for batch in chunked(pairs, 100):
        body = {"inputs": [{"from": {"id": str(a)}, "to": {"id": str(b)}} for a, b in batch]}
        resp = request_with_retry(
            "POST", f"{BASE}/crm/v4/associations/{from_type}/{to_type}/batch/associate/default", json=body
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"batch association failed: {resp.status_code} {resp.text[:500]}")


# ------------------------------------------------- marketing events (0-54) --

def list_marketing_events():
    """Returns {event_name_lower: objectId} for all HubSpot Marketing Events
    (object 0-54). Returns {} on any failure (e.g. the app lacks the
    crm.objects.marketing_events.read scope) so the pipeline degrades to a
    no-op instead of erroring. Events created in the HubSpot UI have
    externalEventId=null and can only be addressed by objectId, which is what
    this returns -- keyed by name so the pipeline can match a row's Event Name."""
    events = {}
    after = None
    for _ in range(50):  # pagination safety cap
        params = {"limit": 100}
        if after:
            params["after"] = after
        resp = request_with_retry("GET", f"{BASE}/marketing/v3/marketing-events", params=params)
        if resp.status_code >= 300:
            return events  # {} on the first page; whatever we have on later failures
        data = resp.json()
        for e in data.get("results", []):
            name = (e.get("eventName") or "").strip().lower()
            oid = e.get("objectId")
            if name and oid:
                events[name] = str(oid)
        after = (data.get("paging", {}).get("next") or {}).get("after")
        if not after:
            break
    return events


def record_marketing_event_attendance(object_id, subscriber_state, email="", vid="", interaction_dt_ms=None):
    """Record a contact's attendance/registration on a Marketing Event
    (object 0-54) via the subscriber-state endpoints -- this is what drives
    HubSpot's marketing-event attribution (a bare CRM association does not).
    Prefers the email-create variant, falls back to the vid (contact id)
    variant. No-op under DRY_RUN. Returns True on success, False if no
    identifier was available."""
    state = (subscriber_state or "REGISTERED").upper()
    if interaction_dt_ms is None:
        interaction_dt_ms = int(time.time() * 1000)
    if email:
        path = f"{BASE}/marketing/v3/marketing-events/{object_id}/attendance/{state}/email-create"
        item = {"email": email, "interactionDateTime": interaction_dt_ms}
    elif vid:
        path = f"{BASE}/marketing/v3/marketing-events/{object_id}/attendance/{state}/create"
        item = {"vid": int(vid) if str(vid).isdigit() else vid, "interactionDateTime": interaction_dt_ms}
    else:
        return False
    if config.DRY_RUN:
        print(f"  [DRY RUN] would record marketing-event {object_id} attendance {state} for {email or vid}")
        return True
    resp = request_with_retry("POST", path, json={"inputs": [item]})
    if resp.status_code >= 300:
        raise RuntimeError(f"marketing-event attendance failed: {resp.status_code} {resp.text[:300]}")
    return True
