#!/usr/bin/env python3
"""DRY RUN for Groups A (collapse+enrich), B (regional child->parent link) and
D (domainless enrich then suppress). Writes NOTHING to HubSpot.

  python scripts/dryrun_groups_abd.py <groupsdir> <PASS_LIST.csv> <hubspot.jsonl> <export.csv>
"""
import csv, json, os, re, sys, time
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding="utf-8")
csv.field_size_limit(10 ** 8)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); os.chdir(HERE)

import requests
from dotenv import dotenv_values
TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "https://api.hubapi.com"

sys.path.insert(0, os.path.join(HERE, "scripts"))
from resolve_dedupe_groups import norm_domain, root_domain  # noqa: E402

_CC2NAME = {"US": "United States", "CA": "Canada", "GB": "United Kingdom", "UK": "United Kingdom",
            "AU": "Australia", "DE": "Germany", "FR": "France", "ES": "Spain", "IT": "Italy",
            "NL": "Netherlands", "JP": "Japan", "MX": "Mexico", "BR": "Brazil", "IN": "India",
            "SG": "Singapore", "NZ": "New Zealand", "IE": "Ireland", "SE": "Sweden"}

# HubSpot company property <- Shopify export column
FIELD_MAP = [
    ("city",                  lambda e: (e.get("city") or "").strip()),
    ("state",                 lambda e: (e.get("state") or "").strip()),
    # country_code -> country name. company_location is a full postal address
    # ("Holland, MI 49423, USA") and must NOT be written into `country`.
    ("country",               lambda e: _CC2NAME.get((e.get("country_code") or "").strip().upper(), "")),
    ("phone",                 lambda e: (e.get("phones") or "").split(":")[0].strip()),
    ("linkedin_company_page", lambda e: (e.get("linkedin_url") or "").strip()),
    ("description",           lambda e: (e.get("description") or "").strip()[:900]),
    ("numberofemployees",     lambda e: (e.get("employee_count") or "").strip()),
    ("website",               lambda e: (e.get("domain_url") or "").strip()),
]
READ_PROPS = [f for f, _ in FIELD_MAP] + ["name", "domain", "pod", "sdr_owner",
                                          "num_associated_contacts", "lifecyclestage"]


def batch_read(ids):
    out = {}
    for i in range(0, len(ids), 100):
        chunk = [{"id": str(x)} for x in ids[i:i+100]]
        for attempt in range(5):
            r = requests.post(f"{BASE}/crm/v3/objects/companies/batch/read", headers=H,
                              json={"inputs": chunk, "properties": READ_PROPS}, timeout=45)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "2")) + 1); continue
            break
        if r.status_code < 300:
            for rec in r.json().get("results", []):
                out[rec["id"]] = rec.get("properties", {}) or {}
    return out


def blank(v):
    return v is None or str(v).strip() == ""


