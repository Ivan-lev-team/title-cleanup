#!/usr/bin/env python3
"""
Seamless enrichment PILOT -- 5-10 companies, end to end, no HubSpot writes.

Flow:
  1  pick pilot companies (Pod RevOps, zero contacts, has a domain)
  2  Seamless search/contacts  -- FREE, title-filtered. The filter runs HERE,
                                  before any credit is spent.
  3  Seamless contacts/research -- PAID, 1 credit per submitted contact
  4  email waterfall: DeBounce verify -> Prospeo (repair) -> LeadMagic (repair)
  5  mobile: Seamless-supplied only this round, no repair provider
  6  dedupe check against HubSpot (LinkedIn www + non-www, and email)
  7  audit summary

Writes nothing to HubSpot. Output lands in runs/seamless_pilot_<date>/.

  python scripts/seamless_pilot.py <outdir> [--companies N] [--per-company N]
                                   [--cohort-csv path] [--from-json path]
                                   [--skip-research]

--cohort-csv    reuse an existing company sample instead of re-pulling
--from-json     skip the Seamless REST calls and load research results from a
                JSON file (use this when the results were pulled via the
                Seamless MCP connector, which a script cannot call)
--skip-research stop after the free search step, spending zero credits
"""
import csv, json, os, sys, time, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)

import requests
from dotenv import dotenv_values

import config
import debounce_client
import leadmagic_client
import prospeo_client
import prospeo_search
import seamless_client

# Title lists live in icp_titles.py (Ivan's INCLUDE/EXCLUDE, 2026-09-18).
#
# Seamless caps jobTitle at 10 entries per call and offers NO title-exclusion
# parameter, so neither list can be passed whole:
#   INCLUDE -> searched in batches of 10 (icp_titles.search_batches()) and
#              unioned client-side. Search is free.
#   EXCLUDE -> applied locally by icp_titles.title_passes() against the `title`
#              that search returns free, BEFORE any credit is spent.
import icp_titles

# Domain match mode, per spec revision 2026-09-18. NOT hardcoded behaviour.
#   strict -- a contact whose returned domain is not the one we submitted is
#             DROPPED: no company credit, no waterfall, no audit row.
#   loose  -- kept, tagged domain_mismatch=true, written to a separate
#             quarantine file, and NEVER attached to the target company.
# Default loose, per spec. Override with --domain-match strict.
#
# Deviation from the spec text, deliberate: the spec puts this check AFTER
# research. The `domain` field is already present in the FREE search response,
# and measured leakage is 42%, so checking after research would burn ~1 credit
# per leaked contact (~49 per 100 companies). The check therefore runs
# pre-research as the primary filter AND again post-research in case the
# researched record reports a different domain. Same semantics, far cheaper.
DOMAIN_MATCH_MODE = "loose"

TARGET_TITLES = icp_titles.INCLUDE
TARGET_SENIORITY = icp_titles.TARGET_SENIORITY
TARGET_DEPARTMENT = icp_titles.TARGET_DEPARTMENT

HUBSPOT_TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
HS = {"Authorization": "Bearer " + HUBSPOT_TOKEN, "Content-Type": "application/json"}
HSBASE = "https://api.hubapi.com"

COMPANY_PROPS = ["name", "domain", "annualrevenue", "country", "marketplaces",
                 "pod", "sdr_owner", "num_associated_contacts", "numberofemployees"]

ROW_FIELDS = [
    "company_id", "company_name", "company_domain",
    "first_name", "last_name", "job_title", "linkedin_url",
    "email", "email_source", "email_seamless_validity", "email_seamless_confidence",
    "email_verify_status", "email_verify_provider", "email_verify_raw",
    "email_lm_status", "email_lm_raw",
    "email_resolve_status", "email_resolve_provider", "email_replacement_source",
    "email_before_repair", "repaired_by", "final_status", "domain_mismatch", "pushable",
    "email2", "email2_validity", "email3", "email3_validity",
    "mobile", "mobile_source", "mobile_datatype", "mobile_confidence",
    "phone1", "phone1_datatype", "phone2", "phone2_datatype",
    "dedupe_state", "hubspot_contact_id",
    "seamless_status", "request_id", "search_result_id", "discovery_layer",
]


def hs_post(path, body, tries=6):
    url = HSBASE + path
    resp = None
    for attempt in range(tries):
        try:
            resp = requests.post(url, headers=HS, json=body, timeout=60)
        except requests.RequestException:
            time.sleep(2 ** attempt); continue
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", "2")) + 1); continue
        if resp.status_code >= 500 or resp.status_code == 400:
            time.sleep(2 ** attempt); continue
        return resp
    return resp


def arg(flag, default=None):
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


# --- step 1 ----------------------------------------------------------------

# Revenue sanity band. The pool's median is ~$1.8M, but a handful of records
# carry the revenue of whatever marketplace landed in their domain field (one
# sits on amazon.com with $638B). Anything above the ceiling is a data defect,
# not a big brand, and would distort a small pilot badly.
REV_FLOOR, REV_CEILING = 500_000, 500_000_000
POOL_MULTIPLE = 40   # how many candidates to gather before sampling n of them


