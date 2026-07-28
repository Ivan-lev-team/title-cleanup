"""
Step 1 of the pipeline: qualify a row BEFORE spending any enrichment credits or
even checking HubSpot. Two independent checks:
  - title_qualify: is this person's job title a decision-maker function?
    (reuses the exact classify_titles.py rules validated on the original project)
  - company_icp_judge: is this company itself a fit for Levanta's ICP (a brand
    selling physical consumer products via Amazon/Walmart/Shopify)?
    Calls the Anthropic API directly since this runs unattended on a server,
    not inside Claude Code -- same judgment rule used for the Social Commerce
    Summit 2026 NYC company screen, just as a single live API call per company
    instead of a batch of parallel agents.
"""
import json
import anthropic

from classify_titles import classify as _classify_title
import config

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

ICP_PROMPT = """Levanta is an affiliate/creator marketing platform. Its ICP (ideal customer profile) is: a brand that sells PHYSICAL CONSUMER PRODUCTS through Amazon, Walmart, and/or Shopify/DTC ecommerce. Levanta's customers are those brands (they buy affiliate marketing services to recruit creators/publishers to promote their products).

IS a fit (should PASS): any company whose core business is MAKING or SELLING PHYSICAL PRODUCTS of any kind. This includes consumer product brands across every category (beauty, wellness/supplements, food & beverage, apparel, home goods, pet products, electronics/accessories, toys, etc.) AND business/industrial/enterprise product companies (hardware, equipment, devices, manufacturing, components, etc.) -- even small/unknown ones, and regardless of whether they sell via Amazon/Walmart/Shopify/DTC or through B2B/wholesale/enterprise channels. If the company sells a physical product, it PASSES. (Broadened per marketing: e.g. an enterprise hardware maker counts, not only DTC consumer brands.)

AGENCY (verdict "AGENCY" -- Partnerships funnel, not a rejection): an agency (not a brand) whose clients are the kind of brands Levanta sells to, in one of these two categories:
  1. Amazon/Walmart/Shopify service agency -- offers services like full account management, advertising management (DSP, Paid Social, Marketplace), or listing management, for brand clients selling on Amazon/Walmart/Shopify.
  2. Influencer/Affiliate/Digital Marketing agency -- offers services like Amazon affiliate marketing management, influencer marketing management, DTC affiliate marketing, performance PR, TikTok Shop marketing/affiliate, or general digital marketing, for brand clients.
  Primary fit for either category: US-based agency whose clients primarily sell in the US Amazon market. Secondary fit: European-based agency whose clients primarily sell in the US Amazon market (even if the agency's own client base is distributed internationally). These aren't Levanta's direct ICP customer, but they're valuable channel/referral partners for the Partnerships team.

TECH PARTNER (verdict "TECH" -- Partnerships/technology funnel, NOT a rejection): a SaaS, software, or technology-platform company (not a brand, not an agency) that plausibly relates to ecommerce, retail, marketplaces, marketing, or commerce enablement -- e.g. ecommerce tools, marketplace/retail/ad tech, PIM/ERP/analytics, TikTok Shop or affiliate tech, or commerce platforms. Any software/tech platform that could be a technology or integration partner goes here. These go to the Partnerships team, not the sales team.

NOT a fit at all (should FAIL): companies that do NOT sell a physical product and are not an agency or tech platform -- i.e. pure SERVICE businesses (dental/medical practices, salons, photography studios, handyman/home-repair services, restaurants), financial institutions/banks/insurance, law firms, management/financial consultancies, nonprofits/religious/government organizations, trade associations, event/conference organizers, staffing firms, and ISPs/telecom portals. Also FAIL: companies you cannot identify at all or that appear to be spam/junk/placeholder entries. (Note: a company that genuinely sells physical products is a PASS even if B2B/enterprise -- only route it to FAIL when it clearly sells no product and is not an agency/tech platform.)

Company name: {company}
Domain: {domain}

Respond with ONLY a JSON object, no other text: {{"verdict": "PASS" or "AGENCY" or "TECH" or "FAIL", "reason": "one short sentence"}}"""


def title_qualify(title):
    """Returns (verdict, reason). Empty title defaults to PASS, matching the
    original project's rule 7 (ambiguous/unknown title -> default pass)."""
    return _classify_title(title or "")


def company_icp_judge(company, domain):
    """Returns (verdict, reason) for a single company via one Anthropic call.
    Falls back to PASS with a warning reason if the model response can't be
    parsed -- never silently drops a row on a parsing hiccup."""
    if not company:
        return "FAIL", "no company name provided"

    prompt = ICP_PROMPT.format(company=company, domain=domain or "(no domain provided)")
    resp = _client.messages.create(
        model="claude-sonnet-5",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        # response may lead with a thinking block on newer models -- take the
        # first text block rather than blindly assuming content[0] is text
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "").strip()
        # model may wrap the JSON in a code fence despite instructions; strip if so
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        data = json.loads(text)
        verdict = data.get("verdict", "").upper()
        reason = data.get("reason", "")
        if verdict not in ("PASS", "AGENCY", "TECH", "FAIL"):
            raise ValueError(f"unexpected verdict value: {verdict}")
        return verdict, reason
    except Exception as e:
        return "PASS", f"could not parse ICP judgment ({e}) -- defaulted to PASS, review manually"
