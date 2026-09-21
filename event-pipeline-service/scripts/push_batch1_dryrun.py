"""
DRY RUN ONLY -- writes nothing to HubSpot.

Plans a push of the enriched Batch 1 contacts to HubSpot, routing each contact to
its company's existing owner (sdr_owner / hubspot_owner_id / pod), and reports
exactly what would be created vs updated vs skipped.

Two dedupe passes matter here:
  - LinkedIn URL: already done during screening.
  - EMAIL: never done -- the screening run had no Email column ("No Email column
    found -- skipping email match"). The 1,480 emails we enriched afterwards have
    never been checked against HubSpot, so without this pass a push would create
    duplicate contacts.

Usage: python push_batch1_dryrun.py <enriched.csv> <outdir>
"""
import csv
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hubspot_client as hc  # noqa: E402

CONTACT_SEARCH = f"{hc.BASE}/crm/v3/objects/contacts/search"
BATCH = 100


def norm_domain(d):
    d = (d or "").lower().strip()
    return re.sub(r"^https?://", "", d).split("/")[0]


def find_contacts_by_email(emails):
    """{email_lower: contact_id} for emails already in HubSpot."""
    found = {}
    batches = [emails[i:i + BATCH] for i in range(0, len(emails), BATCH)]
    for bi, b in enumerate(batches):
        body = {
            "filterGroups": [{"filters": [{"propertyName": "email", "operator": "IN", "values": b}]}],
            "properties": ["email", "hubspot_owner_id", "jobtitle", "mobilephone", "phone"],
            "limit": 100,
        }
        after = None
        while True:
            if after:
                body["after"] = after
            d = hc.request_with_retry("POST", CONTACT_SEARCH, json=body).json()
            for c in d.get("results", []):
                em = (c.get("properties", {}).get("email") or "").lower()
                if em:
                    found.setdefault(em, c)
            after = (d.get("paging", {}).get("next", {}) or {}).get("after")
            if not after:
                break
        print(f"  email dedupe batch {bi+1}/{len(batches)}: {len(found)} matched so far", flush=True)
        time.sleep(0.2)
    return found


def companies_by_domain(domains):
    """{domain: [company records]} -- keeps ALL matches so duplicates are visible."""
    out = defaultdict(list)
    for batch in hc.chunked(sorted({d for d in domains if d}), hc.DOMAIN_BATCH_SIZE):
        body = {
            "filterGroups": [{"filters": [{"propertyName": "domain", "operator": "IN", "values": batch}]}],
            "properties": ["name", "domain", "sdr_owner", "pod", "hubspot_owner_id"],
            "limit": 100,
        }
        after = None
        while True:
            if after:
                body["after"] = after
            d = hc.request_with_retry("POST", f"{hc.BASE}/crm/v3/objects/companies/search", json=body).json()
            for r in d.get("results", []):
                dom = norm_domain(r["properties"].get("domain"))
                if dom:
                    out[dom].append(r)
            after = (d.get("paging", {}).get("next", {}) or {}).get("after")
            if not after:
                break
    return out


