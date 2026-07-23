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
        "properties": ["email", "jobtitle", "phone", "hs_linkedin_url", "hs_lead_status"],
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
        "properties": ["name", "domain", "pod", "sdr_owner"],
        "limit": 1,
    }
    resp = request_with_retry("POST", f"{BASE}/crm/v3/objects/companies/search", json=body)
    if resp.status_code >= 300:
        raise RuntimeError(f"company search failed: {resp.status_code} {resp.text[:300]}")
    results = resp.json().get("results", [])
    return results[0] if results else None


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

def create_company(properties):
    resp = request_with_retry("POST", f"{BASE}/crm/v3/objects/companies", json={"properties": properties})
    if resp.status_code >= 300:
        raise RuntimeError(f"company create failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()["id"]


def update_company(company_id, properties):
    if not properties:
        return
    resp = request_with_retry(
        "PATCH", f"{BASE}/crm/v3/objects/companies/{company_id}", json={"properties": properties}
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"company update failed: {resp.status_code} {resp.text[:500]}")


def create_contact(properties):
    resp = request_with_retry("POST", f"{BASE}/crm/v3/objects/contacts", json={"properties": properties})
    if resp.status_code >= 300:
        raise RuntimeError(f"contact create failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()["id"]


def update_contact(contact_id, properties):
    if not properties:
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
    body = {"inputs": [{"from": {"id": str(contact_id)}, "to": {"id": str(company_id)}}]}
    resp = request_with_retry(
        "POST", f"{BASE}/crm/v4/associations/contacts/companies/batch/create/default", json=body
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"association failed: {resp.status_code} {resp.text[:500]}")


def batch_associate_default(from_type, to_type, pairs):
    """Bulk version, carried over from push_event_to_hubspot.py."""
    for batch in chunked(pairs, 100):
        body = {"inputs": [{"from": {"id": str(a)}, "to": {"id": str(b)}} for a, b in batch]}
        resp = request_with_retry(
            "POST", f"{BASE}/crm/v4/associations/{from_type}/{to_type}/batch/create/default", json=body
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"batch association failed: {resp.status_code} {resp.text[:500]}")
