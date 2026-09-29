"""
Multi-provider "waterfall" enrichment: try providers in a fixed order and stop
at the first good result. Used by pipeline.py Step 3 (email) and Step 3.5
(mobile).

Order (Levanta's chosen provider preference):
  - Email:  LeadMagic -> Prospeo         (stop at first VERIFIED email; strict --
            catch-all / unknown are recorded for review, never auto-used)
  - Mobile: Prospeo -> Forager -> LeadMagic   (stop at first number found)
  - Revenue (company, by domain): StoreLeads -> LeadMagic -> Prospeo (config
            REVENUE_ORDER); stop at the first provider with a real annual figure

Each tier auto-skips when its key (or, for Forager, its account id / a LinkedIn
handle) is missing, so the waterfall degrades gracefully: a row with no
LinkedIn URL can still get a mobile from Prospeo (name+company), just not from
Forager/LeadMagic, which both require a LinkedIn profile.
"""
import re
import json

import anthropic

import leadmagic_client
import forager_client
import prospeo_client
import storeleads_client
import zenrows_client
import config

_llm = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

_LLM_FILL_PROMPT = """You are filling missing firmographic fields for a company, grounded ONLY in the website text provided below (plus well-established public fact). This is the LAST-RESORT gap-filler after data providers came up empty, so accuracy matters more than coverage.

STRICT RULE: return a value ONLY when you are HIGHLY confident it is correct. If you are not highly confident, return null. NEVER guess or approximate. It is correct and expected to return null for most fields.

Company: {company}
Domain: {domain}
Website text (may be truncated):
\"\"\"
{context}
\"\"\"

Return ONLY a JSON object with these keys (null unless highly confident):
{{"industry": string|null, "founded_year": "YYYY"|null, "employee_count": integer|null, "revenue_usd": number|null, "company_linkedin": string|null, "city": string|null, "state": string|null, "country": string|null}}

Field rules:
- revenue_usd: ONLY if a concrete annual revenue figure is explicitly stated/derivable (very rare on a company's own site) -- otherwise null. Do NOT estimate from headcount or vibes.
- company_linkedin: must be a real linkedin.com/company/... URL seen in the text -- else null.
- employee_count: only a stated headcount -- else null.
- founded_year: a 4-digit year stated on the site -- else null.
- industry: the company's product category, only if clear from what they sell."""

_LLM_COL = {
    "industry": "Industry", "founded_year": "Founded", "employee_count": "Employee Count",
    "revenue_usd": "Estimated Revenue (USD)", "company_linkedin": "Company LinkedIn",
    "city": "City", "state": "State/Region", "country": "Country/Region",
}


def llm_fill_firmographics(company_name, domain, context, missing_cols):
    """LAST-RESORT tier: extract ONLY the still-missing firmographic fields from
    scraped website `context`, high-confidence only. Returns {sheet-col: value}
    for fields in `missing_cols`. {} on no context / parse failure."""
    if not context or not (company_name or domain):
        return {}
    prompt = _LLM_FILL_PROMPT.format(company=company_name or "(unknown)", domain=domain or "(unknown)", context=context[:6000])
    try:
        resp = _llm.messages.create(model="claude-sonnet-5", max_tokens=400,
                                    messages=[{"role": "user", "content": prompt}])
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        data = json.loads(text)
    except Exception:
        return {}
    out = {}
    for k, col in _LLM_COL.items():
        v = data.get(k)
        if v not in (None, "", "null") and col in missing_cols:
            out[col] = v
    return out


