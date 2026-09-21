"""
StoreLeads company/revenue client -- tier-1 of the revenue waterfall
(enrichment.find_revenue_band). Ecommerce/DTC-native: given a domain it returns
an estimated ANNUAL sales figure, the most accurate revenue signal for
Levanta's brands. Skips (returns None) when STORELEADS_KEY is blank, so the
tier is dormant until a key is provided.

Auth: Bearer token. Base https://storeleads.app/json/api/v1/all.
NOTE: StoreLeads' docs are ambiguous on whether estimated_sales is in cents or
dollars -- verify against a known store on the first live call and adjust
_STORELEADS_UNITS below if the magnitude is off by 100x.
"""
import time
import requests

import config

BASE = "https://storeleads.app/json/api/v1/all"
# StoreLeads returns estimated_sales in CENTS of USD (confirmed live: OLIPOP
# estimated_sales_yearly 7,311,574,896 == ~$73M/yr). Multiply by this to get USD.
_UNITS = 0.01


def _get_store(domain):
    """Fetch and unwrap the StoreLeads store object for a domain. Returns the
    store dict, or {} on miss / no key / error."""
    if not config.STORELEADS_KEY or not domain:
        return {}
    resp = None
    for attempt in range(4):
        try:
            resp = requests.get(
                f"{BASE}/domain/{domain}",
                headers={"Authorization": f"Bearer {config.STORELEADS_KEY}"},
                params={"follow_redirects": "true"}, timeout=30,
            )
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", "2")))
            continue
        break
    if resp is None or resp.status_code >= 300:
        return {}
    data = resp.json() if resp.content else {}
    store = data
    for key in ("domain", "store", "result"):
        if isinstance(data.get(key), dict):
            store = data[key]
            break
    return store if isinstance(store, dict) else {}


def _revenue_from_store(store):
    yearly = store.get("estimated_sales_yearly")
    monthly = store.get("estimated_sales")
    if yearly:
        return float(yearly) * _UNITS
    if monthly:
        return float(monthly) * 12 * _UNITS
    return None


def company_annual_revenue(domain):
    """Returns estimated ANNUAL revenue in USD (float), or None when no key /
    not found / no estimate."""
    return _revenue_from_store(_get_store(domain))


def company_firmographics(domain):
    """Firmographics from StoreLeads (ecommerce-native). Returns a dict with any
    of: revenue_usd (float), employee_count (int), industry (str), city, state,
    country, founded_year. Empty dict on miss. Category path is trimmed to its
    most specific leaf, e.g. '/Beauty & Fitness/.../Massage Therapy' -> that leaf."""
    store = _get_store(domain)
    if not store:
        return {}
    out = {}
    rev = _revenue_from_store(store)
    if rev:
        out["revenue_usd"] = rev
    if store.get("employee_count"):
        out["employee_count"] = store["employee_count"]
    cats = store.get("categories")
    if isinstance(cats, list) and cats:
        leaf = [seg for seg in str(cats[0]).split("/") if seg.strip()]
        if leaf:
            out["industry"] = leaf[-1].strip()
    for k_out, k_in in (("city", "city"), ("country", "country_code")):
        if store.get(k_in):
            out[k_out] = str(store[k_in]).strip()
    # NOTE: StoreLeads' "state" is the STORE's status ("Active"/"Redirects"), not a
    # geographic region -- mapping it to state wrote "Active" into State/Region on
    # hundreds of rows. The real region is the middle token of "location", which is
    # formatted "City, ST, COUNTRY" (e.g. "Salt Lake City, UT, USA").
    loc = [seg.strip() for seg in str(store.get("location") or "").split(",") if seg.strip()]
    if len(loc) >= 3:
        # the segment can carry a postcode ("UT 84096"); keep only the non-numeric part
        region = " ".join(t for t in loc[-2].split() if not t.replace("-", "").isdigit())
        if region:
            out["state"] = region
    created = str(store.get("created_at") or "")[:4]
    if created.isdigit():
        out["founded_year"] = created  # store-creation year (weak; last-resort only)
    return out
