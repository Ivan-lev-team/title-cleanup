#!/usr/bin/env python3
"""Rebuild PASS_LIST.csv and PUSH_LIST.csv from the surviving decision layer.

Inputs (all durable):
  - groups/bucket_assignment.json  : domain -> bucket, for all 94,001 PASS rows
  - SHOPIFY EXPORT.csv             : the original 98,539-row source

Deterministic name normalization only (no LLM, no network). Rows whose name
cannot be tied to the domain are flagged `name_method=weak` so an optional LLM
polish can target just those later.

  python scripts/rebuild_push_list.py <export.csv> <outdir>
"""
import csv, json, os, re, sys
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8")
csv.field_size_limit(10 ** 8)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "scripts"))
from resolve_dedupe_groups import norm_domain, root_domain

KEEP = {"net_new", "B_regional_keep", "C_distinct_keep"}
SEP = re.compile(r"\s*[|•·–—]\s*|\s+[-–]\s+|\s*:\s+")
SEO_TAIL = re.compile(
    r"\b(official (site|store|website|online store)|free shipping[^|]*|shop now|buy online|"
    r"online store|online shop|home ?page|welcome|best sellers?|new arrivals|"
    r"shop all|usa|us|uk|canada|australia|since \d{4})\b\.?\s*$", re.I)
SEO_LEAD = re.compile(r"^(welcome to|shop|buy|the official)\s+", re.I)
TM = re.compile(r"[®™©]")
TLD = re.compile(r"(?:https?://)?(?:www\.)?\b([\w&'\- ]+?)\.(com|ca|net|org|co|io|store|shop|us|uk|"
                 r"au|de|fr|nl|es|it|life|club|xyz|online|app)\b\.?", re.I)
GENERIC = {"home","shop","store","official site","products","collections","catalog",
           "my store","index","untitled","new page","home page","welcome","main"}
CC2NAME = {"US": "United States", "CA": "Canada", "GB": "United Kingdom", "AU": "Australia",
           "DE": "Germany", "FR": "France", "ES": "Spain", "IT": "Italy", "NL": "Netherlands",
           "JP": "Japan", "MX": "Mexico", "BR": "Brazil", "IN": "India", "SG": "Singapore",
           "NZ": "New Zealand", "IE": "Ireland", "SE": "Sweden"}


def key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("&", "and"))


def clean_seg(s):
    s = TM.sub("", s or "").strip(" -–—|·•,")
    s = SEO_LEAD.sub("", s)
    prev = None
    while prev != s:
        prev = s
        s = SEO_TAIL.sub("", s).strip(" -–—|·•,")
    return s.strip()


def strip_tld(n):
    out = TLD.sub(lambda m: m.group(1), n).strip(" .-|,")
    out = re.sub(r"\s{2,}", " ", out)
    return out if len(out) >= 2 else n


def normalize(title, domain):
    """(name, method). method: exact|substr|token|weak"""
    stem = root_domain(norm_domain(domain)).split(".")[0]
    stem = re.sub(r"[^a-z0-9]", "", stem.lower())
    raw = TM.sub("", (title or "")).strip()
    segs = [clean_seg(x) for x in SEP.split(raw) if clean_seg(x)]
    if not segs and clean_seg(raw):
        segs = [clean_seg(raw)]
    best, score = None, -1
    for s in segs:
        if not s or s.lower() in GENERIC:
            continue
        k = key(s)
        if not k:
            continue
        if k == stem:
            sc = 4
        elif k in stem or stem in k:
            sc = 3
        elif set(re.findall(r"[a-z0-9]{4,}", s.lower())) & {stem}:
            sc = 2
        else:
            sc = 0
        if sc > score or (sc == score and best and len(s) < len(best)):
            best, score = s, sc
    method = {4: "exact", 3: "substr", 2: "token"}.get(score, "weak")
    if best and score >= 2 and len(best) <= 60:
        return strip_tld(best), method
    # no segment ties to the domain: derive a readable name from the stem
    parts = [p for p in re.split(r"[-_]", root_domain(norm_domain(domain)).split(".")[0]) if p]
    return (" ".join(p.capitalize() for p in parts) or domain), "weak"


def main():
    export_csv, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    bucket = json.load(open(os.path.join(outdir, "groups", "bucket_assignment.json"),
                            encoding="utf-8"))
    print(f"decision layer: {len(bucket):,} domains")

    cols = ["domain", "root_domain", "name", "name_raw", "name_method", "bucket",
            "city", "state", "country", "phone", "linkedin_company_page",
            "description", "numberofemployees", "website", "categories",
            "estimated_yearly_sales"]
    npass = npush = 0
    methods, buckets = Counter(), Counter()
    fpass = open(os.path.join(outdir, "PASS_LIST.csv"), "w", encoding="utf-8-sig", newline="")
    fpush = open(os.path.join(outdir, "PUSH_LIST.csv"), "w", encoding="utf-8-sig", newline="")
    wpass, wpush = csv.writer(fpass), csv.writer(fpush)
    wpass.writerow(cols); wpush.writerow(cols)

    with open(export_csv, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            d = norm_domain(r.get("domain"))
            b = bucket.get(d)
            if b is None:
                continue                      # a FAIL row, not in the decision layer
            name, method = normalize(r.get("title"), d)
            row = [d, root_domain(d), name, (r.get("title") or "").strip(), method, b,
                   (r.get("city") or "").strip(), (r.get("state") or "").strip(),
                   CC2NAME.get((r.get("country_code") or "").strip().upper(), ""),
                   (r.get("phones") or "").split(":")[0].strip(),
                   (r.get("linkedin_url") or "").strip(),
                   (r.get("description") or "").strip()[:900],
                   (r.get("employee_count") or "").strip(),
                   (r.get("domain_url") or "").strip(),
                   (r.get("categories") or "").strip(),
                   re.sub(r"[^0-9.]", "", r.get("estimated_yearly_sales") or "")]
            wpass.writerow(row); npass += 1
            methods[method] += 1; buckets[b] += 1
            if b in KEEP:
                wpush.writerow(row); npush += 1
    fpass.close(); fpush.close()

    print(f"\nPASS_LIST.csv rows : {npass:,}")
    print(f"PUSH_LIST.csv rows : {npush:,}")
    print(f"\nbucket mix: {dict(buckets)}")
    print("name method mix:")
    for m, c in methods.most_common():
        print(f"   {m:8} {c:7,}  ({100*c/npass:5.1f}%)")
    print(f"\nwrote to {outdir} (outside %TEMP%, not subject to Storage Sense)")


if __name__ == "__main__":
    main()
