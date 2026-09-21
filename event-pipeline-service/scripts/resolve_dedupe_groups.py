#!/usr/bin/env python3
"""Classify the held subdomain rows into Groups A/B/C and prepare Group D.

DRY-RUN ONLY by default. Nothing is written to HubSpot unless --live is passed,
and --live is not used until the dry-run has been reviewed.

  python scripts/resolve_dedupe_groups.py <PASS_LIST.csv> <hubspot.jsonl> <export.csv> <outdir>
"""
import csv, json, os, re, sys
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding="utf-8")
csv.field_size_limit(10 ** 8)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

MULTI = {"co.uk","com.au","co.nz","co.za","com.br","co.jp","com.mx","co.in","com.tr","co.il",
         "co.kr","com.sg","com.hk","co.th","com.ph","com.my","com.ar","com.co","co.id"}

# --- Group A: infrastructure / non-storefront hosts ---
INFRA = {"checkout","cart","staging","stage","dev","test","qa","preview","beta",
         "www2","www1","www3","secure","pay","payments","order","orders","portal","admin","api",
         "cdn","assets","static","mobile","demo","sandbox","internal","backup","old","new2",
         "account","accounts","login","auth","email","mail","webmail","links","link","track",
         # non-storefront content/service hosts -- added after the first pass showed
         # returns/help/support sitting in the ambiguous pile
         "returns","help","support","faq","info","blog","careers","jobs","press","investors",
         "news","docs","status","community","forum","events","media","survey","feedback",
         "ftp","vpn","remote","smtp","ns1","ns2","autodiscover","calendar"}
# store/shop are storefront faces, not infra: routed to A only when no region is
# present, otherwise they are a regional storefront (B)
SOFT_STORE = {"store","stores"}
INFRA_RE = re.compile(r"(^|-)(stage|staging|dev|test|qa|preview|sandbox|uat)(-|$)")

# --- Group B: regional / language ---
CC = {"uk","ca","au","de","eu","fr","es","it","nl","jp","kr","cn","mx","br","in","sg","hk","nz",
      "se","no","dk","fi","pl","pt","ie","ch","at","be","ae","sa","za","us","gb","ru","tr","il",
      "cl","ar","co","pe","ph","th","vn","my","id","tw","gr","cz","hu","ro","ua","kz"}
REGION_WORDS = {"global","intl","international","emea","apac","latam","row","worldwide",
                "canada","mexico","australia","france","germany","spain","italy","japan","korea",
                "china","brazil","india","europe","asia","america","usa","uk","britain","ireland",
                "netherlands","sweden","norway","denmark","poland","portugal","switzerland"}
LANGS = {"en","fr","de","es","it","nl","pt","ja","ko","zh","ar","sv","da","no","fi","pl","tr","ru"}


def norm_domain(d):
    d = (d or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split("?")[0].split(":")[0].rstrip(".")
    return d[4:] if d.startswith("www.") else d


def root_domain(d):
    d = norm_domain(d); b = d.split(".")
    if len(b) >= 3 and ".".join(b[-2:]) in MULTI:
        return ".".join(b[-3:])
    return ".".join(b[-2:]) if len(b) >= 2 else d


def sub_label(d):
    d, rt = norm_domain(d), root_domain(d)
    return d[:-(len(rt) + 1)] if d.endswith(rt) and len(d) > len(rt) else ""


def classify_sub(label):
    """-> 'A' infra | 'B' regional | 'C' ambiguous"""
    if not label:
        return "C"
    parts = label.split(".")
    # hard infra wins outright -- a regional checkout page is still not workable
    for p in parts:
        if p in INFRA or INFRA_RE.search(p):
            return "A"
    # region beats a soft storefront label: ca.store.x.com is a regional store
    for p in parts:
        segs = re.split(r"[-_]", p)
        if p in CC or p in REGION_WORDS:
            return "B"
        # sa-en, en-ca, de-de, fr-ca ...
        if len(segs) == 2 and (segs[0] in CC or segs[0] in LANGS) and (segs[1] in CC or segs[1] in LANGS):
            return "B"
        if len(p) == 2 and p in CC:
            return "B"
    # bare store/stores with no region = another face of the parent -> collapse
    for p in parts:
        if p in SOFT_STORE:
            return "A"
    return "C"


def main():
    pass_csv, hs_jsonl, export_csv, outdir = sys.argv[1:5]
    os.makedirs(outdir, exist_ok=True)

    by_root, by_exact, domainless = defaultdict(list), defaultdict(list), []
    for line in open(hs_jsonl, encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        d = norm_domain(r.get("domain") or r.get("website") or "")
        if not d or "." not in d:
            domainless.append(r); continue
        by_root[root_domain(d)].append(r)
        by_exact[d].append(r)

    export = {}
    for r in csv.DictReader(open(export_csv, encoding="utf-8-sig", newline="")):
        export[norm_domain(r.get("domain"))] = r

    rows = list(csv.DictReader(open(pass_csv, encoding="utf-8-sig")))
    groups = defaultdict(list)
    for row in rows:
        dom = norm_domain(row["domain"]); rt = root_domain(dom)
        if dom.count(".") <= rt.count("."):
            continue                       # not a subdomain
        if not by_root.get(rt) or by_exact.get(dom):
            continue                       # not in the held 2,742 set
        label = sub_label(dom)
        g = classify_sub(label)
        parent = by_root[rt][0]
        groups[g].append({
            "child_domain": dom, "label": label, "child_name": row["name"],
            "root": rt, "parent_id": parent["id"], "parent_name": parent.get("name", ""),
            "parent_domain": parent.get("domain", ""), "parent_pod": parent.get("pod", ""),
            "parent_contacts": parent.get("contacts", "0"),
            "export": export.get(dom, {}),
        })

    total = sum(len(v) for v in groups.values())
    print(f"held subdomain rows re-derived: {total:,}")
    for g in ("A", "B", "C"):
        print(f"  Group {g}: {len(groups[g]):,}")
    print()
    for g in ("A", "B", "C"):
        lab = Counter(r["label"] for r in groups[g])
        print(f"Group {g} top labels: {dict(lab.most_common(14))}")
    for g in ("A", "B", "C"):
        json.dump(groups[g], open(os.path.join(outdir, f"group_{g}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
    print(f"\nwrote group_A/B/C.json to {outdir}")


if __name__ == "__main__":
    main()