def clean_domain(d):
    """Normalize a domain/URL to a bare host: strip scheme, www, and any path/
    query, lowercased. 'https://www.iDerive.com/about' -> 'iderive.com'."""
    d = (d or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    return d.split("/")[0].split("?")[0].strip()


_FREE_EMAIL_DOMAINS = {
    # webmail
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.ca", "yahoo.co.uk", "yahoo.com.au",
    "ymail.com", "rocketmail.com", "hotmail.com", "hotmail.co.uk", "hotmail.ca", "outlook.com",
    "live.com", "live.ca", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com",
    "protonmail.com", "proton.me", "gmx.com", "gmx.de", "gmx.net", "mail.com", "zoho.com",
    "yandex.com", "hey.com", "fastmail.com", "hushmail.com",
    # ISPs (personal, not companies)
    "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "bellsouth.net", "cox.net",
    "charter.net", "earthlink.net", "frontier.com", "windstream.net", "roadrunner.com",
    "bigpond.com", "bigpond.net.au", "optusnet.com.au", "iinet.net.au", "telstra.com",
    "rogers.com", "sympatico.ca", "shaw.ca", "telus.net", "videotron.ca",
    "btinternet.com", "sky.com", "virginmedia.com", "orange.fr", "free.fr", "wanadoo.fr",
    "web.de", "t-online.de", "libero.it", "naver.com", "qq.com", "163.com", "126.com",
    "rediffmail.com",
}


def is_free_email_domain(domain):
    return (domain or "").strip().lower() in _FREE_EMAIL_DOMAINS


# Marketplaces, social platforms and site builders. A seller's Amazon/Walmart/
# Etsy storefront or Instagram handle is NOT their company domain: writing one
# here makes the row inherit the PLATFORM's firmographics ($1B-$10B revenue,
# tens of thousands of employees) and poisons every downstream lookup. Per the
# playbook these belong in `amazon_storefront_url`, never in `domain`.
_MARKETPLACE_DOMAINS = {
    "amazon.com", "amazon.co.uk", "amazon.ca", "amazon.de", "amazon.fr", "amazon.it",
    "amazon.es", "amazon.com.au", "amazon.co.jp", "amazon.in", "amazon.com.mx",
    "amazon.com.br", "amzn.to", "amazon.com services llc",
    "walmart.com", "target.com", "ebay.com", "etsy.com", "faire.com",
    "aliexpress.com", "alibaba.com", "temu.com", "wayfair.com", "chewy.com",
    "instagram.com", "facebook.com", "tiktok.com", "linkedin.com", "youtube.com",
    "pinterest.com", "twitter.com", "x.com", "snapchat.com", "threads.net",
    "shopify.com", "wix.com", "squarespace.com", "bigcommerce.com", "wordpress.com",
    "godaddy.com", "linktr.ee", "beacons.ai", "shopmy.us", "ltk.com",
}

# Values upstream tools write when they could not find a domain. Clay in
# particular fills its Company Domain column with the COMPANY NAME (or a
# literal "unknown") on a failed lookup, which then reaches us as a domain.
_DOMAIN_PLACEHOLDERS = {
    "", "-", ".", "n/a", "na", "none", "null", "nil", "unknown", "tbd", "tba",
    "not found", "no domain", "no website", "not available", "undefined", "false",
}

# A syntactically real hostname: labels of alphanumerics/hyphens, a TLD of 2+
# letters, no spaces, no commas, total length within DNS limits.
_DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,24}$"
)


def domain_rejection_reason(domain):
    """Why `domain` is not usable as a company domain, or "" when it is fine.

    Guards the single most damaging input error we see: a company NAME, a free
    webmail host, or a marketplace/social URL sitting in a domain field. Each
    of those silently breaks the email and firmographic waterfalls (every
    provider lookup is keyed on domain) or, worse, succeeds against the wrong
    company. Callers should drop the value and fall back to a company-name
    lookup rather than enrich against it."""
    d = clean_domain(domain)
    if d in _DOMAIN_PLACEHOLDERS:
        return "placeholder" if d else ""
    if not _DOMAIN_RE.match(d):
        return "not a domain"
    if is_free_email_domain(d):
        return "free email provider"
    if d in _MARKETPLACE_DOMAINS:
        return "marketplace/social platform"
    return ""


def valid_company_domain(domain):
    """True when `domain` is a usable corporate domain. See
    domain_rejection_reason for what gets rejected and why."""
    d = clean_domain(domain)
    return bool(d) and not domain_rejection_reason(d)


def sanitize_company_domain(domain):
    """(clean_domain, reason). `reason` is "" when the domain is kept; when it
    is set the domain comes back "" so the caller never enriches against it."""
    d = clean_domain(domain)
    reason = domain_rejection_reason(d)
    return ("", reason) if reason else (d, "")


