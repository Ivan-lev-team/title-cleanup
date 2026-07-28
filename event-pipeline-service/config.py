import os
from dotenv import load_dotenv

load_dotenv()


def _require(name):
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


HUBSPOT_TOKEN = _require("HUBSPOT_TOKEN")
ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = _require("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = _require("GOOGLE_SHEET_ID")

# Enrichment providers. All optional -- each waterfall tier auto-skips when its
# key is blank (Forager also needs FORAGER_ACCOUNT_ID, which is part of every
# Forager API path). Waterfall order is fixed in enrichment.py:
#   email  = LeadMagic -> Prospeo
#   mobile = Prospeo -> Forager -> LeadMagic
PROSPEO_KEY = os.environ.get("PROSPEO_KEY", "")
LEADMAGIC_KEY = os.environ.get("LEADMAGIC_KEY", "")
FORAGER_KEY = os.environ.get("FORAGER_KEY", "")
FORAGER_ACCOUNT_ID = os.environ.get("FORAGER_ACCOUNT_ID", "")

# Mobile lookups are the expensive tier (Prospeo ~10cr, Forager 15cr,
# LeadMagic 5cr per hit), so they're gated behind this flag. Email enrichment
# always runs; set ENRICH_MOBILE=false to skip the mobile waterfall entirely.
ENRICH_MOBILE = os.environ.get("ENRICH_MOBILE", "true").strip().lower() == "true"

# Company REVENUE enrichment (brand-only, pod-gated): for a Brand not already in
# a pod, look up estimated annual revenue by domain and bucket it into the
# estimated_annual_revenue codes. Waterfall order (each tier skips if its key is
# blank): StoreLeads (ecommerce-native, most accurate) -> LeadMagic (headcount-
# derived band, only used when >= $1M) -> Prospeo (reliable numeric band).
STORELEADS_KEY = os.environ.get("STORELEADS_KEY", "")
REVENUE_ORDER = [p.strip() for p in os.environ.get("REVENUE_ORDER", "storeleads,leadmagic,prospeo").split(",") if p.strip()]
ENRICH_REVENUE = os.environ.get("ENRICH_REVENUE", "true").strip().lower() == "true"

CONTACT_SHEET_NAME = os.environ.get("CONTACT_SHEET_NAME", "Contact Import")
COMPANY_SHEET_NAME = os.environ.get("COMPANY_SHEET_NAME", "Company Import")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
LEAD_SOURCE_VALUE = os.environ.get("LEAD_SOURCE_VALUE", "Event/Tradeshow")

# Marketing-team HubSpot field mapping (see the "post-event upload" requirements doc).
# Contact Type: the ICP judge's verdict maps to one of the contact_type property's
# existing options. Brand -> sales/Pod funnel; Agency + Tech Partner -> Partnerships.
CONTACT_TYPE_BRAND = os.environ.get("CONTACT_TYPE_BRAND", "eCommerce Brand")
CONTACT_TYPE_AGENCY = os.environ.get("CONTACT_TYPE_AGENCY", "Agency")
CONTACT_TYPE_TECH = os.environ.get("CONTACT_TYPE_TECH", "Technology Provider")

# Qualification (contact property gold___ent__qualification, options
# Qualified / Disqualified / Needs Further Qualification -- there is no
# "Unqualified", so not-qualified maps to Disqualified). A contact is Qualified
# when its company is a Brand with estimated annual revenue >= $1M (the
# estimated_annual_revenue codes below) OR the company already has a Pod.
QUALIFICATION_PROPERTY = "gold___ent__qualification"
# estimated_annual_revenue option codes that count as >= $1M: 3=$1M-$10M, 4=$10M+
QUALIFIED_REVENUE_CODES = set(os.environ.get("QUALIFIED_REVENUE_CODES", "3,4").split(","))

# Marketing Event (object 0-54) attribution: after pushing a contact, record its
# attendance on the HubSpot Marketing Event whose name matches the row's Event
# Name, so it shows up for marketing attribution. Requires the
# crm.objects.marketing_events.read + .write scopes; degrades to a no-op (just a
# note) if the scope is missing or no event matches the name.
MARKETING_EVENT_ENABLED = os.environ.get("MARKETING_EVENT_ENABLED", "true").strip().lower() == "true"
# subscriber state to record: REGISTERED (everyone on an uploaded list) or ATTENDED
MARKETING_EVENT_STATE = os.environ.get("MARKETING_EVENT_STATE", "REGISTERED").strip().upper()

# When true: all HubSpot reads/searches still run live, but every
# create/update/associate call is skipped and logged instead of executed.
# Lets you test qualify -> dedupe -> enrich -> round-robin end to end
# against real HubSpot data before ever writing to it.
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

# ENRICH_ONLY mode: run qualify (ICP) + enrichment (email/mobile/revenue) + a
# read-only HubSpot status check (already-in-HubSpot / customer / open deal),
# then write the classified+enriched result back to the sheet -- NO routing, NO
# push, NO HubSpot writes at all. Turns the pipeline into a pure list
# enrichment + ICP scoring tool. Leave false for the full event pipeline.
ENRICH_ONLY = os.environ.get("ENRICH_ONLY", "false").strip().lower() == "true"

HUBSPOT_BASE = "https://api.hubapi.com"

# Round-robin pods, validated against the Social Commerce Summit 2026 NYC run.
# Pod RevOps intentionally excluded. Update this table if the pod roster changes --
# owner-vs-pod fit is picked live off HubSpot company counts, not a fixed cycle position,
# so this table only needs to stay accurate on WHICH owners belong to WHICH pod.
POD_OWNERS = {
    "Pod 1": ["80046048", "87811820"],
    "Pod 2": ["87755705", "91884999"],
    "Pod 3": ["82954422", "91884994"],
    "Pod 4": ["91884997", "83840015"],
    "Pod 5": ["87811818", "89148921"],
    "Pod 6": ["86070116"],
    "Pod 7": ["87811816", "87811817"],
}

# Agencies (Anthropic ICP judge verdict "AGENCY") are not Levanta's direct
# ICP but are valuable channel partners -- routed to Partnerships instead of
# the normal Pod round-robin. "pod" is intentionally left blank for these
# companies (HubSpot's "pod" enumeration has no Partnerships value); only
# sdr_owner is set, round-robined between these two. Per the Partnerships
# team's "ICP for PDRs when Prospecting" doc, this covers BOTH agency
# sub-types (Amazon/Walmart/Shopify service agencies AND Influencer/
# Affiliate/Digital Marketing agencies) -- the doc's other listed PDRs for
# the marketing-agency sub-type (Jose Alvarado, Iliana Santos) are no longer
# on the team, so both sub-types share this one pair for now.
PARTNERSHIP_OWNERS = ["87811821", "82954574"]  # Katerina Timchevska, Milosh Dimitrijevikj
