#!/usr/bin/env python3
"""Levanta event-lead operations: one entry point for the bulk flow.

    Clay -> sheet -> enrich -> assign owner/pod -> bulk push to HubSpot

Every command is read-only by default. Nothing writes without --mode, and
--mode test writes only the first 5 records, so a bad mapping costs 5 rows
and not 700. All writes FILL BLANKS ONLY: a value already in HubSpot or in
the sheet is never overwritten, because anything a human has already touched
is treated as the system of record.

    python scripts/event_ops.py audit
    python scripts/event_ops.py assign-routing --mode test
    python scripts/event_ops.py assign-routing --mode apply
    python scripts/event_ops.py all --mode apply      # the standard sequence

Requires GOOGLE_SERVICE_ACCOUNT_JSON pointing at a real service-account file
(scp it from /etc/levanta-event-pipeline/google_service_account.json) plus
GOOGLE_SHEET_ID and HUBSPOT_TOKEN in the service .env.
"""
import argparse
import collections
import difflib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gspread
import requests

import config
import enrichment

HEADERS = {"Authorization": f"Bearer {config.HUBSPOT_TOKEN}", "Content-Type": "application/json"}
BASE = "https://api.hubapi.com"

CONTACT_TAB = "Contact Import"
PARTNER_TAB = config.PARTNERSHIP_SHEET_NAME or "Agencies/Tech"
EVENT_LIST = config.EVENT_LIST_ID                              # 7887, DYNAMIC
PARTNER_LIST = os.environ.get("PARTNERSHIP_LIST_ID", "8037")   # 8037, MANUAL

VALID_PODS = {"Pod 1", "Pod 2", "Pod 3", "Pod 4", "Pod 5", "Pod 6", "Pod 7",
              "Pod RevOps", "Pod Partnership"}
# The sheet used to say "Partnership"; HubSpot's pod enum only has "Pod Partnership".
POD_ALIASES = {"Partnership": "Pod Partnership"}

# 87811820 (Milosh Mihajlovikj) is ARCHIVED in HubSpot. Excluded from new
# round-robin picks, and unresolvable if the raw id turns up in a sheet cell.
ARCHIVED_OWNER_IDS = {"87811820"}

PARTNERSHIP_SPLIT = 3  # Katerina : Milosh, 3:1


# ------------------------------------------------------------------ helpers --

def hs(method, path, **kw):
    return requests.request(method, f"{BASE}{path}", headers=HEADERS, timeout=30, **kw)


def chunks(seq, n=100):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def paged(path, key="results"):
    out, after = [], None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        j = hs("GET", path, params=params).json()
        out += j.get(key, [])
        after = (j.get("paging") or {}).get("next", {}).get("after")
        if not after:
            return out


def g(d, k):
    return (d.get(k) or "").strip()


def open_sheet():
    return gspread.service_account(
        filename=config.GOOGLE_SERVICE_ACCOUNT_JSON).open_by_key(config.GOOGLE_SHEET_ID)


def read_tab(sheet, name):
    values = sheet.worksheet(name).get_all_values()
    header = values[0]
    rows = [(i, dict(zip(header, r))) for i, r in enumerate(values[1:], start=2)
            if any(x.strip() for x in r)]
    return header, rows


def sheet_by_email(sheet, tabs=None):
    """Every sheet row keyed by email; the first tab listed wins a collision."""
    out = {}
    for tab in (tabs or [CONTACT_TAB, PARTNER_TAB]):
        for _, d in read_tab(sheet, tab)[1]:
            e = g(d, "Email").lower()
            if "@" in e and e not in out:
                out[e] = d
    return out


def owner_ids():
    """Owner display name -> id, archived owners excluded, so a stale name in
    the sheet resolves to nothing rather than to a deactivated user."""
    out = {}
    for o in paged("/crm/v3/owners"):
        if str(o["id"]) in ARCHIVED_OWNER_IDS:
            continue
        out[f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()] = str(o["id"])
    return out


def contacts_by_email(emails):
    """email -> {id, properties}, keyed on the address we asked for."""
    out = {}
    props = ["email", "firstname", "lastname", "jobtitle", "phone", "city", "state",
             "country", "sdr_owner", "lifecyclestage", "associatedcompanyid",
             "how_did_you_hear_about_us___drill_down", "gold___ent__qualification"]
    asked = {e.lower() for e in emails}
    for b in chunks(emails):
        j = hs("POST", "/crm/v3/objects/contacts/batch/read",
               json={"idProperty": "email", "properties": props,
                     "inputs": [{"id": e} for e in b]}).json()
        for o in j.get("results", []):
            e = (o["properties"].get("email") or "").lower()
            # HubSpot can match on a SECONDARY address, returning an email we
            # never asked for. Keying on that would KeyError against the sheet
            # index, so such a record is dropped rather than acted on with a
            # row that is not actually its own.
            if e in asked:
                out[e] = o
    return out


