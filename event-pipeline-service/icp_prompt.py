"""Single source of truth for the ICP prompts.

Deliberately dependency-free (no `config` import) so both the server pipeline
(`qualify.py`) and the standalone batch scripts (`scripts/*.py`) can import it
without needing the full `.env` to be present.

Two prompts live here:
  - ICP_PROMPT_4WAY       : the contact-pipeline judge (PASS/AGENCY/TECH/FAIL).
                            Lifted verbatim from qualify.py; the drifted copy
                            that used to live in scripts/icp_judge_run.py is
                            gone.
  - PHYSICAL_SYSTEM       : the physical-product judge for Shopify-export runs
                            (PASS/FAIL). Split into a static system block and a
                            per-company user block so the static half can be
                            prompt-cached across the whole batch.
"""
import json

# ---------------------------------------------------------------------------
# 1. Contact-pipeline judge (4-way). Verbatim from qualify.py -- do not edit
#    without also re-validating pipeline.py's PASS/AGENCY/TECH branches.
# ---------------------------------------------------------------------------
ICP_PROMPT_4WAY = """Levanta is an affiliate/creator marketing platform. Its ICP (ideal customer profile) is: a brand that sells PHYSICAL CONSUMER PRODUCTS through Amazon, Walmart, and/or Shopify/DTC ecommerce. Levanta's customers are those brands (they buy affiliate marketing services to recruit creators/publishers to promote their products).

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


# ---------------------------------------------------------------------------
# 2. Physical-product judge (PASS/FAIL). Static half -- cacheable.
# ---------------------------------------------------------------------------
PHYSICAL_SYSTEM = """You are screening Shopify stores for ONE question only:

Does this company sell a PHYSICAL product -- a tangible good that is
manufactured or stocked, packed, and SHIPPED to the customer?

That is the only question. Do not assess company size, quality, sales channel,
partnership potential, or whether the company would be a good customer.

=== CRITICAL GUARDRAIL ===
A live Shopify store is NOT evidence of a physical product. Courses,
memberships, digital downloads, services, bookings, and gift-card/donation
stores all run on Shopify and must FAIL.

Every company in this file is an active Shopify store, so that fact carries
ZERO information and must never be cited as a reason to PASS. High sales, a
large installed-app count, and a polished storefront are likewise NOT evidence
of a physical product. Reason only from what the store actually sells.

The reverse also holds: the ABSENCE of any of those signals is never a reason
to FAIL.

=== BURDEN OF PROOF: DEFAULT TO PASS ===
This population is already revenue-qualified US/Canada ecommerce, so selling a
physical product is the DEFAULT state, not the thing that must be proven.

- Assume PHYSICAL unless there is an AFFIRMATIVE service/digital signal.
- Absence of proof is NOT a fail reason. A thin, vague, or generic description
  is a PASS, not a FAIL.
- FAIL requires positive evidence that the core business is something other
  than shipping tangible goods.

=== MIXED BUSINESSES PASS ===
An affirmative service or digital signal only causes FAIL when it accounts for
essentially the WHOLE business. If the company also ships tangible goods, PASS.
A bookstore selling ebooks alongside printed books is a PASS. A fitness company
selling equipment plus a subscription is a PASS. A restaurant group that also
ships packaged goods is a PASS. A museum with a gift shop is a PASS.

=== WHAT COUNTS AS AN AFFIRMATIVE SERVICE/DIGITAL SIGNAL (-> FAIL) ===
The core business is:
  - Digital products only: courses, masterclasses, coaching programs, ebooks,
    printables, templates, presets, stock media, software, apps, plugins, fonts
  - Memberships, communities, or content subscriptions only
  - Services performed for the customer: salons, clinics, studios, repair,
    cleaning, photography, legal, financial, medical, consulting
  - Bookings, appointments, reservations, tickets, events, travel
  - Gift cards, donations, fundraising, or charity-only stores
  - Software, SaaS, or technology platforms of any kind
  - Agencies of any kind, including Amazon/Walmart/Shopify service agencies and
    influencer/affiliate agencies
  - Media, publishing, or licensing that ships nothing

On THIS run, agencies and technology platforms are FAIL. They do not sell a
physical product.

=== EVIDENCE HIERARCHY (strongest first) ===
1. description / meta_description, where they name what is actually sold
2. categories path
3. installed apps (see the asymmetry rule below)
4. company name and domain -- weakest, corroboration only

Where fields conflict, prefer the description over the category path; a category
path can be stale or wrong. A blank field is not evidence.

=== THE APP RULE IS DELIBERATELY ASYMMETRIC ===
- Physical-fulfillment apps are a NEAR-AIRTIGHT PASS. If the store runs
  ShipBob, ShipStation, ShipMonk, Loop Returns, Returnly, AfterShip, Printful,
  Printify, Route, Easyship, Shippo, Happy Returns, Narvar, ReturnGO,
  ShipperHQ, Shiprocket, Amazon MCF, Deliverr, Navidium, Redo, or an address
  validator, it is shipping parcels. PASS.
