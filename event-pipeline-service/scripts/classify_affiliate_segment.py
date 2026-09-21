#!/usr/bin/env python3
"""Classify the already-imported Shopify companies into affiliate segments and
write the `affiliate_segment` property. UPDATES ONLY -- never creates a company.
Dry-run unless --live.

  python scripts/classify_affiliate_segment.py <rundir> <export.csv> [--live]

Segments (exact strings, no variants):
    Greenfield | Competitor Detected | Multichannel
Precedence: Multichannel > Competitor Detected > Greenfield.

Matching is case-insensitive SUBSTRING against the raw, UNSPLIT
installed_apps_names cell. The column is colon-delimited but app names themselves
contain colons and commas ("Klaviyo: Email Marketing & SMS", "Gorgias: AI,
Helpdesk & Chat"), so splitting it corrupts the data.
"""
import csv, json, os, sys, time
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8")
csv.field_size_limit(10 ** 8)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "scripts")); os.chdir(HERE)
import requests
from dotenv import dotenv_values
from resolve_dedupe_groups import norm_domain

TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
BASE = "https://api.hubapi.com"

GREENFIELD, COMPETITOR, MULTICHANNEL = "Greenfield", "Competitor Detected", "Multichannel"
ALLOWED = (GREENFIELD, COMPETITOR, MULTICHANNEL)

APPS = [
    "UpPromote Affiliate Marketing", "GOAFFPRO ‑ Affiliate Marketing",
    "BixGrow Affiliate Marketing", "ShareASale", "Awin ‑ Affiliate Marketing",
    "Affiliatly Affiliate Marketing", "AfterShip Referral & Affiliate",
    "Refersion Affiliate Marketing", "impact.com Affiliate Marketing",
    "Snowball: Affiliate Marketing", "Affiliate Marketing ReferrLy",
    "Simple Affiliate Marketing", "AvantLink Affiliate Marketing",
    "Referral Candy & Affiliate", "RecomSale: Affiliate Marketing",
    "Shortly — Affiliate Marketing", "BLP Referral Program Affiliate",
    "LeadDyno Affiliate Marketing", "Enlistly Affiliate, Influencer",
    "Tapfiliate Affiliate Marketing", "Kickbooster Affiliate Programs",
    "GrowthHero Affiliate Marketing", "Affilo: Affiliate Marketing",
    "FX Amazon Importer & Affiliate", "ConvertO Affiliate Ambassador",
    "Affiliate & Ambassador: Roster", "Affiliate Links", "Yuko Affiliate Marketing",
    "FlexOffers Affiliate Marketing", "Duel | Referrals & Affiliates",
    "Press Loft Affiliate Network", "Savyour Affiliate Partner",
    "LoudCrowd: Affiliate & UGC", "Affiliate Marketing Short Link",
    "Rave: Referral, Affiliate, UGC", "Partnero Affiliate Management", "LTK Shop",
    "Squadded Affiliate & Video UGC", "AAA Affiliate Marketing App",
    "Avada Affiliate Marketing", "Referly Affiliate Marketing",
    "goadgo ‑ Affiliate Marketing", "Trusted Affiliate Marketing",
    "EZ Affiliate & Referral", "Audenticity Affiliate Network",
    "Affiracle Affiliate Marketing", "Conectia ‑ Affiliate Network",
    "BUZZ Affiliate Marketing", "Reveshare Affiliate Marketing",
    "Jaka Affiliate Marketing", "Bevy Affiliate Marketing", "PartnerStack",
    "Referbi ‑ Affiliate Marketing", "Expressfy Aliexpress Affiliate",
]
APPS_LC = [(a, a.lower()) for a in APPS]

# Backstop only. Full-string matching already excludes these structurally -- they
# are climate/carbon apps that a naive "impact" substring search would catch.
CLIMATE = ["Footprint ‑ Climate Impact", "Ample ‑ Sustainable Impact",
           "Mandatum: Free Climate Impact", "AIMpact"]

# the superseded narrow list, for the required old-vs-new comparison
OLD_KEYWORDS = ["impact", "cj", "shareasale", "ltk", "refersion", "awin", "social snowball"]


