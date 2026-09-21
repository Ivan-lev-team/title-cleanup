#!/usr/bin/env python3
"""Recall-biased dedupe of the PASS list against HubSpot. COUNTS ONLY, no writes.

Bias: a re-imported duplicate is cheap; a dropped net-new is not. Suppression
therefore requires either an identity-grade domain match (Rule 1) or two
independent agreeing signals (Rule 2). Everything else is net-new.

  python scripts/dedupe_pass_list.py <PASS_LIST.csv> <hubspot_companies.jsonl> <shopify_export.csv>
"""
import csv, json, re, sys, random
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding="utf-8")
csv.field_size_limit(10 ** 8)

MULTI = {"co.uk","com.au","co.nz","co.za","com.br","co.jp","com.mx","co.in","com.tr","co.il",
         "co.kr","com.sg","com.hk","co.th","com.ph","com.my","com.ar","com.co","co.id"}
LEAD = ("get","shop","try","buy","the","my","we","go","drink","wear","join","live","hey")
TRAIL = ("shop","store","hq","co","online","official","usa","us","brand")
STOP_STEM = {"shop","store","beauty","coffee","home","pet","pets","kids","style","gear","luxe",
             "outlet","market","supply","supplies","goods","brand","boutique","company","direct",
             "wholesale","apparel","clothing","fashion","jewelry","candles","soap","tea","wine",
             "books","toys","games","music","art","design","studio","collective","group","global",
             "world","usa","america","organic","natural","health","fitness","sports","outdoors"}
LEGAL = {"inc","llc","ltd","limited","co","corp","corporation","company","gmbh","bv","plc","sa","srl"}
GENERIC_NAME_TOK = STOP_STEM | LEGAL | {"and","of","for","the"}