COMPANY_PROPS = ["domain", "name", "pod", "sdr_owner", "annualrevenue", "numberofemployees",
                 "linkedin_company_page", "founded_year", "industry", "lifecyclestage"]


def companies_by_id(ids):
    out = {}
    for b in chunks(ids):
        j = hs("POST", "/crm/v3/objects/companies/batch/read",
               json={"properties": COMPANY_PROPS, "inputs": [{"id": x} for x in b]}).json()
        for o in j.get("results", []):
            out[o["id"]] = o["properties"]
    return out


def companies_by_domain(domains):
    """domain -> {id, properties}. `domain` is not a unique idProperty, so
    batch/read cannot key on it; search is the only thing that works."""
    out = {}
    for b in chunks(domains):
        after = None
        while True:
            body = {"filterGroups": [{"filters": [
                        {"propertyName": "domain", "operator": "IN", "values": b}]}],
                    "properties": COMPANY_PROPS, "limit": 100}
            if after:
                body["after"] = after
            j = hs("POST", "/crm/v3/objects/companies/search", json=body).json()
            for o in j.get("results", []):
                out.setdefault((o["properties"].get("domain") or "").lower(), o)
            after = (j.get("paging") or {}).get("next", {}).get("after")
            if not after:
                break
    return out


def list_members(list_id):
    return [x["recordId"] for x in paged(f"/crm/v3/lists/{list_id}/memberships")]


def write_batch(obj, updates, mode, label):
    """updates: {id: {property: value}}. Returns records actually written."""
    items = [{"id": k, "properties": v} for k, v in updates.items() if v]
    if mode == "test":
        items = items[:5]
    if mode == "dry" or not items:
        return 0
    done = 0
    for b in chunks(items):
        r = hs("POST", f"/crm/v3/objects/{obj}/batch/update", json={"inputs": b})
        if r.status_code < 300:
            done += len(b)
        else:
            print(f"   {label} FAILED {r.status_code}: {r.text[:200]}")
    return done


def sheet_write(worksheet, header, cells, mode):
    """cells: [(row_number, column_name, value)]. Batched: per-cell updates
    blow past Sheets' 60 writes/minute cap and 429 the whole run."""
    if mode == "test":
        cells = cells[:5]
    if mode == "dry" or not cells:
        return 0
    data = [{"range": gspread.utils.rowcol_to_a1(r, header.index(c) + 1), "values": [[v]]}
            for r, c, v in cells]
    for b in chunks(data, 500):
        worksheet.batch_update(b, value_input_option="USER_ENTERED")
    return len(data)


# -------------------------------------------------------------- normalizers --

def clean_phone(v):
    """Phone -> E.164, or "" when it cannot be trusted.

    HubSpot stores whatever it is handed, so a list arrives with
    '(877) 835-3010', '929.286.2526' and bare '6049100595' side by side.
    10 digits is assumed US; 11+ is assumed to already carry a country code;
    anything shorter is dropped rather than guessed at."""
    v = (v or "").strip()
    if not v:
        return ""
    plus = v.startswith("+")
    digits = re.sub(r"\D", "", v)
    if not digits:
        return ""
    if plus:
        return "+" + digits if 7 <= len(digits) <= 15 else ""
    if len(digits) == 10:
        return "+1" + digits
    if 11 <= len(digits) <= 15:
        return "+" + digits
    return ""


def clean_int(v):
    """'3097799.28' -> '3097799'. HubSpot number fields mangle decimals."""
    v = (v or "").strip().replace(",", "")
    if not v:
        return ""
    m = re.match(r"^-?(\d+)(\.\d+)?$", v)
    return m.group(1) if m else re.sub(r"\D", "", v)


def clean_year(v):
    v = clean_int(v)
    return v if v and 1600 <= int(v) <= 2100 else ""


def clean_linkedin(v):
    """Bare 'linkedin.com/company/x' -> a real URL. A numeric id is not a page,
    so it is dropped rather than stored as a broken link."""
    v = (v or "").strip()
    if not v or v.isdigit():
        return ""
    if v.lower().startswith("http"):
        return v
    if "linkedin.com" in v.lower():
        return "https://" + v.lstrip("/")
    return ""


def clean_pod(v):
    v = POD_ALIASES.get((v or "").strip(), (v or "").strip())
    return v if v in VALID_PODS else ""


# ------------------------------------------------------------- industry map --

