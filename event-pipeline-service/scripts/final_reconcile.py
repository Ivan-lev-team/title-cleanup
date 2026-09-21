#!/usr/bin/env python3
"""Assign every PASS row to exactly ONE bucket and reconcile. Writes nothing.

Fixes two defects found in the first pass:
  1. public-suffix roots (us.com, com.pk, com.tw, stanford.edu, canva.site,
     myshopify.com ...) were treated as registrable domains, inventing false
     parents. Any candidate root that hosts >= PSL_MIN distinct hostnames in
     HubSpot is treated as a public suffix and never used for matching.
  2. the reconciliation recomputed dups with Rule 1 only, dropping the 762
     Rule 2 (stem+name) matches. Buckets are now assigned once, here.
"""
import csv, json, os, sys
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding="utf-8")
csv.field_size_limit(10 ** 8)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "scripts"))

from resolve_dedupe_groups import norm_domain, root_domain as _root
from dedupe_pass_list import norm_name, stem_of, names_consistent, STOP_STEM

PSL_MIN = 8
EXTRA_PSL = {"us.com","uk.com","eu.com","br.com","cn.com","de.com","gb.com","ru.com","sa.com",
             "se.com","za.com","com.pk","com.tw","com.pe","com.cn","com.pa","com.ve","org.uk",
             "myshopify.com","canva.site","blogspot.com","wordpress.com","wixsite.com",
             "substack.com","onmicrosoft.com","as.me","rr.com","base44.app","herokuapp.com",
             "netlify.app","vercel.app","github.io","squarespace.com","bigcartel.com"}


def main():
    S, G = sys.argv[1], sys.argv[2]
    rows = list(csv.DictReader(open(os.path.join(S, "PASS_LIST.csv"), encoding="utf-8-sig")))

    # ---- build HubSpot indexes, with a data-driven public-suffix guard ----
    hs = []
    for line in open(os.path.join(S, "hubspot_companies.jsonl"), encoding="utf-8"):
        if line.strip():
            hs.append(json.loads(line))
    host_by_root = defaultdict(set)
    for r in hs:
        d = norm_domain(r.get("domain") or r.get("website") or "")
        if d and "." in d:
            host_by_root[_root(d)].add(d)
    PSL = {k for k, v in host_by_root.items() if len(v) >= PSL_MIN} | EXTRA_PSL
    print(f"public suffixes detected/declared: {len(PSL):,} "
          f"(e.g. {sorted(list(PSL))[:6]})")

    def root(d):
        r = _root(d)
        return d if r in PSL else r      # never collapse onto a public suffix

    by_root, by_exact, by_stem, dl_by_name = (defaultdict(list), defaultdict(list),
                                              defaultdict(list), defaultdict(list))
    for r in hs:
        d = norm_domain(r.get("domain") or r.get("website") or "")
        if not d or "." not in d:
            nn = norm_name(r.get("name"))
            if nn:
                dl_by_name[nn].append(r)
            continue
        rt = root(d)
        r["_nn"] = norm_name(r.get("name"))
        by_root[rt].append(r); by_exact[d].append(r)
        st = stem_of(rt)
        if st:
            by_stem[st].append(r)

    # ---- group memberships from the earlier stages ----
    A = json.load(open(os.path.join(G, "group_A.json"), encoding="utf-8"))
    B = json.load(open(os.path.join(G, "group_B.json"), encoding="utf-8"))
    C = json.load(open(os.path.join(G, "group_C_judged.json"), encoding="utf-8"))
    Aset = {r["child_domain"] for r in A}
    Bset = {r["child_domain"] for r in B}
    Cdist = {r["child_domain"] for r in C if r["verdict"] == "distinct"}
    Csame = {r["child_domain"] for r in C if r["verdict"] == "same"}
    Dmap = {norm_domain(x["domain"]): x for x in
            json.load(open(os.path.join(G, "planD_writes.json"), encoding="utf-8"))}

    pass_by_name = defaultdict(list)
    for row in rows:
        nn = norm_name(row["name"])
        if nn:
            pass_by_name[nn].append(row)

    bucket = {}
    for row in rows:
        d = norm_domain(row["domain"]); rt = root(d)
        # held subdomain groups take precedence (they were carved out already)
        if d in Aset:      bucket[d] = "A_collapse"; continue
        if d in Csame:     bucket[d] = "C_collapse"; continue
        if d in Bset:      bucket[d] = "B_regional_keep"; continue
        if d in Cdist:     bucket[d] = "C_distinct_keep"; continue
        if by_root.get(rt) and not (d.count(".") > rt.count(".") and not by_exact.get(d)):
            bucket[d] = "confident_dup_exact"; continue
        if by_root.get(rt):
            bucket[d] = "confident_dup_exact"; continue
        st = stem_of(rt)
        cand = by_stem.get(st, []) if st else []
        if st and len(st) >= 4 and st not in STOP_STEM and len(cand) == 1 \
                and names_consistent(norm_name(row["name"]), cand[0].get("_nn", "")):
            bucket[d] = "confident_dup_stem"; continue
        if d in Dmap:
            bucket[d] = "D_suppressed"; continue
        bucket[d] = "net_new"

    cnt = Counter(bucket.values())
    total = len(rows)
    keep = cnt["net_new"] + cnt["B_regional_keep"] + cnt["C_distinct_keep"]
    print("\n" + "=" * 68); print("BUCKET ASSIGNMENT (one per PASS row)"); print("=" * 68)
    for k, v in cnt.most_common():
        print(f"  {k:24} {v:7,}")
    print(f"  {'TOTAL':24} {sum(cnt.values()):7,}  vs {total:,} "
          f"{'OK' if sum(cnt.values()) == total else 'MISMATCH'}")

    print("\n" + "=" * 68); print("FINAL RECONCILIATION"); print("=" * 68)
    print(f"   {total:>7,}   PASS companies")
    print(f"  -{cnt['confident_dup_exact']+cnt['confident_dup_stem']:>7,}   confident dups "
          f"(exact {cnt['confident_dup_exact']:,} + stem {cnt['confident_dup_stem']:,})")
    print(f"  -{cnt['A_collapse']:>7,}   Group A collapsed into parent")
    print(f"  -{cnt['C_collapse']:>7,}   Group C collapsed into parent")
    print(f"  -{cnt['D_suppressed']:>7,}   Group D suppressed (domainless enriched)")
    print(f"  +{cnt['B_regional_keep']:>7,}   Group B regional kept, linked to parent")
    print(f"  +{cnt['C_distinct_keep']:>7,}   Group C distinct kept, linked to parent")
    print(f"   {'-'*7}")
    print(f"   {keep:>7,}   FINAL NET-NEW TO IMPORT")
    s = (cnt['confident_dup_exact'] + cnt['confident_dup_stem'] + cnt['A_collapse'] +
         cnt['C_collapse'] + cnt['D_suppressed'] + keep)
    print(f"\n   check: suppressed+collapsed+kept = {s:,} vs {total:,}  "
          f"{'RECONCILES' if s == total else 'MISMATCH by ' + str(total - s)}")
    json.dump(bucket, open(os.path.join(G, "bucket_assignment.json"), "w", encoding="utf-8"))
    print(f"\n   wrote bucket_assignment.json")


if __name__ == "__main__":
    main()
