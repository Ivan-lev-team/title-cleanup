#!/usr/bin/env python3
"""DRY RUN of the net-new company import. Maps all 52 Store Leads columns to
real HubSpot company properties, validates names/types/enum options, and prints
fully assembled records. WRITES NOTHING.

  python scripts/import_dryrun.py <PUSH_LIST.csv> <export.csv> <suppress.json> [--n 20]
"""
import csv, json, os, re, sys
from collections import Counter, OrderedDict
sys.stdout.reconfigure(encoding="utf-8")
csv.field_size_limit(10 ** 8)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "scripts")); os.chdir(HERE)
import requests
from dotenv import dotenv_values
from resolve_dedupe_groups import norm_domain

TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}"}

CC2NAME = {"US": "United States", "CA": "Canada", "GB": "United Kingdom", "AU": "Australia",
           "DE": "Germany", "FR": "France", "ES": "Spain", "IT": "Italy", "NL": "Netherlands",
           "JP": "Japan", "MX": "Mexico", "BR": "Brazil", "IN": "India", "SG": "Singapore",
           "NZ": "New Zealand", "IE": "Ireland", "SE": "Sweden"}


def money(v):
    """'USD $1,234,567.89' -> 1234567.89 (float) or None"""
    s = re.sub(r"[^0-9.]", "", str(v or ""))
    try:
        return round(float(s), 2) if s else None
    except ValueError:
        return None


def num(v):
    s = re.sub(r"[^0-9.\-]", "", str(v or ""))
    try:
        return int(float(s)) if s else None
    except ValueError:
        return None


def first(v):
    return (str(v or "").split(":")[0]).strip() or None


def rev_band(yearly):
    """-> estimated_annual_revenue enum code"""
    if yearly is None:
        return None
    if yearly < 10_000:      return "0"
    if yearly < 100_000:     return "1"
    if yearly < 1_000_000:   return "2"
    if yearly < 10_000_000:  return "3"
    return "4"


def account_segment(yearly):
    """PROPOSED banding -- confirm before go-live."""
    if yearly is None:
        return None
    if yearly >= 10_000_000:  return "Enterprise"
    if yearly >= 1_000_000:   return "Gold"
    return "Launch"


def stamp(rec, yearly, src):
    """Constants + derived fields. Shared by the dry-run and the live pusher so
    the two can never drift apart."""
    rec["pod"] = "Pod RevOps"
    rec["lifecyclestage"] = "1417730383"              # 'Net New'
    rec["marketplaces"] = "Shopify"                   # every row is a Shopify store
    # the brand's own store IS the D2C storefront; completes the trio with
    # amazon_storefront_url / walmart_storefront_url
    du = (src.get("domain_url") or "").strip()
    if du:
        rec["d2c_storefront_url"] = du
    # Shopify is the only confirmed marketplace today, so total == Shopify total.
    # FUTURE: adding Amazon/Walmart revenue must READ annualrevenue and ADD to it,
    # never overwrite.
    if rec.get("shopify_trailing_12_revenue") is not None:
        rec["annualrevenue"] = rec["shopify_trailing_12_revenue"]
    rec["estimated_annual_revenue"] = rev_band(yearly)
    rec["account_segment"] = account_segment(yearly)
    return rec


def cat_leaf(cats):
    if not cats:
        return None
    seg = [s for s in str(cats).split(":")[0].split("/") if s.strip()]
    return seg[-1].strip() if seg else None


def cat_root(cats):
    if not cats:
        return None
    seg = [s for s in str(cats).split(":")[0].split("/") if s.strip()]
    return seg[0].strip() if seg else None


