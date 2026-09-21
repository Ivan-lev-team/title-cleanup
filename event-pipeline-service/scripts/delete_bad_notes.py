"""
Delete the "no official brand website" notes written by a throttled
fix_list_domains.py run.

Background: a 565-company run at 14 workers hit ZenRows rate limiting. The
bounded fetch of the day had no 429 backoff, so nearly every homepage fetch
returned empty, no candidate was ever judged, and 492 companies were given a
note asserting that no brand website exists -- for companies that were never
actually checked (Anchor Hocking among them).

This removes only notes that carry BOTH signature phrases from that template AND
belong to a company on the supplied id list. Anything else on those companies --
notes from reps, other tooling, earlier runs -- is left untouched.

    python scripts/delete_bad_notes.py --ids-file data/bad_note_ids.txt --dry-run
    python scripts/delete_bad_notes.py --ids-file data/bad_note_ids.txt
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hubspot_client as hs
from fix_list_domains import BASE, log

# Both must be present. The pair is specific to the generated template, so a
# human-written note that happens to mention a missing website is never matched.
SIGNATURES = (
    "No official brand website could be verified for",
    "Domain left blank deliberately",
)


def notes_for(company_id):
    resp = hs.request_with_retry(
        "GET", f"{BASE}/crm/v4/objects/companies/{company_id}/associations/notes")
    if resp.status_code >= 300:
        return []
    return [r["toObjectId"] for r in resp.json().get("results", [])]


def note_bodies(note_ids):
    out = {}
    for batch in hs.chunked(note_ids, 100):
        resp = hs.request_with_retry(
            "POST", f"{BASE}/crm/v3/objects/notes/batch/read",
            json={"properties": ["hs_note_body"],
                  "inputs": [{"id": str(i)} for i in batch]})
        if resp.status_code >= 300:
            continue
        for r in resp.json().get("results", []):
            out[r["id"]] = (r.get("properties") or {}).get("hs_note_body") or ""
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-file", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    company_ids = [i for i in open(args.ids_file).read().split(",") if i.strip()]
    log(f"scanning {len(company_ids)} companies for generated no-website notes")

    matched, skipped = [], 0
    for i, cid in enumerate(company_ids, 1):
        ids = notes_for(cid)
        if not ids:
            continue
        for note_id, body in note_bodies(ids).items():
            if all(sig in body for sig in SIGNATURES):
                matched.append(note_id)
            else:
                skipped += 1
        if i % 100 == 0:
            log(f"  scanned {i}/{len(company_ids)}, {len(matched)} matched")

    log(f"\nnotes matching the generated template : {len(matched)}")
    log(f"other notes on these companies (kept)  : {skipped}")

    if args.dry_run:
        log("dry run -- nothing deleted")
        return

    deleted = 0
    for batch in hs.chunked(matched, 100):
        resp = hs.request_with_retry(
            "POST", f"{BASE}/crm/v3/objects/notes/batch/archive",
            json={"inputs": [{"id": str(i)} for i in batch]})
        if resp.status_code >= 300:
            log(f"  batch archive failed: {resp.status_code} {resp.text[:200]}")
            continue
        deleted += len(batch)
    log(f"deleted {deleted} notes")


if __name__ == "__main__":
    main()
