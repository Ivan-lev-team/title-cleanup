"""
All HubSpot API interaction: dedupe lookups, live round-robin balancing,
and create/update/associate. Uses the real REST API directly (not the
10-per-batch tool cap we hit in Claude Code), since this runs as its own
service with normal network access.

Two hardening lessons carried over from the manual Social Commerce Summit
2026 push:
  1. HubSpot's `domain IN [...]` search filter can occasionally over-match
     (one bad value in a batch matching thousands of companies), which then
     hits HubSpot's 10,000-result search pagination cap and 400s. Guard:
     small batches (25) + a sanity check that falls back to per-domain EQ
     lookups if a batch's result count looks implausible.
  2. Round-robin state should never live in fragile local counters that can
     desync across restarts/concurrent runs -- pod/owner assignment is
     decided live off HubSpot's current company counts per pod each time.
"""
import time
import requests

import config

HEADERS = {"Authorization": f"Bearer {config.HUBSPOT_TOKEN}", "Content-Type": "application/json"}
BASE = config.HUBSPOT_BASE

DOMAIN_BATCH_SIZE = 25
OVERMATCH_RATIO = 4  # if a batch returns more than N x batch size, treat as suspicious


def _chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _request(method, url, **kwargs):
    for attempt in range(6):
        resp = requests.request(method, url, headers=HEADERS, timeout=30, **kwargs)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "2"))
            time.sleep(wait)
            continue
        return resp
    return resp


def _search(object_type, filter_groups, properties, limit=100):
    """Single search call, no pagination -- callers that need pagination
    handle it themselves so they can apply the over-match guard per page."""
    body = {"filterGroups": filter_groups, "properties": properties, "limit": limit}
    resp = _request("POST", f"{BASE}/crm/v3/objects/{object_type}/search", json=body)
    if resp.status_code >= 300:
        raise RuntimeError(f"{object_type} search failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()


def find_contact_by_email(email):
    if not email:
        return None
    data = _search(
        "contacts",
        [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        ["email", "jobtitle", "phone", "hs_linkedin_url", "hs_lead_status"],
        limit=1,
    )
    results = data.get("results", [])
    return results[0] if results else None


def find_company_by_domain(domain):
    """Single-domain exact lookup -- the safe, always-correct path. Used
    directly for one-row-at-a-time processing (this service processes rows
    as they appear in the sheet, not in giant batches), so the over-match
    bug from bulk IN-filter batches doesn't apply here at all -- kept the
    batch/guard helper below anyway for any future bulk-reconciliation use."""
    if not domain:
        return None
    data = _search(
        "companies",
        [{"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]}],
        ["name", "domain", "pod", "sdr_owner"],
        limit=1,
    )
    results = data.get("results", [])
    return results[0] if results else None


def find_companies_by_domains_bulk(domains):
    """Batch lookup for bulk reconciliation jobs. Returns {domain: record}.
    Guards against the IN-over-match bug: any batch whose result count looks
    implausible relative to its input size falls back to per-domain EQ."""
    found = {}
    for batch in _chunked(sorted(set(d for d in domains if d)), DOMAIN_BATCH_SIZE):
        try:
            data = _search(
                "companies",
                [{"filters": [{"propertyName": "domain", "operator": "IN", "values": batch}]}],
                ["name", "domain", "pod", "sdr_owner"],
                limit=100,
            )
        except RuntimeError:
            data = {"results": [], "total": 10**9}  # force fallback below

        suspicious = data.get("total", 0) > len(batch) * OVERMATCH_RATIO
        if suspicious:
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


def least_loaded_pod():
    """Live round-robin: count existing companies per pod right now, pick
    whichever has the fewest. Self-balancing -- no state to lose, no drift
    across restarts or concurrent runs. Pod RevOps is never a candidate."""
    counts = {}
    for pod in config.POD_OWNERS:
        data = _search(
            "companies",
            [{"filters": [{"propertyName": "pod", "operator": "EQ", "value": pod}]}],
            [],
            limit=1,  # we only need the "total" count, not the records
        )
        counts[pod] = data.get("total", 0)
    return min(counts, key=counts.get)


def least_loaded_owner_in_pod(pod):
    """Within the chosen pod, balance between its 1-2 owners the same way --
    live counts, not a local alternating counter."""
    owners = config.POD_OWNERS[pod]
    if len(owners) == 1:
        return owners[0]
    counts = {}
    for owner in owners:
        data = _search(
            "companies",
            [{"filters": [
                {"propertyName": "pod", "operator": "EQ", "value": pod},
                {"propertyName": "sdr_owner", "operator": "EQ", "value": owner},
            ]}],
            [],
            limit=1,
        )
        counts[owner] = data.get("total", 0)
    return min(counts, key=counts.get)


def create_company(name, domain, pod, owner):
    props = {"name": name}
    if domain:
        props["domain"] = domain
    if pod:
        props["pod"] = pod
        props["sdr_owner"] = owner
    resp = _request("POST", f"{BASE}/crm/v3/objects/companies", json={"properties": props})
    if resp.status_code >= 300:
        raise RuntimeError(f"company create failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()["id"]


def update_company(company_id, properties):
    if not properties:
        return
    resp = _request(
        "PATCH", f"{BASE}/crm/v3/objects/companies/{company_id}", json={"properties": properties}
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"company update failed: {resp.status_code} {resp.text[:500]}")


def create_contact(properties):
    resp = _request("POST", f"{BASE}/crm/v3/objects/contacts", json={"properties": properties})
    if resp.status_code >= 300:
        raise RuntimeError(f"contact create failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()["id"]


def update_contact(contact_id, properties):
    if not properties:
        return
    resp = _request(
        "PATCH", f"{BASE}/crm/v3/objects/contacts/{contact_id}", json={"properties": properties}
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"contact update failed: {resp.status_code} {resp.text[:500]}")


def associate_contact_to_company(contact_id, company_id):
    resp = _request(
        "PUT",
        f"{BASE}/crm/v3/objects/contacts/{contact_id}/associations/default/companies/{company_id}",
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"association failed: {resp.status_code} {resp.text[:500]}")
