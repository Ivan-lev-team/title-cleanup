"""
ZenRows scraping client -- the LAST-resort tier of identity resolution
(enrichment.resolve_identity). When the enrichment APIs can't turn a
(personal) email + name into a LinkedIn URL, this scrapes a search engine via
ZenRows (which handles the anti-bot/proxy layer) to discover the person's
linkedin.com/in/ URL, which the pipeline then turns into a company via the
profile->company APIs. Skips (returns "") when ZENROWS_KEY is blank.
"""
import re
import urllib.parse
import requests

import config

ZENROWS_URL = "https://api.zenrows.com/v1/"
_LI_RE = re.compile(r"https?://([a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+", re.IGNORECASE)


def find_linkedin(full_name, hint=""):
    """Search-engine scrape (via ZenRows) for the person's LinkedIn profile
    URL. `hint` can be a company name or free-email local-part to disambiguate.
    Returns the first linkedin.com/in/ URL found, or "" on miss/no key/error."""
    if not config.ZENROWS_KEY or not full_name:
        return ""
    query = f'"{full_name}" {hint} site:linkedin.com/in'.strip()
    target = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
    try:
        r = requests.get(
            ZENROWS_URL,
            params={"apikey": config.ZENROWS_KEY, "url": target,
                    "js_render": "true", "premium_proxy": "true"},
            timeout=70,
        )
    except requests.RequestException:
        return ""
    if r.status_code >= 300:
        return ""
    m = _LI_RE.search(r.text or "")
    if not m:
        return ""
    return m.group(0).split("?")[0].rstrip("/")
