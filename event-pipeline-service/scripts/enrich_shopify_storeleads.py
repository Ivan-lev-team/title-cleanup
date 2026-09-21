"""
Enrich the Shopify brands on a HubSpot company list from StoreLeads.

Runs after scripts/fix_list_domains.py has established verified domains --
StoreLeads is keyed by domain, so a wrong domain means wrong firmographics.

What it writes:
  affiliate_history          the buying signal. StoreLeads lists a store's
                             installed apps, so a store running GoAffPro /
                             UpPromote / Refersion is already managing affiliates
                             in-house, and one on Awin / ShareASale / impact.com
                             is on a large network.
  estimated_annual_revenue   banded 0-4 (the codes qualify.py keys off)
  annualrevenue              the exact figure
  numberofemployees          headcount
  industry                   mapped from the store's product category
  city / state / country      HQ
  amazon_storefront_url      StoreLeads exposes sales-channel URLs, which fills
                             gaps left by the domain-repair pass

FILL-BLANKS-ONLY. Nothing already in HubSpot is overwritten. Two reasons:
the playbook's revenue rule is MAX-across-sources and StoreLeads only sees the
SHOPIFY channel (it excludes Amazon, which is the bigger channel for many of
these brands, so its figure can understate them); and affiliate_history may
have been set by a rep from a real conversation, which beats an app scan.

Note `est__monthly_revenue` is "Amazon - Estimated MRR" and is deliberately
NEVER written here -- Shopify revenue does not belong in an Amazon field.

Run from the repo root:
    python scripts/enrich_shopify_storeleads.py --dry-run
    python scripts/enrich_shopify_storeleads.py
"""
import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import hubspot_client as hs
import storeleads_client
from fix_list_domains import (
    BASE, DATA_DIR, DEFAULT_LIST_ID, bare_domain, cached, load_cache, log,
    save_cache, write_csv,
)

ENRICH_PROPS = [
    "name", "domain", "amazon_storefront_url", "marketplaces",
    "estimated_annual_revenue", "annualrevenue", "numberofemployees",
    "industry", "city", "state", "country",
]

# Affiliate platforms that are large public networks vs. self-managed Shopify
# apps. Matched case-insensitively against installed app names.
AFFILIATE_NETWORKS = (
    "shareasale", "cj affiliate", "impact.com", "awin", "rakuten",
    "partnerize", "flexoffers", "avantlink", "levanta", "archer",
)
AFFILIATE_APPS = (
    "refersion", "goaffpro", "affiliatly", "leaddyno", "uppromote", "snowball",
    "tapfiliate", "omnistar", "referrly", "affilo", "bixgrow", "loudcrowd",
    "affiliate", "partner program",
)