def pick_companies(n, seed=None, rev_floor=None, rev_ceiling=None):
    """Random-sample n Pod RevOps zero-contact companies fit to enrich.

    Keyset-paginated (hs_object_id > last) because HubSpot search caps
    offset+limit at 10,000.

    Three hygiene rules, learned from the first attempt at this sample, which
    returned a record named "2K" sitting on amazon.com with Amazon's $638B
    revenue plus a www.-prefixed domain that no provider would match:

      1. domain normalized through enrichment.clean_domain (strips scheme/www/path)
      2. marketplace and non-brand hosts excluded, reusing the existing
         NON_BRAND_HOSTS set in scripts/fix_list_domains.py rather than a
         second copy of the same list
      3. revenue must be present and inside REV_FLOOR..REV_CEILING, so the
         pilot sample is representative of the ICP band instead of whatever
         happens to sort first by object id

    Sampling is random with the seed recorded by the caller, so the pilot is
    reproducible and nobody has to wonder whether a good or bad result was an
    artifact of where the id sort happened to land.
    """
    import random
    import enrichment
    from fix_list_domains import NON_BRAND_HOSTS

    lo = REV_FLOOR if rev_floor is None else int(rev_floor)
    hi = REV_CEILING if rev_ceiling is None else int(rev_ceiling)
    print("revenue band: $%s - $%s" % (format(lo, ","), format(hi, ",")))
    target_pool = max(n * POOL_MULTIPLE, 200)
    cands, last, seen_domains = [], "0", set()
    while len(cands) < target_pool:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "pod", "operator": "EQ", "value": "Pod RevOps"},
                {"propertyName": "num_associated_contacts", "operator": "EQ", "value": "0"},
                {"propertyName": "domain", "operator": "HAS_PROPERTY"},
                {"propertyName": "annualrevenue", "operator": "GTE", "value": str(lo)},
                {"propertyName": "annualrevenue", "operator": "LT", "value": str(hi)},
                {"propertyName": "hs_object_id", "operator": "GT", "value": last},
            ]}],
            "properties": COMPANY_PROPS,
            "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
            "limit": 100,
        }
        r = hs_post("/crm/v3/objects/companies/search", body)
        if r is None or r.status_code >= 300:
            print("company search failed:", getattr(r, "status_code", "?"),
                  getattr(r, "text", "")[:200])
            sys.exit(1)
        res = r.json().get("results", [])
        if not res:
            break
        for x in res:
            p = x["properties"]
            dom = enrichment.clean_domain(p.get("domain") or "")
            if not dom or "." not in dom:
                continue
            if any(dom == h or dom.endswith("." + h) for h in NON_BRAND_HOSTS):
                continue
            if dom in seen_domains:       # one record per domain in a pilot
                continue
            seen_domains.add(dom)
            cands.append({
                "company_id": x["id"],
                "company_name": (p.get("name") or "").strip(),
                "company_domain": dom,
                "annualrevenue": p.get("annualrevenue") or "",
                "country": p.get("country") or "",
                "hs_employees": p.get("numberofemployees") or "",
            })
        last = res[-1]["id"]

    rng = random.Random(seed)
    picked = rng.sample(cands, min(n, len(cands)))
    print(f"candidate pool after hygiene: {len(cands):,}  ->  sampled {len(picked)} "
          f"(seed={seed})")
    return picked


# --- step 6 ----------------------------------------------------------------

def dedupe_check(rows):
    """Mark each row net-new or already-exists.

    LinkedIn is checked in BOTH canonical forms because HubSpot stores them
    differently across two properties -- hs_linkedin_url without www,
    linkedin_personal_url with www. Missing that is how duplicates get in.
    Reuses the approach in scripts/hubspot_dedupe_leadership.py.
    """
    import re

    def handle(url):
        m = re.search(r"linkedin\.com/in/([a-z0-9\-_%\.]+)", (url or "").lower())
        return m.group(1).strip("/") if m else ""

    by_handle, by_email = {}, {}
    handles = sorted({handle(r.get("linkedin_url", "")) for r in rows} - {""})
    emails = sorted({(r.get("email") or "").strip().lower() for r in rows} - {""})

    for i in range(0, len(handles), 100):
        b = handles[i:i + 100]
        body = {"filterGroups": [
            {"filters": [{"propertyName": "hs_linkedin_url", "operator": "IN",
                          "values": [f"https://linkedin.com/in/{h}" for h in b]}]},
            {"filters": [{"propertyName": "linkedin_personal_url", "operator": "IN",
                          "values": [f"https://www.linkedin.com/in/{h}" for h in b]}]}],
            "properties": ["hs_linkedin_url", "linkedin_personal_url", "email"],
            "limit": 100}
        r = hs_post("/crm/v3/objects/contacts/search", body)
        if r is None or r.status_code >= 300:
            continue
        for x in r.json().get("results", []):
            p = x["properties"]
            for key in ("hs_linkedin_url", "linkedin_personal_url"):
                h = handle(p.get(key) or "")
                if h:
                    by_handle[h] = x["id"]

    for i in range(0, len(emails), 100):
        b = emails[i:i + 100]
        body = {"filterGroups": [{"filters": [
                    {"propertyName": "email", "operator": "IN", "values": b}]}],
                "properties": ["email"], "limit": 100}
        r = hs_post("/crm/v3/objects/contacts/search", body)
        if r is None or r.status_code >= 300:
            continue
        for x in r.json().get("results", []):
            e = (x["properties"].get("email") or "").strip().lower()
            if e:
                by_email[e] = x["id"]

    for r in rows:
        hid = by_handle.get(handle(r.get("linkedin_url", ""))) or \
              by_email.get((r.get("email") or "").strip().lower())
        r["hubspot_contact_id"] = hid or ""
        r["dedupe_state"] = "already-exists" if hid else "net-new"
    return rows


# --- step 4 ----------------------------------------------------------------

