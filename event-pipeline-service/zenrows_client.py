"""
ZenRows scraping client -- the LAST-resort tier of identity resolution
(enrichment.resolve_identity). When the enrichment APIs can't turn a
(personal) email + name into a LinkedIn URL, this scrapes a search engine via
ZenRows (which handles the anti-bot/proxy layer) to discover the person's
linkedin.com/in/ URL, which the pipeline then turns into a company via the
profile->company APIs. Skips (returns "") when ZENROWS_KEY is blank.
"""
import re
import time
import urllib.parse
import requests

import config

ZENROWS_URL = "https://api.zenrows.com/v1/"
_LI_RE = re.compile(r"https?://([a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+", re.IGNORECASE)


def _fetch(url):
    """GET a URL through ZenRows (handles the proxy/anti-bot layer). Retries on
    transient 422/429/5xx (ZenRows concurrency limits) with backoff. Returns raw
    HTML, or "" on miss / no key / persistent error."""
    if not config.ZENROWS_KEY:
        return ""
    # Escalate proxy strength on retry: plain -> premium proxy -> premium+JS.
    # Many brand sites 422 ZenRows' basic proxy (anti-bot); premium+JS gets through.
    variants = [{}, {"premium_proxy": "true"}, {"premium_proxy": "true", "js_render": "true"}]
    for extra in variants:
        try:
            r = requests.get(ZENROWS_URL, params={"apikey": config.ZENROWS_KEY, "url": url, **extra}, timeout=70)
        except requests.RequestException:
            time.sleep(1)
            continue
        if r.status_code < 300 and r.text:
            return r.text
        if r.status_code in (422, 429) or r.status_code >= 500:
            time.sleep(1)
            continue
        return ""
    return ""


def _html_to_text(html):
    """Strip HTML to visible text (drop script/style), collapse whitespace."""
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def company_context(domain):
    """Scrape a company's own site (homepage + an about page, best-effort) via
    ZenRows and return concatenated visible text (capped ~8k chars) to GROUND an
    LLM firmographics extraction. "" on no key / nothing fetched. Homepage footer
    usually carries the LinkedIn URL + tagline; about/contact pages carry founded
    year + HQ; product pages reveal the true category. Max 2 fetches per company."""
    if not config.ZENROWS_KEY or not domain:
        return ""
    host = domain.lower().replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
    texts = []
    for path in ("", "about-us"):  # homepage, then one about page
        html = _fetch(f"https://{host}/{path}".rstrip("/"))
        if html:
            t = _html_to_text(html)
            if t:
                texts.append(t[:5000])
        if sum(len(x) for x in texts) >= 5000:
            break
    return " ".join(texts)[:8000]


def find_linkedin(full_name, hint=""):
    """Search-engine scrape (via ZenRows) for the person's LinkedIn profile
    URL. `hint` can be a company name or free-email local-part to disambiguate.
    Returns the first linkedin.com/in/ URL found, or "" on miss/no key/error."""
    if not config.ZENROWS_KEY or not full_name:
        return ""
    query = f"{full_name} {hint} linkedin".strip()
    # DuckDuckGo's HTML endpoint returns clean, parseable results (no JS needed)
    # and works without a premium proxy. Result links are wrapped in a uddg=
    # redirect param, so URL-decode the whole page before matching.
    target = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    try:
        r = requests.get(ZENROWS_URL, params={"apikey": config.ZENROWS_KEY, "url": target}, timeout=70)
    except requests.RequestException:
        return ""
    if r.status_code >= 300:
        return ""
    text = urllib.parse.unquote(r.text or "")
    m = _LI_RE.search(text)
    return m.group(0).split("?")[0].rstrip("/") if m else ""
