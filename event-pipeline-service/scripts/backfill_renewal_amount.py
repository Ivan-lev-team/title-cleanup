#!/usr/bin/env python3
"""
Backfill Amount on renewal deals from the company's OG contract deal.

Designed to run hourly from cron. Idempotent by construction: it only ever
targets deals whose amount is already empty, so a re-run after a partial
failure simply finishes the job. Nothing is ever overwritten.

Correctness over coverage. A deal is filled only when every one of these holds:

  1. pipeline 68563319, created_by_renewal_workflow = true, amount empty
  2. the company has EXACTLY ONE qualifying OG deal
     (default pipeline, hs_is_closed_won = true, amount > 0)
  3. the company does NOT have multiple live tagged renewal deals
     at 90-day / 60-day Notice  (duplicate-wave guard)
  4. neither the deal nor the company name contains TEST / ZZZ / XXXX

Deliberately absent: any "most recent" tiebreak. Picking the latest closed-won
deal selects small add-ons over the main contract (observed live: PETIQ 3,600
over 14,400; Upper Echelon 1,200 over 5,400). If the OG deal is not unique the
company is skipped for manual review rather than guessed at.

Usage:
    python scripts/backfill_renewal_amount.py              # dry run (default)
    python scripts/backfill_renewal_amount.py --live       # apply
    python scripts/backfill_renewal_amount.py --live --quiet   # cron
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

SVC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SVC)

import config                                    # noqa: E402
import hubspot_client as hs                      # noqa: E402

RENEWAL_PIPELINE = "68563319"
OG_PIPELINE = "default"
LIVE_NOTICE_STAGES = {"183442735", "183442736"}  # 90-day Notice, 60-day Notice
TEST_MARKERS = ("TEST", "ZZZ", "XXXX")
REQUIRED_SCOPES = ("crm.objects.deals.read", "crm.objects.deals.write")

LOG_DIR = os.path.join(SVC, "logs")
LOG_FILE = os.path.join(LOG_DIR, "backfill_renewal_amount.jsonl")

DEAL_PROPS = ["dealname", "pipeline", "dealstage", "amount",
              "hs_is_closed_won", "created_by_renewal_workflow"]


# --------------------------------------------------------------------------
# startup: fail fast if the token cannot do the job
# --------------------------------------------------------------------------
def assert_deal_scopes():
    """The pipeline's HUBSPOT_TOKEN is scoped for contacts/companies. This job
    also needs deal read+write. Fail before touching anything rather than
    half-running and leaving a partial backfill behind."""
    resp = hs.request_with_retry(
        "POST", f"{hs.BASE}/oauth/v2/private-apps/get/access-token-info",
        json={"tokenKey": config.HUBSPOT_TOKEN})

    if resp is not None and resp.status_code < 300:
        scopes = set(resp.json().get("scopes") or [])
        missing = [s for s in REQUIRED_SCOPES if s not in scopes]
        if missing:
            sys.exit(
                f"FATAL: HUBSPOT_TOKEN is missing required scope(s): {', '.join(missing)}.\n"
                f"       Grant them on the private app (Settings -> Integrations ->\n"
                f"       Private Apps -> Scopes), then re-run. The existing token keeps\n"
                f"       working once the scope is added; no need to reissue.\n"
                f"       Token currently has {len(scopes)} scope(s).")
        return "verified"

    # token-info unavailable: probe deal read directly, and be explicit that
    # write cannot be proven without writing.
    probe = hs.request_with_retry("GET", f"{hs.BASE}/crm/v3/objects/deals?limit=1")
    if probe is None or probe.status_code == 403:
        sys.exit("FATAL: HUBSPOT_TOKEN cannot read deals (403). "
                 "Grant crm.objects.deals.read and crm.objects.deals.write.")
    if probe.status_code >= 300:
        sys.exit(f"FATAL: deal read probe failed: {probe.status_code} {probe.text[:200]}")
    return "read-only probe (write scope unverified until first write)"


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
def search_blank_renewal_deals():
    out, after = [], None
    while True:
        body = {"filterGroups": [{"filters": [
            {"propertyName": "pipeline", "operator": "EQ", "value": RENEWAL_PIPELINE},
            {"propertyName": "created_by_renewal_workflow", "operator": "EQ", "value": "true"},
            {"propertyName": "amount", "operator": "NOT_HAS_PROPERTY"}]}],
            "properties": DEAL_PROPS, "limit": 100}
        if after:
            body["after"] = after
        resp = hs.request_with_retry(
            "POST", f"{hs.BASE}/crm/v3/objects/deals/search", json=body)
        if resp.status_code >= 300:
            raise RuntimeError(f"deal search failed: {resp.status_code} {resp.text[:300]}")
        page = resp.json()
        out += page.get("results", [])
        after = page.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return out


def batch_assoc(from_type, to_type, ids):
    """v4 batch association read -- one call per 100 ids instead of one per record."""
    pairs = defaultdict(list)
    for batch in hs.chunked(list(ids), 100):
        resp = hs.request_with_retry(
            "POST", f"{hs.BASE}/crm/v4/associations/{from_type}/{to_type}/batch/read",
            json={"inputs": [{"id": str(i)} for i in batch]})
        if resp.status_code >= 300:
            raise RuntimeError(f"assoc {from_type}->{to_type} failed: "
                               f"{resp.status_code} {resp.text[:300]}")
        for row in resp.json().get("results", []):
            src = str(row["from"]["id"])
            pairs[src] += [str(t["toObjectId"]) for t in row.get("to", [])]
    return pairs


def batch_read(object_type, ids, properties):
    out = {}
    for batch in hs.chunked(list(ids), 100):
        resp = hs.request_with_retry(
            "POST", f"{hs.BASE}/crm/v3/objects/{object_type}/batch/read",
            json={"properties": properties, "inputs": [{"id": str(i)} for i in batch]})
        if resp.status_code >= 300:
            raise RuntimeError(f"{object_type} batch read failed: "
                               f"{resp.status_code} {resp.text[:300]}")
        for r in resp.json().get("results", []):
            out[r["id"]] = r["properties"]
    return out


def as_amount(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def is_test(*names):
    blob = " ".join(n or "" for n in names).upper()
    return any(m in blob for m in TEST_MARKERS)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true",
                      help="apply the writes")
    mode.add_argument("--dry-run", action="store_true",
                      help="report only, change nothing (this is the default)")
    ap.add_argument("--quiet", action="store_true",
                    help="summary only, no per-deal listing (use in cron)")
    args = ap.parse_args()
    live = args.live

    started = datetime.now(timezone.utc)
    print(f"backfill_renewal_amount  {started.isoformat(timespec='seconds')}  "
          f"{'LIVE' if live else 'DRY RUN'}")
    print(f"  token scopes: {assert_deal_scopes()}")

    targets = search_blank_renewal_deals()
    if not targets:
        print("  nothing to do: no renewal deals with a blank amount.")
        return

    deal_to_co = batch_assoc("deals", "companies", [d["id"] for d in targets])
    company_ids = {c for v in deal_to_co.values() for c in v}
    companies = batch_read("companies", company_ids, ["name"])
    co_to_deals = batch_assoc("companies", "deals", company_ids)

    sibling_ids = {d for v in co_to_deals.values() for d in v}
    siblings = batch_read("deals", sibling_ids, DEAL_PROPS)

    fills, skips, log = [], Counter(), []

    for t in targets:
        did = t["id"]
        dp = t["properties"]
        cids = deal_to_co.get(did, [])
        cid = cids[0] if cids else None
        cname = companies.get(cid, {}).get("name") if cid else None

        def note(action, og=None, amount=None):
            skips[action] += 1
            log.append({"ts": started.isoformat(), "deal_id": did,
                        "dealname": dp.get("dealname"), "company_id": cid,
                        "company_name": cname, "og_deal_id": og,
                        "og_amount": amount, "action": action})

        if is_test(dp.get("dealname"), cname):
            note("skipped-test")
            continue
        if not cid:
            note("skipped-no-company")
            continue

        pool = {sid: siblings[sid] for sid in co_to_deals.get(cid, []) if sid in siblings}

        # guard: duplicate wave -- more than one live tagged renewal deal
        live_renewals = [s for s, p in pool.items()
                         if p.get("pipeline") == RENEWAL_PIPELINE
                         and p.get("created_by_renewal_workflow") == "true"
                         and p.get("dealstage") in LIVE_NOTICE_STAGES]
        if len(live_renewals) > 1:
            note("skipped-dup-wave")
            continue

        # the OG contract deal must be unique -- never a "most recent" tiebreak
        ogs = [(s, as_amount(p.get("amount"))) for s, p in pool.items()
               if p.get("pipeline") == OG_PIPELINE
               and p.get("hs_is_closed_won") == "true"
               and as_amount(p.get("amount")) is not None]
        if not ogs:
            note("skipped-no-og")
            continue
        if len({amt for _, amt in ogs}) > 1 or len(ogs) > 1:
            note("skipped-multi-og")
            continue

        og_id, og_amount = ogs[0]
        fills.append({"deal_id": did, "dealname": dp.get("dealname"),
                      "company_id": cid, "company_name": cname,
                      "og_deal_id": og_id, "og_amount": og_amount})

    # ---------------- report ----------------
    if fills and not args.quiet:
        print(f"\n  {'RENEWAL DEAL':<46} {'AMOUNT':>11}  OG DEAL")
        print("  " + "-" * 78)
        for f in sorted(fills, key=lambda x: (x["dealname"] or "").lower()):
            print(f"  {(f['dealname'] or '')[:46]:<46} {f['og_amount']:>11,.0f}  {f['og_deal_id']}")

    print(f"\n  candidates scanned : {len(targets)}")
    print(f"  would fill         : {len(fills)}" if not live else
          f"  filling            : {len(fills)}")
    for reason, n in sorted(skips.items()):
        print(f"    {reason:<22} {n}")
    print(f"  total amount       : {sum(f['og_amount'] for f in fills):,.0f}")

    if not live:
        print("\nDRY RUN. Nothing was sent. Re-run with --live to apply.")
        _write_log(log + [{**f, "ts": started.isoformat(), "action": "would-fill"}
                          for f in fills], started, dry=True)
        return

    # ---------------- apply ----------------
    if fills:
        inputs = [{"id": f["deal_id"], "properties": {"amount": str(f["og_amount"])}}
                  for f in fills]
        hs.batch_update("deals", inputs)          # batches of 100, raises on failure

        # verify: re-read and confirm every value landed
        check = batch_read("deals", [f["deal_id"] for f in fills], ["amount"])
        good = sum(1 for f in fills
                   if as_amount(check.get(f["deal_id"], {}).get("amount")) == f["og_amount"])
        print(f"  verified           : {good}/{len(fills)} now carry the expected amount")
        for f in fills:
            f["verified"] = (as_amount(check.get(f["deal_id"], {}).get("amount"))
                             == f["og_amount"])

    _write_log(log + [{**f, "ts": started.isoformat(), "action": "filled"} for f in fills],
               started, dry=False)
    print(f"  log                : {LOG_FILE}")


def _write_log(entries, started, dry):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps({**e, "dry_run": dry}) + "\n")


if __name__ == "__main__":
    main()