def name_domain_consistent(company_name, domain):
    """Sanity-check a RESOLVED company against its domain: they should share a
    stem. Catches bad resolutions like company 'Consumer Reports' + domain
    'meta.com'. Conservative -- may flag a legit parent/brand mismatch, which we
    surface as 'verify' rather than dropping."""
    n = re.sub(r"[^a-z0-9]", "", (company_name or "").lower())
    d = re.sub(r"[^a-z0-9]", "", (domain or "").split(".")[0].lower())
    if not n or not d:
        return False
    return d in n or n in d or (len(d) >= 5 and d[:5] in n)


def resolve_identity(full_name, email, linkedin_url=""):
    """For a row missing a company: resolve {linkedin, company_name, domain,
    title, provider} from a (often personal) email + name. Waterfall:
    Forager reverse-email -> Prospeo reverse-email (both one-shot: linkedin +
    company), then LeadMagic email->profile, then profile->company via LeadMagic
    or Prospeo. Returns whatever was found (may be partial: a linkedin with no
    company for solo sellers). Each tier auto-skips when its key is missing."""
    out = {"linkedin": linkedin_url or "", "company_name": "", "domain": "", "title": "",
           "provider": "", "low_confidence": False}

    def _merge(r, provider):
        for k in ("linkedin", "company_name", "domain", "title"):
            if r.get(k) and not out[k]:
                out[k] = r[k]
        if (out["company_name"] or out["domain"]) and not out["provider"]:
            out["provider"] = provider

    # 1) one-shot reverse-email providers (linkedin + company together)
    if email:
        _merge(forager_client.reverse_email(email), "forager")
        if out["company_name"] or out["domain"]:
            return out
        _merge(prospeo_client.resolve_person(email=email), "prospeo")
        if out["company_name"] or out["domain"]:
            return out
    # 2) LeadMagic reverse-email -> profile_url (only gives a URL)
    if email and not out["linkedin"]:
        pu = leadmagic_client.email_to_profile(personal_email=email)
        if pu:
            out["linkedin"] = pu
    # 2b) ZenRows LinkedIn discovery (search-engine scrape) -- last resort when
    # the enrichment APIs can't turn the (personal) email into a profile.
    if full_name and not out["linkedin"]:
        li = zenrows_client.find_linkedin(full_name)
        if li:
            out["linkedin"] = li
    # 3) LinkedIn URL -> company (LeadMagic first, then Prospeo) -- strongest leg
    if out["linkedin"] and not (out["company_name"] or out["domain"]):
        _merge(leadmagic_client.profile_to_company(out["linkedin"]), "leadmagic")
        if not (out["company_name"] or out["domain"]):
            _merge(prospeo_client.resolve_person(linkedin_url=out["linkedin"]), "prospeo")
    # Resolved domains often arrive as full URLs. They are also where the
    # creator/personal-email false positives surface: a solo seller's profile
    # resolves to instagram.com or etsy.com, and the row then inherits the
    # platform's firmographics. Reject those the same way as an input domain.
    _resolved, _reject = sanitize_company_domain(out["domain"])
    if _reject:
        out["domain_rejected"] = f"{clean_domain(out['domain'])} ({_reject})"
        out["low_confidence"] = True
    out["domain"] = _resolved
    if (out["company_name"] or out["domain"]) and not name_domain_consistent(out["company_name"], out["domain"]):
        out["low_confidence"] = True  # resolved company/domain don't line up -- verify
    return out


def _bucket_annual_revenue(dollars):
    """Map an annual revenue (USD) to HubSpot's estimated_annual_revenue option
    code: 0=$0-10k, 1=$10k-100k, 2=$100k-1M, 3=$1M-10M, 4=$10M+."""
    if not dollars:
        return ""
    if dollars >= 10_000_000:
        return "4"
    if dollars >= 1_000_000:
        return "3"
    if dollars >= 100_000:
        return "2"
    if dollars >= 10_000:
        return "1"
    return "0"


def find_revenue_band(domain, order=None):
    """Company-revenue waterfall by domain. Returns
    {"code": <estimated_annual_revenue code or "">, "dollars": <float|None>,
    "provider": <name or "">}. Stops at the first provider returning a real
    annual-revenue figure. Order defaults to config.REVENUE_ORDER."""
    if not domain:
        return {"code": "", "dollars": None, "provider": ""}
    providers = {
        "storeleads": storeleads_client.company_annual_revenue,
        "leadmagic": leadmagic_client.company_revenue,
        "prospeo": prospeo_client.enrich_company_revenue,
    }
    for name in (order or config.REVENUE_ORDER):
        fn = providers.get(name)
        if not fn:
            continue
        dollars = fn(domain)
        if dollars:
            return {"code": _bucket_annual_revenue(dollars), "dollars": dollars, "provider": name}
    return {"code": "", "dollars": None, "provider": ""}