INDUSTRY_ALIASES = {
    "retail": "RETAIL",
    "manufacturing": "CONSUMER_GOODS",
    "advertising services": "MARKETING_AND_ADVERTISING",
    "marketing services": "MARKETING_AND_ADVERTISING",
    "marketing and advertising": "MARKETING_AND_ADVERTISING",
    "wellness and fitness services": "HEALTH_WELLNESS_AND_FITNESS",
    "consumer goods": "CONSUMER_GOODS",
    "personal care product manufacturing": "COSMETICS",
    "face and body care": "COSMETICS",
    "make-up and cosmetics": "COSMETICS",
    "cosmetics": "COSMETICS",
    "food and beverage services": "FOOD_BEVERAGES",
    "food and beverage manufacturing": "FOOD_PRODUCTION",
    "food and beverages": "FOOD_BEVERAGES",
    "retail apparel and fashion": "APPAREL_FASHION",
    "clothing accessories": "APPAREL_FASHION",
    "apparel": "APPAREL_FASHION",
    "business consulting and services": "MANAGEMENT_CONSULTING",
    "pharmaceutical manufacturing": "PHARMACEUTICALS",
    "pharmaceuticals": "PHARMACEUTICALS",
    "software development": "COMPUTER_SOFTWARE",
    "retail health and personal care products": "RETAIL",
    "medical equipment manufacturing": "MEDICAL_DEVICES",
    "appliances; electrical; and electronics manufacturing": "ELECTRICAL_ELECTRONIC_MANUFACTURING",
    "computers and electronics manufacturing": "CONSUMER_ELECTRONICS",
    "internet": "INTERNET",
    "technology; information and internet": "INFORMATION_TECHNOLOGY_AND_SERVICES",
    "vitamins and supplements": "HEALTH_WELLNESS_AND_FITNESS",
    "gardening and landscaping": "CONSUMER_GOODS",
    "media production and publishing": "MEDIA_PRODUCTION",
    "wholesale": "WHOLESALE",
    "sporting goods": "SPORTING_GOODS",
    "furniture": "FURNITURE",
    "toys and games": "CONSUMER_GOODS",
    "household appliances": "CONSUMER_ELECTRONICS",
    "jewelry": "LUXURY_GOODS_JEWELRY",
    "pet products": "CONSUMER_GOODS",
    "automotive": "AUTOMOTIVE",
    "e-commerce": "INTERNET",
    "ecommerce": "INTERNET",
    # --- second pass, from the values this corpus actually produced ---
    "human resources services": "HUMAN_RESOURCES",
    "motor vehicle manufacturing": "AUTOMOTIVE",
    "autos and vehicles": "AUTOMOTIVE",
    "consumer products - automotive accessories": "AUTOMOTIVE",
    "perfumes and fragrances": "COSMETICS",
    "skin and nail care": "COSMETICS",
    "skincare / scar treatment products": "COSMETICS",
    "skin conditions": "HEALTH_WELLNESS_AND_FITNESS",
    "sleep disorders": "HEALTH_WELLNESS_AND_FITNESS",
    "food": "FOOD_BEVERAGES",
    "home and interior decor": "FURNITURE",
    "home appliances": "CONSUMER_ELECTRONICS",
    "home and garden": "CONSUMER_GOODS",
    "interior design": "DESIGN",
    "furniture and home furnishings manufacturing": "FURNITURE",
    "furniture (tables and chairs manufacturing/wholesale)": "FURNITURE",
    "wood product manufacturing": "PAPER_FOREST_PRODUCTS",
    "business services": "MANAGEMENT_CONSULTING",
    "general manufacturing": "CONSUMER_GOODS",
    "sporting goods manufacturing": "SPORTING_GOODS",
    "it services and it consulting": "INFORMATION_TECHNOLOGY_AND_SERVICES",
    "it system custom software development": "COMPUTER_SOFTWARE",
    "transportation; logistics; supply chain and storage": "LOGISTICS_AND_SUPPLY_CHAIN",
    "warehousing and storage": "WAREHOUSING",
    "appliances, electrical, and electronics manufacturing": "ELECTRICAL_ELECTRONIC_MANUFACTURING",
    "wholesale appliances; electrical; and electronics": "WHOLESALE",
    "wholesale metals and minerals": "MINING_METALS",
    "wholesale import and export": "IMPORT_AND_EXPORT",
    "wholesale distribution": "WHOLESALE",
    "retail office equipment": "BUSINESS_SUPPLIES_AND_EQUIPMENT",
    "online and mail order retail": "RETAIL",
    "online fashion retail": "APPAREL_FASHION",
    "e-commerce services": "INTERNET",
    "e-commerce marketing agency": "MARKETING_AND_ADVERTISING",
    "amazon marketplace management / e-commerce agency": "MARKETING_AND_ADVERTISING",
    "chemicals industry": "CHEMICALS",
    "plastics manufacturing": "PLASTICS",
    "textile manufacturing": "TEXTILES",
    "packaging and containers manufacturing": "PACKAGING_AND_CONTAINERS",
    "nursery and playroom": "CONSUMER_GOODS",
    "pet services": "VETERINARY",
    "animal health / pet products": "VETERINARY",
    "entertainment providers": "ENTERTAINMENT",
    "artists and writers": "WRITING_AND_EDITING",
}