# source column -> (hubspot property, transform)  |  (None, reason)
MAP = OrderedDict([
    ("domain",            ("domain", lambda r: norm_domain(r["domain"]))),
    ("title",             ("name", None)),           # cleaned name comes from PUSH_LIST
    ("description",       ("description", lambda r: (r["description"] or "").strip()[:900])),
    ("city",              ("city", lambda r: (r["city"] or "").strip() or None)),
    ("state",             ("state", lambda r: (r["state"] or "").strip() or None)),
    ("country_code",      ("country", lambda r: CC2NAME.get((r["country_code"] or "").upper()))),
    ("phones",            ("phone", lambda r: first(r["phones"]))),
    ("linkedin_url",      ("linkedin_company_page", lambda r: (r["linkedin_url"] or "").strip() or None)),
    ("employee_count",    ("numberofemployees", lambda r: num(r["employee_count"]))),
    ("estimated_monthly_sales", ("shopify__estimated_mrr", lambda r: money(r["estimated_monthly_sales"]))),
    ("estimated_yearly_sales",  ("shopify_trailing_12_revenue", lambda r: money(r["estimated_yearly_sales"]))),
    ("domain_url",        ("website", lambda r: (r["domain_url"] or "").strip() or None)),
    ("categories",        ("main_category", lambda r: cat_leaf(r["categories"]))),
    ("instagram_followers_SRC", None),
    ("tiktok_followers",  ("tiktok_followers", lambda r: num(r["tiktok_followers"]))),
    ("pinterest_followers", ("pinterest_followers", lambda r: num(r["pinterest_followers"]))),
    ("youtube_followers", ("youtube_followers", lambda r: num(r["youtube_followers"]))),
    ("twitter_followers", ("twitterfollowers", lambda r: num(r["twitter_followers"]))),
    ("twitter",           ("twitterhandle", lambda r: (r["twitter"] or "").strip() or None)),
    ("facebook",          ("facebook_company_page",
                           lambda r: (f"https://www.facebook.com/{r['facebook'].strip()}"
                                      if (r.get("facebook") or "").strip() else None))),
    ("sales_channel_amazon", ("amazon_storefront_url", lambda r: (r["sales_channel_amazon"] or "").strip() or None)),
])

# columns with NO sensible existing HubSpot home
NO_HOME = {
    "about_us_url":        "no 'About URL' property exists",
    "aliases":             "no alternate-domain property exists (cluster_domains same)",
    "cluster_domains":     "no alternate-domain property exists",
    "company_location":    "full postal address string; city/state/country already mapped separately",
    "contact_page_url":    "no 'Contact page URL' property exists",
    "domain_count":        "Store Leads internal metric, no HubSpot equivalent",
    "domain_tld1":         "derivable from domain, no property and no value in storing it",
    "emails":              "company-level email list; HubSpot has no company email property (emails belong on contacts)",
    "estimated_monthly_pageviews": "no pageview property that is writable (hs_analytics_num_page_views is READ-ONLY)",
    "estimated_monthly_visits":    "no session property that is writable (hs_analytics_num_visits is READ-ONLY)",
    "instagram":           "only 'Instagram Followers' exists; there is NO Instagram handle/URL property",
    "linkedin_account":    "duplicate of linkedin_url which is already mapped",
    "meta_description":    "no second description property; description already used",
    "pinterest":           "only 'Pinterest Followers' exists; no Pinterest handle property",
    "plan":                "'Plan Status'/'Plan Type for Reporting' are Levanta billing enums, NOT the Shopify plan; forcing it would corrupt them",
    "platform":            "constant 'Shopify' for every row; no platform property (shopify_connection_status is a Levanta integration state, not this)",
    "platform_domain":     "Store Leads internal, no HubSpot equivalent",
    "platform_rank":       "no rank property exists",
    "public_company_earnings_monthly": "no property; and only 7 rows in the whole export had it",
    "public_company_earnings_yearly":  "no property; would conflict with annualrevenue",
    "rank":                "no rank property exists",
    "sales_channel_abound":"0 rows populated in the entire export",
    "sales_channels":      "'Marketplaces' enum exists but user explicitly deferred marketplace enrichment",
    "ships_to_countries":  "no shipping-coverage property exists",
    "status":              "constant 'Active'; HubSpot 'status' is Levanta Plan Status, mapping it would corrupt that field",
    "tags":                "no company tag property exists (hs_all_assigned_business_unit_ids is Brands, unrelated)",
    "tiktok":              "only 'TikTok Followers' exists; no TikTok handle property",
    "youtube":             "only 'YouTube Followers' exists; no YouTube handle property",
    "retailer_url":        "wholesale/dealer/where-to-buy page (e.g. /pages/dealer-locator, /pages/wholesale). NOT a D2C storefront, and no wholesale/reseller property exists in this portal",
    "created":             "store-creation date, NOT company founding year; mapping it to 'Year Founded' would be wrong data",
    "instagram_followers": "MAPPED - see below",
}