def _to_num(v):
    """Best-effort parse of a revenue-ish value to float. Returns None if not numeric."""
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


_CODE_DOLLARS = {"4": 1e7, "3": 1e6, "2": 1e5, "1": 1e4, "0": 0.0}


def find_firmographics(domain, hs_rev_code="", company_name=""):
    """Fill company firmographics from ALL providers, cost-efficiently:
    StoreLeads + LeadMagic first (both credited/cheap, and each sees channels the
    other misses), then Prospeo ONLY to fill a field still empty, then a LAST-
    RESORT Claude tier that scrapes the company's own site (ZenRows) and extracts
    high-confidence values for whatever is STILL missing. Revenue is the MAX
    across every source (+ any HubSpot figure): a brand may sell across
    Shopify + Amazon + Walmart, so no single channel's number should understate
    it and wrongly disqualify. Returns:
      {"fields": {sheet-column: value, ...},   # only non-empty
       "revenue_dollars": float|None,          # the MAX
       "revenue_code": str,                     # bucketed MAX (or "")
       "providers": [names that contributed]}
    """
    blank = {"fields": {}, "revenue_dollars": None, "revenue_code": "", "providers": []}
    if not domain:
        return blank
    sl = storeleads_client.company_firmographics(domain)
    lm = leadmagic_client.company_firmographics(domain)
    providers = []
    if sl:
        providers.append("storeleads")
    if lm:
        providers.append("leadmagic")

    def pick(*vals):
        for v in vals:
            if v not in (None, "", []):
                return v
        return ""

    # Prefer LeadMagic for headcount/industry/linkedin/founded (LinkedIn-derived,
    # cleaner); either for location. Revenue handled separately (max) below.
    employees = pick(lm.get("employee_count"), sl.get("employee_count"))
    industry = pick(lm.get("industry"), sl.get("industry"))
    linkedin = pick(lm.get("linkedin_url"))
    founded = pick(lm.get("founded_year"), sl.get("founded_year"))
    city = pick(lm.get("city"), sl.get("city"))
    state = pick(lm.get("state"), sl.get("state"))
    country = pick(lm.get("country"), sl.get("country"))

    # Cost-efficient Prospeo fallback: only call when a field is STILL missing.
    pr = {}
    if not all([employees, industry, linkedin, founded]):
        pr = prospeo_client.enrich_company_full(domain) or {}
        if pr:
            providers.append("prospeo")
            employees = employees or pr.get("Employee Count") or ""
            industry = industry or pr.get("Industry") or ""
            linkedin = linkedin or pr.get("Company LinkedIn") or ""
            founded = founded or pr.get("Founded") or ""
            city = city or pr.get("City") or ""
            state = state or pr.get("State/Region") or ""
            country = country or pr.get("Country/Region") or ""

    # ---- LAST-RESORT Claude tier: fill whatever is STILL missing, grounded on
    # the company's own website (ZenRows scrape). High-confidence values only.
    llm_rev = None
    still = {"Employee Count": employees, "Industry": industry, "Company LinkedIn": linkedin,
             "Founded": founded, "City": city, "State/Region": state, "Country/Region": country}
    missing_cols = {c for c, v in still.items() if not v}
    prov_rev = [x for x in [_to_num(sl.get("revenue_usd")), _to_num(lm.get("revenue_usd")),
                            _to_num(pr.get("Estimated Revenue (USD)"))] if x]
    if not prov_rev:
        missing_cols.add("Estimated Revenue (USD)")
    if missing_cols:
        context = zenrows_client.company_context(domain)
        if context:
            llm = llm_fill_firmographics(company_name, domain, context, missing_cols)
            if llm:
                providers.append("claude")
                employees = employees or llm.get("Employee Count") or ""
                industry = industry or llm.get("Industry") or ""
                linkedin = linkedin or llm.get("Company LinkedIn") or ""
                founded = founded or llm.get("Founded") or ""
                city = city or llm.get("City") or ""
                state = state or llm.get("State/Region") or ""
                country = country or llm.get("Country/Region") or ""
                llm_rev = _to_num(llm.get("Estimated Revenue (USD)"))

    # MAX revenue across ALL sources (providers + HubSpot code + high-conf LLM).
    candidates = [x for x in [
        _to_num(sl.get("revenue_usd")), _to_num(lm.get("revenue_usd")),
        _to_num(pr.get("Estimated Revenue (USD)")), _CODE_DOLLARS.get((hs_rev_code or "").strip()),
        llm_rev,
    ] if x]
    max_rev = max(candidates) if candidates else None

    fields = {}
    if max_rev:
        fields["Estimated Revenue (USD)"] = int(max_rev)
    if employees:
        fields["Employee Count"] = employees
    if industry:
        fields["Industry"] = industry
    if linkedin:
        fields["Company LinkedIn"] = linkedin
    if founded:
        fields["Founded"] = founded
    if city:
        fields["City"] = city
    if state:
        fields["State/Region"] = state
    if country:
        fields["Country/Region"] = country
    return {"fields": fields, "revenue_dollars": max_rev,
            "revenue_code": _bucket_annual_revenue(max_rev) if max_rev else "", "providers": providers}