def industry_options():
    j = hs("GET", "/crm/v3/properties/companies/industry").json()
    return [o["value"] for o in j.get("options", [])]


def map_industry(value, options, cutoff=0.86):
    """Sheet industry text -> a real HubSpot enum option, or "".

    companies.industry is a 148-option enum and HubSpot SILENTLY DROPS a value
    that is not one of them, so an unmapped write looks like success while
    nothing lands. Exact, then the curated alias table, then a strict fuzzy
    match. Below the cutoff we return "" and report it unmapped rather than
    guess a wrong industry onto a real account."""
    v = (value or "").strip()
    if not v:
        return ""
    if v.upper().replace(" ", "_") in options:
        return v.upper().replace(" ", "_")
    key = v.lower().replace("&", "and").strip()
    if key in INDUSTRY_ALIASES:
        return INDUSTRY_ALIASES[key]
    pretty = {o: o.replace("_", " ").lower() for o in options}
    match = difflib.get_close_matches(key, list(pretty.values()), n=1, cutoff=cutoff)
    if match:
        return next(o for o, p in pretty.items() if p == match[0])
    return ""


# ------------------------------------------------------------------ commands --

def cmd_audit(args):
    """Report coverage across the sheet and both HubSpot lists. Read-only."""
    sheet = open_sheet()
    for tab in (CONTACT_TAB, PARTNER_TAB):
        _, rows = read_tab(sheet, tab)
        n = max(len(rows), 1)
        print(f"\n== sheet tab {tab!r}: {len(rows)} rows")
        for col in ("Email", "Company Domain", "Event Name", "POD", "SDR Owner", "Qualification"):
            have = sum(1 for _, d in rows if g(d, col))
            print(f"   {col:18} {have:5d}  {have / n * 100:5.1f}%")
        bad = [g(d, "POD") for _, d in rows if g(d, "POD") and not clean_pod(g(d, "POD"))]
        if bad:
            print(f"   INVALID POD values: {collections.Counter(bad).most_common()}")
    for lid, name in ((EVENT_LIST, "event list"), (PARTNER_LIST, "partnership list")):
        ids = list_members(lid)
        if not ids:
            print(f"\n== HubSpot list {lid} ({name}): 0 members")
            continue
        contacts = {}
        for b in chunks(ids):
            j = hs("POST", "/crm/v3/objects/contacts/batch/read",
                   json={"properties": ["sdr_owner", "jobtitle", "phone", "associatedcompanyid"],
                         "inputs": [{"id": x} for x in b]}).json()
            for o in j.get("results", []):
                contacts[o["id"]] = o["properties"]
        comp_ids = {g(p, "associatedcompanyid") for p in contacts.values()
                    if g(p, "associatedcompanyid")}
        comps = companies_by_id(comp_ids)
        n = len(ids)
        sdr = sum(1 for p in contacts.values() if g(p, "sdr_owner"))
        assoc = sum(1 for p in contacts.values() if g(p, "associatedcompanyid"))
        badph = sum(1 for p in contacts.values()
                    if g(p, "phone") and clean_phone(g(p, "phone")) != g(p, "phone"))
        print(f"\n== HubSpot list {lid} ({name}): {n} members")
        print(f"   contact sdr_owner   {sdr:5d}  {sdr / n * 100:5.1f}%")
        print(f"   associated company  {assoc:5d}  {assoc / n * 100:5.1f}%")
        print(f"   phone needs cleanup {badph:5d}")
        if comps:
            m = len(comps)
            for k in ("pod", "sdr_owner", "industry", "annualrevenue"):
                v = sum(1 for p in comps.values() if g(p, k))
                print(f"   company.{k:16} {v:5d}  {v / m * 100:5.1f}%  (of {m})")


