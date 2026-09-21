"""
DeBounce email verification client -- the verification gate in front of the
Seamless pilot flow (see scripts/seamless_pilot.py).

Mirrors leadmagic_client / prospeo_client shape: every function returns a
normalized dict and returns the empty result when DEBOUNCE_KEY is blank, so
DeBounce drops out of the flow just by leaving the key unset.

Auth: key as a query param (`api` / `email`). Base https://api.debounce.io/v1.

Policy note: DeBounce is deliberately the ONLY verifier here that is not also
an email finder. Seamless, Prospeo and LeadMagic all return their own validity
opinion on addresses they themselves sold us; DeBounce is the independent
check. Only a DeBounce "valid" verdict is ever treated as pushable, matching
the strict posture in leadmagic_client._VERIFIED_EMAIL_STATUSES.
"""
import time
import requests

import config
import ratelimit

BASE = "https://api.debounce.io/v1"

# DeBounce result codes (the `code` field on a /v1 response), mapped to the
# three buckets this pipeline cares about.
#
# CONFIRMED by live tests 2026-09-18:
#   4 -> "Risky"        (accept-all/catch-all; returned for BOTH a real and a
#                        fabricated mailbox on levanta.io, which is catch-all)
#   5 -> "Safe to Send" (valid; marc@researchanddesign.com)
#   6 -> "Invalid"      (non-existent domain, and a wrong-pattern mailbox at a
#                        real domain: marc.conaway@researchanddesign.com)
# Codes 0/1/2/3/7/8 are from DeBounce's published list and not yet observed
# live. `raw_status` is always captured and the fallback below reads it when a
# code is unmapped, so a wrong guess surfaces rather than silently passing.
_CODE_MEANING = {
    "0": "unknown",            # could not verify
    "1": "invalid",            # syntax error
    "2": "invalid",            # unreachable / no MX
    "3": "invalid",            # disposable
    "4": "risky",              # accept-all / catch-all           [CONFIRMED]
    "5": "valid",              # deliverable / safe to send       [UNCONFIRMED]
    "6": "invalid",            # invalid                          [CONFIRMED]
    "7": "risky",              # role account (info@, sales@)
    "8": "unknown",            # unknown
}

# Only this verdict is auto-pushable. "risky" (catch-all) and "unknown" are
# parked for review rather than discarded -- the caller keeps the row with its
# status so the audit stays honest about what we could and could not verify.
VERIFIED_STATUSES = {"valid"}

_EMPTY = {
    "status": "",
    "code": "",
    "raw_status": "",
    "verified": False,
    "free_email": "",
    "role": "",
    "did_you_mean": "",
    "send_transactional": "",
    "provider": "DeBounce",
    "error": "",
}


def _get(path, params, tries=6):
    """GET with the retry posture used across this repo's clients: honor
    Retry-After on 429, exponential backoff on 5xx and connection errors,
    up to `tries` attempts. Returns the Response, or None if all attempts
    failed to connect."""
    url = f"{BASE}{path}"
    resp = None
    for attempt in range(tries):
        ratelimit.DEBOUNCE.acquire()
        try:
            resp = requests.get(url, params=params, timeout=30)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429:
            time.sleep(ratelimit.retry_after_seconds(resp) + 1)
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        return resp
    return resp


def verify_email(email):
    """Verify one address.

    Returns a dict with:
      status      -- normalized verdict: valid | invalid | risky | unknown
      code        -- DeBounce's raw numeric code (string)
      raw_status  -- DeBounce's own textual result
      verified    -- True only when status == "valid" (the pushable gate)
      free_email  -- "true"/"false" per DeBounce
      role        -- "true"/"false", role account (info@, sales@, ...)
      did_you_mean-- DeBounce's typo suggestion, when offered
      error       -- populated when the call itself failed; status stays ""

    Returns the empty result (all fields blank, verified False) when the key is
    unset or the input is empty -- never raises, so a verification outage
    degrades the flow instead of killing a run mid-way.
    """
    out = dict(_EMPTY)
    if not config.DEBOUNCE_KEY:
        out["error"] = "DEBOUNCE_KEY not set"
        return out
    email = (email or "").strip()
    if not email:
        return out

    resp = _get("/", {"api": config.DEBOUNCE_KEY, "email": email})
    if resp is None:
        out["error"] = "connection failed after retries"
        return out
    if resp.status_code >= 300:
        out["error"] = f"http {resp.status_code}: {resp.text[:120]}"
        return out

    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        out["error"] = f"non-json response: {resp.text[:120]}"
        return out

    # DeBounce nests the verdict under "debounce"; "success" is "1"/"0".
    if str(data.get("success", "1")) == "0":
        out["error"] = str(data.get("debounce", {}).get("error") or data.get("error") or "unsuccessful")
        return out

    d = data.get("debounce") or {}
    code = str(d.get("code") or "").strip()
    raw = (d.get("result") or "").strip()
    status = _CODE_MEANING.get(code, "")
    if not status:
        # Fall back to DeBounce's textual result if an unmapped code appears,
        # so a new code surfaces as its own label rather than silently passing.
        low = raw.lower()
        if low in ("safe to send", "deliverable"):
            status = "valid"
        elif low in ("accept-all", "accept all", "catch-all", "unknown"):
            status = "risky" if "all" in low else "unknown"
        elif low:
            status = "invalid"

    out.update({
        "status": status,
        "code": code,
        "raw_status": raw,
        "verified": status in VERIFIED_STATUSES,
        "free_email": str(d.get("free_email") or ""),
        "role": str(d.get("role") or ""),
        "did_you_mean": str(d.get("did_you_mean") or ""),
        "send_transactional": str(d.get("send_transactional") or ""),
    })
    return out


def account_balance():
    """Remaining DeBounce credits, for a pre-run check. Returns {} when the key
    is unset or the call fails."""
    if not config.DEBOUNCE_KEY:
        return {}
    resp = _get("/account/", {"api": config.DEBOUNCE_KEY})
    if resp is None or resp.status_code >= 300:
        return {}
    try:
        return resp.json() if resp.content else {}
    except ValueError:
        return {}