def main():
    if len(sys.argv) != 3:
        print("Usage: python push_batch1_dryrun.py <enriched.csv> <outdir>")
        sys.exit(1)
    in_path, outdir = sys.argv[1], Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(in_path, dtype=str, keep_default_na=False)
    df["_domain"] = df["Company Domain"].map(norm_domain)
    df["_email"] = df["Email"].str.strip().str.lower()
    df["_mobile"] = df["Mobile Phone Number"].str.strip()
    print(f"{len(df)} enriched contacts loaded", flush=True)

    # ---- company lookup (owner routing) ----
    print("resolving companies by domain...", flush=True)
    comp = companies_by_domain(df["_domain"].tolist())
    print(f"  {len(comp)} domains matched a HubSpot company", flush=True)

    owner_cache = {}

    def oname(oid):
        oid = (oid or "").strip()
        if not oid:
            return ""
        if oid not in owner_cache:
            owner_cache[oid] = hc.get_owner_name(oid) or oid
        return owner_cache[oid]

    # ---- EMAIL dedupe (the pass that was never run) ----
    emails = sorted({e for e in df["_email"] if e})
    print(f"checking {len(emails)} enriched emails against HubSpot...", flush=True)
    existing_by_email = find_contacts_by_email(emails)
    print(f"  {len(existing_by_email)} of those emails ALREADY exist in HubSpot", flush=True)

    rows = []
    for _, r in df.iterrows():
        dom = r["_domain"]
        recs = comp.get(dom, [])
        # prefer a company that actually has an sdr_owner set
        chosen = next((x for x in recs if (x["properties"].get("sdr_owner") or "").strip()), recs[0] if recs else None)

        if chosen:
            p = chosen["properties"]
            sdr_id = (p.get("sdr_owner") or "").strip()
            comp_owner = (p.get("hubspot_owner_id") or "").strip()
            company_id, company_nm, pod = chosen["id"], p.get("name") or "", (p.get("pod") or "").strip()
        else:
            sdr_id = comp_owner = company_id = company_nm = pod = ""

        em = r["_email"]
        existing = existing_by_email.get(em) if em else None

        if existing:
            action = "UPDATE_EXISTING_CONTACT"
        elif not em and not r["_mobile"]:
            action = "SKIP_NO_EMAIL_NO_MOBILE"
        elif not em:
            action = "CREATE_MOBILE_ONLY"
        else:
            action = "CREATE"

        rows.append({
            "full_name": r.get("Full Name", ""),
            "first_name": r.get("First Name", ""),
            "last_name": r.get("Last Name", ""),
            "job_title": r.get("Job Title", ""),
            "company_name_source": r.get("Company Table Data", ""),
            "domain": dom,
            "email": em,
            "email_source": r.get("Email Source", ""),
            "mobile": r["_mobile"],
            "mobile_source": r.get("Mobile Source", ""),
            "linkedin": r.get("LinkedIn Profile", ""),
            "action": action,
            "existing_contact_id": existing["id"] if existing else "",
            "hubspot_company_id": company_id,
            "hubspot_company_name": company_nm,
            "company_duplicates": len(recs) if len(recs) > 1 else "",
            "route_sdr_owner_id": sdr_id,
            "route_sdr_owner_name": oname(sdr_id),
            "route_company_owner_id": comp_owner,
            "route_company_owner_name": oname(comp_owner),
            "route_pod": pod,
        })

    out = pd.DataFrame(rows)
    out.to_csv(outdir / "PUSH_DRYRUN_batch1.csv", index=False, quoting=csv.QUOTE_MINIMAL)

    print("\n=========== DRY RUN SUMMARY (nothing written) ===========")
    print("\nActions:")
    for a, n in out["action"].value_counts().items():
        print(f"  {a:26s} {n}")

    print("\nRouting by SDR Owner (contacts that would be created/updated):")
    live = out[out["action"] != "SKIP_NO_EMAIL_NO_MOBILE"]
    for nm, n in live["route_sdr_owner_name"].replace("", "(no SDR owner)").value_counts().items():
        print(f"  {nm:26s} {n}")

    print(f"\nNo HubSpot company matched: {(out['hubspot_company_id'] == '').sum()} contacts")
    print(f"Ambiguous (domain has >1 company record): {(out['company_duplicates'] != '').sum()} contacts")
    print(f"\nWould CREATE: {out['action'].str.startswith('CREATE').sum()}")
    print(f"Would UPDATE: {(out['action'] == 'UPDATE_EXISTING_CONTACT').sum()}")
    print(f"Would SKIP:   {(out['action'] == 'SKIP_NO_EMAIL_NO_MOBILE').sum()}")
    print(f"\nDetail -> {outdir / 'PUSH_DRYRUN_batch1.csv'}")


if __name__ == "__main__":
    main()
