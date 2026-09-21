"""
Repair the domain / storefront fields on a HubSpot company list (default: list
7565, 291 companies).

A prior enrichment pass left this list in several broken states:
  - domain = "amazon.com" (the Amazon storefront landed in the domain field)
  - domain empty (mostly cryptic Amazon-only sellers)
  - amazon_storefront_url = the literal string "True" (~120 records)
  - website = the literal string "True"
  - Walmart / Etsy URLs sitting in amazon_storefront_url
  - one generic Amazon store page reused across unrelated brands
  - a domain that is populated but simply wrong (PhysiciansCare ->
    asrhealthbenefits.com, Nevlers -> nevlers.mockins.com)

Flow (see also ENRICHMENT_PLAYBOOK.md):
  1  pull      -- list memberships + batch-read, cached to data/list7565_raw.json
  2  sanitize  -- deterministic rules, no provider spend
  3  resolve   -- ZenRows-first waterfall for the real corporate domain
  4  verify    -- Anthropic gate; NOTHING is written unverified
  5  presence  -- Shopify/Walmart/Amazon presence + a note for no-site brands
  6  apply     -- auto-write high-confidence results, CSV for the rest

Auto mode is the default: deterministic cleanups and MATCH verdicts at
confidence >= MIN_CONFIDENCE are written straight to HubSpot; everything else
is held back in data/list7565_review.csv for a human.

Run from the repo root:
    python scripts/fix_list_domains.py --dry-run --limit 20
    python scripts/fix_list_domains.py
"""
import argparse
import collections
import csv
import json
import os
import re
import sys
import itertools
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
import requests

import config
import hubspot_client as hs
import storeleads_client
import zenrows_client
from icp_prompt import parse_verdict

BASE = config.HUBSPOT_BASE
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

DEFAULT_LIST_ID = "7565"
COMPANY_PROPS = [
    "name", "domain", "website", "amazon_storefront_url", "walmart_storefront_url",
    "marketplaces", "phone", "country", "main_category", "num_associated_contacts",
]

FREEMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "aol.com", "live.com", "msn.com", "protonmail.com", "gmx.com",
}

CLAUDE_SEARCH_MODEL = "claude-sonnet-5"

# Verdict >= this confidence is auto-applied; below it the row is held for review.
MIN_CONFIDENCE = 80
# Paid verifier calls allowed per company. The waterfall is ordered best-first,
# so if six candidates have all failed, a seventh is very unlikely to be the
# brand's real site -- capping here is the single biggest cost control on the
# hard tail of unresolvable Amazon-only sellers.
MAX_VERIFY_PER_COMPANY = 6

# Runtime switches (set from CLI flags in main()).
# LLM_SEARCH is OFF by default: a Claude web_search call measured 18.4s per
# company and was 93% of per-company latency, while finding candidate domains is
# a search-engine job that ZenRows does in 1.4s. The functions are kept for the
# hard tail -- re-run just the unresolved records with --llm-search.
LLM_SEARCH = False
# DEEP trades speed for recall on the hard tail: escalate to js_render (many big
# corporate sites return a content-free shell without it), query every search
# engine instead of stopping at the first, and allow more verifier attempts.
DEEP = False
# DOMAINS_ONLY skips the StoreLeads Shopify probe and the Walmart search, which
# exist only to populate `marketplaces` and have no bearing on finding a domain.
DOMAINS_ONLY = False
VERIFY_MODEL = "claude-sonnet-5"
NOTE_TO_COMPANY_ASSOC = 190

# Values a previous import wrote as a stringified boolean rather than a real value.
JUNK_VALUES = {"true", "false", "none", "null", "n/a", "na", "-", "#n/a"}

# Amazon's own Seattle corporate number. Its presence is a reliable marker that
# the record came from the bad enrichment pass, not from the brand itself.
AMAZON_CORP_PHONE = "+12062661000"

MARKETPLACE_DOMAINS = {
    "amazon.com", "amazon.ca", "amazon.co.uk", "amazon.de", "amazon.fr",
    "walmart.com", "etsy.com", "ebay.com", "target.com", "alibaba.com",
    "aliexpress.com", "wayfair.com", "costco.com", "homedepot.com", "lowes.com",
}
# Hosts that are never a brand's corporate domain -- excluded from candidates.
NON_BRAND_HOSTS = MARKETPLACE_DOMAINS | {
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "tiktok.com", "pinterest.com", "reddit.com", "wikipedia.org",
    "yelp.com", "bbb.org", "crunchbase.com", "bloomberg.com", "zoominfo.com",
    "glassdoor.com", "indeed.com", "trustpilot.com", "duckduckgo.com",
    "google.com", "bing.com", "apple.com", "play.google.com", "sellercentral.amazon.com",
    # Boilerplate hosts that appear in every HTML document (XML namespaces,
    # schema markup, CDNs, fonts) and are never a brand's own domain.
    "w3.org", "schema.org", "gstatic.com", "googleapis.com", "googletagmanager.com",
    "google-analytics.com", "cloudflare.com", "cloudfront.net", "jsdelivr.net",
    "unpkg.com", "bootstrapcdn.com", "jquery.com", "fontawesome.com", "shopify.com",
    "cdn.shopify.com", "wp.com", "wordpress.org", "gravatar.com", "mozilla.org",
    "purl.org", "ogp.me", "creativecommons.org", "adobe.com", "microsoft.com",
    "gov", "archive.org", "doubleclick.net", "klaviyo.com", "hubspot.com",
    "shop.app", "shopifycdn.com", "myshopify.com", "squarespace.com", "wix.com",
    "bigcommerce.com", "godaddy.com", "namecheap.com", "sedo.com", "afternic.com",
    # Aggregators, directories, manual dumps and review farms. These rank well
    # for obscure brand names and, left in, they burn the per-company candidate
    # budget so the brand's actual domain never gets evaluated.
    "manuals.plus", "manualslib.com", "manualsonline.com", "manualzz.com",
    "cherrypicksreviews.com", "yellowpages.com", "cufonfonts.com", "alura.io",
    "knoji.com", "similarweb.com", "owler.com", "dnb.com", "apollo.io",
    "rocketreach.co", "leadiq.com", "signalhire.com", "zoominfo.com", "lusha.com",
    "producthunt.com", "productreview.com.au", "influenster.com", "fakespot.com",
    "camelcamelcamel.com", "keepa.com", "junglescout.com", "helium10.com",
    "sellerapp.com", "amzscout.net", "slickdeals.net", "retailmenot.com",
    "coupons.com", "honey.com", "capterra.com", "g2.com", "glassdoor.co.uk",
    "issuu.com", "scribd.com", "slideshare.net", "pinterest.co.uk", "quora.com",
    "tripadvisor.com", "ebay.co.uk", "mercadolibre.com", "shopee.com",
    # Bing SERP chrome and regional classifieds that crowd out real candidates.
    "live.com", "msn.com", "bing.net", "virtualearth.net", "windows.net",
    "olx.com", "olx.com.br", "aliexpress.us", "temu.com", "wish.com",
    # More manual-dump and unrelated-service hosts seen ranking for cryptic
    # brand names on list 7694 (the verifier rejected all of them correctly --
    # blocking them just stops paying a verify call to find that out).
    "manuals.ca", "manualsdir.com", "manua.ls", "manualowl.com",
    "ppy.sh", "thekitchn.com", "yumpu.com", "calameo.com", "docplayer.net",
}

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
_cache_lock = threading.Lock()
_chrome_lock = threading.Lock()
_spend_lock = threading.Lock()
# Live count of PAID provider calls actually made (cache hits are free and are
# deliberately not counted). Reported at the end of every run so credit burn is
# visible rather than inferred.
SPEND = collections.Counter()