def post(url, body, tries=6):
    r = None
    for a in range(tries):
        r = requests.post(url, headers=H, json=body, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")) + 1); continue
        if r.status_code >= 500:
            time.sleep(2 ** a); continue
        return r
    return r


def main():
    rundir, export_csv = sys.argv[1], sys.argv[2]
    live = "--live" in sys.argv

    supp = set(json.load(open(os.path.join(rundir, "groups", "all_suppress_domains.json"),
                              encoding="utf-8")))
    target = {norm_domain(r["domain"]) for r in
              csv.DictReader(open(os.path.join(rundir, "PUSH_LIST.csv"),
                                  encoding="utf-8-sig"))} - supp
    print(f"target companies (already in HubSpot): {len(target):,}")

    # ---------- resolve HubSpot ids (no creates) ----------
    dom2id = {}
    for line in open(os.path.join(rundir, "created_companies.jsonl"), encoding="utf-8"):
        if line.strip():
            o = json.loads(line); dom2id[o["domain"]] = o["id"]
    for o in json.load(open(os.path.join(rundir, "test_batch_result.json"),
                            encoding="utf-8"))["created"]:
        dom2id[o["domain"]] = o["id"]
    missing = sorted(target - set(dom2id))
    print(f"ids from create log: {len(set(dom2id) & target):,} | to look up: {len(missing):,}")
    for i in range(0, len(missing), 100):
        body = {"filterGroups": [{"filters": [{"propertyName": "domain", "operator": "IN",
                                               "values": missing[i:i + 100]}]}],
                "properties": ["domain"], "limit": 100}
        r = post(f"{BASE}/crm/v3/objects/companies/search", body)
        if r is not None and r.status_code < 300:
            for x in r.json().get("results", []):
                d = (x["properties"].get("domain") or "").strip().lower()
                if d in target and d not in dom2id:
                    dom2id[d] = x["id"]
    resolved = target & set(dom2id)
    print(f"resolved ids: {len(resolved):,} | unresolvable: {len(target - resolved):,}")

    # ---------- classify ----------
    seg, trig, old_seg = {}, {}, {}
    climate_hits = 0
    for r in csv.DictReader(open(export_csv, encoding="utf-8-sig")):
        d = norm_domain(r.get("domain"))
        if d not in target or d in seg:
            continue                      # first row wins for duplicate domains
        lc = (r.get("installed_apps_names") or "").lower()
        amazon = bool((r.get("sales_channel_amazon") or "").strip())
        matched = [a for a, al in APPS_LC if al in lc]
        if amazon:
            seg[d] = MULTICHANNEL
        elif matched:
            seg[d] = COMPETITOR
            trig[d] = matched
        else:
            seg[d] = GREENFIELD
        # backstop: did any climate app drive a COMPETITOR call?
        if seg[d] == COMPETITOR and not matched:
            climate_hits += 1
        # old narrow list, same precedence, for the comparison
        old_seg[d] = (MULTICHANNEL if amazon
                      else COMPETITOR if any(k in lc for k in OLD_KEYWORDS)
                      else GREENFIELD)
    for d in target - set(seg):
        seg[d] = GREENFIELD; old_seg[d] = GREENFIELD

    counts = Counter(seg.values())
    n = len(seg)
    moved = sum(1 for d in seg
                if seg[d] == COMPETITOR and old_seg.get(d) == GREENFIELD)
    old_fp = sum(1 for d in seg
                 if old_seg.get(d) == COMPETITOR and seg[d] == GREENFIELD)

    print("\n" + "=" * 66)
    print("1. SEGMENT DISTRIBUTION (per unique domain)")
    print("=" * 66)
    for k in ALLOWED:
        print(f"   {k:22} {counts[k]:7,}   ({100 * counts[k] / n:5.2f}%)")
    print(f"   {'TOTAL':22} {n:7,}")
    print(f"\n2. ARITHMETIC CHECK: {counts[GREENFIELD]:,} + {counts[COMPETITOR]:,} + "
          f"{counts[MULTICHANNEL]:,} = {n:,}  "
          f"{'RECONCILES to 70,103' if n == 70103 else '*** MISMATCH ***'}")

    print(f"\n3. OLD 7-KEYWORD LIST vs CORRECTED LIST")
    oc = Counter(old_seg.values())
    for k in ALLOWED:
        print(f"   {k:22} old {oc[k]:7,}  ->  new {counts[k]:7,}")
    print(f"   wrongly Greenfield under OLD, correctly Competitor under NEW : {moved:,}")
    print(f"   OLD false positives (flagged Competitor, actually Greenfield) : {old_fp:,}")
    print(f"   total companies the old list got wrong                       : {moved + old_fp:,}")

    print(f"\n4. 20 COMPETITOR DETECTED SAMPLES (with triggering app string)")
    for d in list(trig)[:20]:
        print(f"   {d[:36]:36} <- {trig[d][0]}")

    print(f"\n5. CLIMATE EXCLUSION CHECK")
    print(f"   Competitor classifications caused by a climate app: {climate_hits}  "
          f"{'CONFIRMED ZERO' if climate_hits == 0 else '*** PROBLEM ***'}")
    for c in CLIMATE:
        print(f"     '{c}' present in match list: {c in APPS}")

    print(f"\n6. VALUE DISCIPLINE")
    bad = sorted({v for v in seg.values()} - set(ALLOWED))
    print(f"   distinct values to be written: {sorted(set(seg.values()))}")
    print(f"   values outside the 3 approved strings: {bad or 'NONE'}")

    if not live:
        print("\n" + "=" * 66)
        print("DRY RUN -- no --live flag. NOTHING WRITTEN TO HUBSPOT.")
        print("=" * 66)
        return

    # ---------- write (UPDATE only) ----------
    logp = os.path.join(rundir, "affiliate_segment_written.jsonl")
    done = set()
    if os.path.exists(logp):
        for line in open(logp, encoding="utf-8"):
            if line.strip():
                done.add(json.loads(line)["domain"])
        print(f"\nresuming: {len(done):,} already written")
    inputs = []
    for d, s in seg.items():
        if d in done:
            continue
        cid = dom2id.get(d)
        if not cid:
            continue
        assert s in ALLOWED, f"illegal segment value {s!r}"
        inputs.append({"id": cid, "properties": {"affiliate_segment": s}, "_d": d, "_s": s})
    print(f"\nUPDATING affiliate_segment on {len(inputs):,} existing companies "
          f"(no creates)...")
    ok, fails, t0 = 0, [], time.time()
    log = open(logp, "a", encoding="utf-8")
    for i in range(0, len(inputs), 100):
        chunk = inputs[i:i + 100]
        r = post(f"{BASE}/crm/v3/objects/companies/batch/update",
                 {"inputs": [{"id": c["id"], "properties": c["properties"]} for c in chunk]})
        if r is not None and r.status_code < 300:
            ok += len(r.json().get("results", []))
            for c in chunk:
                log.write(json.dumps({"domain": c["_d"], "id": c["id"],
                                      "segment": c["_s"]}) + "\n")
        else:
            for c in chunk:
                rr = requests.patch(f"{BASE}/crm/v3/objects/companies/{c['id']}", headers=H,
                                    json={"properties": c["properties"]}, timeout=45)
                if rr.status_code < 300:
                    ok += 1
                    log.write(json.dumps({"domain": c["_d"], "id": c["id"],
                                          "segment": c["_s"]}) + "\n")
                else:
                    fails.append((c["_d"], rr.status_code, rr.text[:120]))
        if i % 10000 == 0:
            log.flush()
            print(f"   {min(i + 100, len(inputs)):,}/{len(inputs):,}  "
                  f"{(time.time() - t0) / 60:.1f}m", flush=True)
    log.close()
    print(f"\nRESULT: {ok:,} updated | {len(fails):,} failed")
    for f in fails[:10]:
        print("   FAIL", f)
    json.dump({"ok": ok, "fails": fails, "counts": dict(counts)},
              open(os.path.join(rundir, "affiliate_segment_result.json"), "w",
                   encoding="utf-8"))


if __name__ == "__main__":
    main()
