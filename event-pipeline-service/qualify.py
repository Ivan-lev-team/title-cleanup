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
import time

import anthropic

from classify_titles import classify as _classify_title
from icp_prompt import (
    ICP_PROMPT_4WAY as ICP_PROMPT,
    PHYSICAL_SYSTEM,
    build_user_block,
    parse_verdict,
)
import config

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

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
        data = parse_verdict(resp, ("PASS", "AGENCY", "TECH", "FAIL"), "PASS")
        return data["verdict"], data.get("reason", "")
    except Exception as e:
        return "PASS", f"could not parse ICP judgment ({e}) -- defaulted to PASS, review manually"


# ---------------------------------------------------------------------------
# Physical-product judge -- Shopify-export runs only.
#
# Deliberately SEPARATE from company_icp_judge. pipeline.py:104-121 branches on
# PASS/AGENCY/TECH and treats anything that isn't FAIL as a live lead, so
# feeding a different verdict space through that function would push unintended
# rows to HubSpot. This function is never called by the contact pipeline.
# ---------------------------------------------------------------------------
_PHYS_MODEL = "claude-sonnet-5"
_PHYS_ALLOWED = ("PASS", "FAIL", "UNRESOLVED")


def physical_product_judge(row, homepage_text=None, model=_PHYS_MODEL, max_retries=3):
    """Does this company sell a physical product? Returns a dict:

        {verdict, fail_reason, evidence, fulfillment, reason, error}

    verdict is PASS / FAIL / UNRESOLVED. UNRESOLVED is an INTERNAL routing
    state, never a final answer: the caller either sends the row for a homepage
    lookup (pass 2) or applies default-to-PASS. The emitted verdict space is
    PASS/FAIL only.

    High-capture posture: on repeated API or parse failure this returns PASS,
    not FAIL, flagged in `error` so those rows stay auditable.
    """
    user_block = build_user_block(row, homepage_text=homepage_text)
    last_err = None
    for attempt in range(max_retries):
        try:
            resp = _client.messages.create(
                model=model,
                max_tokens=500,
                system=[{
                    "type": "text",
                    "text": PHYSICAL_SYSTEM,
                    # the static block is identical for all ~98k calls; caching
                    # it turns the per-row input cost into a cache read
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_block}],
            )
            data = parse_verdict(resp, _PHYS_ALLOWED, "PASS")
            verdict = data["verdict"]
            # normalise the dependent fields rather than trusting the model
            fail_reason = data.get("fail_reason") if verdict == "FAIL" else None
            if verdict == "FAIL" and fail_reason not in (
                "service_digital_signal", "unconfirmed"
            ):
                fail_reason = "service_digital_signal"
            fulfillment = data.get("fulfillment") if verdict == "PASS" else None
            if verdict == "PASS" and fulfillment not in ("1PL", "3PL", "unknown"):
                fulfillment = "unknown"
            return {
                "verdict": verdict,
                "fail_reason": fail_reason,
                "evidence": (data.get("evidence") or "")[:400],
                "fulfillment": fulfillment,
                "reason": (data.get("reason") or "")[:300],
                "error": None,
            }
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(1.5 * (attempt + 1))
    return {
        "verdict": "PASS",
        "fail_reason": None,
        "evidence": "",
        "fulfillment": "unknown",
        "reason": "judge error, defaulted to PASS per high-capture policy",
        "error": str(last_err),
    }