def charge(kind, n=1):
    with _spend_lock:
        SPEND[kind] += n
_print_lock = threading.Lock()
_cache = {}


def log(msg):
    with _print_lock:
        print(msg, flush=True)


# ------------------------------------------------------------------ caching --

def _cache_path():
    return os.path.join(DATA_DIR, "list_domains_cache.json")


def load_cache():
    global _cache
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            _cache = json.load(f)
    except (OSError, ValueError):
        _cache = {}


def save_cache():
    os.makedirs(DATA_DIR, exist_ok=True)
    with _cache_lock:
        snapshot = dict(_cache)
    with open(_cache_path(), "w", encoding="utf-8") as f:
        json.dump(snapshot, f)


def cached(key, producer):
    """Memoize an expensive provider call across runs. Provider misses are cached
    too -- a miss is a real answer and re-asking costs the same credits."""
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    value = producer()
    with _cache_lock:
        _cache[key] = value
    return value


# ------------------------------------------------------- stage 1: pull list --

def fetch_list_members(list_id):
    """All record IDs in an ILS list, following the paging cursor."""
    ids, after = [], None
    while True:
        params = {"limit": 250}
        if after:
            params["after"] = after
        resp = hs.request_with_retry(
            "GET", f"{BASE}/crm/v3/lists/{list_id}/memberships", params=params
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"list fetch failed: {resp.status_code} {resp.text[:500]}")
        body = resp.json()
        ids.extend(str(r["recordId"]) for r in body.get("results", []))
        after = (body.get("paging") or {}).get("next", {}).get("after")
        if not after:
            return ids


def fetch_companies(ids):
    out = []
    for batch in hs.chunked(ids, 100):
        resp = hs.request_with_retry(
            "POST", f"{BASE}/crm/v3/objects/companies/batch/read",
            json={"properties": COMPANY_PROPS, "inputs": [{"id": i} for i in batch]},
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"batch read failed: {resp.status_code} {resp.text[:500]}")
        out.extend(resp.json().get("results", []))
    return out


def pull(list_id, refresh=False):
    path = os.path.join(DATA_DIR, f"list{list_id}_raw.json")
    if os.path.exists(path) and not refresh:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    ids = fetch_list_members(list_id)
    log(f"list {list_id}: {len(ids)} members")
    companies = fetch_companies(ids)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(companies, f, indent=1)
    return companies


# ------------------------------------------------------ stage 2: sanitize ----

def is_junk(value):
    return (value or "").strip().lower() in JUNK_VALUES


def bare_domain(value):
    """'https://www.Foo.com/bar?x=1' -> 'foo.com'. Empty string when unusable."""
    v = (value or "").strip().lower()
    if not v or is_junk(v):
        return ""
    v = re.sub(r"^[a-z]+://", "", v)
    v = v.split("/")[0].split("?")[0].split("#")[0].split("@")[-1]
    if v.startswith("www."):
        v = v[4:]
    # A bare domain needs a dot and no spaces; anything else is a junk value.
    return v if ("." in v and " " not in v and not v.endswith(".")) else ""


def is_marketplace_domain(domain):
    d = bare_domain(domain)
    return bool(d) and any(d == m or d.endswith("." + m) for m in MARKETPLACE_DOMAINS)


def storefront_page_id(url):
    """The GUID identifying an Amazon brand-store page, used to spot the same
    generic store page being reused across unrelated companies."""
    m = re.search(r"/page/([0-9A-Fa-f-]{20,})", url or "")
    return m.group(1).upper() if m else ""


def sanitize(companies):
    """Deterministic field repair. Returns {id: {...record...}} where `plan`
    holds property writes that need no research and can always be applied."""
    # Find storefront page IDs claimed by more than one company -- these are
    # generic/placeholder pages, not any one brand's store.
    page_owners = {}
    for c in companies:
        pid = storefront_page_id((c.get("properties") or {}).get("amazon_storefront_url") or "")
        if pid:
            page_owners.setdefault(pid, set()).add(c["id"])
    shared_pages = {pid for pid, owners in page_owners.items() if len(owners) > 1}
    if shared_pages:
        log(f"shared/placeholder storefront pages detected: {len(shared_pages)}")

    records = {}
    for c in companies:
        p = c.get("properties") or {}
        rec = {
            "id": c["id"],
            "name": (p.get("name") or "").strip(),
            "orig_domain": (p.get("domain") or "").strip(),
            "orig_amazon": (p.get("amazon_storefront_url") or "").strip(),
            "orig_walmart": (p.get("walmart_storefront_url") or "").strip(),
            "orig_website": (p.get("website") or "").strip(),
            "orig_marketplaces": (p.get("marketplaces") or "").strip(),
            "phone": (p.get("phone") or "").strip(),
            "country": (p.get("country") or "").strip(),
            "main_category": (p.get("main_category") or "").strip(),
            "num_associated_contacts": int(p.get("num_associated_contacts") or 0),
            "plan": {},
            "flags": [],
            "verdict": "",
            "confidence": 0,
            "source": "",
            "candidate": "",
            "note": "",
        }

        amazon, walmart = rec["orig_amazon"], rec["orig_walmart"]

        # amazon_storefront_url holding something that is not an Amazon URL.
        if is_junk(amazon):
            rec["plan"]["amazon_storefront_url"] = ""
            rec["flags"].append("cleared junk amazon_storefront_url")
            amazon = ""
        elif amazon and "walmart.com" in amazon.lower():
            if not walmart or is_junk(walmart):
                rec["plan"]["walmart_storefront_url"] = amazon
                walmart = amazon
            rec["plan"]["amazon_storefront_url"] = ""
            rec["flags"].append("moved Walmart URL out of amazon_storefront_url")
            amazon = ""
        elif amazon and "amazon." not in amazon.lower():
            rec["plan"]["amazon_storefront_url"] = ""
            rec["flags"].append(f"cleared non-Amazon URL from amazon_storefront_url ({amazon[:60]})")
            amazon = ""
        elif amazon and storefront_page_id(amazon) in shared_pages:
            rec["plan"]["amazon_storefront_url"] = ""
            rec["flags"].append("cleared storefront page shared across multiple companies")
            amazon = ""

        if is_junk(walmart):
            rec["plan"]["walmart_storefront_url"] = ""
            rec["flags"].append("cleared junk walmart_storefront_url")
            walmart = ""

        if is_junk(rec["orig_website"]):
            rec["plan"]["website"] = ""
            rec["flags"].append("cleared junk website")

        # domain holding a marketplace instead of the brand's own site.
        domain = bare_domain(rec["orig_domain"])
        if rec["orig_domain"] and not domain:
            rec["flags"].append(f"unusable domain value ({rec['orig_domain'][:60]})")
        if domain and is_marketplace_domain(domain):
            rec["flags"].append(f"domain was a marketplace ({domain})")
            # Preserve a real storefront path that only survived in the domain field.
            if not amazon and "amazon." in rec["orig_domain"].lower() and "/stores/" in rec["orig_domain"].lower():
                rec["plan"]["amazon_storefront_url"] = rec["orig_domain"]
                amazon = rec["orig_domain"]
            domain = ""
            rec["plan"]["domain"] = ""
        elif domain and domain != rec["orig_domain"].strip().lower():
            # Normalize www./protocol/path noise even when the domain itself is fine.
            rec["plan"]["domain"] = domain

        if rec["phone"].replace(" ", "") == AMAZON_CORP_PHONE:
            rec["flags"].append("phone is Amazon corporate (bad-enrichment marker)")

        rec["amazon"], rec["walmart"], rec["domain"] = amazon, walmart, domain
        records[c["id"]] = rec
    return records