- Digital-delivery and booking apps are only a WEIGHTED SIGNAL, subordinate to
  category and description. SendOwl, Sky Pilot, FetchApp, Digital Downloads,
  Thinkific, Teachable, Uscreen, Bold Memberships, Sesami, Appointo and
  BookThatApp appear constantly on genuine physical brands -- a jewellery brand
  books try-on appointments, a rocket-kit maker sells a PDF manual, a tea shop
  books tastings. NEVER FAIL a store on a digital/booking app alone. Only treat
  it as corroboration when the description or category ALSO points digital.

=== NON-DECISIVE CATEGORY ROOTS ===
/Arts & Entertainment and /People & Society are NOT decisive on their own. They
cover art prints, instruments and collectibles (physical) as well as music,
media, religion and community organisations (often not). If one of those two
roots is the ONLY physical evidence, do not PASS on the category alone --
require the description to confirm an actual physical product. Do NOT hard-FAIL
them either. If the description confirms goods, PASS; if the description
affirmatively shows a service/digital business, FAIL; if the description is
silent or absent, return UNRESOLVED.

=== NOT-OPERATING IS THE ONE EXCEPTION TO DEFAULT-TO-PASS ===
Default-to-PASS does NOT apply to a store that is not operating. If the domain
is parked, expired, for sale, under construction, empty, or the content is
placeholder/spam/lorem-ipsum, return FAIL with fail_reason "unconfirmed".
"I cannot tell what they sell" is a PASS. "There is no evidence this store
exists or operates" is a FAIL/unconfirmed. Do not conflate the two.

=== UNRESOLVED ===
Return verdict "UNRESOLVED" ONLY when the supplied fields contain essentially
no usable information at all (no meaningful description, no category, no apps)
and you would be guessing. UNRESOLVED is not a soft FAIL. It routes the row to
a homepage lookup. If there is ANY usable evidence, commit to PASS or FAIL.

=== FULFILLMENT (independent enrichment field) ===
Answer this ONLY after the verdict is decided, and ONLY when the verdict is
PASS. It must NEVER influence the verdict. On FAIL or UNRESOLVED, return null.
  - "3PL": ShipBob, ShipMonk, Deliverr, Amazon MCF, "fulfilled by", or fast
    multi-region shipping promises.
  - "1PL": "ships from our warehouse", made-to-order, single location, or long
    handling times.
  - "unknown": anything else. Most brands never disclose this. A large
    "unknown" tail is EXPECTED and correct. Guessing is a defect -- stamp
    "unknown" honestly.

=== OUTPUT ===
Respond with ONLY a JSON object, no other text:
{"verdict": "PASS" or "FAIL" or "UNRESOLVED",
 "fail_reason": "service_digital_signal" or "unconfirmed" or null,
 "evidence": "the specific field text that decided it, quoted",
 "fulfillment": "1PL" or "3PL" or "unknown" or null,
 "reason": "one short sentence"}

fail_reason is null unless verdict is FAIL. fulfillment is null unless verdict
is PASS. evidence must quote actual supplied text, never "runs on Shopify",
"active store", sales figures, or app counts."""


_MAXLEN = 600


def _clip(s, n=_MAXLEN):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + " ..."


def build_user_block(row, homepage_text=None):
    """Per-company half of the physical-product prompt. Kept short so the big
    static system block dominates the cache."""
    g = lambda k: (row.get(k) or "").strip()
    sales = g("estimated_yearly_sales").replace("USD $", "").replace(",", "")
    parts = [
        f"Name: {g('title') or '(blank)'}",
        f"Domain: {g('domain') or '(blank)'}",
        f"Categories: {g('categories') or '(blank)'}",
        f"Description: {_clip(g('description')) or '(blank)'}",
        f"Meta description: {_clip(g('meta_description')) or '(blank)'}",
        f"Installed apps (colon-delimited; app names may themselves contain "
        f"colons): {_clip(g('installed_apps_names'), 900) or '(none listed)'}",
        f"Estimated yearly store sales (USD): {sales or '(unknown)'}",
    ]
    if homepage_text:
        parts.append(
            "\n--- HOMEPAGE TEXT (fetched because the fields above were empty) ---\n"
            + _clip(homepage_text, 3000)
        )
    return "\n".join(parts)


def parse_verdict(resp, allowed, default_verdict):
    """Shared JSON extraction for both judges.

    Takes the first text block (newer models may lead with a thinking block),
    strips a code fence if present, validates the verdict against `allowed`.
    Returns a dict on success. Raises on failure so the caller decides the
    default -- the two judges default in opposite directions.
    """
    text = next(
        (b.text for b in resp.content if getattr(b, "type", None) == "text"), ""
    ).strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    data = json.loads(text)
    verdict = (data.get("verdict") or "").upper()
    if verdict not in allowed:
        raise ValueError(f"unexpected verdict value: {verdict!r}")
    data["verdict"] = verdict
    return data