# StoreLeads product-category leaf -> HubSpot `industry` option. Deliberately
# partial: HubSpot's industry list is LinkedIn-style job-market codes, not retail
# categories, so anything without a confident mapping is left blank. A wrong
# industry is worse than an empty one for segmentation.
INDUSTRY_MAP = {
    "skin & nail care": "COSMETICS", "face & body care": "COSMETICS",
    "hair care": "COSMETICS", "perfumes & fragrances": "COSMETICS",
    "cosmetics": "COSMETICS", "makeup": "COSMETICS", "bath & body": "COSMETICS",
    "vitamins & supplements": "HEALTH_WELLNESS_AND_FITNESS",
    "health": "HEALTH_WELLNESS_AND_FITNESS",
    "fitness": "HEALTH_WELLNESS_AND_FITNESS",
    "medical supplies & equipment": "MEDICAL_DEVICES",
    "apparel": "APPAREL_FASHION", "clothing": "APPAREL_FASHION",
    "shoes": "APPAREL_FASHION", "accessories": "APPAREL_FASHION",
    "jewelry": "LUXURY_GOODS_JEWELRY", "watches": "LUXURY_GOODS_JEWELRY",
    "food": "FOOD_BEVERAGES", "food & drink": "FOOD_BEVERAGES",
    "beverages": "FOOD_BEVERAGES", "grocery": "FOOD_BEVERAGES",
    "candy & sweets": "FOOD_BEVERAGES",
    "pet food & supplies": "VETERINARY", "pet supplies": "VETERINARY",
    "furniture": "FURNITURE", "sporting goods": "SPORTING_GOODS",
    "outdoor recreation": "SPORTING_GOODS", "sports": "SPORTING_GOODS",
    "music & audio": "CONSUMER_ELECTRONICS",
    "consumer electronics": "CONSUMER_ELECTRONICS",
    "electronics": "CONSUMER_ELECTRONICS",
    "automotive": "AUTOMOTIVE", "auto parts": "AUTOMOTIVE",
    "textiles": "TEXTILES", "packaging": "PACKAGING_AND_CONTAINERS",
    "make-up & cosmetics": "COSMETICS", "nail care": "COSMETICS",
    "clothing accessories": "APPAREL_FASHION", "athletic apparel": "APPAREL_FASHION",
    "headwear": "APPAREL_FASHION", "dance & electronic music": "MUSIC",
    "audio equipment": "CONSUMER_ELECTRONICS",
    "mobile & wireless accessories": "CONSUMER_ELECTRONICS",
    "radio & communications": "CONSUMER_ELECTRONICS",
    "alcoholic beverages": "FOOD_BEVERAGES",
    "cookware & diningware": "CONSUMER_GOODS", "cleaning": "CONSUMER_GOODS",
    "home storage & shelving": "CONSUMER_GOODS", "nursery & playroom": "CONSUMER_GOODS",
    "gardening & landscaping": "CONSUMER_GOODS", "golf": "SPORTING_GOODS",
    "diabetes": "MEDICAL_DEVICES", "parts & services": "AUTOMOTIVE",
    "toys & hobbies": "CONSUMER_GOODS", "toys": "CONSUMER_GOODS",
    "home & garden": "CONSUMER_GOODS", "lamps & lighting": "CONSUMER_GOODS",
    "kitchen & dining": "CONSUMER_GOODS", "household supplies": "CONSUMER_GOODS",
    "office supplies": "CONSUMER_GOODS", "baby & toddler": "CONSUMER_GOODS",
    "arts & crafts": "CONSUMER_GOODS", "luggage & bags": "CONSUMER_GOODS",
    "tools": "CONSUMER_GOODS", "hardware": "CONSUMER_GOODS",
}

FIELDS = [
    "id", "name", "domain", "shopify", "platform", "shopify_tag_removed",
    "marketplaces", "affiliate_history", "affiliate_apps",
    "estimated_annual_revenue", "revenue_band_label", "annualrevenue",
    "numberofemployees", "industry", "city", "state", "country",
    "amazon_storefront_url", "skipped_because_set", "applied",
]

BAND_LABELS = {"0": "<$10k", "1": "$10k-100k", "2": "$100k-1M", "3": "$1M-10M", "4": "$10M+"}


def revenue_band(annual_usd):
    """HubSpot estimated_annual_revenue option code for a USD annual figure."""
    if annual_usd >= 10_000_000:
        return "4"
    if annual_usd >= 1_000_000:
        return "3"
    if annual_usd >= 100_000:
        return "2"
    if annual_usd >= 10_000:
        return "1"
    return "0"


def affiliate_verdict(store):
    """(affiliate_history option, matched app names). ('', []) when no signal.

    A large network is the stronger claim, so it wins when a store runs both.
    """
    names = [a.get("name", "") for a in (store.get("apps") or []) if isinstance(a, dict)]
    matched = [n for n in names
               if any(k in n.lower() for k in AFFILIATE_NETWORKS + AFFILIATE_APPS)]
    if not matched:
        return "", []
    if any(any(k in n.lower() for k in AFFILIATE_NETWORKS) for n in matched):
        return "Yes - Large Affiliate Networks", matched
    return "Yes - Managed InHouse", matched


def industry_for(store):
    for cat in (store.get("categories") or [])[:1]:
        leaf = [seg.strip() for seg in str(cat).split("/") if seg.strip()]
        if leaf:
            return INDUSTRY_MAP.get(leaf[-1].lower(), "")
    return ""


def get_store(domain):
    return cached("sl:" + domain, lambda: storeleads_client._get_store(domain)) or {}


