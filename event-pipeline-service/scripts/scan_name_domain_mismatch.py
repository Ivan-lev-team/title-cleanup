#!/usr/bin/env python3
"""Size the "record name and domain describe different companies" problem across
the whole HubSpot company base. FREE heuristic pass + a verified sample to
estimate the true-positive rate. Fixes nothing.

  python scripts/scan_name_domain_mismatch.py <hubspot.jsonl> [--verify N]
"""
import json, os, re, sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "scripts")); os.chdir(HERE)
from resolve_dedupe_groups import norm_domain, root_domain
from dedupe_pass_list import norm_name, jaro_winkler

# hosts that are not a company's own site -- a name/domain disagreement there is
# expected, not a mislabel
PLATFORM = re.compile(
    r"^(amazon|amzn|sellercentral|ebay|etsy|walmart|shopify|myshopify|wixsite|wix|squarespace|"
    r"bigcartel|blogspot|wordpress|substack|canva|linkedin|facebook|instagram|youtube|tiktok|"
    r"google|gmail|yahoo|hotmail|outlook|onmicrosoft|godaddy|weebly|webflow|carrd|linktr|"
    r"shopgrid|faire|abound|alibaba|aliexpress|temu|alibabagroup)\.", re.I)


def stem_of(d):
    return re.sub(r"[^a-z0-9]", "", root_domain(norm_domain(d)).split(".")[0].lower())


def overlaps(name, stem):
    """does the company name share any real signal with its domain stem?"""
    n = norm_name(name)
    if not n or not stem:
        return True                      # nothing to judge -> not a mismatch
    flat = n.replace(" ", "")
    if flat and (flat in stem or stem in flat):
        return True
    for tok in [t for t in n.split() if len(t) >= 4]:
        if tok in stem:
            return True
    # acronym: "American Bath Group" -> abg
    acro = "".join(t[0] for t in n.split() if t)
    if len(acro) >= 3 and acro in stem:
        return True
    if jaro_winkler(flat, stem) >= 0.85:
        return True
    return False


def main():
    path = sys.argv[1]
    verify_n = int(sys.argv[sys.argv.index("--verify") + 1]) if "--verify" in sys.argv else 0
    tot = with_dom = platform = mism = 0
    kinds = Counter()
    cands = []
    for line in open(path, encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        tot += 1
        d = norm_domain(r.get("domain") or r.get("website") or "")
        name = (r.get("name") or "").strip()
        if not d or "." not in d or not name:
            continue
        with_dom += 1
        if PLATFORM.match(d):
            platform += 1
            kinds["on_a_platform_host"] += 1
            continue
        if not overlaps(name, stem_of(d)):
            mism += 1
            cands.append({"id": r.get("id"), "name": name, "domain": d,
                          "pod": r.get("pod", ""), "contacts": r.get("contacts", "0")})
    print("=" * 70)
    print("NAME vs DOMAIN MISMATCH SCAN (free heuristic)")
    print("=" * 70)
    print(f"  companies scanned                : {tot:,}")
    print(f"  ...with both a name and a domain : {with_dom:,}")
    print(f"  ...on a platform host (amazon/etsy/shopify/social etc.) : {platform:,}")
    print(f"     -> excluded: a mismatch there is expected, not a defect")
    print(f"  CANDIDATE mismatches             : {mism:,}  "
          f"({100*mism/max(1,with_dom):.2f}% of named+domained records)")
    podded = sum(1 for c in cands if (c["pod"] or "").strip())
    withc = sum(1 for c in cands if int(float(c["contacts"] or 0)) > 0)
    print(f"     of those, pod set   : {podded:,}")
    print(f"     of those, contacts  : {withc:,}")
    json.dump(cands, open(os.path.join(os.path.dirname(path), "name_domain_mismatch.json"),
                          "w", encoding="utf-8"), ensure_ascii=False)
    print("\n  sample of 20 candidates:")
    for c in cands[:20]:
        print(f"    {c['name'][:34]:34} {c['domain'][:34]:34} pod={(c['pod'] or '-'):8} "
              f"contacts={c['contacts']}")

    if verify_n:
        import requests
        requests.packages.urllib3.disable_warnings()
        UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
        OG = re.compile(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)', re.I)
        TIT = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

        def fetch(c):
            for url in (f"https://{c['domain']}", f"https://www.{c['domain']}"):
                try:
                    r = requests.get(url, headers={"User-Agent": UA}, timeout=8,
                                     verify=False, allow_redirects=True)
                    if r.status_code >= 400 or not r.text:
                        continue
                    h = r.text[:200000]
                    m, t = OG.search(h), TIT.search(h)
                    site = (m.group(1) if m else (t.group(1) if t else ""))
                    site = re.sub(r"&(nbsp|amp|#39|quot);", " ", site)
                    return {**c, "site": re.sub(r"\s+", " ", site).strip()[:70], "ok": True}
                except Exception:
                    continue
            return {**c, "site": "", "ok": False}

        import random
        samp = random.Random(11).sample(cands, min(verify_n, len(cands)))
        with ThreadPoolExecutor(max_workers=20) as ex:
            res = list(ex.map(fetch, samp))
        reach = [r for r in res if r["ok"] and r["site"]]
        true_mis = [r for r in reach
                    if not (norm_name(r["name"]) in norm_name(r["site"])
                            or norm_name(r["site"]) in norm_name(r["name"])
                            or jaro_winkler(norm_name(r["name"]), norm_name(r["site"])) >= 0.85)]
        print(f"\n  --- VERIFIED SAMPLE (n={len(samp)}, reachable {len(reach)}) ---")
        print(f"  confirmed mislabel (site brand != record name): {len(true_mis)}/{len(reach)} "
              f"= {100*len(true_mis)/max(1,len(reach)):.0f}%")
        print(f"  ESTIMATED true mislabels across the base: "
              f"~{int(mism * len(true_mis)/max(1,len(reach))):,}")
        print("\n  confirmed examples:")
        for r in true_mis[:12]:
            print(f"    name='{r['name'][:26]:26}' domain={r['domain'][:26]:26} "
                  f"site says='{r['site'][:34]}'")
        print("\n  false positives (heuristic fired but brand does match):")
        for r in [x for x in reach if x not in true_mis][:6]:
            print(f"    name='{r['name'][:26]:26}' domain={r['domain'][:26]:26} "
                  f"site says='{r['site'][:34]}'")


if __name__ == "__main__":
    main()