def cmd_assign_routing(args):
    """Fill blank POD / SDR Owner on the Contact Import tab.

    Precedence: a value already in the cell, then the matched HubSpot
    company's pod/sdr_owner, then least-loaded round-robin. Rows that must not
    be worked (existing customer, open deal, ICP FAIL, Error) are left alone;
    Disqualified rows are skipped unless --with-disqualified."""
    sheet = open_sheet()
    ws = sheet.worksheet(CONTACT_TAB)
    header, rows = read_tab(sheet, CONTACT_TAB)
    by_id = {v: k for k, v in owner_ids().items()}
    active = {p: [o for o in ids if o not in ARCHIVED_OWNER_IDS]
              for p, ids in config.POD_OWNERS.items()}
    todo, skipped = [], collections.Counter()
    for rn, d in rows:
        if g(d, "POD") and g(d, "SDR Owner"):
            continue
        st, icp, qual = g(d, "Pipeline Status"), g(d, "ICP Verdict"), g(d, "Qualification")
        if st.startswith("Skipped"):
            skipped["existing customer / open deal"] += 1
            continue
        if st == "Rejected" or icp == "FAIL":
            skipped["ICP FAIL / Rejected"] += 1
            continue
        if st == "Error":
            skipped["Error row, re-run first"] += 1
            continue
        if qual == "Disqualified" and not args.with_disqualified:
            skipped["Disqualified"] += 1
            continue
        todo.append((rn, d))
    comps = companies_by_id({g(d, "HubSpot Company ID") for _, d in todo
                             if g(d, "HubSpot Company ID")})
    load = collections.Counter(g(d, "SDR Owner") for _, d in rows if g(d, "SDR Owner"))
    podload = collections.Counter(g(d, "POD") for _, d in rows if g(d, "POD"))
    cells, src = [], collections.Counter()
    for rn, d in todo:
        hsc = comps.get(g(d, "HubSpot Company ID"), {})
        pod, sdr = g(d, "POD"), g(d, "SDR Owner")
        if not pod and clean_pod(g(hsc, "pod")):
            pod = clean_pod(g(hsc, "pod"))
            src["pod from HubSpot"] += 1
        if not sdr and g(hsc, "sdr_owner"):
            sdr = by_id.get(g(hsc, "sdr_owner"), "")
            src["sdr from HubSpot"] += 1
        if g(d, "ICP Verdict") in ("AGENCY", "TECH"):
            if not pod:
                pod = "Pod Partnership"
                src["pod = Pod Partnership"] += 1
            if not sdr:
                pair = ([by_id.get(config.PARTNERSHIP_OWNERS[0], "")] * PARTNERSHIP_SPLIT
                        + [by_id.get(config.PARTNERSHIP_OWNERS[1], "")])
                sdr = min(pair, key=lambda nm: (load[nm], nm))
                load[sdr] += 1
                src["sdr from Partnership split"] += 1
        else:
            if not pod:
                pod = min(active, key=lambda x: (podload[x], x))
                podload[pod] += 1
                src["pod round-robin"] += 1
            if not sdr:
                cands = [by_id[o] for o in active.get(pod, []) if o in by_id]
                if cands:
                    sdr = min(cands, key=lambda nm: (load[nm], nm))
                    load[sdr] += 1
                    src["sdr round-robin"] += 1
                else:
                    src["no owners configured for " + pod] += 1
        if pod and not g(d, "POD"):
            cells.append((rn, "POD", pod))
        if sdr and not g(d, "SDR Owner"):
            cells.append((rn, "SDR Owner", sdr))
    print(f"eligible rows {len(todo)} | cells to write {len(cells)}")
    for k, n in skipped.most_common():
        print(f"   not routed, {k}: {n}")
    for k, n in src.most_common():
        print(f"   {k}: {n}")
    print(f"WROTE {sheet_write(ws, header, cells, args.mode)} cells ({args.mode})")


def cmd_mirror_partnership(args):
    """Append Partnership-owned rows onto the Agencies/Tech tab, de-duped on
    email, falling back to name + domain for rows that arrive without one."""
    sheet = open_sheet()
    ws = sheet.worksheet(PARTNER_TAB)
    header, existing = read_tab(sheet, PARTNER_TAB)
    by_id = {v: k for k, v in owner_ids().items()}
    pair = {by_id.get(o, "") for o in config.PARTNERSHIP_OWNERS} - {""}
    key = lambda d: "|".join([g(d, "Email").lower(), g(d, "First Name").lower(),
                              g(d, "Last Name").lower(), g(d, "Company Domain").lower()])
    seen = {key(d) for _, d in existing}
    out = []
    for _, d in read_tab(sheet, CONTACT_TAB)[1]:
        if g(d, "SDR Owner") not in pair or key(d) in seen:
            continue
        seen.add(key(d))
        out.append([str(d.get(c, "")) for c in header])
    print(f"rows to append to {PARTNER_TAB!r}: {len(out)}")
    if args.mode == "test":
        out = out[:5]
    if args.mode != "dry" and out:
        ws.append_rows(out, value_input_option="RAW", table_range="A1")
        print(f"APPENDED {len(out)} rows ({args.mode})")
    else:
        print(f"APPENDED 0 rows ({args.mode})")


def _pushable(sheet):
    """Sheet rows eligible to push: real domain, mappable event, Qualified."""
    rows, out, why = sheet_by_email(sheet), [], collections.Counter()
    for e, d in rows.items():
        if not enrichment.valid_company_domain(g(d, "Company Domain")):
            why["no valid domain"] += 1
            continue
        ev = config.EVENT_NAME_TO_HUBSPOT.get(g(d, "Event Name"))
        if not ev:
            why["event not mappable: " + (g(d, "Event Name") or "(blank)")] += 1
            continue
        if g(d, "Qualification") not in ("Qualified", "PASS"):
            why["not Qualified"] += 1
            continue
        out.append((e, d, ev))
    return out, why


