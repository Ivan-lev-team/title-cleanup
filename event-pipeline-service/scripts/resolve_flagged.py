#!/usr/bin/env python3
"""Resolve the flagged Amazon-collision rows (review list + post-write mismatches).

Free method: read each side's own homepage (og:site_name / <title>).

Key rule: a HubSpot record still parked on amazon.com carries NO independent
brand signal, so for those we compare the record's NAME against what the push
domain actually is. Only a record with a real, reachable, non-Amazon domain that
disagrees with its own name is called MISLABEL.

  SUPPRESS - same brand -> drop the push row from net-new
  KEEP     - different companies -> push row stays net-new
  MISLABEL - HubSpot record's name and its own domain describe different firms
"""
import json, os, re, sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "scripts")); os.chdir(HERE)
import requests
requests.packages.urllib3.disable_warnings()
from resolve_dedupe_groups import norm_domain, root_domain
from dedupe_pass_list import norm_name, jaro_winkler
from fix_amazon_collisions import batch_read

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
OG = re.compile(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)', re.I)
OG2 = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:site_name["\']', re.I)
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
AMZ = re.compile(r"amazon\.|amzn\.|sellercentral", re.I)


def clean(t):
    t = re.sub(r"&(nbsp|amp|#39|quot);", " ", t or "")
    return re.sub(r"\s+", " ", t).strip()


def brand_of(domain):
    d = norm_domain(domain)
    if not d:
        return {"domain": "", "brand": "", "title": "", "status": "no-domain"}
    for url in (f"https://{d}", f"https://www.{d}"):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=9,
                             verify=False, allow_redirects=True)
            if r.status_code >= 400 or not r.text:
                continue
            h = r.text[:300000]
            m = OG.search(h) or OG2.search(h)
            t = TITLE.search(h)
            return {"domain": d, "brand": clean(m.group(1))[:60] if m else "",
                    "title": clean(t.group(1))[:90] if t else "", "status": "ok"}
        except Exception:
            continue
    return {"domain": d, "brand": "", "title": "", "status": "unreachable"}


def fp_names(f):
    return [norm_name(x) for x in (f.get("brand"), f.get("title")) if norm_name(x)]


def same_brand(a, b):
    for nx in fp_names(a):
        for ny in fp_names(b):
            if nx == ny or nx in ny or ny in nx or jaro_winkler(nx, ny) >= 0.92:
                return True
    return False


def main():
    D = r"C:\Users\Ivan\levanta-shopify-run"
    col = json.load(open(os.path.join(D, "amazon_collisions.json"), encoding="utf-8"))
    review, conf = col["review"], col["confident"]

    ids = [c["hs_id"] for c in conf] + [r["hs_id"] for r in review]
    cur = batch_read(ids, ["name", "domain"])
    rows = []
    for c in conf:
        d = (cur.get(c["hs_id"], {}).get("domain") or "").strip().lower()
        if d and d != c["push_domain"].lower():
            rows.append({"hs_id": c["hs_id"], "hs_name": cur[c["hs_id"]].get("name"),
                         "hs_domain": d, "push_domain": c["push_domain"],
                         "src": "post_write_mismatch"})
    for r in review:
        p = cur.get(r["hs_id"], {})
        rows.append({"hs_id": r["hs_id"], "hs_name": p.get("name"),
                     "hs_domain": (p.get("domain") or "").strip().lower(),
                     "push_domain": r["push_domain"], "src": "review_list"})
    print(f"flagged rows: {len(rows)}")

    doms = sorted({r["hs_domain"] for r in rows if r["hs_domain"] and not AMZ.search(r["hs_domain"])}
                  | {r["push_domain"] for r in rows})
    print(f"fetching {len(doms)} homepages (skipping amazon.com, it tells us nothing)...")
    with ThreadPoolExecutor(max_workers=20) as ex:
        fp = {d: f for d, f in zip(doms, ex.map(brand_of, doms))}

    out, decisions = Counter(), []
    for r in rows:
        hsd = r["hs_domain"]
        a = fp.get(hsd, {"status": "skipped-amazon"})
        b = fp.get(r["push_domain"], {"status": "no-domain"})
        hs_n = norm_name(r.get("hs_name"))
        stem = re.sub(r"[^a-z0-9]", "",
                      root_domain(norm_domain(r["push_domain"])).split(".")[0])
        hs_blind = (not hsd) or bool(AMZ.search(hsd)) or a.get("status") != "ok"

        def name_matches_push():
            if not hs_n:
                return False
            hn = hs_n.replace(" ", "")
            if hn and (hn in stem or stem in hn):
                return True
            for ny in fp_names(b):
                if hs_n in ny or ny in hs_n or jaro_winkler(hs_n, ny) >= 0.90:
                    return True
            return False

        if hs_blind:
            if name_matches_push():
                call = "SUPPRESS"
                why = (f"same brand: HubSpot name '{r['hs_name']}' == "
                       f"'{b.get('brand') or b.get('title','')[:34]}' ({r['push_domain']}); "
                       f"HubSpot side still on {hsd or 'no domain'}")
            else:
                call = "KEEP"
                why = (f"HubSpot name '{r['hs_name']}' does not match push brand "
                       f"'{b.get('brand') or b.get('title','')[:34]}'")
        elif same_brand(a, b):
            call = "SUPPRESS"
            why = (f"same brand both sides: '{a.get('brand') or a.get('title','')[:30]}' ~ "
                   f"'{b.get('brand') or b.get('title','')[:30]}'")
        else:
            fits = any(hs_n and (hs_n in ny or ny in hs_n or jaro_winkler(hs_n, ny) >= 0.90)
                       for ny in fp_names(a))
            if fits:
                call = "KEEP"
                why = (f"different companies: HubSpot '{a.get('brand') or a.get('title','')[:26]}' "
                       f"({hsd}) vs push '{b.get('brand') or b.get('title','')[:26]}'")
            else:
                call = "MISLABEL"
                why = (f"HubSpot name '{r['hs_name']}' matches neither its own domain {hsd} "
                       f"('{a.get('brand') or a.get('title','')[:26]}') nor the push row")
        out[call] += 1
        decisions.append({**r, "call": call, "why": why})

    print("\n=== DECISIONS ===", dict(out))
    for c in ("SUPPRESS", "KEEP", "MISLABEL"):
        sel = [d for d in decisions if d["call"] == c]
        if not sel:
            continue
        print(f"\n--- {c} ({len(sel)}) ---")
        for d in sel:
            print(f"  {d['push_domain'][:34]:34} HS {d['hs_id']:>12} "
                  f"'{str(d['hs_name'])[:24]:24}' {d['hs_domain'][:22]:22}")
            print(f"     {d['why'][:118]}")
    json.dump(decisions, open(os.path.join(D, "groups", "flagged_decisions.json"), "w",
                              encoding="utf-8"), ensure_ascii=False, indent=1)
    supp = sorted({d["push_domain"] for d in decisions if d["call"] == "SUPPRESS"})
    json.dump(supp, open(os.path.join(D, "groups", "flagged_suppress_domains.json"), "w",
                         encoding="utf-8"))
    print(f"\nextra push domains to suppress: {len(supp)}")


if __name__ == "__main__":
    main()
