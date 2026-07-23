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

NOT a fit (should FAIL): social/tech/media platforms, pure B2B SaaS/software companies, marketing/PR/creative agencies, management consultancies, law firms, financial institutions/banks/insurance, trade associations, staffing/talent agencies, event/conference organizers, and services companies that sell TO brands rather than being a brand themselves. Also FAIL: companies you cannot identify at all or that appear to be spam/junk/placeholder entries.

IS a fit (should PASS): any company that is itself a physical consumer product brand across any category (beauty, wellness/supplements, food & beverage, apparel, home goods, pet products, electronics accessories, toys, etc.) that plausibly sells via Amazon/Walmart/Shopify/DTC -- even small/unknown brands, as long as they're a genuine product brand, not a service provider.

Company name: {company}
Domain: {domain}

Respond with ONLY a JSON object, no other text: {{"verdict": "PASS" or "FAIL", "reason": "one short sentence"}}"""


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
    text = resp.content[0].text.strip()
    try:
        # model may wrap the JSON in a code fence despite instructions; strip if so
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        data = json.loads(text)
        verdict = data.get("verdict", "").upper()
        reason = data.get("reason", "")
        if verdict not in ("PASS", "FAIL"):
            raise ValueError(f"unexpected verdict value: {verdict}")
        return verdict, reason
    except Exception as e:
        return "PASS", f"could not parse ICP judgment ({e}) -- defaulted to PASS, review manually"