def cmd_push_event_list(args):
    """Make sheet rows qualify for the dynamic event list.

    7887 is DYNAMIC: there is no membership to add, so this writes the two
    properties its filter reads and lets the list re-evaluate. The list's own
    filter already excludes customers, opt-outs and late-stage deals."""
    sheet = open_sheet()
    cand, why = _pushable(sheet)
    print(f"candidates {len(cand)}")
    for k, n in why.most_common(6):
        print(f"   excluded, {k}: {n}")
    found = contacts_by_email([e for e, _, _ in cand])
    upd, create = {}, []
    for e, d, ev in cand:
        hit = found.get(e)
        props = {"how_did_you_hear_about_us___drill_down": ev,
                 "gold___ent__qualification": "Qualified"}
        if hit:
            if g(hit["properties"], "lifecyclestage") == "customer":
                continue
            props = {k: v for k, v in props.items() if not g(hit["properties"], k)}
            if props:
                upd[hit["id"]] = props
        else:
            props.update({"email": e, "firstname": g(d, "First Name"),
                          "lastname": g(d, "Last Name")})
            if g(d, "Job Title"):
                props["jobtitle"] = g(d, "Job Title")
            create.append({"properties": props})
    print(f"to update {len(upd)} | to create {len(create)}")
    print(f"updated {write_batch('contacts', upd, args.mode, 'contacts')}")
    if args.mode == "test":
        create = create[:5]
    made = 0
    if args.mode != "dry":
        for b in chunks(create):
            r = hs("POST", "/crm/v3/objects/contacts/batch/create", json={"inputs": b})
            if r.status_code < 300:
                made += len(b)
                continue
            # A batch create dies on the FIRST conflict, so fall back per
            # record and recover the existing id out of the 409 message.
            for one in b:
                r2 = hs("POST", "/crm/v3/objects/contacts", json=one)
                if r2.status_code < 300:
                    made += 1
                elif r2.status_code == 409:
                    m = re.search(r"Existing ID:\s*(\d+)", r2.text)
                    if m:
                        hs("PATCH", "/crm/v3/objects/contacts/" + m.group(1),
                           json={"properties": one["properties"]})
                        made += 1
    print(f"created/merged {made} ({args.mode})")


def cmd_push_partnership_list(args):
    """Add the Partnership-owned contacts to the MANUAL partnership list.

    8037 is MANUAL, so membership is set explicitly here, unlike 7887.
    Only Katerina's and Milosh's contacts belong on it."""
    sheet = open_sheet()
    by_id = {v: k for k, v in owner_ids().items()}
    pair = {by_id.get(o, "") for o in config.PARTNERSHIP_OWNERS} - {""}
    rows = sheet_by_email(sheet, tabs=[PARTNER_TAB, CONTACT_TAB])
    want = {e: d for e, d in rows.items() if g(d, "SDR Owner") in pair}
    found = contacts_by_email(list(want))
    keep = [o["id"] for e, o in found.items()
            if g(o["properties"], "lifecyclestage") != "customer"]
    current = set(list_members(PARTNER_LIST))
    add = [i for i in keep if i not in current]
    drop = [i for i in current if i not in set(keep)]
    print(f"partnership-owned sheet rows {len(want)} | resolved contacts {len(found)}")
    print(f"list {PARTNER_LIST}: currently {len(current)} | to add {len(add)} | to remove {len(drop)}")
    if args.mode == "test":
        add, drop = add[:5], drop[:5]
    if args.mode != "dry":
        for b in chunks(add):
            r = hs("PUT", f"/crm/v3/lists/{PARTNER_LIST}/memberships/add", json=b)
            print("   add:", r.status_code, len(b))
        if args.prune:
            for b in chunks(drop):
                r = hs("PUT", f"/crm/v3/lists/{PARTNER_LIST}/memberships/remove", json=b)
                print("   remove:", r.status_code, len(b))
        elif drop:
            print(f"   {len(drop)} non-partnership members left in place (pass --prune to remove)")
    print(f"DONE ({args.mode})")