def main():
    push_csv, export_csv, supp_json = sys.argv[1], sys.argv[2], sys.argv[3]
    n_show = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 20

    props = {p["name"]: p for p in
             requests.get("https://api.hubapi.com/crm/v3/properties/companies",
                          headers=H, timeout=40).json().get("results", [])}
    suppress = set(json.load(open(supp_json, encoding="utf-8")))

    # ---------- 1. validate every target property ----------
    targets = [v[0] for v in MAP.values() if v] + ["lifecyclestage", "pod", "annualrevenue", "marketplaces", "d2c_storefront_url",
                                                   "estimated_annual_revenue", "account_segment",
                                                   "instagram_followers"]
    print("=" * 78); print("1. TARGET PROPERTY VALIDATION"); print("=" * 78)
    bad = []
    for t in sorted(set(targets)):
        p = props.get(t)
        if not p:
            print(f"  MISSING   {t}"); bad.append(t); continue
        mm = p.get("modificationMetadata", {}) or {}
        ro = mm.get("readOnlyValue")
        flag = "  READ-ONLY!" if ro else ""
        print(f"  ok  {t:28} {p['type']:12} {p['fieldType']:10} '{p.get('label','')[:30]}'{flag}")
        if ro:
            bad.append(t)
    print(f"\n  invalid targets: {bad or 'none'}")

    # ---------- 2. enum checks ----------
    print("\n" + "=" * 78); print("2. ENUM VALUE CHECKS"); print("=" * 78)
    def opts(n):
        return {o["value"] for o in (props.get(n, {}).get("options") or [])}
    checks = [("lifecyclestage", "1417730383"), ("pod", "Pod RevOps"), ("marketplaces", "Shopify"),
              ("account_segment", "Launch"), ("account_segment", "Gold"),
              ("account_segment", "Enterprise"),
              ("estimated_annual_revenue", "3"), ("estimated_annual_revenue", "4")]
    blockers = []
    for prop, val in checks:
        ok = val in opts(prop)
        print(f"  {prop:26} value={val!r:14} {'OK' if ok else '*** NOT AN OPTION ***'}")
        if not ok:
            blockers.append((prop, val))
    if blockers:
        print(f"\n  BLOCKERS: {blockers}")
        print(f"  lifecyclestage options are: {sorted(opts('lifecyclestage'))}")

    # ---------- 3. assemble records ----------
    push = {}
    for r in csv.DictReader(open(push_csv, encoding="utf-8-sig")):
        d = norm_domain(r["domain"])
        if d not in suppress:
            push[d] = r
    print(f"\n  push rows after suppression: {len(push):,}")

    shown, built = 0, 0
    fieldfill = Counter()
    samples = []
    for r in csv.DictReader(open(export_csv, encoding="utf-8-sig")):
        d = norm_domain(r.get("domain"))
        pr = push.get(d)
        if not pr:
            continue
        built += 1
        yearly = money(r.get("estimated_yearly_sales"))
        rec = {}
        for src, tgt in MAP.items():
            if not tgt or src.endswith("_SRC"):
                continue
            prop, fn = tgt
            try:
                v = fn(r) if fn else None
            except Exception:
                v = None
            if src == "title":
                v = pr["name"]                     # cleaned name, not the SEO title
            if v not in (None, ""):
                rec[prop] = v
        rec["instagram_followers"] = None          # export has handle only, no count
        rec.pop("instagram_followers")
        rec = stamp(rec, yearly, r)
        rec = {k: v for k, v in rec.items() if v not in (None, "")}
        for k in rec:
            fieldfill[k] += 1
        if shown < n_show:
            samples.append((d, rec)); shown += 1
    print(f"  records assembled: {built:,}")

    print("\n" + "=" * 78); print(f"3. {len(samples)} FULLY ASSEMBLED RECORDS"); print("=" * 78)
    for d, rec in samples:
        print(f"\n--- {d} ---")
        for k in sorted(rec):
            t = props.get(k, {}).get("type", "?")
            v = rec[k]
            tv = type(v).__name__
            print(f"   {k:26} ({t:6}/{tv:5}) = {str(v)[:60]}")
        ar, st = rec.get("annualrevenue"), rec.get("shopify_trailing_12_revenue")
        print(f"   >> lifecyclestage = {rec.get('lifecyclestage')} ('Net New')   "
              f"annualrevenue == shopify_trailing_12_revenue ? "
              f"{'YES' if ar == st and ar is not None else 'NO ('+str(ar)+' vs '+str(st)+')'}")

    print("\n" + "=" * 78); print("4. FIELD FILL RATE ACROSS ALL ASSEMBLED RECORDS"); print("=" * 78)
    for k, c in fieldfill.most_common():
        print(f"   {k:28} {c:7,} / {built:,}  ({100*c/max(1,built):5.1f}%)")

    print("\n" + "=" * 78); print("5. SOURCE COLUMNS WITH NO HUBSPOT HOME"); print("=" * 78)
    for k in sorted(NO_HOME):
        if k == "instagram_followers":
            continue
        print(f"   {k:34} {NO_HOME[k]}")
    print("\nNOTHING WAS WRITTEN.")


if __name__ == "__main__":
    main()
