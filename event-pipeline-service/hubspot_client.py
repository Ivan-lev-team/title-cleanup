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
import re
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
        "properties": ["email", "jobtitle", "phone", "hs_linkedin_url", "hs_lead_status", "lifecyclestage"],
        "limit": 1,
    }
    resp = request_with_retry("POST", f"{BASE}/crm/v3/objects/contacts/search", json=body)
    if resp.status_code >= 300:
        raise RuntimeError(f"contact search failed: {resp.status_code} {resp.text[:300]}")
    results = resp.json().get("results", [])
    return results[0] if results else None


COMPANY_PROPERTIES = ["name", "domain", "pod", "sdr_owner", "lifecyclestage"]


def _normalize_domain(domain):
    d = (domain or "").strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    if d.startswith("www."):
        d = d[4:]
    return d.rstrip("/").rstrip(".")


def _company_search_eq(prop, value):
    body = {
        "filterGroups": [{"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]}],
        "properties": COMPANY_PROPERTIES,
        "limit": 1,
    }
    resp = request_with_retry("POST", f"{BASE}/crm/v3/objects/companies/search", json=body)
    if resp.status_code >= 300:
        raise RuntimeError(f"company search failed: {resp.status_code} {resp.text[:300]}")
    results = resp.json().get("results", [])
    return results[0] if results else None


_COMPANY_SUFFIXES = (
    " inc", " incorporated", " llc", " l.l.c", " ltd", " limited", " corp",
    " corporation", " co", " company", " group", " holdings", " plc", " pbc",
)

_DOMAIN_TLDS = (
    ".com", ".co", ".io", ".net", ".org", ".us", ".ai", ".shop", ".store",
    ".biz", ".info", ".life", ".xyz",
)

_MIN_SLUG_MATCH_LEN = 8  # below this, containment checks are too likely to false-positive


def _normalize_company_name(name):
    """Strips punctuation and common corporate suffixes so "Lux Decor
    Collection", "Lux Decor Collection, Inc.", and "LDC Lux Decor Collection"
    can be recognized as plausibly-the-same-company variations."""
    n = (name or "").strip().lower()
    n = re.sub(r"[^\w\s]", " ", n)  # strip punctuation (periods, commas, &, ®, etc.)
    n = re.sub(r"\s+", " ", n).strip()
    changed = True
    while changed:
        changed = False
        for suffix in _COMPANY_SUFFIXES:
            if n.endswith(suffix.strip()) and n != suffix.strip():
                n = n[: -len(suffix.strip())].strip()
                changed = True
    return n


def _slug(text):
    """Alphanumeric-only, no spaces -- for comparing a name against a domain
    root ("Lux Decor Collection" -> "luxdecorcollection", matchable against
    domain "luxdecorcollection.com" or a legacy record stored as just
    "luxdecorcollection")."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _domain_root_slug(domain):
    d = _normalize_domain(domain)
    for tld in _DOMAIN_TLDS:
        if d.endswith(tld):
            d = d[: -len(tld)]
            break
    return _slug(d)


def _acronym_of(name):
    """First letter of each significant word, e.g. "Lux Decor Collection" -> "ldc"."""
    words = _normalize_company_name(name).split()
    return "".join(w[0] for w in words if w)


def _names_plausibly_match(name_a, name_b):
    """True if two company names are plausibly the same company, tried via
    several independent signals so a single quirky formatting difference
    doesn't cause a miss. Every non-exact path here trades a little false-
    positive risk for meaningfully higher recall -- deliberate, since missing
    an existing account (especially a customer) is the worse failure mode."""
    if not name_a or not name_b:
        return False
    a_lower, b_lower = name_a.strip().lower(), name_b.strip().lower()
    if a_lower == b_lower:
        return True

    norm_a, norm_b = _normalize_company_name(name_a), _normalize_company_name(name_b)
    if norm_a and norm_a == norm_b:
        return True

    slug_a, slug_b = _slug(norm_a), _slug(norm_b)
    if slug_a and slug_b:
        if slug_a == slug_b:
            return True
        shorter, longer = sorted((slug_a, slug_b), key=len)
        if len(shorter) >= _MIN_SLUG_MATCH_LEN and shorter in longer:
            return True

    # acronym in either direction: "LDC" vs "Lux Decor Collection"
    for short_name, long_name in ((name_a, name_b), (name_b, name_a)):
        short_slug = _slug(short_name)
        if 2 <= len(short_slug) <= 6 and short_slug.isalpha() and short_slug == _acronym_of(long_name):
            return True

    return False


def _slugs_plausibly_match(slug_a, slug_b):
    """Same containment logic as _names_plausibly_match's slug check, for
    comparing two already-slugged strings directly (e.g. two domain roots)."""
    if slug_a == slug_b:
        return True
    shorter, longer = sorted((slug_a, slug_b), key=len)
    return len(shorter) >= _MIN_SLUG_MATCH_LEN and shorter in longer


def _company_name_search(query_text, limit=10):
    body = {"query": query_text, "properties": COMPANY_PROPERTIES, "limit": limit}
    resp = request_with_retry("POST", f"{BASE}/crm/v3/objects/companies/search", json=body)
    if resp.status_code >= 300:
        raise RuntimeError(f"company name search failed: {resp.status_code} {resp.text[:300]}")
    return resp.json().get("results", [])


def find_company_by_domain(domain, company_name=None):
    """Looks up a company by domain first (normalized variants, since real
    HubSpot data isn't always clean -- e.g. a legacy record stored with
    domain "example" instead of "example.com" would silently dodge a bare
    exact match and cause a duplicate company to get created).

    Automatically falls back to name-based matching when domain search comes
    up empty -- tries MULTIPLE independent variations (exact name, corporate-
    suffix-stripped, slug/no-spaces comparison against both the candidate's
    name AND its own domain root, acronym detection) across candidates
    pulled from more than one search query, so a single formatting quirk
    can't cause a miss. This is deliberately biased toward higher recall:
    missing an existing account -- especially an existing customer -- and
    re-tagging them as a fresh cold lead is the worse failure mode, worse
    than an occasional over-eager match. Every fallback match still gets a
    "_dedupe_method" key on the returned properties dict so callers can note
    which signal found it."""
    normalized = _normalize_domain(domain)
    for candidate in filter(None, [normalized, ("www." + normalized) if normalized else None]):
        record = _company_search_eq("domain", candidate)
        if record:
            record["properties"]["_dedupe_method"] = "domain"
            return record

    if not company_name and not domain:
        return None

    # Pull candidates from every angle we've got: the name as given, and the
    # domain's root word (covers cases where HubSpot's stored name differs a
    # lot from ours, but the domain root still resembles it, e.g. our row
    # says "LDC" but HubSpot has "Lux Decor Collection" at luxdecorcollection.com).
    seen_ids = set()
    candidates = []
    domain_root = _domain_root_slug(domain) if domain else ""
    for query_text in filter(None, [company_name, domain_root]):
        for r in _company_name_search(query_text):
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                candidates.append(r)

    if not candidates:
        return None

    # exact case-insensitive name match first (highest confidence)
    if company_name:
        name_lower = company_name.strip().lower()
        for r in candidates:
            if (r["properties"].get("name") or "").strip().lower() == name_lower:
                r["properties"]["_dedupe_method"] = "name_exact"
                return r

    # then every other signal: normalized name, slug containment, acronym,
    # AND cross-checking the candidate's own domain root against our name/domain
    for r in candidates:
        candidate_name = r["properties"].get("name")
        candidate_domain_root = _domain_root_slug(r["properties"].get("domain"))
        matched = (
            (company_name and _names_plausibly_match(company_name, candidate_name))
            or (domain_root and candidate_domain_root and _slugs_plausibly_match(domain_root, candidate_domain_root))
            or (domain_root and candidate_name and _names_plausibly_match(domain_root, candidate_name))
        )
        if matched:
            r["properties"]["_dedupe_method"] = "name_variation"
            return r

    return None


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
