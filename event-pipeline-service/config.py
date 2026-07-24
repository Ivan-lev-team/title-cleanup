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
PROSPEO_KEY = os.environ.get("PROSPEO_KEY", "")  # optional -- enrichment skipped if blank
GOOGLE_SERVICE_ACCOUNT_JSON = _require("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = _require("GOOGLE_SHEET_ID")

CONTACT_SHEET_NAME = os.environ.get("CONTACT_SHEET_NAME", "Contact Import")
COMPANY_SHEET_NAME = os.environ.get("COMPANY_SHEET_NAME", "Company Import")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
LEAD_SOURCE_VALUE = os.environ.get("LEAD_SOURCE_VALUE", "Event/Tradeshow")

# When true: all HubSpot reads/searches still run live, but every
# create/update/associate call is skipped and logged instead of executed.
# Lets you test qualify -> dedupe -> enrich -> round-robin end to end
# against real HubSpot data before ever writing to it.
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

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

# Lifecyclestage values (this portal's actual internal values, not labels --
# see HubSpot's "lifecyclestage" enumeration) that mean "already a known,
# engaged account" -- these should never get re-tagged as a fresh event lead
# or have enrichment credits spent on them. Found via a real incident: an
# existing contact (already Sales Qualified Lead, tied to a company with 6
# closed deals and lifecyclestage "customer"/Termed) got swept into an event
# push and mis-tagged hs_lead_status=NEW because this check didn't exist yet.
EXCLUDED_LIFECYCLE_STAGES = {
    "customer",      # "Termed" in this portal's picklist -- a former/lapsed customer
    "252225308",     # "Working" -- actively being worked by sales
    "1195095563",    # "Sales Qualified Lead"
    "53309303",      # "Trial"
    "53292289",      # "Paid Monthly"
    "252145708",     # "Winback"
}
