"""
Clay discovery layer -- real HTTP client against the Clay Public API.

Was a JSON reader for MCP output; the Public API makes it scriptable, which the
MCP connector never could be (MCP is callable by the assistant in-session, not
by a Python process, and the `clay` CLI ships darwin/linux binaries only -- no
Windows build at v1.2.0, confirmed from the plugin repo's checksums.txt).

Auth: header `clay-api-key`, NOT Bearer. Base https://api.clay.com/public/v0
Key comes from app.clay.com -> Settings -> Account -> API keys (beta).

Search is a three-step, forward-only iterator with no cursor:
    POST /search/filters-mode              -> search_id
    POST /search/filters-mode/{id}/run     -> next page (repeat while has_more)
    GET  /search/filters-mode/fields       -> valid filter names

COST, measured 2026-09-21: 10 company searches returning 25 people cost
**2.0 credits** (223874.6 -> 223872.6), i.e. ~0.2 per company. Cheap, but NOT
free -- the third provider this session whose "free" search turned out to bill.
Balance is readable at GET /credits/balance, so cost is always verifiable.

Stale associations: a returned person whose `domain` differs from the domain
searched is an alumnus/investor/contractor. Clay's own guidance says to split
these out; merged unchecked they attach someone to a company they left.
"""
import re
import time
import requests

import config
import ratelimit

BASE = "https://api.clay.com/public/v0"

# Undocumented rate limit; deliberately conservative until measured.
CLAY = ratelimit.RateLimiter(per_second=4, per_minute=180, name="clay")

DEFAULT_EXCLUDE = ["Intern", "Assistant", "Coordinator"]


def _headers():
    return {"clay-api-key": config.CLAY_API_KEY, "Content-Type": "application/json"}


def _req(method, path, tries=4, **kw):
    resp = None
    for a in range(tries):
        CLAY.acquire()
        try:
            resp = requests.request(method, BASE + path, headers=_headers(),
                                    timeout=90, **kw)
        except requests.RequestException:
            time.sleep(2 ** a)
            continue
        if resp.status_code == 429:
            time.sleep(ratelimit.retry_after_seconds(resp) + 1)
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** a)
            continue
        return resp
    return resp


def credits_balance():
    r = _req("GET", "/credits/balance")
    j = ratelimit.safe_json(r)
    return j.get("balance")


def search_people(domain, job_titles=None, exclude_titles=None, max_pages=2):
    """Find people at one company domain.

    Returns {"people": [...], "quarantined": [...], "error": str}. `people` are
    rows whose returned domain matches the one searched; `quarantined` are
    stale associations, reported separately and never merged.
    """
    out = {"people": [], "quarantined": [], "error": ""}
    if not config.CLAY_API_KEY:
        out["error"] = "CLAY_API_KEY not set"
        return out
    dom = (domain or "").strip().lower()
    if not dom or "." not in dom:
        out["error"] = "unusable domain %r" % domain
        return out

    filters = {"company_identifier": [dom]}
    if job_titles:
        filters["job_title_keywords"] = list(job_titles)
    filters["job_title_exclude_keywords"] = list(exclude_titles or DEFAULT_EXCLUDE)

    r = _req("POST", "/search/filters-mode",
             json={"source_type": "people", "filters": filters})
    j = ratelimit.safe_json(r)
    sid = j.get("search_id") or j.get("searchId")
    if r is None or r.status_code >= 300 or not sid:
        out["error"] = "create failed: %s %s" % (
            getattr(r, "status_code", "?"), str(j)[:120])
        return out

    for _ in range(max_pages):
        rr = _req("POST", "/search/filters-mode/%s/run" % sid, json={})
        jj = ratelimit.safe_json(rr)
        recs = jj.get("records") or jj.get("results") or jj.get("data") or []
        for p in recs:
            if not isinstance(p, dict):
                continue
            actual = (p.get("domain") or "").strip().lower()
            row = {
                "first_name": (p.get("first_name") or "").strip(),
                "last_name": (p.get("last_name") or "").strip(),
                "full_name": (p.get("name") or "").strip(),
                "job_title": (p.get("title") or p.get("job_title")
                              or p.get("current_job_title") or "").strip(),
                "linkedin_url": (p.get("url") or p.get("linkedin_url") or "").strip(),
                "company_domain": dom,
                "clay_actual_domain": actual,
                "source": "Clay",
            }
            if actual and actual != dom:
                out["quarantined"].append(row)
            else:
                out["people"].append(row)
        if not (jj.get("has_more") or jj.get("hasMore")):
            break
    return out