def main():
    gdir, pass_csv, hs_jsonl, export_csv = sys.argv[1:5]
    A = json.load(open(os.path.join(gdir, "group_A.json"), encoding="utf-8"))
    B = json.load(open(os.path.join(gdir, "group_B.json"), encoding="utf-8"))

    # ================= GROUP A =================
    print("=" * 74); print("GROUP A DRY RUN - collapse into parent, ENRICH-ONLY"); print("=" * 74)
    parents = sorted({r["parent_id"] for r in A})
    print(f"child rows: {len(A):,}  ->  distinct parents: {len(parents):,}")
    cur = batch_read(parents)
    print(f"parent records read: {len(cur):,}")

    by_parent = defaultdict(list)
    for r in A:
        by_parent[r["parent_id"]].append(r)

    planned, field_counts = {}, Counter()
    for pid, kids in by_parent.items():
        props = cur.get(pid)
        if props is None:
            continue
        adds = {}
        for field, getter in FIELD_MAP:
            if not blank(props.get(field)):
                continue                      # NEVER overwrite an existing value
            for k in kids:
                v = getter(k.get("export") or {})
                if v:
                    adds[field] = v
                    break
        if adds:
            planned[pid] = adds
            field_counts.update(adds.keys())
    print(f"\nparents that would get >=1 field added: {len(planned):,} of {len(cur):,}")
    print("fields that would be added:")
    for f, c in field_counts.most_common():
        print(f"   {f:24} {c:6,}")
    print(f"total individual field writes: {sum(field_counts.values()):,}")
    print("parents that would be touched but have a pod: "
          f"{sum(1 for p in planned if not blank(cur[p].get('pod'))):,}")

    # one full before/after
    demo = next((p for p in planned if not blank(cur[p].get("pod"))), next(iter(planned), None))
    if demo:
        print("\n--- FULL BEFORE/AFTER for one real parent ---")
        print(f"parent id {demo}  ({cur[demo].get('name')})")
        kids = [k['child_domain'] for k in by_parent[demo]]
        print(f"collapsing children: {kids}")
        print(f"{'property':26} {'BEFORE':38} {'AFTER':38}")
        for f in READ_PROPS:
            b = cur[demo].get(f)
            a = planned[demo].get(f, b)
            mark = "  <== ADDED" if f in planned[demo] else ""
            print(f"  {f:24} {str(b)[:36]:38} {str(a)[:36]:38}{mark}")
        untouched = [f for f in READ_PROPS if f not in planned[demo]]
        print(f"\n  fields left untouched: {len(untouched)}/{len(READ_PROPS)}")
        print(f"  any pre-existing value overwritten? "
              f"{'NO' if all(blank(cur[demo].get(f)) for f in planned[demo]) else 'YES - BUG'}")
    json.dump({k: v for k, v in planned.items()},
              open(os.path.join(gdir, "planA_writes.json"), "w", encoding="utf-8"))

    # ================= GROUP B =================
    print("\n" + "=" * 74); print("GROUP B DRY RUN - regional kept, linked child->parent"); print("=" * 74)
    linked = [r for r in B if r.get("parent_id")]
    noparent = [r for r in B if not r.get("parent_id")]
    print(f"regional rows: {len(B):,}")
    print(f"  would be imported AND linked to a parent : {len(linked):,}")
    print(f"  'no parent found' (import standalone)    : {len(noparent):,}")
    print("\nNOTE: hs_parent_company_id is READ-ONLY in this portal, so the link is")
    print("made with the v4 association typeId 14 ('Parent Company'), child -> parent.")
    print("HubSpot then derives hs_parent_company_id and hs_num_child_companies itself.")
    print("\n--- 5 sample links ---")
    for r in linked[:5]:
        print(f"  child {r['child_domain'][:34]:34} '{r['child_name'][:22]:22}'")
        print(f"     -> PUT /crm/v4/objects/companies/<childId>/associations/companies/{r['parent_id']}")
        print(f"        typeId 14  parent='{r['parent_name'][:34]}' ({r['parent_domain']})")
    json.dump(B, open(os.path.join(gdir, "planB_links.json"), "w", encoding="utf-8"), ensure_ascii=False)

    # ================= GROUP D =================
    print("\n" + "=" * 74); print("GROUP D DRY RUN - domainless: enrich domain, then suppress"); print("=" * 74)
    from dedupe_pass_list import norm_name  # reuse the same normalizer
    dl_by_name = defaultdict(list)
    for line in open(hs_jsonl, encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        if norm_domain(r.get("domain") or r.get("website") or ""):
            continue
        nn = norm_name(r.get("name"))
        if nn:
            dl_by_name[nn].append(r)
    rows = list(csv.DictReader(open(pass_csv, encoding="utf-8-sig")))
    pass_by_name = defaultdict(list)
    for row in rows:
        nn = norm_name(row["name"])
        if nn:
            pass_by_name[nn].append(row)

    clean, amb_hs, amb_pass = [], 0, 0
    for nn, hs_recs in dl_by_name.items():
        pr = pass_by_name.get(nn)
        if not pr:
            continue
        if len(hs_recs) > 1:
            amb_hs += 1; continue          # same name on several HubSpot records
        if len(pr) > 1:
            amb_pass += 1; continue        # same name on several PASS rows
        clean.append((pr[0], hs_recs[0]))
    print(f"exact-name matches total                 : {sum(1 for nn in dl_by_name if nn in pass_by_name):,}")
    print(f"  ambiguous (name on >1 HubSpot record)  : {amb_hs:,}  -> SKIPPED")
    print(f"  ambiguous (name on >1 PASS row)        : {amb_pass:,}  -> SKIPPED")
    print(f"  1:1 and safe to enrich                 : {len(clean):,}")
    pod = sum(1 for _, h in clean if (h.get("pod") or "").strip())
    con = sum(1 for _, h in clean if int(float(h.get("contacts") or 0)) > 0)
    print(f"     of those, parent has a pod          : {pod:,}")
    print(f"     of those, parent has contacts       : {con:,}")
    print("\n--- 5 before/after (only `domain` is added) ---")
    ids = [h["id"] for _, h in clean[:5]]
    live = batch_read(ids)
    for prow, h in clean[:5]:
        p = live.get(h["id"], {})
        print(f"  HubSpot id {h['id']}  name='{h.get('name')}'")
        print(f"    BEFORE domain={p.get('domain')!r}  website={p.get('website')!r}  "
              f"pod={p.get('pod')!r} contacts={p.get('num_associated_contacts')!r}")
        print(f"    AFTER  domain={norm_domain(prow['domain'])!r}  website={p.get('website')!r}  "
              f"pod={p.get('pod')!r} contacts={p.get('num_associated_contacts')!r}")
        print(f"    fields changed: ['domain']   (all others identical)")
    json.dump([{"hs_id": h["id"], "hs_name": h.get("name"), "domain": norm_domain(p["domain"]),
                "pass_name": p["name"]} for p, h in clean],
              open(os.path.join(gdir, "planD_writes.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print(f"\nplans written to {gdir} (planA_writes.json / planB_links.json / planD_writes.json)")
    print("NOTHING WAS WRITTEN TO HUBSPOT.")


if __name__ == "__main__":
    main()