def verify_and_repair(rows):
    """Step 4 email waterfall, per spec revision 2026-09-18.

        1. DeBounce verify            valid -> done
        2. LeadMagic RESOLVE          run on risky AND invalid (LeadMagic
                                      resolves some invalids, not just risky)
        3. Prospeo FIND replacement   a new address, not a re-check
        4. LeadMagic VERIFY           the Prospeo replacement

    Nothing is ever discarded. A contact that exhausts all four steps gets
    final_status = "no_valid_email_found" and keeps its row with company,
    name, title and LinkedIn intact. Only a `valid` verdict from step 1, 2 or
    4 is pushable; everything else is "not pushable yet".

    LeadMagic is only ever a VERIFIER here, never a finder, so it is never
    grading an address it produced itself.
    """
    for r in rows:
        r.setdefault("email_resolve_status", "")
        r.setdefault("email_resolve_provider", "")
        r.setdefault("email_replacement_source", "")
        r.setdefault("email_before_repair", "")
        r.setdefault("repaired_by", "")
        r["final_status"] = ""

        email = (r.get("email") or "").strip()

        # ---- step 1: DeBounce
        if email:
            d = debounce_client.verify_email(email)
            r["email_verify_status"] = d.get("status") or ""
            r["email_verify_provider"] = "DeBounce"
            r["email_verify_raw"] = d.get("raw_status") or d.get("error") or ""
        else:
            r["email_verify_status"] = ""
            r["email_verify_provider"] = ""
            r["email_verify_raw"] = "no email from Seamless"

        if r["email_verify_status"] == "valid":
            r["final_status"] = "pushable"
            r["pushable"] = "yes"
            continue

        # ---- step 2: LeadMagic RESOLVE (risky AND invalid, per spec)
        if email and r["email_verify_status"] in ("risky", "invalid", "unknown"):
            lm = leadmagic_client.validate_email(email)
            r["email_resolve_status"] = lm.get("status") or ""
            r["email_resolve_provider"] = "LeadMagic"
            r["email_lm_status"] = lm.get("status") or ""
            r["email_lm_raw"] = lm.get("raw_status") or lm.get("error") or ""
            if lm.get("status") == "valid":
                r["email_verify_status"] = "valid"
                r["email_verify_provider"] = "DeBounce+LeadMagic(resolve)"
                r["final_status"] = "pushable"
                r["pushable"] = "yes"
                continue

        # ---- step 3: Prospeo FIND a replacement
        if r.get("first_name") or r.get("last_name"):
            full = (r.get("first_name", "") + " " + r.get("last_name", "")).strip()
            pr = prospeo_client.enrich_person(full, r.get("company_name", ""),
                                              r.get("company_domain", ""),
                                              linkedin_url=r.get("linkedin_url", "")) or {}
            repl = (pr.get("email") or "").strip()
            if repl and repl.lower() != email.lower():
                r["email_replacement_source"] = "Prospeo"
                r["email_before_repair"] = email
                r["repaired_by"] = "Prospeo"
                r["email"] = repl
                r["email_source"] = "Prospeo"
                # ---- step 4: LeadMagic VERIFY the replacement
                v = leadmagic_client.validate_email(repl)
                r["email_verify_status"] = v.get("status") or ""
                r["email_verify_provider"] = "LeadMagic (repair pass)"
                r["email_verify_raw"] = v.get("raw_status") or v.get("error") or ""
                if v.get("status") == "valid":
                    r["final_status"] = "pushable"
                    r["pushable"] = "yes"
                    continue
                r["final_status"] = "no_valid_email_found"
                r["pushable"] = "no"
                continue
            r["email_replacement_source"] = "Prospeo (no replacement found)"

        # exhausted -- retained, not discarded
        r["final_status"] = "no_valid_email_found"
        r["pushable"] = "no"
    return rows


PROSPEO_MAX_TITLES = 50   # 50 accepted, 113 returns INVALID_FILTERS
# Prospeo must NEVER supply a mobile. Mobile accuracy is exactly what this
# pilot exists to measure FOR SEAMLESS, so letting a second provider fill that
# column would contaminate the only metric it is testing. Also drops the reveal
# cost from 10 credits to 1. Set by Ivan 2026-09-18.
PROSPEO_REVEAL_MOBILE = False