def find_email(first_name, last_name, full_name, company_name, domain, linkedin_url=""):
    """Returns:
      {
        "email": str,             # first VERIFIED email found (else "")
        "provider": str,          # provider that produced it (else "")
        "linkedin_url": str,      # backfilled from any provider that returned one
        "unverified_email": str,  # first non-verified email seen (for Notes)
        "unverified_note": str,   # human-readable note about the unverified hit
      }
    """
    linkedin_out = linkedin_url or ""
    unverified_email = ""
    unverified_note = ""

    # (label, callable) in waterfall order. Prospeo's lambda reads linkedin_out
    # at call time, so it benefits from any handle an earlier tier backfilled.
    tiers = [
        ("LeadMagic", lambda: leadmagic_client.find_email(first_name, last_name, company_name, domain)),
        ("Prospeo", lambda: prospeo_client.enrich_person(full_name, company_name, domain,
                                                         linkedin_url=linkedin_out)),
    ]
    for provider, call in tiers:
        res = call()
        li = (res.get("linkedin_url") or "").strip()
        if li and not linkedin_out:
            linkedin_out = li
        email = (res.get("email") or "").strip()
        if not email:
            continue
        # normalize the two client shapes: prospeo -> {"email","status",...};
        # leadmagic -> {"email","verified","raw_status",...}.
        if "verified" in res:
            verified, raw = res["verified"], res.get("raw_status", "")
        else:
            raw = res.get("status", "")
            verified = raw == "VERIFIED"
        if verified:
            return {"email": email, "provider": provider, "linkedin_url": linkedin_out,
                    "unverified_email": "", "unverified_note": ""}
        if not unverified_email:  # remember the first unverified hit for the Notes column
            unverified_email = email
            unverified_note = f"{provider} found an unverified email ({raw or 'unverified'}) -- not auto-used"

    return {"email": "", "provider": "", "linkedin_url": linkedin_out,
            "unverified_email": unverified_email, "unverified_note": unverified_note}


def find_mobile(full_name, first_name, last_name, company_name, domain, linkedin_url="", email=""):
    """Returns {"mobile": str, "provider": str}; ("", "") when nothing found or
    no tier is runnable. Prospeo works from name+company; Forager and LeadMagic
    both need a LinkedIn URL and skip themselves when it's absent."""
    tiers = [
        ("Prospeo", lambda: prospeo_client.find_mobile(full_name, company_name, domain,
                                                       linkedin_url=linkedin_url, email=email)),
        ("Forager", lambda: forager_client.find_mobile(linkedin_url)),
        ("LeadMagic", lambda: leadmagic_client.find_mobile(linkedin_url, work_email=email)),
    ]
    for provider, call in tiers:
        mobile = (call().get("mobile") or "").strip()
        if mobile:
            return {"mobile": mobile, "provider": provider}
    return {"mobile": "", "provider": ""}