def norm_domain(d):
    d = (d or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split("?")[0].split(":")[0]
    d = d.rstrip(".")
    if d.startswith("www."):
        d = d[4:]
    return d


def root_domain(d):
    d = norm_domain(d)
    b = d.split(".")
    if len(b) >= 3 and ".".join(b[-2:]) in MULTI:
        return ".".join(b[-3:])
    return ".".join(b[-2:]) if len(b) >= 2 else d


def stem_of(root):
    s = root.split(".")[0]
    s = re.sub(r"[^a-z0-9]", "", s)
    changed = True
    while changed:
        changed = False
        for p in LEAD:
            if s.startswith(p) and len(s) - len(p) >= 4:
                s = s[len(p):]; changed = True; break
        for p in TRAIL:
            if s.endswith(p) and len(s) - len(p) >= 4:
                s = s[:-len(p)]; changed = True; break
    return s


def norm_name(n):
    n = re.sub(r"[®™©]", " ", (n or "").lower())
    n = re.sub(r"[^a-z0-9& ]+", " ", n)
    toks = [t for t in n.split() if t]
    while toks and toks[0] == "the":
        toks = toks[1:]
    toks = [t for t in toks if t not in LEGAL]
    return " ".join(toks).strip()


def jaro_winkler(a, b):
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    md = max(len(a), len(b)) // 2 - 1
    if md < 0:
        md = 0
    af = [False] * len(a); bf = [False] * len(b); m = 0
    for i, ca in enumerate(a):
        for j in range(max(0, i - md), min(len(b), i + md + 1)):
            if not bf[j] and b[j] == ca:
                af[i] = bf[j] = True; m += 1; break
    if not m:
        return 0.0
    k = t = 0
    for i in range(len(a)):
        if af[i]:
            while not bf[k]:
                k += 1
            if a[i] != b[k]:
                t += 1
            k += 1
    t //= 2
    jd = (m / len(a) + m / len(b) + (m - t) / m) / 3
    p = 0
    for x, y in zip(a, b):
        if x == y and p < 4:
            p += 1
        else:
            break
    return jd + p * 0.1 * (1 - jd)


def names_consistent(a, b):
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = a.split(), b.split()
    if set(ta) == set(tb):
        return True
    # whole-token containment (one is a prefix-run of the other)
    if len(ta) != len(tb):
        short, long_ = (ta, tb) if len(ta) < len(tb) else (tb, ta)
        if long_[:len(short)] == short:
            return True
    return False


def main():
    pass_csv, hs_jsonl, export_csv = sys.argv[1], sys.argv[2], sys.argv[3]

    # ---------- HubSpot index ----------
    by_root = defaultdict(list)
    by_stem = defaultdict(list)
    by_exact_domain = defaultdict(list)
    domainless = []
    hs_n = 0
    with open(hs_jsonl, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line); hs_n += 1
            d = r.get("domain") or r.get("website") or ""
            nd = norm_domain(d)
            if not nd or "." not in nd:
                domainless.append(r); continue
            rt = root_domain(nd)
            r["_root"] = rt; r["_nd"] = nd; r["_nn"] = norm_name(r.get("name"))
            by_root[rt].append(r)
            by_exact_domain[nd].append(r)
            st = stem_of(rt)
            if st:
                by_stem[st].append(r)
    print(f"HubSpot records: {hs_n:,} | with domain {hs_n-len(domainless):,} | domainless {len(domainless):,}")
    print(f"  distinct root domains {len(by_root):,} | distinct stems {len(by_stem):,}\n")

    # ---------- sales, for the coin-flip value gate ----------
    sales = {}
    with open(export_csv, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            v = re.sub(r"[^0-9.]", "", (r.get("estimated_yearly_sales") or ""))
            try:
                sales[norm_domain(r.get("domain"))] = float(v or 0)
            except ValueError:
                pass

    # ---------- classify ----------
    buckets = Counter()
    rule_used = Counter()
    dup_rows, subdom_rows, coin_rows, netnew_rows = [], [], [], []
    multi_hit = 0
    rows = list(csv.DictReader(open(pass_csv, encoding="utf-8-sig")))
    for row in rows:
        dom = norm_domain(row["domain"])
        rt = root_domain(dom)
        nn = norm_name(row["name"])
        is_sub = dom.count(".") > rt.count(".")

        rec = {"domain": row["domain"], "name": row["name"], "root": rt,
               "fulfillment": row.get("fulfillment", "")}

        # Rule 1: exact root-domain identity
        hits = by_root.get(rt, [])
        if len(hits) > 1:
            multi_hit += 1
        if hits:
            h = hits[0]
            rec["hs_name"] = h.get("name", ""); rec["hs_domain"] = h.get("domain", "")
            rec["hs_pod"] = h.get("pod", ""); rec["hs_contacts"] = h.get("contacts", "0")
            rec["hs_id"] = h.get("id")
            # did it match ONLY because we collapsed a subdomain?
            if is_sub and not by_exact_domain.get(dom):
                rec["rule"] = "subdomain_collapse_only"
                subdom_rows.append(rec); buckets["subdomain_collapse_only"] += 1
            else:
                rec["rule"] = "exact_domain"
                dup_rows.append(rec); rule_used["exact_domain"] += 1
                buckets["confident_dup"] += 1
            continue

        # Rule 2: stem variant, needs BOTH signals
        st = stem_of(rt)
        cand = by_stem.get(st, []) if st else []
        if st and len(st) >= 4 and st not in STOP_STEM and len(cand) == 1:
            h = cand[0]
            hn = h.get("_nn", "")
            rec["hs_name"] = h.get("name", ""); rec["hs_domain"] = h.get("domain", "")
            rec["hs_pod"] = h.get("pod", ""); rec["hs_contacts"] = h.get("contacts", "0")
            rec["hs_id"] = h.get("id"); rec["stem"] = st
            if names_consistent(nn, hn):
                rec["rule"] = "stem_plus_name"
                dup_rows.append(rec); rule_used["stem_plus_name"] += 1
                buckets["confident_dup"] += 1
                continue
            jw = jaro_winkler(nn, hn)
            if 0.80 <= jw <= 0.92 and sales.get(dom, 0) >= 10_000_000:
                rec["rule"] = "coin_flip"; rec["jw"] = round(jw, 3)
                rec["sales"] = sales.get(dom, 0)
                coin_rows.append(rec); buckets["coin_flip"] += 1
                continue

        rec["rule"] = "net_new"
        netnew_rows.append(rec); buckets["net_new"] += 1

    total = len(rows)
    cd = buckets["confident_dup"]; sd = buckets["subdomain_collapse_only"]
    print("=" * 72)
    print(f"PASS rows: {total:,}")
    print("=" * 72)
    print(f"\n1. CONFIDENT DUPS (suppress)      : {cd:7,}  ({100*cd/total:5.2f}%)")
    for k, v in rule_used.most_common():
        print(f"     via {k:22} {v:7,}")
    print(f"   including subdomain subset     : {cd+sd:7,}  ({100*(cd+sd)/total:5.2f}%)")
    print(f"\n2. SUBDOMAIN-COLLAPSE-ONLY (held) : {sd:7,}  ({100*sd/total:5.2f}%)  <- NOT suppressed")
    print(f"\n3. NET-NEW                        : {buckets['net_new']:7,}  ({100*buckets['net_new']/total:5.2f}%)")
    print(f"\n4. COIN-FLIP (your glance)        : {buckets['coin_flip']:7,}")
    print(f"\n   bucket sum check: {cd+sd+buckets['net_new']+buckets['coin_flip']:,} == {total:,} "
          f"{'OK' if cd+sd+buckets['net_new']+buckets['coin_flip']==total else 'MISMATCH'}")

    print("\n5. WITHIN CONFIDENT DUPS")
    pod = sum(1 for r in dup_rows if (r.get("hs_pod") or "").strip())
    con = sum(1 for r in dup_rows if int(float(r.get("hs_contacts") or 0)) > 0)
    print(f"   have a pod       : {pod:7,}  ({100*pod/max(1,cd):5.2f}%)")
    print(f"   have contacts    : {con:7,}  ({100*con/max(1,cd):5.2f}%)")
    print(f"   neither          : {sum(1 for r in dup_rows if not (r.get('hs_pod') or '').strip() and int(float(r.get('hs_contacts') or 0))==0):7,}")

    if coin_rows:
        print("\n6. COIN-FLIP LIST")
        for r in sorted(coin_rows, key=lambda x: -x.get("sales", 0)):
            print(f"   {r['domain'][:30]:30} {r['name'][:26]:26} | HS: {r.get('hs_name','')[:26]:26} "
                  f"{r.get('hs_domain','')[:24]:24} jw={r.get('jw')} ${r.get('sales',0)/1e6:.1f}M")

    print(f"\n7. SANITY")
    print(f"   PASS root domains hitting >1 HubSpot record: {multi_hit:,}")
    print(f"   suppressions driven by name alone          : 0 (structurally impossible)")
    if subdom_rows:
        print("\n   subdomain-collapse-only sample:")
        for r in random.Random(5).sample(subdom_rows, min(12, len(subdom_rows))):
            print(f"     {r['domain'][:32]:32} -> HS {r.get('hs_domain','')[:28]:28} {r.get('hs_name','')[:26]}")
    print("\n   confident-dup sample:")
    for r in random.Random(5).sample(dup_rows, min(15, len(dup_rows))):
        print(f"     [{r['rule'][:14]:14}] {r['domain'][:28]:28} {r['name'][:24]:24} "
              f"-> {r.get('hs_domain','')[:26]:26} {r.get('hs_name','')[:24]}")

    # ---------- 6. domainless blind spot, report only ----------
    print("\n" + "=" * 72)
    print("8. DOMAINLESS BLIND SPOT (report only, nothing suppressed)")
    dl_exact = defaultdict(list)
    dl_block = defaultdict(list)
    for r in domainless:
        nn = norm_name(r.get("name"))
        if not nn:
            continue
        r["_nn"] = nn
        dl_exact[nn].append(r)
        dl_block[re.sub(r"[^a-z0-9]", "", nn)[:4]].append(r)
    ex_hits, near_hits, samples = 0, 0, []
    for row in rows:
        nn = norm_name(row["name"])
        if not nn:
            continue
        if nn in dl_exact:
            ex_hits += 1
            if len(samples) < 40:
                samples.append(("exact", row["name"], dl_exact[nn][0].get("name", ""), 1.0))
            continue
        blk = dl_block.get(re.sub(r"[^a-z0-9]", "", nn)[:4], [])
        for c in blk[:60]:
            jw = jaro_winkler(nn, c["_nn"])
            if jw >= 0.95:
                near_hits += 1
                if len(samples) < 40:
                    samples.append(("near", row["name"], c.get("name", ""), round(jw, 3)))
                break
    def is_generic(n):
        t = [x for x in norm_name(n).split() if x]
        return bool(t) and all(x in GENERIC_NAME_TOK for x in t) or len(t) <= 1 and len(norm_name(n)) <= 6
    gen = sum(1 for s in samples if is_generic(s[1]))
    print(f"   domainless HubSpot records with a usable name: {len(dl_exact):,} distinct names")
    print(f"   PASS names matching EXACTLY                  : {ex_hits:,}")
    print(f"   PASS names matching NEAR (JW>=0.95, blocked) : {near_hits:,}")
    print(f"   combined blind-spot upper bound              : {ex_hits+near_hits:,} "
          f"({100*(ex_hits+near_hits)/total:.2f}% of PASS)")
    print(f"   of the sample shown, generic-looking names   : {gen}/{len(samples)}")
    print("   sample:")
    for kind, a, b, s in samples[:15]:
        print(f"     [{kind:5}] {a[:36]:36} ~ {b[:36]:36} {s}")
    print("\n   NOTE: near-match uses first-4-char blocking, so it is a lower bound.")
    print("=" * 72)
    print("\nNo files written, nothing suppressed in HubSpot.")


if __name__ == "__main__":
    main()
