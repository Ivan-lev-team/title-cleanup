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
import leadmagic_client
import forager_client
import prospeo_client
import storeleads_client
import zenrows_client
import config


_FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.ca", "yahoo.co.uk", "ymail.com",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com", "icloud.com",
    "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me", "gmx.com", "comcast.net",
    "verizon.net", "att.net", "sbcglobal.net", "mail.com", "zoho.com",
}


def is_free_email_domain(domain):
    return (domain or "").strip().lower() in _FREE_EMAIL_DOMAINS


def resolve_identity(full_name, email, linkedin_url=""):
    """For a row missing a company: resolve {linkedin, company_name, domain,
    title, provider} from a (often personal) email + name. Waterfall:
    Forager reverse-email -> Prospeo reverse-email (both one-shot: linkedin +
    company), then LeadMagic email->profile, then profile->company via LeadMagic
    or Prospeo. Returns whatever was found (may be partial: a linkedin with no
    company for solo sellers). Each tier auto-skips when its key is missing."""
    out = {"linkedin": linkedin_url or "", "company_name": "", "domain": "", "title": "", "provider": ""}

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