def prospeo_discovery(zero_yield, per_company, outdir):
    """LAYER 2. Find contacts at companies where Seamless returned nobody.

    Free search -> local ICP gate -> require email_status == VERIFIED -> reveal.

    The VERIFIED pre-screen is the point of this layer. Prospeo shows the
    email's verification status while the address is still masked and unpaid,
    so unlike Seamless we never spend a credit on an address already known to
    be unverified.
    """
    stats = {"companies_probed": len(zero_yield), "companies_with_hits": 0,
             "people_seen": 0, "passed_gate": 0, "verified_email": 0,
             "revealed": 0, "credits_before": prospeo_search.credits_remaining()}
    if not zero_yield:
        print("\nLAYER 2 (Prospeo): no zero-yield companies to probe.")
        return [], stats

    titles = list(dict.fromkeys(list(icp_titles.INCLUDE) +
                                list(icp_titles.CONDITIONAL_INCLUDE)))
    batches = [titles[i:i + PROSPEO_MAX_TITLES]
               for i in range(0, len(titles), PROSPEO_MAX_TITLES)]
    print("\nLAYER 2 (Prospeo discovery) on %d companies Seamless missed"
          % len(zero_yield))
    print("  %d title batches of <=%d, seniority=%s, search is FREE"
          % (len(batches), PROSPEO_MAX_TITLES, ",".join(prospeo_search.PROSPEO_SENIORITY)))

    out, rejected = [], []
    for c in zero_yield:
        dom = c["company_domain"]
        seen, people = set(), []
        for tb in batches:
            r = prospeo_search.search_person(dom, job_titles=tb,
                                             seniority=prospeo_search.PROSPEO_SENIORITY)
            if r["error"]:
                continue
            for p in r["people"]:
                if p["person_id"] and p["person_id"] not in seen:
                    seen.add(p["person_id"])
                    people.append(p)
        stats["people_seen"] += len(people)
        if not people:
            continue
        stats["companies_with_hits"] += 1

        kept = 0
        for p in people:
            if kept >= per_company:
                break
            ok, why = icp_titles.title_passes(p["job_title"],
                                              employee_count=p.get("employee_count"))
            if not ok:
                rejected.append((dom, p, why))
                continue
            stats["passed_gate"] += 1
            if p["email_status"] != "VERIFIED":
                rejected.append((dom, p, "email_status=%s (not VERIFIED, not revealed)"
                                 % (p["email_status"] or "none")))
                continue
            stats["verified_email"] += 1
            rev = prospeo_search.reveal_person(p["person_id"],
                                               enrich_mobile=PROSPEO_REVEAL_MOBILE)
            if rev["error"] or not rev["email"]:
                rejected.append((dom, p, "reveal failed: %s" % (rev["error"] or "no email")))
                continue
            stats["revealed"] += 1
            kept += 1
            # Deliberately blank. Prospeo is an EMAIL-only discovery layer here;
            # its mobile_status is recorded for reference but never used as a
            # number, so Seamless remains the sole source of mobile data.
            mob = ""
            prospeo_mobile_available = rev.get("mobile_status") or ""
            out.append({
                "company_id": c.get("company_id", ""),
                "company_name": c.get("company_name", "") or p["company_name"],
                "company_domain": dom,
                "first_name": p["first_name"], "last_name": p["last_name"],
                "job_title": p["job_title"], "linkedin_url": p["linkedin_url"],
                "email": rev["email"], "email_source": "Prospeo (discovery)",
                "email_seamless_validity": "", "email_seamless_confidence": "",
                "mobile": mob, "mobile_source": "Prospeo" if mob else "",
                "mobile_datatype": "mobile" if mob else "",
                "mobile_confidence": "",
                "prospeo_mobile_available": prospeo_mobile_available,
                "phone1": mob, "phone1_datatype": "mobile" if mob else "",
                "phone2": "", "phone2_datatype": "",
                "seamless_status": "n/a (prospeo)", "request_id": "",
                "search_result_id": p["person_id"],
                "discovery_layer": "Prospeo",
            })
        print("    %-34s %2d found | %d kept" % (dom[:34], len(people), kept))

    with open(os.path.join(outdir, "prospeo_layer2_rejected.csv"), "w",
              newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["domain", "name", "title", "employees", "email_status", "reason"])
        for dom, p, why in rejected:
            w.writerow([dom, p["full_name"], p["job_title"], p.get("employee_count", ""),
                        p["email_status"], why])

    stats["credits_after"] = prospeo_search.credits_remaining()
    cb, ca = stats["credits_before"], stats["credits_after"]
    stats["credits_used"] = (cb - ca) if None not in (cb, ca) else None
    print("  LAYER 2 result: %d/%d companies had anyone | %d people seen | "
          "%d passed ICP gate | %d had a VERIFIED email | %d revealed"
          % (stats["companies_with_hits"], stats["companies_probed"],
             stats["people_seen"], stats["passed_gate"],
             stats["verified_email"], stats["revealed"]))
    print("  prospeo credits used: %s" % stats["credits_used"])
    return out, stats


# --- step 7 ----------------------------------------------------------------

