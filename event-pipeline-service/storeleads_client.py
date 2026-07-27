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
# multiply the raw StoreLeads figure by this to get USD (1 if dollars, 0.01 if cents)
_UNITS = float(config.__dict__.get("STORELEADS_UNITS", 1)) if hasattr(config, "STORELEADS_UNITS") else 1.0


def company_annual_revenue(domain):
    """Returns estimated ANNUAL revenue in USD (float), or None when no key /
    not found / no estimate."""
    if not config.STORELEADS_KEY or not domain:
        return None
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
        return None
    data = resp.json() if resp.content else {}
    # the store object may sit at the top level or nested -- check common shapes
    store = data
    for key in ("domain", "store", "result"):
        if isinstance(data.get(key), dict):
            store = data[key]
            break
    if not isinstance(store, dict):
        return None
    yearly = store.get("estimated_sales_yearly")
    monthly = store.get("estimated_sales")
    if yearly:
        return float(yearly) * _UNITS
    if monthly:
        return float(monthly) * 12 * _UNITS
    return None