def build(rec):
    """Returns (row, properties_to_write). Fill-blanks-only."""
    p = rec["properties"]
    domain = bare_domain(p.get("domain") or "")
    row = {"id": rec["id"], "name": p.get("name") or "", "domain": domain,
           "shopify": "", "applied": "no", "skipped_because_set": ""}
    if not domain:
        return row, {}

    store = get_store(domain)
    platform = (store.get("platform") or "").lower()
    row["platform"] = platform
    props, skipped = {}, []

    if platform != "shopify":
        row["shopify"] = "no"
        # Correct an over-claim from the domain-repair pass: it set the
        # Marketplaces "Shopify" value whenever StoreLeads returned ANY store,
        # but StoreLeads also tracks WooCommerce, Magento, BigCommerce and
        # custom carts. Those companies are not on Shopify, and the enum has no
        # value for their platform, so the tag is simply removed.
        current = {v.strip() for v in (p.get("marketplaces") or "").split(";") if v.strip()}
        if "Shopify" in current:
            fixed = ";".join(sorted(current - {"Shopify"}))
            props["marketplaces"] = fixed
            row["marketplaces"] = fixed
            row["shopify_tag_removed"] = "yes"
        return row, props

    row["shopify"] = "yes"

    def put(field, value):
        """Write only when HubSpot has nothing there."""
        if value in ("", None):
            return
        if (p.get(field) or "").strip():
            skipped.append(field)
            return
        props[field] = value
        row[field] = value

    hist, apps = affiliate_verdict(store)
    row["affiliate_apps"] = "; ".join(apps)[:120]
    put("affiliate_history", hist)

    yearly = store.get("estimated_sales_yearly")
    monthly = store.get("estimated_sales")
    annual = (float(yearly) * 0.01) if yearly else (float(monthly) * 12 * 0.01 if monthly else 0)
    if annual:
        band = revenue_band(annual)
        row["revenue_band_label"] = BAND_LABELS[band]
        put("estimated_annual_revenue", band)
        put("annualrevenue", str(int(round(annual))))

    if store.get("employee_count"):
        put("numberofemployees", str(int(store["employee_count"])))

    put("industry", industry_for(store))
    put("city", (store.get("city") or "").strip())
    put("state", (store.get("administrative_area_level_1") or store.get("state_name") or "").strip()
        if (store.get("administrative_area_level_1") or "") != "Active" else "")
    put("country", (store.get("country_code") or "").strip())

    # StoreLeads records each sales channel's URL; use it to fill an Amazon
    # storefront the domain-repair pass could not find.
    amz = (store.get("sales_channel_urls") or {}).get("Amazon") or ""
    if amz and "amazon." in amz.lower():
        put("amazon_storefront_url", amz)

    row["skipped_because_set"] = ",".join(sorted(set(skipped)))
    return row, props


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-id", default=DEFAULT_LIST_ID)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    config.DRY_RUN = args.dry_run
    load_cache()

    ids = []
    after = None
    while True:
        params = {"limit": 250}
        if after:
            params["after"] = after
        r = hs.request_with_retry("GET", f"{BASE}/crm/v3/lists/{args.list_id}/memberships", params=params)
        body = r.json()
        ids += [str(x["recordId"]) for x in body.get("results", [])]
        after = (body.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break

    recs = []
    for batch in hs.chunked(ids, 100):
        r = hs.request_with_retry(
            "POST", f"{BASE}/crm/v3/objects/companies/batch/read",
            json={"properties": ENRICH_PROPS, "inputs": [{"id": i} for i in batch]})
        recs += r.json().get("results", [])
    log(f"{len(recs)} companies read from list {args.list_id}")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, out in enumerate(pool.map(build, recs), 1):
            results.append(out)
            if i % 40 == 0:
                log(f"  {i}/{len(recs)}")
                save_cache()
    save_cache()

    updates = [{"id": row["id"], "properties": props} for row, props in results if props]
    rows = [row for row, _ in results]
    shopify = sum(1 for r in rows if r["shopify"] == "yes")

    def count(f):
        return sum(1 for r in rows if r.get(f))

    log(f"\nShopify stores found        : {shopify}")
    log(f"  affiliate_history         : {count('affiliate_history')}")
    log(f"  estimated_annual_revenue  : {count('estimated_annual_revenue')}")
    log(f"  annualrevenue             : {count('annualrevenue')}")
    log(f"  numberofemployees         : {count('numberofemployees')}")
    log(f"  industry                  : {count('industry')}")
    log(f"  city                      : {count('city')}")
    log(f"  amazon_storefront_url     : {count('amazon_storefront_url')}")
    log(f"companies to update         : {len(updates)}")

    for row, props in results:
        if props and not args.dry_run:
            row["applied"] = "yes"
    write_csv(os.path.join(DATA_DIR, f"list{args.list_id}_storeleads.csv"), rows, FIELDS)
    log(f"CSV: data/list{args.list_id}_storeleads.csv")

    if args.dry_run:
        log("dry run -- nothing written to HubSpot")
        return
    if updates:
        hs.batch_update("companies", updates)
        log(f"applied {len(updates)} company updates")


if __name__ == "__main__":
    main()