def audit(rows, companies, per_company, outdir, credits_before, credits_after,
          leak_count=0):
    n = len(rows)
    def pct(a, b):
        return (100.0 * a / b) if b else 0.0

    with_any_email = sum(1 for r in rows if (r.get("email") or "").strip())
    valid = sum(1 for r in rows if r.get("email_verify_status") == "valid")
    risky = sum(1 for r in rows if r.get("email_verify_status") == "risky")
    invalid = sum(1 for r in rows if r.get("email_verify_status") == "invalid")
    unknown = sum(1 for r in rows if r.get("email_verify_status") == "unknown")

    mobile = sum(1 for r in rows if (r.get("mobile") or "").strip())
    main_only = sum(1 for r in rows if not (r.get("mobile") or "").strip()
                    and (r.get("mobile_datatype") or "") == "main")
    no_number = n - mobile - main_only

    netnew = sum(1 for r in rows if r.get("dedupe_state") == "net-new")
    dupes = sum(1 for r in rows if r.get("dedupe_state") == "already-exists")

    # Seamless's own EmailAI verdict vs DeBounce's -- nothing existing measures this.
    # Seamless and DeBounce use different words for the same verdict:
    # Seamless "accept all" == DeBounce "risky" (both mean catch-all domain).
    # Comparing the raw strings counted those as disagreements and understated
    # agreement badly (reported 5/9 when it was really 9/9).
    SYN = {"accept all": "risky", "accept-all": "risky", "catch all": "risky",
           "catch-all": "risky", "safe to send": "valid", "deliverable": "valid"}
    agree, disagree, matrix = 0, 0, {}
    for r in rows:
        s = (r.get("email_seamless_validity") or "").strip().lower()
        s = SYN.get(s, s)
        d = (r.get("email_verify_status") or "").strip().lower()
        d = SYN.get(d, d)
        if not s or not d:
            continue
        matrix[(s, d)] = matrix.get((s, d), 0) + 1
        if s == d:
            agree += 1
        else:
            disagree += 1

    src = {}
    for r in rows:
        k = r.get("email_source") or "(none)"
        src[k] = src.get(k, 0) + 1

    per_co = {}
    for r in rows:
        per_co.setdefault(r["company_id"], 0)
        per_co[r["company_id"]] += 1

    L = []
    L.append("=" * 72)
    L.append("SEAMLESS PILOT AUDIT")
    L.append("=" * 72)
    L.append(f"companies in pilot     : {len(companies)}")
    L.append(f"target contacts/company: {per_company}")
    L.append(f"contacts returned      : {n}")
    L.append("")
    L.append("--- contacts found vs targeted, per company ---")
    for c in companies:
        got = per_co.get(c["company_id"], 0)
        flag = "" if got >= per_company else "   <-- under target"
        if got == 0:
            flag = "   <-- NO QUALIFIED CONTACT FOUND"
        L.append(f"  {c['company_domain']:<34} {got}/{per_company}{flag}")
    L.append("")
    L.append("--- email coverage ---")
    L.append(f"  any email            : {with_any_email}/{n} ({pct(with_any_email, n):.1f}%)")
    L.append(f"  DeBounce valid       : {valid}/{n} ({pct(valid, n):.1f}%)  <- pushable")
    L.append(f"  risky (catch-all)    : {risky} ({pct(risky, n):.1f}%)")
    L.append(f"  invalid              : {invalid} ({pct(invalid, n):.1f}%)")
    L.append(f"  unknown              : {unknown} ({pct(unknown, n):.1f}%)")
    L.append("")
    L.append("--- email source attribution ---")
    for k, v in sorted(src.items(), key=lambda kv: -kv[1]):
        L.append(f"  {k:<20} {v}")
    L.append("")
    L.append("--- Seamless EmailAI vs DeBounce (new stat) ---")
    L.append(f"  agree    : {agree}")
    L.append(f"  disagree : {disagree}")
    if matrix:
        L.append("  breakdown (seamless -> debounce):")
        for (s, d), v in sorted(matrix.items(), key=lambda kv: -kv[1]):
            L.append(f"     {s:<10} -> {d:<10} {v}")
    L.append("")
    L.append("--- invalid rate vs known baselines ---")
    rate = pct(invalid, with_any_email)
    L.append(f"  this pilot           : {rate:.2f}% of found emails invalid")
    L.append("  legacy Seamless stock: 11.41%  (pre-2026-02-01 cohort)")
    L.append("  post-Feb cohorts     : 2-4%")
    if rate >= 8.0:
        L.append("  ** FLAG: within range of the 11.41% legacy Seamless figure.")
        L.append("  ** Do not scale to 5k until this is understood.")
    elif with_any_email < 20:
        L.append("  (sample too small to compare meaningfully -- pilot only)")
    L.append("")
    L.append("--- mobile coverage (Seamless-supplied, no repair pass) ---")
    L.append(f"  DataType == mobile   : {mobile}/{n} ({pct(mobile, n):.1f}%)")
    L.append(f"  only a 'main' number : {main_only} ({pct(main_only, n):.1f}%)  <- switchboard, not used")
    L.append(f"  no number at all     : {no_number} ({pct(no_number, n):.1f}%)")
    L.append("")
    L.append("--- domain leakage (DOMAIN_MATCH_MODE = %s) ---" % DOMAIN_MATCH_MODE)
    total_seen = n + leak_count
    L.append("  returned a domain we never submitted : %s of %s (%.1f%%)"
             % (leak_count, total_seen, pct(leak_count, total_seen)))
    L.append("  disposition                          : %s"
             % ("dropped before research (strict)" if DOMAIN_MATCH_MODE == "strict"
                else "quarantined, never attached (loose)"))
    L.append("  credits saved by checking pre-research: %s" % leak_count)
    L.append("")
    L.append("--- waterfall end state (nothing discarded) ---")
    fs = {}
    for r in rows:
        k = r.get("final_status") or "(unset)"
        fs[k] = fs.get(k, 0) + 1
    for k, v in sorted(fs.items(), key=lambda kv: -kv[1]):
        L.append("  %-24s %s (%.1f%%)" % (k, v, pct(v, n)))
    nve = fs.get("no_valid_email_found", 0)
    L.append("")
    L.append("  no_valid_email_found is the RETAINED-but-unresolved bucket:")
    L.append("  %s contacts exhausted all four waterfall steps and kept their" % nve)
    L.append("  row (company, name, title, LinkedIn). None were discarded.")
    L.append("")
    L.append("--- provider attribution through the waterfall ---")
    for col, label in (("email_verify_provider", "final verifier"),
                       ("email_resolve_provider", "resolve step (LeadMagic)"),
                       ("email_replacement_source", "replacement step (Prospeo)")):
        cc = {}
        for r in rows:
            k = (r.get(col) or "").strip() or "(not reached)"
            cc[k] = cc.get(k, 0) + 1
        L.append("  %s:" % label)
        for k, v in sorted(cc.items(), key=lambda kv: -kv[1]):
            L.append("     %-38s %s" % (k, v))
    L.append("")
    L.append("--- dedupe ---")
    L.append(f"  net-new              : {netnew}/{n} ({pct(netnew, n):.1f}%)")
    L.append(f"  already in HubSpot   : {dupes}/{n} ({pct(dupes, n):.1f}%)")
    L.append("")
    L.append("--- phone accuracy / connection rate ---")
    L.append("  PLACEHOLDER -- requires live dials by SDRs. No data yet, and")
    L.append("  none is estimated here.")
    L.append("")
    L.append("--- credits ---")
    L.append(f"  before : {credits_before}")
    L.append(f"  after  : {credits_after}")
    L.append("  (compare to see which pool moved -- universal vs intent is unconfirmed)")
    L.append("")
    L.append("NOTHING WAS WRITTEN TO HUBSPOT.")

    text = "\n".join(L)
    print(text)
    with open(os.path.join(outdir, "AUDIT.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    json.dump({
        "companies": len(companies), "contacts": n,
        "any_email": with_any_email, "valid": valid, "risky": risky,
        "invalid": invalid, "unknown": unknown,
        "invalid_rate_of_found": round(rate, 2),
        "mobile": mobile, "main_only": main_only, "no_number": no_number,
        "net_new": netnew, "already_exists": dupes,
        "seamless_vs_debounce_agree": agree, "seamless_vs_debounce_disagree": disagree,
        "email_source": src,
        "credits_before": credits_before, "credits_after": credits_after,
    }, open(os.path.join(outdir, "audit_summary.json"), "w"), indent=1)


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    outdir = sys.argv[1]
    n_companies = int(arg("--companies", "8"))
    per_company = int(arg("--per-company", "5"))
    cohort_csv = arg("--cohort-csv")
    from_json = arg("--from-json")
    skip_research = "--skip-research" in sys.argv
    skip_prospeo = "--skip-prospeo" in sys.argv
    prospeo_first = "--prospeo-first" in sys.argv
    global DOMAIN_MATCH_MODE
    DOMAIN_MATCH_MODE = (arg("--domain-match", DOMAIN_MATCH_MODE) or "loose").strip().lower()
    if DOMAIN_MATCH_MODE not in ("strict", "loose"):
        print("--domain-match must be 'strict' or 'loose'"); sys.exit(2)
    print("DOMAIN_MATCH_MODE = %s" % DOMAIN_MATCH_MODE)

    if not TARGET_TITLES:
        print("=" * 72)
        print("BLOCKED: TARGET_TITLES is empty.")
        print("=" * 72)
        print("The title filter runs at search time (free) precisely so that no")
        print("credit is spent on contacts we would then discard. Running with an")
        print("invented list would poison every number in the step-7 audit.")
        print("")
        print("Fill in TARGET_TITLES at the top of this script, then re-run.")
        print("Nothing was called. No credits spent.")
        sys.exit(3)

    os.makedirs(outdir, exist_ok=True)

    # step 1
    if cohort_csv:
        companies = list(csv.DictReader(open(cohort_csv, encoding="utf-8")))[:n_companies]
        print(f"reusing {len(companies)} companies from {cohort_csv}")
    else:
        seed = int(arg("--seed", "20260918"))
        companies = pick_companies(n_companies, seed=seed,
                                   rev_floor=arg("--rev-floor"),
                                   rev_ceiling=arg("--rev-ceiling"))
        json.dump({"seed": seed, "n": len(companies),
                   "rev_floor": arg("--rev-floor"), "rev_ceiling": arg("--rev-ceiling")},
                  open(os.path.join(outdir, "sample_seed.json"), "w"), indent=1)
        print(f"picked {len(companies)} pilot companies")
    with open(os.path.join(outdir, "pilot_companies.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(companies[0].keys()))
        w.writeheader(); w.writerows(companies)

    credits_before, credits_after = {}, {}

    # ORDER. Default is Seamless first, Prospeo on what it missed. With
    # --prospeo-first the order inverts, because Prospeo's search is free and
    # exposes a VERIFIED flag before any credit is spent, so it is the cheaper
    # first pass. The trade is attribution: the two sources overlap ~38% on the
    # same companies (measured 2026-09-18), so whichever runs first is credited
    # with shared contacts and the second one's apparent reach is understated.
    prospeo_rows, p_stats, prospeo_covered = [], {}, set()
    if prospeo_first and not skip_prospeo:
        prospeo_rows, p_stats = prospeo_discovery(companies, per_company, outdir)
        prospeo_covered = {r["company_domain"] for r in prospeo_rows}
        print("  prospeo-first covered %d/%d companies; Seamless will run on the "
              "remaining %d" % (len(prospeo_covered), len(companies),
                                len(companies) - len(prospeo_covered)))

    seamless_companies = [c for c in companies
                          if c["company_domain"] not in prospeo_covered]
    rows = []

    if from_json:
        # Results pulled via the Seamless MCP connector, which a script cannot
        # call. Expect a list of raw poll entries, or {"results": [...]}.
        raw = json.load(open(from_json, encoding="utf-8"))
        entries = raw.get("results") if isinstance(raw, dict) else raw
        by_domain = {c["company_domain"]: c for c in seamless_companies}
        for e in entries or []:
            row = seamless_client.normalize_contact(e)
            c = by_domain.get(row.get("company_domain", "").lower(), {})
            row["company_id"] = c.get("company_id", "")
            row["company_name"] = row.get("company_name") or c.get("company_name", "")
            row["search_result_id"] = str(e.get("searchResultId") or "")
            rows.append(row)
        print(f"loaded {len(rows)} research results from {from_json}")
    else:
        if not config.SEAMLESS_API_KEY:
            print("BLOCKED: SEAMLESS_API_KEY is not set in .env, and --from-json")
            print("was not supplied. Either add the key, or pull results via the")
            print("Seamless MCP connector and pass them with --from-json.")
            sys.exit(4)

        # step 2 -- FREE. Batched: companyDomain takes up to 100 and jobTitle
        # up to 10, so all pilot domains go in ONE call per title batch --
        # 12 title batches total, not 12 calls per company.
        domains = [c["company_domain"] for c in seamless_companies]
        by_domain = {c["company_domain"]: c for c in seamless_companies}
        seen, raw_hits = set(), []
        batches = icp_titles.search_batches()
        print("searching %d domains x %d title batches (%d include terms)..."
              % (len(domains), len(batches), len(TARGET_TITLES)))
        for bi, titles in enumerate(batches, 1):
            for i in range(0, len(domains), icp_titles.MAX_DOMAINS_PER_CALL):
                dchunk = domains[i:i + icp_titles.MAX_DOMAINS_PER_CALL]
                res = seamless_client.search_contacts(
                    dchunk, titles, seniority=TARGET_SENIORITY,
                    department=TARGET_DEPARTMENT, limit=100)
                credits_before = credits_before or res.get("credits", {})
                for g in res.get("results") or []:
                    sid = g.get("searchResultId")
                    if sid and sid not in seen:
                        seen.add(sid)
                        raw_hits.append(g)
            print("  batch %d/%d  union so far: %d" % (bi, len(batches), len(raw_hits)))
        print()
        print("union of all title batches: %d contacts" % len(raw_hits))

        # employeeCount is absent from title-FILTERED responses (verified
        # 2026-09-18: an unfiltered search returns 30 for researchanddesign.com,
        # the filtered one returns blank). The conditional-include headcount
        # gate needs it, so fetch it with one extra FREE unfiltered search per
        # domain chunk, and fall back to HubSpot's numberofemployees.
        headcount = {}
        for c in seamless_companies:
            if str(c.get("hs_employees") or "").strip():
                headcount[c["company_domain"]] = c["hs_employees"]
        for i in range(0, len(domains), icp_titles.MAX_DOMAINS_PER_CALL):
            dchunk = domains[i:i + icp_titles.MAX_DOMAINS_PER_CALL]
            res = seamless_client.search_contacts_unfiltered(dchunk, limit=100)
            for g in res.get("results") or []:
                n = g.get("employeeCount")
                for d in ([(g.get("domain") or "").lower()] +
                          [(x or "").lower() for x in (g.get("domains") or [])]):
                    if d in by_domain and n:
                        headcount.setdefault(d, n)
        print("headcount resolved for %d/%d domains" % (len(headcount), len(domains)))

        def _hc(g):
            """Headcount for a search hit: its own field, else the company map."""
            if g.get("employeeCount"):
                return g.get("employeeCount")
            for d in ([(g.get("domain") or "").lower()] +
                      [(x or "").lower() for x in (g.get("domains") or [])]):
                if d in headcount:
                    return headcount[d]
            return None

        # Local ICP gate: the FULL include/exclude lists, applied to the title
        # search already returned for free. This is the step Seamless cannot do
        # for us -- jobTitle has no exclusion parameter.
        passed, rejected = [], []
        for g in raw_hits:
            # employeeCount arrives free from search and gates the conditional
            # includes (e.g. General Manager only under 50 employees).
            ok, why = icp_titles.title_passes(g.get("title") or "",
                                              employee_count=_hc(g))
            (passed if ok else rejected).append((g, why))
        print("local ICP title gate: %d pass | %d reject" % (len(passed), len(rejected)))
        with open(os.path.join(outdir, "title_gate_rejected.csv"), "w", newline="",
                  encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["domain", "name", "title", "employees", "seniority", "reason"])
            for g, why in rejected:
                w.writerow([g.get("domain", ""), g.get("name", ""), g.get("title", ""),
                            _hc(g) or "", g.get("seniority", ""), why])

        # Dedupe BEFORE paying. Search hands back liUrl for free, so anyone
        # already in HubSpot is dropped instead of researched at 1 credit.
        pre = [{"linkedin_url": g.get("liUrl") or "", "email": "",
                "_g": g, "_why": why} for g, why in passed]
        pre = dedupe_check(pre)
        fresh = [r for r in pre if r["dedupe_state"] == "net-new"]
        known = [r for r in pre if r["dedupe_state"] != "net-new"]
        print("pre-research dedupe: %d net-new | %d already in HubSpot "
              "(%d credits saved)" % (len(fresh), len(known), len(known)))
        with open(os.path.join(outdir, "already_in_hubspot.csv"), "w", newline="",
                  encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["domain", "name", "title", "linkedin_url", "hubspot_contact_id"])
            for r in known:
                g = r["_g"]
                w.writerow([g.get("domain", ""), g.get("name", ""), g.get("title", ""),
                            g.get("liUrl", ""), r["hubspot_contact_id"]])

        # Collapse duplicates WITHIN the result set before paying. Seamless can
        # return one human as several searchResultIds under different titles
        # (verified 2026-09-18: Dennis Salemi as "Managing Partner" AND
        # "Entrepreneur"; Annabelle as two surnames), which bills a credit each
        # time. liUrl is the strongest free key; fall back to name+domain.
        def _person_key(g):
            li = (g.get("liUrl") or "").strip().lower().rstrip("/")
            if li:
                return ("li", li)
            nm = " ".join((g.get("name") or "").lower().split())
            return ("nd", nm, (g.get("domain") or "").lower())
        seen_person, collapsed = {}, 0
        deduped = []
        for r in fresh:
            k = _person_key(r["_g"])
            if k in seen_person:
                collapsed += 1
                continue
            seen_person[k] = True
            deduped.append(r)
        if collapsed:
            print("intra-batch dedupe: collapsed %d duplicate person record(s) "
                  "(%d credits saved)" % (collapsed, collapsed))
        fresh = deduped

        # Cap per company AFTER filtering, so the cap is spent on qualified people.
        globals()["_per_co"] = per_co = {}
        search_hits, no_match = [], []
        unmapped = []
        for r in fresh:
            g = r["_g"]
            # A Seamless company can carry several domains ("domains": [...]),
            # and the primary "domain" is not always the one we searched --
            # belle-anna.com also answers as bellethelabel.com. Matching on
            # "domain" alone silently drops those contacts, so check every
            # domain the record claims before giving up.
            cand = [(g.get("domain") or "").lower()]
            cand += [(d or "").lower() for d in (g.get("domains") or [])]
            dom = next((d for d in cand if d in by_domain), "")
            if not dom:
                unmapped.append(g)
                continue
            if per_co.get(dom, 0) >= per_company:
                continue
            per_co[dom] = per_co.get(dom, 0) + 1
            search_hits.append((c_map := by_domain[dom], g))
        # DOMAIN_MATCH_MODE branch. Pre-research so a leaked contact costs
        # no credit (42% leakage measured on the 100-company run).
        if unmapped:
            qpath = os.path.join(outdir, "domain_mismatch_quarantine.csv")
            with open(qpath, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["submitted_domains", "returned_domain", "name", "title",
                            "linkedin_url", "mode", "disposition"])
                for g in unmapped:
                    w.writerow(["|".join(domains[:5]) + ("|..." if len(domains) > 5 else ""),
                                g.get("domain", ""), g.get("name", ""), g.get("title", ""),
                                g.get("liUrl", ""), DOMAIN_MATCH_MODE,
                                "dropped" if DOMAIN_MATCH_MODE == "strict" else "quarantined"])
            print("  domain leakage: %d contacts returned a domain we never submitted "
                  "-> %s (%s)" % (len(unmapped),
                                  "DROPPED" if DOMAIN_MATCH_MODE == "strict" else "QUARANTINED",
                                  os.path.basename(qpath)))
            print("     never attached to any target company in either mode.")
            for g in unmapped[:5]:
                print("       %-26s %-22s %s" % (g.get("name", "")[:26],
                      (g.get("title") or "")[:22], g.get("domains") or g.get("domain")))
            if len(unmapped) > 5:
                print("       ... and %d more (see the quarantine csv)" % (len(unmapped) - 5))
        leak_count = len(unmapped)

        json.dump([c["company_domain"] for c in no_match],
                  open(os.path.join(outdir, "no_qualified_contact.json"), "w"), indent=1)
        globals()["_no_match_l1"] = no_match
        print()
        print("to research: %d | companies with none: %d"
              % (len(search_hits), len(no_match)))

        if skip_research:
            print()
            print("--skip-research: stopping before any paid call. 0 credits spent.")
            return

        # step 3 -- PAID
        ids = [g.get("searchResultId") for _, g in search_hits if g.get("searchResultId")]
        print(f"\nsubmitting {len(ids)} contacts for research "
              f"({len(ids)} credits at 1/contact)...")
        try:
            sub = seamless_client.research_contacts(ids)
        except seamless_client.SeamlessCreditError as e:
            print("SEAMLESS CREDIT/LICENSE ERROR:", e)
            print("  productCategory:", e.product_category)
            print("  additionalCreditsNeeded:", e.additional_credits_needed)
            sys.exit(5)
        results = seamless_client.wait_for_research(sub.get("request_ids") or [])
        by_sid = {g.get("searchResultId"): c for c, g in search_hits}
        for rid, entry in results.items():
            row = seamless_client.normalize_contact(entry)
            sid = str(entry.get("searchResultId") or "")
            c = by_sid.get(sid) or {}
            row["company_id"] = c.get("company_id", "")
            row["company_name"] = row.get("company_name") or c.get("company_name", "")
            row["company_domain"] = row.get("company_domain") or c.get("company_domain", "")
            row["search_result_id"] = sid
            rows.append(row)
        credits_after = seamless_client.poll_research(
            list(results.keys())[:1]).get("credits", {})

    # ---- LAYER 2: Prospeo discovery on companies Seamless found nobody at.
    # Seamless returned zero contacts for 59 of 100 companies on the previous
    # run. Prospeo /search-person is a second, independent people database and
    # its search is FREE, so probing the gap costs nothing but wall time. It
    # also returns a VERIFICATION STATUS on the masked email, which means we
    # can require status == VERIFIED before spending a credit -- the opposite
    # of Seamless, where the credit is spent before quality is known.
    if not skip_prospeo and not prospeo_first:
        zero = [c for c in companies if per_co.get(c["company_domain"], 0) == 0] \
               if "per_co" in dir() else []
        zero = zero or [c for c in companies
                        if c["company_domain"] not in {r.get("company_domain") for r in rows}]
        prospeo_rows, p_stats = prospeo_discovery(zero, per_company, outdir)
    rows.extend(prospeo_rows)

    if not rows:
        print("no research results to process. stopping.")
        return

    # steps 4-6
    print(f"\nverifying {len(rows)} emails (DeBounce -> LeadMagic -> Prospeo repair)...")
    rows = verify_and_repair(rows)
    print("dedupe check against HubSpot...")
    rows = dedupe_check(rows)

    with open(os.path.join(outdir, "pilot_contacts.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ROW_FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)

    # step 7
    print("")
    audit(rows, companies, per_company, outdir, credits_before, credits_after,
          leak_count=locals().get("leak_count", 0) or 0)
    print(f"\nartifacts in {outdir}/")


if __name__ == "__main__":
    main()