# ------------------------------------------- stage 3: resolve the domain -----

_HOST_RE = re.compile(r"https?://([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")


def _slug(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def storefront_text(url):
    """Visible text of an Amazon brand store -- the strongest signal for cryptic
    seller names, since the store page carries the real brand name and its
    product categories."""
    if not url:
        return ""
    def run():
        charge("zenrows_storefront")
        return zenrows_client._html_to_text(fetch_bounded(url))[:6000]

    return cached("sf:" + url, run)


# Amazon brand-store pages are mostly Amazon's own shell -- department menus,
# keyboard-shortcut help, delivery notices. Measured over 574 cached storefronts,
# 303 vocabulary words appear in >70% of them. Those words are chrome, not
# product signal, and feeding them to verify() as "storefront text" meant the
# verifier was comparing candidate homepages against Amazon's navigation menu.
# The chrome set is derived from the storefronts actually seen this run rather
# than hard-coded, so it tracks whatever Amazon's shell looks like today.
_CHROME_DOC_RATIO = 0.7
_MIN_PRODUCT_TERMS = 8
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z&'-]{2,}")
_chrome_vocab = None


def _build_chrome_vocab():
    """Words common to most cached storefronts. Needs a decent sample to be
    meaningful; below that, return an empty set so nothing is stripped."""
    with _cache_lock:
        texts = [v for k, v in _cache.items() if k.startswith("sf:") and v]
    if len(texts) < 15:
        return set()
    doc_freq = collections.Counter()
    for text in texts:
        doc_freq.update({w.lower() for w in _WORD_RE.findall(text)})
    cutoff = len(texts) * _CHROME_DOC_RATIO
    return {w for w, n in doc_freq.items() if n > cutoff}


def chrome_vocab():
    global _chrome_vocab
    if _chrome_vocab is None:
        with _chrome_lock:
            if _chrome_vocab is None:
                _chrome_vocab = _build_chrome_vocab()
                log(f"chrome vocabulary: {len(_chrome_vocab)} words")
    return _chrome_vocab


def storefront_products(url, raw=None):
    """Product terms from an Amazon brand store, with Amazon's chrome removed.

    This is what makes Chris's manual method reproducible ("looked at the amazon
    account, saw a product, then googled the brand name and product") and it is
    also the corrected product context for verify(). Falls back to the raw text
    when too little survives -- a noisy signal beats none."""
    text = raw if raw is not None else storefront_text(url)
    if not text:
        return ""
    chrome = chrome_vocab()
    terms = [w for w in _WORD_RE.findall(text) if w.lower() not in chrome]
    if len(terms) < _MIN_PRODUCT_TERMS:
        return text[:400]
    return " ".join(dict.fromkeys(terms))[:400]


# Search engines, in the order they are tried. Measured on the hard cryptic
# brands from list 7565: Bing and DuckDuckGo return near-identical candidates
# (identical for WKONCLDY), and Bing is *worse* on some (DECYOOL -> Brazilian
# classifieds instead of decool.store). So Bing is a FALLBACK, not a second
# fetch on every company -- querying both everywhere would double ZenRows calls
# for almost no new candidates. Google is not usable: it returns 0 bytes through
# ZenRows (blocked), verified before this was written.
SEARCH_ENGINES = (
    ("ddg", "https://html.duckduckgo.com/html/?q="),
    ("bing", "https://www.bing.com/search?q="),
)
MIN_SERP_CANDIDATES = 2


def _scrape_serp(engine_url, query):
    """One SERP scrape -> ordered candidate hosts. Cached per engine+query."""

    def run():
        charge("zenrows_search")
        html = fetch_bounded(engine_url + urllib.parse.quote(query))
        if not html:
            return []
        text = urllib.parse.unquote(html)
        hosts, seen = [], set()
        for m in _HOST_RE.finditer(text):
            host = bare_domain(m.group(1))
            if not host or host in seen:
                continue
            seen.add(host)
            if any(host == b or host.endswith("." + b) for b in NON_BRAND_HOSTS):
                continue
            hosts.append(host)
        return hosts[:8]

    # Deliberately NOT cached on empty: an empty SERP is nearly always a throttle,
    # not "this brand has no results". Caching empties here poisoned 1004 of 1081
    # cached queries during a throttled run -- LG could never generate a candidate
    # again because `serp:...|LG official site` was permanently []. Same rule as
    # homepage_text; it must hold on EVERY fetch path, not just one.
    key = f"serp:{engine_url}|{query}"
    with _cache_lock:
        hit = _cache.get(key)
    if hit:
        return hit
    value = run()
    if value:
        with _cache_lock:
            _cache[key] = value
    return value


def search_candidates(brand, extra=""):
    """Harvest candidate brand domains from search engines via ZenRows.

    `extra` grounds the query in something specific to this brand -- its product
    terms or its CRM category -- which is what disambiguates a cryptic seller
    name that means nothing on its own. This is Chris's manual method
    ("googled the brand name and product") and it is what surfaces
    wkoncldystore.com, sewantausa.com and cornstick.com, all of which the
    brand-name-only query missed.

    Engines are tried in order and the loop stops as soon as one returns enough
    candidates, so the second engine costs nothing on the common path.
    """
    query = f"{brand} {extra} official site".strip()
    found, seen = [], set()
    for _name, url in SEARCH_ENGINES:
        for host in _scrape_serp(url, query):
            if host not in seen:
                seen.add(host)
                found.append(host)
        if not DEEP and len(found) >= MIN_SERP_CANDIDATES:
            break
    return found[:8]


def guess_candidates(brand):
    """Slug guesses, DNS-gated later so dead ones cost nothing.

    The length floor is 2, not 3: a `len < 3` cutoff meant a two-letter brand
    ("LG", "ON", "KS") produced no guesses at all and so never tried the obvious
    apex. Below 2 characters a slug is too generic to be worth probing.
    """
    s = _slug(brand)
    if len(s) < 2:
        return []
    guesses = [f"{s}.com", f"{s}usa.com", f"the{s}.com", f"shop{s}.com", f"{s}.co", f"get{s}.com"]
    # A very short slug makes the decorated variants meaningless ("shoplg.com");
    # the apex and the .co are the only ones worth a probe.
    return guesses if len(s) >= 4 else [f"{s}.com", f"{s}usa.com", f"{s}.co"]


# zenrows_client._fetch escalates through 3 proxy variants at a 70s timeout each,
# so one unreachable domain can block a worker for 210s -- and wrapping it in a
# retry made that 420s. At 12 candidates per company that is over an hour for a
# single stubborn record, which is what stalled the first 7694 run at company 12.
#
# For bulk candidate probing we want a *fast answer*, not a guaranteed one: a real
# brand homepage responds quickly, and the exotic premium+JS tier rarely rescues a
# candidate that already failed twice. One plain attempt, then one premium attempt,
# both short. Worst case ~40s per candidate instead of 420s.
FETCH_TIMEOUT_PLAIN = 15
FETCH_TIMEOUT_PREMIUM = 25


# ZenRows answers 429 (and sometimes 422) when too many requests are in flight.
# Those are BACK-OFF-AND-RETRY signals, not "this page is empty" -- the original
# zenrows_client._fetch slept and retried on them. An earlier version of this
# function dropped that retry, and a 565-company run at 14 workers came back with
# 492 "no site found" verdicts for pages that were merely throttled. Never treat a
# throttle as a verdict.
FETCH_TIMEOUT_PLAIN = 15
FETCH_TIMEOUT_PREMIUM = 25
FETCH_MAX_ATTEMPTS = 4
THROTTLE_CODES = (422, 429)


def fetch_bounded(url):
    """Time-bounded ZenRows GET with throttle backoff.

    Returns HTML, or "" only after genuinely exhausting retries. Deliberately does
    NOT escalate to js_render -- that tier is slow and rarely rescues a page that
    already failed twice.
    """
    if not config.ZENROWS_KEY:
        return ""
    plan = [
        ({}, FETCH_TIMEOUT_PLAIN),
        ({"premium_proxy": "true"}, FETCH_TIMEOUT_PREMIUM),
    ]
    if DEEP:
        # Anti-bot corporate sites (LG, Medline, Kilz, Koss) serve a JS shell with
        # almost no extractable text until it is rendered.
        plan.append(({"premium_proxy": "true", "js_render": "true"}, 40))
    backoff = 2.0
    for attempt in range(FETCH_MAX_ATTEMPTS):
        extra, timeout = plan[min(attempt, len(plan) - 1)]
        try:
            r = requests.get(
                zenrows_client.ZENROWS_URL,
                params={"apikey": config.ZENROWS_KEY, "url": url, **extra},
                timeout=timeout,
            )
        except requests.RequestException:
            time.sleep(backoff)
            backoff *= 2
            continue
        if r.status_code < 300 and r.text:
            return r.text
        if r.status_code in THROTTLE_CODES or r.status_code >= 500:
            charge("zenrows_throttled")
            wait = r.headers.get("Retry-After")
            time.sleep(float(wait) if (wait or "").replace(".", "").isdigit() else backoff)
            backoff *= 2
            continue
        return ""  # a real 4xx for this URL -- retrying will not help
    return ""


def resolves(domain):
    """DNS check, milliseconds, before spending a ZenRows fetch on a domain.

    This is the difference between a 5-minute run and an hour-long one:
    zenrows_client._fetch escalates through 3 proxy variants at a 70s timeout
    each, so one dead domain costs minutes. Most slug guesses (shopfoo.com,
    getfoo.com) don't exist at all, and DNS rejects them instantly."""
    import socket

    def run():
        try:
            socket.getaddrinfo(domain, 443)
            return True
        except (socket.gaierror, UnicodeError, OSError):
            return False

    return cached("dns:" + domain, run)


def homepage_text(domain):
    """Fetch a candidate domain's homepage through ZenRows. '' means dead or
    unreachable, which disqualifies the candidate before it costs a model call."""
    if not domain or not resolves(domain):
        return ""

    def run():
        charge("zenrows_homepage")
        html = fetch_bounded(f"https://{domain}")
        return zenrows_client._html_to_text(html)[:6000] if html else ""

    # Deliberately NOT cached on miss: a transient ZenRows failure would
    # otherwise be remembered forever and permanently disqualify a live domain
    # (this is what dropped labcharge.com on the second validation run).
    key = "home:" + domain
    with _cache_lock:
        hit = _cache.get(key)
    if hit:
        return hit
    value = run()
    if value:
        with _cache_lock:
            _cache[key] = value
    return value


def shopify_store(brand, domain=""):
    """StoreLeads lookup -- doubles as the Shopify-presence check."""
    if not config.STORELEADS_KEY:
        return {}
    if domain:
        return cached("sl:" + domain, lambda: storeleads_client._get_store(domain)) or {}
    return {}


def walmart_storefront(brand):
    """Search Walmart via ZenRows for a brand page. Returns the URL or ''."""
    if not brand:
        return ""

    def run():
        target = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(
            f"site:walmart.com {brand} brand"
        )
        html = zenrows_client._fetch(target)
        if not html:
            return ""
        text = urllib.parse.unquote(html)
        for m in re.finditer(r"https?://www\.walmart\.com/(brand|browse)/[A-Za-z0-9\-/_]+", text):
            return m.group(0)
        return ""

    return cached("wm:" + brand, run)


def contact_email_domain(company_id):
    """The domain of an associated contact's corporate (non-freemail) email --
    the strongest possible signal per the enrichment playbook's domain policy,
    and free (no provider spend). Cached on miss too, same rationale as other
    lookups: a company with only freemail/no-email contacts stays that way."""

    def run():
        resp = hs.request_with_retry(
            "GET", f"{BASE}/crm/v4/objects/companies/{company_id}/associations/contacts"
        )
        if resp.status_code >= 300:
            return ""
        ids = [r["toObjectId"] for r in resp.json().get("results", [])][:10]
        if not ids:
            return ""
        contacts = hs.request_with_retry(
            "POST", f"{BASE}/crm/v3/objects/contacts/batch/read",
            json={"properties": ["email"], "inputs": [{"id": i} for i in ids]},
        )
        if contacts.status_code >= 300:
            return ""
        for c in contacts.json().get("results", []):
            email = (c.get("properties") or {}).get("email") or ""
            domain = bare_domain(email.split("@")[-1]) if "@" in email else ""
            if domain and domain not in FREEMAIL_DOMAINS:
                return domain
        return ""

    return cached("contact-email:" + str(company_id), run)


CLAUDE_SEARCH_PROMPT = """Find the official corporate website domain for this physical
consumer-product brand. It sells on Amazon and/or other marketplaces -- do NOT return
a marketplace, reseller, distributor, or review-site domain, only the brand's own site.
If you cannot find a genuine official site, say so.

Brand name (as recorded in the CRM): {brand}
{category_line}
{marketplace_line}

Search the web if needed, then respond with ONLY a JSON object, no prose:
{{"candidates": ["<bare domain, no scheme, e.g. example.com>", ...up to 3, best first]}}
Return an empty list if no plausible official site exists."""


def claude_search_candidates(rec):
    """One Claude + web_search round trip per company -- no ZenRows fetch needed,
    so this is deliberately kept independent of storefront_text (which requires a
    ZenRows fetch): it must be answerable from name + category + marketplace alone
    so it can run BEFORE any slow scraping. Candidates still go through the same
    homepage fetch + verify() gate as every other source -- this only changes
    where candidates come from, never the write gate."""
    brand = rec["name"]
    if not brand:
        return []

    def run():
        category_line = f"Category: {rec['main_category']}" if rec.get("main_category") else ""
        marketplace_line = (
            f"Known marketplace presence: {rec['orig_marketplaces']}"
            if rec.get("orig_marketplaces") else ""
        )
        prompt = CLAUDE_SEARCH_PROMPT.format(
            brand=brand, category_line=category_line, marketplace_line=marketplace_line,
        )
        try:
            charge("claude_search")
            resp = _client.messages.create(
                model=CLAUDE_SEARCH_MODEL, max_tokens=500,
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:  # noqa: BLE001 - a failed lookup just yields no candidates
            return []
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        try:
            data = json.loads(text[text.find("{"):text.rfind("}") + 1])
            return [c for c in data.get("candidates", []) if isinstance(c, str)][:3]
        except (ValueError, TypeError):
            return []

    return cached("claude-search:" + brand, run)


PRODUCT_SEARCH_PROMPT = """Find the official corporate website domain for this physical
consumer-product brand, using the products it actually sells as the identifying clue.

Brand name (as recorded in the CRM): {brand}
{category_line}
Products this brand sells on its Amazon storefront:
{products}

Search the web for the brand name together with these products -- that combination is
what distinguishes it from unrelated companies sharing a similar name. Do NOT return a
marketplace, reseller, distributor, or review-site domain, only the brand's own site.
A company in a different line of business with a similar name is NOT a match.

Respond with ONLY a JSON object, no prose:
{{"candidates": ["<bare domain, no scheme>", ...up to 3, best first]}}
Return an empty list if no plausible official site exists."""


def claude_search_with_products(rec):
    """Chris's method, automated: search the brand name TOGETHER WITH a real
    product from its Amazon storefront. Brand names on this list are frequently
    meaningless on their own (WKONCLDY, DECYOOL, Ahuccf) -- the products are what
    make the brand findable.

    Lives in the slow tier because it needs the storefront scrape first. Its
    candidates go through the same homepage fetch + verify() gate as every other
    source; this only changes where candidates come from, never the write gate."""
    brand, products = rec["name"], rec.get("storefront_products") or ""
    if not brand or not products:
        return []

    def run():
        prompt = PRODUCT_SEARCH_PROMPT.format(
            brand=brand,
            category_line=f"Category: {rec['main_category']}" if rec.get("main_category") else "",
            products=products[:400],
        )
        try:
            charge("claude_search")
            resp = _client.messages.create(
                model=CLAUDE_SEARCH_MODEL, max_tokens=500,
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:  # noqa: BLE001 - a failed lookup just yields no candidates
            return []
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        try:
            data = json.loads(text[text.find("{"):text.rfind("}") + 1])
            return [c for c in data.get("candidates", []) if isinstance(c, str)][:3]
        except (ValueError, TypeError):
            return []

    return cached("claude-product:" + brand + "|" + products[:80], run)


WIKIDATA_API = "https://www.wikidata.org/w/api.php"


def wikidata_domains(brand):
    """Official-website domains (Wikidata property P856) for a brand.

    High precision for established brands and silent on cryptic marketplace
    sellers, which is exactly the tail the SERP scrape struggles with: LG ->
    lg.com, Merck -> merck.com, Anchor Hocking -> anchorhocking.com, nothing for
    WKONCLDY. Those brands' own sites often block scraping, so their absence from
    the earlier tiers is an anti-bot artefact rather than evidence.

    Routed through ZenRows: Wikimedia answers 403 to direct datacenter requests.
    """
    if not brand:
        return []

    def run():
        query = urllib.parse.quote(brand)
        html = fetch_bounded(
            f"{WIKIDATA_API}?action=wbsearchentities&search={query}"
            "&language=en&format=json&limit=3")
        if not html:
            return []
        try:
            hits = json.loads(html).get("search", [])
        except ValueError:
            return []
        out = []
        for hit in hits[:2]:
            entity = fetch_bounded(
                f"https://www.wikidata.org/wiki/Special:EntityData/{hit['id']}.json")
            if not entity:
                continue
            try:
                claims = list(json.loads(entity)["entities"].values())[0].get("claims", {})
            except (ValueError, KeyError, IndexError):
                continue
            for claim in claims.get("P856", []):
                url = (claim.get("mainsnak") or {}).get("datavalue", {}).get("value")
                domain = bare_domain(url or "")
                if domain and domain not in out:
                    out.append(domain)
        return out

    return cached("wikidata:" + brand, run)


def _domain_gate(seen):
    """Shared filter+dedup used by both candidate stages below."""

    def check(domain):
        d = bare_domain(domain)
        if not d or d in seen:
            return None
        if any(d == b or d.endswith("." + b) for b in NON_BRAND_HOSTS):
            return None
        # Government, military and academic hosts are never a consumer brand
        # (a search for "MiiKARE" surfaced cnrk.cnic.navy.mil).
        if d.rsplit(".", 1)[-1] in ("mil", "gov", "edu", "int"):
            return None
        seen.add(d)
        return d

    return check


def fast_candidate_domains(rec, seen):
    """Zero or one network round trip per source, no ZenRows: existing domain,
    a contact's corporate email domain, then Claude+web_search. Tried first so
    the slow ZenRows waterfall below never runs for a brand these can already
    answer -- this ordering, not just the extra sources, is the speed fix."""
    check = _domain_gate(seen)

    if rec["domain"]:
        d = check(rec["domain"])
        if d:
            yield (d, "existing")

    if rec.get("num_associated_contacts"):
        d = check(contact_email_domain(rec["id"]))
        if d:
            yield (d, "contact-email")

    # Exactly ONE paid Claude web search per company. When the company has an
    # Amazon storefront, defer it to the slow tier so it can be grounded in real
    # product terms (strictly better answers, same cost). Only brands with no
    # storefront to ground against spend it here, ungrounded.
    if LLM_SEARCH and not rec["amazon"]:
        rec["claude_search_spent"] = True
        for candidate in claude_search_candidates(rec):
            d = check(candidate)
            if d:
                yield (d, "claude-search")


def slow_candidate_domains(rec, seen):
    """The ZenRows waterfall, spent ONE TIER AT A TIME.

    This is a generator on purpose, and the laziness is the cost control: a
    consumer that finds a verified domain in tier 0 stops iterating, so tiers 1-4
    are never paid for. Gathering every source up front to rank them globally
    (the previous shape) meant a brand that matched on its first candidate had
    already paid for two Claude web searches and three ZenRows scrapes.

    Ranking still holds because source quality dominates: candidates are sorted
    WITHIN a tier, and tiers are emitted in quality order, which is exactly what
    the old global sort produced.
    """
    brand = rec["name"]
    check = _domain_gate(seen)
    slug = _slug(brand)

    def ordered(domains, source):
        """Sort within a tier: exact slug match first, then shallower/shorter."""
        out = []
        for domain in domains:
            d = check(domain)
            if d:
                out.append(d)
        out.sort(key=lambda d: (
            0 if d.split(".")[0] == slug else (1 if slug and slug in _slug(d) else 2),
            d.count("."), len(d),
        ))
        return [(d, source) for d in out]

    # --- tier 0: the storefront we may already have paid to fetch ---
    sf_text = storefront_text(rec["amazon"]) if rec["amazon"] else ""
    rec["storefront_text"] = sf_text
    # Chrome-stripped product terms: grounding for the product searches below,
    # and the corrected product context handed to verify().
    rec["storefront_products"] = storefront_products(rec["amazon"], raw=sf_text)
    products = rec["storefront_products"]
    yield from ordered((m.group(1) for m in _HOST_RE.finditer(sf_text)), "storefront-scrape")

    # --- tier 1: the one Claude web search, grounded in real products ---
    # Chris's method: brand name + a product it actually sells. If the storefront
    # gave us nothing usable and the fast tier deferred its search, fall back to
    # the ungrounded form here so a brand never loses the search entirely.
    if LLM_SEARCH and not rec.get("claude_search_spent"):
        rec["claude_search_spent"] = True
        if products:
            yield from ordered(claude_search_with_products(rec), "claude-search-product")
        else:
            yield from ordered(claude_search_candidates(rec), "claude-search")

    # --- tier 2: the same product grounding, via the cheap scrape ---
    if products:
        top_terms = " ".join(products.split()[:4])
        yield from ordered(search_candidates(brand, top_terms), "product-scrape")

    # --- tier 3: category grounding, else a bare brand-name search ---
    # One query, not both: the category-grounded form is strictly more specific,
    # so the bare query is only worth paying for when there is no category.
    category = rec.get("main_category") or ""
    yield from ordered(search_candidates(brand, category), "search-scrape")

    # --- tier 4: slug guesses, gated by DNS so dead ones cost nothing ---
    yield from ordered(guess_candidates(brand), "slug-guess")

    # --- tier 5: Wikidata, last because it only pays off for established
    # brands. Lazy generation means the ~400 cryptic sellers never reach it.
    yield from ordered(wikidata_domains(brand), "wikidata")


# ------------------------------------------------------- stage 4: verify -----

VERIFY_PROMPT = """You are validating whether a website is the OFFICIAL website of a specific brand.

Every brand in this dataset is a PHYSICAL CONSUMER-PRODUCT brand that sells on
Amazon and/or Walmart. If the candidate site belongs to a company in a different
line of business (software, SaaS, a clinic, an agency, a hotel, a consultancy)
that merely shares the name, that is a MISMATCH no matter how exact the name match is.

Brand name (as recorded in the CRM): {brand}
Candidate domain: {domain}

Amazon storefront text for this brand (may be empty):
---
{storefront}
---

Candidate website homepage text:
---
{homepage}
---

Decide which of these the candidate website is:
- MATCH: the official website operated by this brand (or by its parent company, where the brand is clearly one of that company's own brands).
- RESELLER: a retailer, distributor, marketplace or affiliate that merely SELLS this brand's products. Not the brand's own site.
- MISMATCH: a different company that happens to share a similar name or word.
- PARKED: a parked domain, a for-sale page, an error page, or empty/no real content.

Be strict. A domain merely containing the brand name is NOT sufficient evidence.
If the homepage text is empty or uninformative, answer PARKED.
If the Amazon storefront text above is empty, you have no product context to
corroborate the name match -- in that case only answer MATCH when the homepage
itself clearly shows a physical consumer-product brand of this exact name;
otherwise answer MISMATCH.
If the product categories on the homepage do not plausibly match the storefront's
product categories, prefer MISMATCH over MATCH.

Answer RESELLER, not MATCH, in all of these cases:
- The site is a regional distributor, importer, licensee or authorised stockist
  that carries the brand in one country. A brand page hosted on ANOTHER
  company's domain (e.g. brand.someretailer.com.au, or a /brands/x page on a
  distributor's site) is a RESELLER page, however official it looks.
- The site sells many unrelated third-party brands alongside this one.
- The domain's registrable name belongs to a different company and this brand is
  merely one of the products it carries.

Answer MATCH for a parent-company site ONLY when the brand is presented as one of
that company's OWN brands that it manufactures or owns, not as a product it resells.

Respond with ONLY a JSON object, no prose:
{{"verdict": "MATCH|RESELLER|MISMATCH|PARKED", "confidence": <integer 0-100>, "reason": "<one short sentence>"}}"""


# Phrases that identify a parked or for-sale page. Recognising these locally
# turns a paid verifier call into a free string check -- and slug-guess
# candidates land on parking pages constantly.
_PARKED_MARKERS = (
    "domain is for sale", "buy this domain", "this domain is parked",
    "domain for sale", "parked free, courtesy", "godaddy.com/domainsearch",
    "the domain you are looking for", "renew this domain", "expired domain",
    "website coming soon", "under construction", "account suspended",
    "default web page", "index of /", "apache2 ubuntu default page",
)
MIN_HOMEPAGE_CHARS = 200


def looks_dead(homepage):
    """Free pre-filter. Returns a reason string when the page is clearly not a
    real brand site, so verify() never spends a token on it."""
    text = (homepage or "").strip()
    if len(text) < MIN_HOMEPAGE_CHARS:
        return f"homepage had only {len(text)} chars of text"
    low = text[:3000].lower()
    for marker in _PARKED_MARKERS:
        if marker in low:
            return f"parked/placeholder page (matched {marker!r})"
    return ""


def verify(brand, domain, storefront, homepage):
    """Anthropic gate. This is what stops another asrhealthbenefits.com landing
    in the domain field, so a parse failure must fail CLOSED (never MATCH)."""
    key = f"verify:{brand}|{domain}"

    def run():
        prompt = VERIFY_PROMPT.format(
            brand=brand, domain=domain,
            storefront=(storefront or "(none)")[:3000],
            homepage=(homepage or "(none)")[:4000],
        )
        try:
            charge("verify")
            resp = _client.messages.create(
                model=VERIFY_MODEL, max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            data = parse_verdict(resp, ("MATCH", "RESELLER", "MISMATCH", "PARKED"), "MISMATCH")
        except Exception as exc:  # noqa: BLE001 - fail closed on any model/parse error
            return {"verdict": "MISMATCH", "confidence": 0, "reason": f"verifier error: {exc}"[:200]}
        try:
            data["confidence"] = int(data.get("confidence") or 0)
        except (TypeError, ValueError):
            data["confidence"] = 0
        return data

    return cached(key, run)


_PUBLIC_SUFFIXES = ("com.au", "co.uk", "co.nz", "com.br", "co.jp", "com.mx", "co.za")


def _registrable(domain):
    """Registrable name of a domain: 'vetone.vetsfirstchoice.com.au' ->
    'vetsfirstchoice'. Handles the two-label public suffixes we actually see."""
    parts = domain.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _PUBLIC_SUFFIXES:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else domain


def _is_subdomain(domain):
    """True when the domain carries a label in front of registrable+suffix. An
    apex like acmeunited.com is not a subdomain -- a parent company's own site is
    a legitimate answer and must not be downgraded."""
    parts = domain.split(".")
    if len(parts) >= 2 and ".".join(parts[-2:]) in _PUBLIC_SUFFIXES:
        return len(parts) > 3
    return len(parts) > 2


def _downgrade_foreign_subdomain(brand, domain, result):
    """A brand page hosted on a *different* company's domain is a distributor or
    licensee page, not the brand's own site -- the model reads these as official
    (vetone.vetsfirstchoice.com.au scored MATCH 96). Whoever owns the registrable
    domain owns the site, so hold these for review instead of auto-writing."""
    if result["verdict"] != "MATCH" or not _is_subdomain(domain):
        return result
    reg, slug = _slug(_registrable(domain)), _slug(brand)
    if not slug or not reg or reg in slug or slug in reg:
        return result
    return {
        "verdict": "RESELLER",
        "confidence": result["confidence"],
        "reason": (f"brand page on a third party's domain ({_registrable(domain)}); "
                   f"held for review. Model said: {result.get('reason', '')}")[:400],
    }


def result_is_better(rec, confidence):
    """Only overwrite the recorded near-miss when nothing better is held yet, so
    the review CSV keeps the most informative rejection."""
    return not rec.get("verdict") or confidence > rec.get("confidence", 0)


def resolve_one(rec):
    """Stages 3-5 for a single company."""
    brand = rec["name"]
    if not brand:
        rec["verdict"] = "SKIP"
        rec["note"] = "company has no name; cannot research"
        return rec

    seen = set()
    candidates = itertools.chain(
        fast_candidate_domains(rec, seen), slow_candidate_domains(rec, seen)
    )
    # Budget raised from 8 with the two product-grounded sources added, so a
    # brand's real domain is not crowded out before it is ever evaluated.
    verifies = 0
    for domain, source in itertools.islice(candidates, 20 if DEEP else 12):
        # Hard ceiling on the paid verifier per company. Past a handful of
        # candidates the answer is almost never "the next one is it", so further
        # calls buy nothing; the record is better off in the review CSV.
        if verifies >= (MAX_VERIFY_PER_COMPANY * 2 if DEEP else MAX_VERIFY_PER_COMPANY):
            rec["flags"].append(f"stopped after {verifies} verification attempts")
            break
        home = homepage_text(domain)
        if not home:
            rec["fetch_failures"] = rec.get("fetch_failures", 0) + 1
            continue
        rec["candidates_judged"] = rec.get("candidates_judged", 0) + 1
        dead = looks_dead(home)
        if dead:
            # Free rejection -- no model call spent on a parking page.
            if result_is_better(rec, 0):
                rec.update({"verdict": "PARKED", "confidence": 0, "source": source,
                            "candidate": domain, "reason": dead})
            continue
        verifies += 1
        result = verify(brand, domain, rec.get("storefront_products", ""), home)
        result = _downgrade_foreign_subdomain(brand, domain, result)
        if source == "existing":
            rec["existing_judged"] = True
        if result["verdict"] == "MATCH" and result["confidence"] >= MIN_CONFIDENCE:
            rec.update({
                "verdict": "MATCH", "confidence": result["confidence"],
                "source": source, "candidate": domain, "reason": result.get("reason", ""),
            })
            # Never trade an existing apex domain for a subdomain of itself
            # (xlrecordings.com -> shopusa.xlrecordings.com is a downgrade for a
            # CRM domain field, even though the shop subdomain verifies fine).
            existing = rec["domain"]
            if existing and domain.endswith("." + existing):
                domain = existing
                rec["flags"].append(f"kept apex {existing} over verified subdomain")
            if domain != rec["domain"]:
                rec["plan"]["domain"] = domain
            if not DOMAINS_ONLY:
                store = shopify_store(brand, domain)
                rec["shopify"] = bool(store)
                _set_marketplaces(rec)
            return rec
        # Keep the best near-miss so the review CSV explains what was rejected.
        if result["confidence"] > rec["confidence"]:
            rec.update({
                "verdict": result["verdict"], "confidence": result["confidence"],
                "source": source, "candidate": domain, "reason": result.get("reason", ""),
            })

    # No verified domain: record marketplace presence and leave a note.
    # "No website exists" is a CLAIM, and it requires evidence: at least one
    # candidate homepage actually fetched and judged. If every fetch failed we
    # know nothing -- say INCONCLUSIVE and write no note. A throttled run once
    # wrote 509 "no site found" notes for companies it never managed to check.
    if not rec.get("candidates_judged"):
        rec["verdict"] = rec["verdict"] or "INCONCLUSIVE"
        rec["reason"] = (rec.get("reason")
                         or f"could not fetch any candidate ({rec.get('fetch_failures', 0)} "
                            f"fetch failures); no conclusion drawn")
        rec["flags"].append("inconclusive - no candidate could be fetched")
        return rec

    rec["verdict"] = rec["verdict"] or "NONE"
    if rec["domain"]:
        if rec.get("existing_judged"):
            # The verifier actually looked at this domain and rejected it.
            rec["plan"]["domain"] = ""
            rec["flags"].append(f"cleared domain rejected by verifier ({rec['domain']})")
        else:
            # We never managed to fetch it, so we have NO evidence against it.
            # Clearing here would destroy good data on a transient scrape failure.
            rec["plan"].pop("domain", None)
            rec["flags"].append(f"kept unreachable existing domain ({rec['domain']}) -- not verified")

    if not DOMAINS_ONLY:
        wm = walmart_storefront(brand)
        if wm and not rec["walmart"]:
            rec["plan"]["walmart_storefront_url"] = wm
            rec["walmart"] = wm
        rec["shopify"] = False
        _set_marketplaces(rec)

    checked = "Amazon storefront scrape, web search, domain probes, StoreLeads (Shopify), Walmart search"
    found = []
    if rec["amazon"]:
        found.append(f"Amazon storefront: {rec['amazon']}")
    if rec["walmart"]:
        found.append(f"Walmart storefront: {rec['walmart']}")
    if rec.get("shopify"):
        found.append("Shopify store found")
    best = ""
    if rec["candidate"]:
        best = (f" Best rejected candidate: {rec['candidate']} "
                f"({rec['verdict']}, confidence {rec['confidence']}: {rec.get('reason', '')}).")
    rec["note"] = (
        f"No official brand website could be verified for {brand}. "
        f"Sources checked: {checked}."
        + (f" Marketplace presence found -- {'; '.join(found)}." if found else
           " No marketplace presence found either.")
        + best
        + " Domain left blank deliberately; do not re-enrich without verification."
    )
    return rec


def _set_marketplaces(rec):
    """Write the marketplaces enum from presence actually observed, never
    dropping values that were already on the record."""
    values = {v.strip() for v in rec["orig_marketplaces"].split(";") if v.strip()}
    if rec["amazon"]:
        values.add("Amazon US")
    if rec["walmart"]:
        values.add("Walmart US")
    if rec.get("shopify"):
        values.add("Shopify")
    joined = ";".join(sorted(values))
    if joined and joined != rec["orig_marketplaces"]:
        rec["plan"]["marketplaces"] = joined


# --------------------------------------------------------- stage 6: apply ----

def create_note(company_id, body):
    """Note engagement + association to the company."""
    resp = hs.request_with_retry(
        "POST", f"{BASE}/crm/v3/objects/notes",
        json={"properties": {"hs_note_body": body, "hs_timestamp": _now_ms()},
              "associations": [{"to": {"id": str(company_id)},
                                "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                           "associationTypeId": NOTE_TO_COMPANY_ASSOC}]}]},
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"note create failed: {resp.status_code} {resp.text[:300]}")


def _now_ms():
    import time
    return int(time.time() * 1000)


def write_csv(path, rows, fields):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


REVIEW_FIELDS = [
    "id", "name", "verdict", "confidence", "source", "candidate", "reason",
    "orig_domain", "new_domain", "orig_amazon", "new_amazon",
    "orig_walmart", "new_walmart", "orig_marketplaces", "new_marketplaces",
    "applied", "flags", "note",
]


def to_row(rec, applied):
    plan = rec["plan"]
    return {
        "id": rec["id"], "name": rec["name"], "verdict": rec["verdict"],
        "confidence": rec["confidence"], "source": rec["source"],
        "candidate": rec["candidate"], "reason": rec.get("reason", ""),
        "orig_domain": rec["orig_domain"], "new_domain": plan.get("domain", ""),
        "orig_amazon": rec["orig_amazon"], "new_amazon": plan.get("amazon_storefront_url", ""),
        "orig_walmart": rec["orig_walmart"], "new_walmart": plan.get("walmart_storefront_url", ""),
        "orig_marketplaces": rec["orig_marketplaces"], "new_marketplaces": plan.get("marketplaces", ""),
        "applied": "yes" if applied else "no",
        "flags": "; ".join(rec["flags"]), "note": rec["note"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-id", default=DEFAULT_LIST_ID)
    ap.add_argument("--limit", type=int, default=0, help="process only the first N companies")
    ap.add_argument("--ids", default="", help="comma-separated company IDs to process")
    ap.add_argument("--dry-run", action="store_true", help="research and report, write nothing")
    ap.add_argument("--refresh", action="store_true", help="re-pull the list from HubSpot")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--llm-search", action="store_true",
                    help="also use Claude web_search for candidates (~18s/company; "
                         "off by default -- use it to re-run the unresolved tail)")
    ap.add_argument("--deep", action="store_true",
                    help="maximise recall on the hard tail: js_render escalation, "
                         "all search engines, more verifier attempts (slower)")
    ap.add_argument("--domains-only", action="store_true",
                    help="skip the StoreLeads Shopify probe and Walmart search "
                         "(they only populate `marketplaces`)")
    ap.add_argument("--missing-domain-only", action="store_true",
                     help="skip companies that already have a clean, non-junk domain")
    args = ap.parse_args()

    config.DRY_RUN = args.dry_run
    global LLM_SEARCH, DOMAINS_ONLY, DEEP
    LLM_SEARCH, DOMAINS_ONLY, DEEP = args.llm_search, args.domains_only, args.deep
    load_cache()

    companies = pull(args.list_id, refresh=args.refresh)
    records = sanitize(companies)
    log(f"{len(records)} companies after sanitize")

    todo = list(records.values())
    if args.missing_domain_only:
        todo = [r for r in todo if not r["domain"]]
        log(f"{len(todo)} companies missing a usable domain")
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",") if i.strip()}
        todo = [r for r in todo if r["id"] in wanted]
    elif args.limit:
        todo = todo[:args.limit]
    log(f"researching {len(todo)} companies ({args.workers} workers, dry_run={args.dry_run})")

    # Write in batches as records complete rather than one write at the very
    # end -- a kill/crash partway through a multi-thousand-company run should
    # only cost the current batch, not every result computed so far.
    FLUSH_EVERY = 50
    flush_batch, rows, total_updates, total_notes = [], [], 0, 0

    def flush(batch):
        nonlocal total_updates, total_notes
        if not batch:
            return
        updates = [{"id": r["id"], "properties": r["plan"]} for r in batch if r["plan"]]
        notes = [(r["id"], r["note"]) for r in batch if r["note"]]
        for r in batch:
            rows.append(to_row(r, bool(r["plan"]) and not args.dry_run))
        if args.dry_run:
            return
        if updates:
            hs.batch_update("companies", updates)
            total_updates += len(updates)
        for company_id, body in notes:
            try:
                create_note(company_id, body)
            except RuntimeError as exc:
                log(f"  note failed for {company_id}: {exc}")
        total_notes += len(notes)
        write_csv(os.path.join(DATA_DIR, f"list{args.list_id}_review.csv"), rows, REVIEW_FIELDS)

    done = 0
    # as_completed, NOT pool.map: map yields results in submission order, so a
    # single slow company head-of-line blocks the progress log, save_cache() AND
    # the incremental flush. The first 7694 run looked frozen at company 12 for
    # minutes while nine workers were in fact busy, and a crash would have
    # discarded all of that work because the periodic save never got a turn.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(resolve_one, rec): rec for rec in todo}
        for future in as_completed(futures):
            done += 1
            try:
                rec = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad record must not kill 565
                rec = futures[future]
                rec["verdict"] = rec.get("verdict") or "ERROR"
                rec["reason"] = f"{type(exc).__name__}: {exc}"[:200]
                log(f"  [{done}/{len(todo)}] {rec['name']}: ERROR {rec['reason']}")
            else:
                log(f"  [{done}/{len(todo)}] {rec['name']}: {rec['verdict']} "
                    f"{rec['confidence']} {rec['candidate']}")
            flush_batch.append(rec)
            if done % 20 == 0:
                save_cache()
            if len(flush_batch) >= FLUSH_EVERY:
                flush(flush_batch)
                log(f"  -- flushed batch: {total_updates} updates, {total_notes} notes so far --")
                flush_batch = []
    flush(flush_batch)
    save_cache()

    write_csv(os.path.join(DATA_DIR, f"list{args.list_id}_review.csv"), rows, REVIEW_FIELDS)

    log("")
    log("paid provider calls this run (cache hits are free and excluded):")
    for kind, n in sorted(SPEND.items()):
        per = n / max(1, len(todo))
        log(f"  {kind:22} {n:6}   ({per:.2f} per company)")
    if not SPEND:
        log("  none -- everything served from cache")

    matched = sum(1 for r in todo if r["verdict"] == "MATCH")
    log(f"\nverified domains: {matched}/{len(todo)}")
    log(f"property updates: {total_updates}   notes: {total_notes}")
    log(f"review CSV: data/list{args.list_id}_review.csv")

    if args.dry_run:
        log("dry run -- nothing written to HubSpot")
        return

    write_csv(os.path.join(DATA_DIR, f"list{args.list_id}_applied.csv"),
              [r for r in rows if r["applied"] == "yes"], REVIEW_FIELDS)


if __name__ == "__main__":
    main()
