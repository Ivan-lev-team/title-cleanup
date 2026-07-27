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
import config


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