def cmd_associate(args):
    """Associate pushed contacts that have no company to the company matching
    their sheet domain. Without one they inherit no pod, because pod is a
    company-level property in this portal."""
    sheet = open_sheet()
    rows = sheet_by_email(sheet)
    found = contacts_by_email(list(rows))
    doms = {}
    for e, o in found.items():
        p = o["properties"]
        if g(p, "associatedcompanyid") or g(p, "lifecyclestage") == "customer":
            continue
        dom = g(rows[e], "Company Domain").lower()
        if enrichment.valid_company_domain(dom):
            doms.setdefault(dom, []).append(o["id"])
    comps = companies_by_domain(list(doms))
    pairs = [(cid, comps[d]["id"]) for d, cids in doms.items() if d in comps for cid in cids]
    print(f"contacts lacking a company {sum(len(v) for v in doms.values())} "
          f"across {len(doms)} domains")
    print(f"matched companies {len(comps)} | associations to make {len(pairs)}")
    missing = [d for d in doms if d not in comps]
    if missing:
        print(f"   no HubSpot company exists for {len(missing)}: {missing[:8]}")
    if args.mode == "test":
        pairs = pairs[:5]
    if args.mode != "dry":
        for b in chunks(pairs):
            r = hs("POST", "/crm/v4/associations/contacts/companies/batch/associate/default",
                   json={"inputs": [{"from": {"id": c}, "to": {"id": co}} for c, co in b]})
            print("   associate:", r.status_code, len(b),
                  r.text[:150] if r.status_code >= 300 else "")
        print(f"DONE {len(pairs)} associations ({args.mode})")
    else:
        print(f"DONE 0 associations ({args.mode})")


def cmd_complete(args):
    """Fill every blank HubSpot field the sheet can answer, on contacts and on
    their companies. Never overwrites. Existing customers are skipped."""
    sheet = open_sheet()
    rows = sheet_by_email(sheet)
    names = owner_ids()
    found = contacts_by_email(list(rows))
    comps = companies_by_id({g(o["properties"], "associatedcompanyid") for o in found.values()
                             if g(o["properties"], "associatedcompanyid")})
    opts = industry_options() if args.industry else []
    cu, co, stat, unmapped = {}, {}, collections.Counter(), collections.Counter()
    for e, o in found.items():
        p, d = o["properties"], rows[e]
        if g(p, "lifecyclestage") == "customer":
            stat["skipped: customer"] += 1
            continue
        oid = names.get(g(d, "SDR Owner"), "")
        if g(d, "SDR Owner") and not oid:
            stat["unresolvable owner: " + g(d, "SDR Owner")] += 1
        want = {"jobtitle": g(d, "Job Title"),
                "phone": clean_phone(g(d, "Phone Number")),
                "city": g(d, "City"), "state": g(d, "State/Region"),
                "country": g(d, "Country/Region"), "sdr_owner": oid,
                "how_did_you_hear_about_us___drill_down":
                    config.EVENT_NAME_TO_HUBSPOT.get(g(d, "Event Name"), ""),
                "gold___ent__qualification":
                    "Qualified" if g(d, "Qualification") in ("Qualified", "PASS") else ""}
        props = {k: v for k, v in want.items() if v and not g(p, k)}
        if props:
            cu[o["id"]] = props
        cid = g(p, "associatedcompanyid")
        if not cid:
            stat["no associated company"] += 1
            continue
        cp = comps.get(cid, {})
        if g(cp, "lifecyclestage") == "customer":
            continue
        cw = {"pod": clean_pod(g(d, "POD")), "sdr_owner": oid,
              "annualrevenue": clean_int(g(d, "Estimated Revenue (USD)")),
              "numberofemployees": clean_int(g(d, "Employee Count")),
              "linkedin_company_page": clean_linkedin(g(d, "Company LinkedIn")),
              "founded_year": clean_year(g(d, "Founded"))}
        if args.industry and g(d, "Industry") and not g(cp, "industry"):
            mapped = map_industry(g(d, "Industry"), opts)
            if mapped:
                cw["industry"] = mapped
            else:
                unmapped[g(d, "Industry")] += 1
        add = {k: v for k, v in cw.items() if v and not g(cp, k)}
        if add:
            co.setdefault(cid, {}).update(add)
    print(f"contacts to fill {len(cu)} | companies to fill {len(co)}")
    for k, n in stat.most_common(6):
        print(f"   {k}: {n}")
    print("   contact fields:",
          collections.Counter(k for v in cu.values() for k in v).most_common())
    print("   company fields:",
          collections.Counter(k for v in co.values() for k in v).most_common())
    if unmapped:
        print("   UNMAPPED industries:", unmapped.most_common(12))
    print(f"contacts written {write_batch('contacts', cu, args.mode, 'contacts')}")
    print(f"companies written {write_batch('companies', co, args.mode, 'companies')}")


