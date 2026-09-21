#!/usr/bin/env python3
"""CONSOLIDATED FINAL DRY RUN. Writes NOTHING to HubSpot.

Applies the locked decisions:
  A            -> collapse into parent, enrich-only
  B (743)      -> keep as net-new + typeId 14 link to parent
  C distinct   -> keep as net-new + typeId 14 link to parent (incl. wholesale)
  C same (922) -> collapse into parent, enrich-only
  D            -> backfill domain on 2,368 safe 1:1 records; 292 ambiguous skipped
  coin-flips   -> stay net-new, no action
"""
import csv, json, os, re, sys, time
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding="utf-8")
csv.field_size_limit(10 ** 8)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "scripts")); os.chdir(HERE)

import requests
from dotenv import dotenv_values
TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "https://api.hubapi.com"

from resolve_dedupe_groups import norm_domain, root_domain as _root
from dedupe_pass_list import norm_name
from final_reconcile import EXTRA_PSL, PSL_MIN
from dryrun_groups_abd import FIELD_MAP, READ_PROPS, batch_read, blank


def main():
    S, G = sys.argv[1], sys.argv[2]
    rows = list(csv.DictReader(open(os.path.join(S, "PASS_LIST.csv"), encoding="utf-8-sig")))
    hs = [json.loads(l) for l in open(os.path.join(S, "hubspot_companies.jsonl"),
                                      encoding="utf-8") if l.strip()]
    host_by_root = defaultdict(set)
    for r in hs:
        d = norm_domain(r.get("domain") or r.get("website") or "")
        if d and "." in d:
            host_by_root[_root(d)].add(d)
    PSL = {k for k, v in host_by_root.items() if len(v) >= PSL_MIN} | EXTRA_PSL

    def root(d):
        r = _root(d)
        return d if r in PSL else r

    by_root = defaultdict(list)
    for r in hs:
        d = norm_domain(r.get("domain") or r.get("website") or "")
        if d and "." in d:
            by_root[root(d)].append(r)

    A = json.load(open(os.path.join(G, "group_A.json"), encoding="utf-8"))
    B = json.load(open(os.path.join(G, "group_B.json"), encoding="utf-8"))
    C = json.load(open(os.path.join(G, "group_C_judged.json"), encoding="utf-8"))
    D = json.load(open(os.path.join(G, "planD_writes.json"), encoding="utf-8"))
    bucket = json.load(open(os.path.join(G, "bucket_assignment.json"), encoding="utf-8"))
    Cdist = [r for r in C if r["verdict"] == "distinct"]
    Csame = [r for r in C if r["verdict"] == "same"]

    print("=" * 76)
    print("FINAL CONSOLIDATED DRY RUN - NOTHING WRITTEN TO HUBSPOT")
    print("=" * 76)

    # ---------- 1. reconciliation ----------
    per_row = Counter(bucket[norm_domain(r["domain"])] for r in rows)
    sup = per_row["confident_dup_exact"] + per_row["confident_dup_stem"]
    keep = per_row["net_new"] + per_row["B_regional_keep"] + per_row["C_distinct_keep"]
    print("\n1. RECONCILIATION")
    print(f"    {len(rows):>7,}   PASS companies")
    print(f"   -{sup:>7,}   confident dups (exact {per_row['confident_dup_exact']:,} "
          f"+ stem {per_row['confident_dup_stem']:,})")
    print(f"   -{per_row['A_collapse']:>7,}   Group A collapsed")
    print(f"   -{per_row['C_collapse']:>7,}   Group C collapsed ('same')")
    print(f"   -{per_row['D_suppressed']:>7,}   Group D suppressed")
    print(f"   +{per_row['B_regional_keep']:>7,}   Group B regional kept + linked")
    print(f"   +{per_row['C_distinct_keep']:>7,}   Group C distinct kept + linked")
    print(f"    {'-'*7}")
    print(f"    {keep:>7,}   FINAL NET-NEW (incl. 4 coin-flips, unchanged)")
    tot = sup + per_row["A_collapse"] + per_row["C_collapse"] + per_row["D_suppressed"] + keep
    print(f"\n    check {tot:,} == {len(rows):,}  "
          f"{'RECONCILES' if tot == len(rows) else 'MISMATCH'}")
    print("    B and C-distinct were already counted as kept, so the locked")
    print("    decisions move the net-new total by 0.")

    # ---------- 2. typeId 14 links ----------
    print("\n2. typeId 14 CHILD -> PARENT LINKS")
    links, orphans = [], []
    for r in B:
        links.append(("B", r["child_domain"], r["parent_id"], r["parent_name"]))
    for r in Cdist:
        rt = root(norm_domain(r["child_domain"]))
        cand = by_root.get(rt, [])
        if cand and rt != norm_domain(r["child_domain"]):
            links.append(("C", r["child_domain"], cand[0]["id"], cand[0].get("name", "")))
        else:
            orphans.append(r)
    print(f"   Group B links                         : {sum(1 for l in links if l[0]=='B'):,}")
    print(f"   Group C distinct links                : {sum(1 for l in links if l[0]=='C'):,}")
    print(f"   TOTAL typeId 14 links to write        : {len(links):,}")
    print(f"   Group C distinct with NO parent in HubSpot (import standalone): {len(orphans):,}")
    if orphans:
        print("\n   --- orphan list (parent not in HubSpot; these were the false-parent")
        print("       rows the public-suffix fix corrected) ---")
        for r in orphans:
            print(f"     {r['child_domain'][:40]:40} old-bogus-parent='{r['parent_name'][:26]}'")
    json.dump([{"group": g, "child_domain": c, "parent_id": p, "parent_name": n}
               for g, c, p, n in links],
              open(os.path.join(G, "final_links.json"), "w", encoding="utf-8"), ensure_ascii=False)

    # ---------- 3. parent enrichment (A + C-same) ----------
    print("\n3. PARENT ENRICHMENT (Group A + Group C collapsed) - ENRICH-ONLY")
    kids = defaultdict(list)
    for r in A + Csame:
        rt = root(norm_domain(r["child_domain"]))
        cand = by_root.get(rt, [])
        if cand:
            kids[cand[0]["id"]].append(r)
    pids = sorted(kids)
    print(f"   collapsing child rows: {len(A)+len(Csame):,}  ->  distinct parents: {len(pids):,}")
    cur = batch_read(pids)
    planned, fc = {}, Counter()
    for pid, ks in kids.items():
        props = cur.get(pid)
        if props is None:
            continue
        adds = {}
        for field, getter in FIELD_MAP:
            if not blank(props.get(field)):
                continue
            for k in ks:
                v = getter(k.get("export") or {})
                if v:
                    adds[field] = v; break
        if adds:
            planned[pid] = adds; fc.update(adds.keys())
    print(f"   parents gaining >=1 field : {len(planned):,} of {len(cur):,}")
    for f, c in fc.most_common():
        print(f"      {f:24} {c:6,}")
    print(f"   total field writes        : {sum(fc.values()):,}")
    print(f"   touched parents with a pod: {sum(1 for p in planned if not blank(cur[p].get('pod'))):,}")
    bad = [(p, v["country"]) for p, v in planned.items()
           if "country" in v and (re.search(r"\d", v["country"]) or "," in v["country"])]
    print(f"\n   COUNTRY FIX CHECK: values containing a digit or comma (postal addresses): {len(bad)}")
    print(f"   distinct country values to be written: {sorted({v['country'] for v in planned.values() if 'country' in v})}")
    json.dump(planned, open(os.path.join(G, "final_parent_enrich.json"), "w", encoding="utf-8"))

    # ---------- 4. group D ----------
    print("\n4. GROUP D DOMAIN BACKFILL")
    print(f"   records to enrich (domain only)   : {len(D):,}")
    print(f"   ambiguous, deliberately skipped   : 292")
    print(f"   PASS rows this removes from net-new: {per_row['D_suppressed']:,}")

    # ---------- 5. write plan ----------
    print("\n" + "=" * 76)
    print("5. ITEMISED WRITE PLAN (on approval)")
    print("=" * 76)
    n_pe = sum(fc.values())
    print(f"   a) PATCH company properties (enrich-only) : {len(planned):,} companies, "
          f"{n_pe:,} fields")
    print(f"   b) PATCH company.domain (Group D)         : {len(D):,} companies, {len(D):,} fields")
    print(f"   c) PUT typeId 14 child->parent links      : {len(links):,} associations")
    print(f"   ------------------------------------------------------------------")
    print(f"   TOTAL HubSpot mutations                   : {len(planned)+len(D)+len(links):,}")
    print(f"\n   NOT in this stage: the {keep:,} net-new companies are NOT imported.")
    print(f"   No company is created. No record is deleted. No existing value is overwritten.")
    print(f"   Group C distinct rows are linked only once they exist, so their")
    print(f"   {sum(1 for l in links if l[0]=='C'):,} links belong to the LATER import stage, not this one.")
    print("\n   HOLDING. Nothing written.")


if __name__ == "__main__":
    main()
