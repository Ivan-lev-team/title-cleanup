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
PROSPEO_API_KEY = os.environ.get("PROSPEO_API_KEY", "")  # optional -- enrichment skipped if blank
GOOGLE_SERVICE_ACCOUNT_JSON = _require("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = _require("GOOGLE_SHEET_ID")

CONTACT_SHEET_NAME = os.environ.get("CONTACT_SHEET_NAME", "Contact Import")
COMPANY_SHEET_NAME = os.environ.get("COMPANY_SHEET_NAME", "Company Import")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
LEAD_SOURCE_VALUE = os.environ.get("LEAD_SOURCE_VALUE", "Event/Tradeshow")

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