def cmd_industry(args):
    """Set companies.industry from the sheet, mapped onto HubSpot's enum."""
    sheet = open_sheet()
    rows = sheet_by_email(sheet)
    opts = industry_options()
    found = contacts_by_email(list(rows))
    src = {}
    for e, o in found.items():
        cid = g(o["properties"], "associatedcompanyid")
        if cid and g(rows[e], "Industry"):
            src.setdefault(cid, g(rows[e], "Industry"))
    comps = companies_by_id(src)
    co, unmapped, mapped = {}, collections.Counter(), collections.Counter()
    for cid, cp in comps.items():
        if g(cp, "industry") or g(cp, "lifecyclestage") == "customer":
            continue
        raw = src.get(cid, "")
        if not raw:
            continue
        val = map_industry(raw, opts)
        if val:
            co[cid] = {"industry": val}
            mapped[raw + " -> " + val] += 1
        else:
            unmapped[raw] += 1
    print(f"companies needing an industry: {len(co) + sum(unmapped.values())}")
    print(f"   mapped {len(co)} | unmapped {sum(unmapped.values())}")
    for k, n in mapped.most_common(20):
        print(f"      {n:4d}  {k}")
    if unmapped:
        print("   UNMAPPED (left blank; add to INDUSTRY_ALIASES to fix):")
        for k, n in unmapped.most_common(15):
            print(f"      {n:4d}  {k}")
    print(f"companies written {write_batch('companies', co, args.mode, 'companies')}")


def cmd_hygiene(args):
    """Repair malformed values already in HubSpot on a list's records: phones
    to E.164, decimal revenue to integers, bare or numeric LinkedIn values to
    real URLs or cleared, out-of-range founded years cleared, legacy pod
    values normalised."""
    ids = list_members(args.list_id)
    contacts = {}
    for b in chunks(ids):
        j = hs("POST", "/crm/v3/objects/contacts/batch/read",
               json={"properties": ["phone", "mobilephone", "associatedcompanyid"],
                     "inputs": [{"id": x} for x in b]}).json()
        for o in j.get("results", []):
            contacts[o["id"]] = o["properties"]
    cu = {}
    for cid, p in contacts.items():
        fix = {}
        for f in ("phone", "mobilephone"):
            cur = g(p, f)
            new = clean_phone(cur)
            if cur and new != cur:
                fix[f] = new
        if fix:
            cu[cid] = fix
    comps = companies_by_id({g(p, "associatedcompanyid") for p in contacts.values()
                             if g(p, "associatedcompanyid")})
    co = {}
    for cid, p in comps.items():
        fix = {}
        for f, fn in (("annualrevenue", clean_int), ("numberofemployees", clean_int),
                      ("founded_year", clean_year), ("linkedin_company_page", clean_linkedin)):
            cur = g(p, f)
            new = fn(cur)
            if cur and new != cur:
                fix[f] = new
        pod = g(p, "pod")
        if pod and clean_pod(pod) and clean_pod(pod) != pod:
            fix["pod"] = clean_pod(pod)
        if fix:
            co[cid] = fix
    print(f"list {args.list_id}: {len(ids)} contacts, {len(comps)} companies")
    print(f"contacts to repair {len(cu)} | companies to repair {len(co)}")
    print("   contact fixes:",
          collections.Counter(k for v in cu.values() for k in v).most_common())
    print("   company fixes:",
          collections.Counter(k for v in co.values() for k in v).most_common())
    print(f"contacts written {write_batch('contacts', cu, args.mode, 'contacts')}")
    print(f"companies written {write_batch('companies', co, args.mode, 'companies')}")


def cmd_all(args):
    """The standard sequence, in dependency order."""
    steps = (("assign-routing", cmd_assign_routing),
             ("mirror-partnership", cmd_mirror_partnership),
             ("push-event-list", cmd_push_event_list),
             ("push-partnership-list", cmd_push_partnership_list),
             ("associate", cmd_associate),
             ("complete", cmd_complete),
             ("industry", cmd_industry),
             ("hygiene", cmd_hygiene))
    for name, fn in steps:
        print("\n" + "=" * 62)
        print("== " + name)
        print("=" * 62)
        fn(args)


COMMANDS = {
    "audit": cmd_audit,
    "assign-routing": cmd_assign_routing,
    "mirror-partnership": cmd_mirror_partnership,
    "push-event-list": cmd_push_event_list,
    "push-partnership-list": cmd_push_partnership_list,
    "associate": cmd_associate,
    "complete": cmd_complete,
    "industry": cmd_industry,
    "hygiene": cmd_hygiene,
    "all": cmd_all,
}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--mode", choices=("dry", "test", "apply"), default="dry",
                    help="dry = report only (default); test = write 5 records; apply = write all")
    ap.add_argument("--with-disqualified", action="store_true",
                    help="assign-routing: also route rows qualified out on revenue")
    ap.add_argument("--industry", action="store_true",
                    help="complete: also map and write companies.industry")
    ap.add_argument("--prune", action="store_true",
                    help="push-partnership-list: remove members who are not Partnership-owned")
    ap.add_argument("--list-id", default=EVENT_LIST,
                    help="hygiene: which HubSpot list to repair (default: the event list)")
    args = ap.parse_args()
    if args.command == "all":
        args.industry = True
    print(f"[{args.command}] mode={args.mode} sheet={config.GOOGLE_SHEET_ID}")
    COMMANDS[args.command](args)


if __name__ == "__main__":
    main()
