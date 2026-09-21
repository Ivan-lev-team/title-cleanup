#!/usr/bin/env python3
"""
Max-contacts pipeline. Two goals at once: find as many contacts as possible,
and test Seamless's email AND mobile data on every single one.

The architecture that makes both work:

  DISCOVER with Prospeo   /search-person is FREE and exposes a per-contact
                          email verification status while the address is still
                          masked, so quality is knowable before any spend.
  RESOLVE with Seamless   research-by-identity, 1 credit per contact, returns
                          email AND mobile. Seamless therefore supplies and is
                          graded on 100% of contacts, at credits == contacts.

Why Seamless does NOT do discovery here: its /search/contacts is billed ~1
credit per 10 results plus a per-call floor (830 of 971 credits in a measured
200-company run), which works out to ~15 credits per verified contact versus
Prospeo's ~1.9.

Layers, in order:
  1  Prospeo search             free   discover people at the domain
  2  local ICP gate             free   icp_titles include/exclude + headcount
  3  HubSpot dedupe             free   skip anyone we already own
  4  Seamless resolve           1 cr   email + MOBILE for every contact
  5  Prospeo reveal (fallback)  1 cr   only where Seamless returned no email
  6  LeadMagic role-finder      free on miss, for companies with nobody
  7  DeBounce -> LeadMagic      verification; nothing is ever discarded

Company pool is selected on HEADCOUNT, not revenue: companies that produced
contacts had a median 8.5 employees, those that produced nothing a median of 2.

  python scripts/contacts_max.py <outdir> [--companies 200] [--per-company 5]
                                [--min-employees 5] [--seed N] [--skip-leadmagic]

Writes nothing to HubSpot.
"""
import csv, json, os, sys, time, collections
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)
sys.path.insert(0, os.path.join(HERE, "scripts"))

import requests
from dotenv import dotenv_values

import config, icp_titles, enrichment
import clay_layer
import discover_all
import contact_cache
import seamless_client as sc
import prospeo_search as ps
import prospeo_client as pc
import leadmagic_client as lm
import debounce_client as db
from fix_list_domains import NON_BRAND_HOSTS

HS = {"Authorization": "Bearer " + dotenv_values(".env")["HUBSPOT_TOKEN"],
      "Content-Type": "application/json"}
HSBASE = "https://api.hubapi.com"
PROSPEO_TITLE_CAP = 50      # 50 accepted, 113 -> INVALID_FILTERS
ROLES = ["owner", "founder", "ceo", "president", "marketing director",
         "ecommerce manager"]
# 6 workers matches the repo convention (scripts/enrich_email_batch1.py). Every
# provider client already retries 429 with Retry-After, so a pool degrades to
# serial under rate limiting instead of failing. Seamless resolve is NOT pooled
# -- it is batched 100/call and its limit is 60/min per endpoint ORG-WIDE, so
# parallelising it would starve other users of the org.
WORKERS = int(os.environ.get("CONTACTS_MAX_WORKERS", "6"))

# Cross-run memory. Checked BEFORE every paid call so the same person is never
# charged for twice -- across resumes, overlapping batches, or re-runs.
CACHE = contact_cache.ContactCache()

FIELDS = ["company_id", "company_name", "company_domain", "employees",
          "first_name", "last_name", "job_title", "linkedin_url",
          "discovery_layer", "prospeo_email_status",
          "email", "email_source",
          "seamless_email", "seamless_email_validity",
          "mobile", "mobile_source", "mobile_datatype", "mobile_confidence",
          "seamless_mobile_status", "seamless_status",
          "verify_status", "verify_provider", "resolve_status",
          "final_status", "pushable", "dedupe_state", "hubspot_contact_id"]


def arg(flag, d=None):
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return d


def hs_post(path, body, tries=6):
    resp = None
    for a in range(tries):
        try:
            resp = requests.post(HSBASE + path, headers=HS, json=body, timeout=60)
        except requests.RequestException:
            time.sleep(2 ** a); continue
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", "2")) + 1); continue
        if resp.status_code >= 500 or resp.status_code == 400:
            time.sleep(2 ** a); continue
        return resp
    return resp


# --- 0. company pool, selected on headcount --------------------------------

def pick_companies(n, min_emp, seed):
    """Pod RevOps, zero contacts, has a domain, >= min_emp employees."""
    import random
    out, last, seen = [], "0", set()
    while len(out) < max(n * 8, 200):
        body = {"filterGroups": [{"filters": [
                    {"propertyName": "pod", "operator": "EQ", "value": "Pod RevOps"},
                    {"propertyName": "num_associated_contacts", "operator": "EQ", "value": "0"},
                    {"propertyName": "domain", "operator": "HAS_PROPERTY"},
                    {"propertyName": "numberofemployees", "operator": "GTE",
                     "value": str(min_emp)},
                    {"propertyName": "hs_object_id", "operator": "GT", "value": last}]}],
                "properties": ["name", "domain", "numberofemployees", "annualrevenue"],
                "sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
                "limit": 100}
        r = hs_post("/crm/v3/objects/companies/search", body)
        if r is None or r.status_code >= 300:
            print("hubspot search failed:", getattr(r, "status_code", "?")); sys.exit(1)
        res = r.json().get("results", [])
        if not res:
            break
        for x in res:
            p = x["properties"]
            d = enrichment.clean_domain(p.get("domain") or "")
            if not d or "." not in d or d in seen:
                continue
            if any(d == h or d.endswith("." + h) for h in NON_BRAND_HOSTS):
                continue
            seen.add(d)
            out.append({"company_id": x["id"],
                        "company_name": (p.get("name") or "").strip(),
                        "company_domain": d,
                        "employees": p.get("numberofemployees") or "",
                        "revenue": p.get("annualrevenue") or ""})
        last = res[-1]["id"]
    picked = random.Random(seed).sample(out, min(n, len(out)))
    print("pool: %s candidates with >=%s employees -> sampled %s (seed %s)"
          % (format(len(out), ","), min_emp, len(picked), seed))
    return picked


# --- 3. dedupe against HubSpot ---------------------------------------------

def dedupe(rows):
    """LinkedIn handle in BOTH canonical forms plus email. HubSpot stores the
    two differently (hs_linkedin_url without www, linkedin_personal_url with),
    and missing that is how duplicates get created."""
    import re

    def handle(u):
        m = re.search(r"linkedin\.com/in/([a-z0-9\-_%\.]+)", (u or "").lower())
        return m.group(1).strip("/") if m else ""

    by_h, by_e = {}, {}
    hs = sorted({handle(r.get("linkedin_url", "")) for r in rows} - {""})
    for i in range(0, len(hs), 100):
        b = hs[i:i + 100]
        r = hs_post("/crm/v3/objects/contacts/search", {"filterGroups": [
            {"filters": [{"propertyName": "hs_linkedin_url", "operator": "IN",
                          "values": ["https://linkedin.com/in/%s" % h for h in b]}]},
            {"filters": [{"propertyName": "linkedin_personal_url", "operator": "IN",
                          "values": ["https://www.linkedin.com/in/%s" % h for h in b]}]}],
            "properties": ["hs_linkedin_url", "linkedin_personal_url"], "limit": 100})
        if r is None or r.status_code >= 300:
            continue
        for x in r.json().get("results", []):
            for k in ("hs_linkedin_url", "linkedin_personal_url"):
                h = handle(x["properties"].get(k) or "")
                if h:
                    by_h[h] = x["id"]
    for r_ in rows:
        hid = by_h.get(handle(r_.get("linkedin_url", "")))
        r_["hubspot_contact_id"] = hid or ""
        r_["dedupe_state"] = "already-exists" if hid else "net-new"
    return rows


# --- 1+2. Prospeo discovery + local gate -----------------------------------

def discover_prospeo(companies, per_company):
    titles = list(dict.fromkeys(list(icp_titles.INCLUDE) +
                                list(icp_titles.CONDITIONAL_INCLUDE)))
    # One UNFILTERED call per company. Prospeo caps job_title at 50, so the
    # 113-term list used to need 3 calls each; dropping the API-side title
    # filter makes it 1 and loses nothing, because icp_titles.title_passes
    # already applies the full include/exclude list to the returned titles.
    print("\n[1] Prospeo discovery (FREE) -- %d companies, 1 unfiltered call each"
          % len(companies))
    print("    paced to Prospeo's published limits (5/sec, 180/min)")
    rows, rejected, seen_person = [], [], set()
    stats = collections.Counter()

    def probe(c):
        r = ps.search_person(c["company_domain"], job_titles=None, seniority=None)
        # An API failure (429 exhausted, connection, INVALID_FILTERS) returned
        # [] and was then attributed to the company as "nobody works here",
        # sending it to the PAID LeadMagic rescue. Surface it instead.
        return c, (r["people"] if not r["error"] else []), r.get("error", "")

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(probe, c) for c in companies]
        for fu in as_completed(futs):
            try:
                c, people, perr = fu.result()
                if perr:
                    stats["prospeo_error"] += 1
            except Exception as ex:
                stats["probe_exception"] += 1
                print("    probe failed: %s: %s" % (type(ex).__name__, str(ex)[:80]))
                continue
            done += 1
            dom = c["company_domain"]
            stats["people_seen"] += len(people)
            if people:
                stats["companies_with_people"] += 1
            kept = 0
            for p in people:
                if kept >= per_company:
                    break
                ok, why = icp_titles.title_passes(
                    p["job_title"],
                    employee_count=p.get("employee_count") or c.get("employees"))
                if not ok:
                    rejected.append((dom, p, why)); continue
                stats["passed_gate"] += 1
                key = (p["linkedin_url"] or "").lower().rstrip("/") or                       (p["first_name"] + p["last_name"] + dom).lower()
                if key in seen_person:
                    stats["intra_dupe"] += 1; continue
                seen_person.add(key)
                if p["email_status"] != "VERIFIED":
                    rejected.append((dom, p, "prospeo email_status=%s"
                                     % (p["email_status"] or "none")))
                    stats["no_verified_email"] += 1
                    continue
                stats["pursued"] += 1
                kept += 1
                rows.append({"company_id": c["company_id"],
                             "company_name": c["company_name"] or p["company_name"],
                             "company_domain": dom, "employees": c.get("employees", ""),
                             "first_name": p["first_name"], "last_name": p["last_name"],
                             "job_title": p["job_title"], "linkedin_url": p["linkedin_url"],
                             "discovery_layer": "Prospeo",
                             "prospeo_email_status": p["email_status"],
                             "_person_id": p["person_id"]})
            if done % 50 == 0:
                print("    %d/%d companies | %d pursued" % (done, len(companies), len(rows)))
    print("    people seen %d | passed ICP gate %d | had VERIFIED email %d | "
          "intra-dupes collapsed %d" % (stats["people_seen"], stats["passed_gate"],
                                        stats["pursued"], stats["intra_dupe"]))
    print("    companies where Prospeo found anyone: %d/%d"
          % (stats["companies_with_people"], len(companies)))
    return rows, rejected, stats


# --- 6. LeadMagic rescue for companies with nobody -------------------------

def discover_leadmagic(dead, per_company):
    print("\n[6] LeadMagic rescue on %d companies with nobody" % len(dead))
    def rescue(c):
        d = ps.root_domain(c["company_domain"])
        for role in ROLES:
            r = lm._post("/role-finder", {"company_domain": d, "job_title": role})
            if r is None or r.status_code >= 300:
                continue
            try:
                j = r.json()
            except ValueError:
                continue
            if (j.get("message") or "") == "Role Found" and j.get("first_name"):
                return {"company_id": c["company_id"], "company_name": c["company_name"],
                        "company_domain": c["company_domain"],
                        "employees": c.get("employees", ""),
                        "discovery_layer": "LeadMagic", "prospeo_email_status": "",
                        "_person_id": "",
                        "first_name": j.get("first_name"),
                        "last_name": j.get("last_name") or "",
                        "job_title": role,
                        "linkedin_url": j.get("profile_url") or ""}
        return None

    out, done = [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fu in as_completed([ex.submit(rescue, c) for c in dead]):
            try:
                g = fu.result()
            except Exception as e:
                print("    rescue failed: %s" % str(e)[:80]); g = None
            done += 1
            if g:
                out.append(g)
            if done % 25 == 0:
                print("    %d/%d probed | %d names" % (done, len(dead), len(out)))
    print("    LeadMagic found a name at %d/%d companies" % (len(out), len(dead)))
    return out


# --- 4. Seamless resolve: email + MOBILE for EVERY contact -----------------

def resolve_seamless(rows):
    """1 credit per contact. This is where Seamless's data gets tested.

    Two behaviours the API documentation does not mention, both found 2026-09-21
    and both silently produced zero results before they were handled:

      status "error"     -> "Contact not found by LinkedIn profile URL". Seamless
                            simply has no record keyed on that URL. A second pass
                            with contactName + domain often finds the same person,
                            so every LinkedIn miss is retried by name.
      status "duplicate" -> "Research is in progress or completed for the initial
                            request." No contact payload is returned at all. The
                            original requestId is in additionalData, so it is
                            followed rather than treated as a failure.

    Submits everything first, then polls all ids together -- the earlier version
    blocked up to 420s per 100-contact batch, which is why a 10-company run took
    8m22s instead of ~1m.
    """
    print("\n[4] Seamless resolve-by-identity on ALL %d contacts (1 credit each)"
          % len(rows))

    # CACHE GATE. A repeat Seamless request answers "duplicate" with NO payload,
    # so the credit is spent and nothing comes back. Check cross-run memory
    # before submitting anything.
    fresh = []
    for r in rows:
        c = CACHE.get(r, "seamless")
        if c:
            CACHE.note_hit("seamless")
            r["seamless_email"] = c.get("email", "")
            r["seamless_email_validity"] = c.get("validity", "")
            r["seamless_mobile_status"] = c.get("mobile_datatype", "") or "none"
            r["seamless_status"] = "cached"
            if c.get("email"):
                r["email"] = c["email"]
                r["email_source"] = "Seamless (cached)"
            if c.get("mobile"):
                r["mobile"] = c["mobile"]
                r["mobile_source"] = "Seamless"
                r["mobile_datatype"] = c.get("mobile_datatype", "")
        else:
            fresh.append(r)
    rows_all = rows
    if len(fresh) != len(rows):
        print("    cache: %d of %d already resolved previously (0 credits)"
              % (len(rows) - len(fresh), len(rows)))
    rows = fresh
    if not rows:
        print("    nothing new to resolve -- every contact was cached")
        return rows_all

    def key_of(r):
        li = (r.get("linkedin_url") or "").strip().lower().rstrip("/")
        return li or (r.get("first_name", "") + r.get("last_name", "") +
                      r.get("company_domain", "")).lower()

    def apply(t, row):
        t["seamless_email"] = row.get("email", "")
        t["seamless_email_validity"] = row.get("email_seamless_validity", "")
        t["seamless_mobile_status"] = row.get("mobile_datatype", "") or "none"
        CACHE.put(t, "seamless", {"email": row.get("email", ""),
                                  "validity": row.get("email_seamless_validity", ""),
                                  "mobile": row.get("mobile", ""),
                                  "mobile_datatype": row.get("mobile_datatype", "")})
        hit = False
        if row.get("email"):
            t["email"] = row["email"]
            t["email_source"] = "Seamless"
            hit = True
        if row.get("mobile"):
            t["mobile"] = row["mobile"]
            t["mobile_source"] = "Seamless"
            t["mobile_datatype"] = row.get("mobile_datatype", "")
            t["mobile_confidence"] = row.get("mobile_confidence", "")
        return hit

    def submit_and_poll(pairs, label):
        """pairs = [(row, identity_dict)]. Returns (emails_found, statuses)."""
        if not pairs:
            return 0, collections.Counter()
        rid_to_row, statuses, got = {}, collections.Counter(), 0
        for i in range(0, len(pairs), 100):
            chunk = pairs[i:i + 100]
            sub = sc.research_by_identity([p[1] for p in chunk])
            ids = [str(x) for x in (sub.get("request_ids") or [])]
            # zip() would silently shift every pairing by one if Seamless drops
            # or reorders an entry, writing person B's email onto person A.
            if len(ids) != len(chunk):
                statuses["id_count_mismatch"] += abs(len(chunk) - len(ids))
                print("    WARNING: submitted %d, got %d request ids -- "
                      "skipping this chunk rather than risk misaligning rows"
                      % (len(chunk), len(ids)))
                continue
            for (r, _ident), rid in zip(chunk, ids):
                rid_to_row[rid] = r
        if not rid_to_row:
            return 0, statuses
        # Deadline scales with volume. A fixed 420s was fine for 20 contacts
        # and catastrophic for 20,000: one sweep of 340 chunks needs ~25 min,
        # so the budget expired with most ids never polled ONCE -- yet a credit
        # had already been charged for every one of them at submit time.
        pending, t0 = set(rid_to_row), time.time()
        budget = max(420, 8 * (len(pending) // 100 + 1) * 60)
        order = list(pending)
        cursor = 0
        while pending and time.time() - t0 < budget:
            # ROTATE the window. list(pending)[:100] re-sliced the same leading
            # 100 ids every iteration, so the tail was never polled at all.
            order = [r for r in order if r in pending] or list(pending)
            if cursor >= len(order):
                cursor = 0
            window = order[cursor:cursor + 100]
            cursor += 100
            for e in (sc.poll_research(window).get("results") or []):
                rid = str(e.get("requestId") or e.get("id") or "")
                st = str(e.get("status") or "")
                if st not in sc.TERMINAL:
                    continue
                pending.discard(rid)
                statuses[st] += 1
                t = rid_to_row.get(rid)
                if t is None:
                    continue
                t["seamless_status"] = st
                if st == "duplicate":
                    # the payload lives on the ORIGINAL request
                    orig = ((e.get("additionalData") or {}) or {}).get("requestId")
                    if orig:
                        for e2 in (sc.poll_research([str(orig)]).get("results") or []):
                            if str(e2.get("status")) == "done":
                                if apply(t, sc.normalize_contact(e2)):
                                    got += 1
                    continue
                if apply(t, sc.normalize_contact(e)):
                    got += 1
            if pending and cursor >= len(order):
                time.sleep(4)
        if pending:
            statuses["timeout"] += len(pending)
            for rid in pending:
                t = rid_to_row.get(rid)
                if t is not None:
                    t["seamless_status"] = "timeout"
        print("    %-22s %d submitted | %d emails | statuses %s"
              % (label, len(rid_to_row), got, dict(statuses)))
        return got, statuses

    # pass 1 -- strongest identity first
    by_li = [(r, {"liProfileUrl": r["linkedin_url"].strip()})
             for r in rows if (r.get("linkedin_url") or "").strip()]
    by_nm = [(r, {"contactName": (r.get("first_name", "") + " " +
                                  r.get("last_name", "")).strip(),
                  "domain": r.get("company_domain", "")})
             for r in rows if not (r.get("linkedin_url") or "").strip()
             and (r.get("first_name") or r.get("last_name"))]
    g1, s1 = submit_and_poll(by_li, "pass1 linkedin")
    g2, s2 = submit_and_poll(by_nm, "pass1 name+domain")

    # pass 2 -- retry the LinkedIn misses by name+domain
    retry = [(r, {"contactName": (r.get("first_name", "") + " " +
                                  r.get("last_name", "")).strip(),
                  "domain": r.get("company_domain", "")})
             for r, _ in by_li
             # gate on an EXPLICIT terminal miss. Gating on "no email" would
             # also resubmit every timeout, double-charging a credit for a
             # contact whose first request may still be completing.
             if r.get("seamless_status") in ("error", "missing", "not found")
             and not (r.get("seamless_email") or "").strip()
             and (r.get("first_name") or r.get("last_name"))
             and r.get("company_domain")]
    g3, s3 = submit_and_poll(retry, "pass2 name retry")

    total = g1 + g2 + g3
    allst = s1 + s2 + s3
    print("    Seamless returned an email for %d/%d contacts" % (total, len(rows)))
    print("    status mix: %s" % dict(allst))
    CACHE.save()
    return rows_all


# --- 5+7. fallback email + verification ------------------------------------

def finish(rows):
    """Fallback email for whatever Seamless could not resolve, then verify.

    Nothing is discarded: a contact that exhausts every provider still gets
    final_status = no_valid_email_found and keeps its row.
    """
    print("\n[5+7] fallback email, then DeBounce -> LeadMagic verification")

    def one(r):
        if not (r.get("email") or "").strip() and r.get("_person_id"):
            pc_cached = CACHE.get(r, "prospeo")
            if pc_cached is not None:
                CACHE.note_hit("prospeo")
                rev = pc_cached
            else:
                rev = ps.reveal_person(r["_person_id"], enrich_mobile=False)
                CACHE.put(r, "prospeo", {"email": rev.get("email", "")})
            if rev.get("email"):
                r["email"] = rev["email"]
                r["email_source"] = "Prospeo (reveal)"
        if not (r.get("email") or "").strip() and (r.get("first_name") or r.get("last_name")):
            f = lm.find_email(r.get("first_name", ""), r.get("last_name", ""),
                              r.get("company_name", ""), r.get("company_domain", "")) or {}
            if f.get("email"):
                r["email"] = f["email"]
                r["email_source"] = "LeadMagic"
            else:
                pr = pc.enrich_person((r.get("first_name", "") + " " +
                                       r.get("last_name", "")).strip(),
                                      r.get("company_name", ""),
                                      r.get("company_domain", ""),
                                      linkedin_url=r.get("linkedin_url", "")) or {}
                if pr.get("email"):
                    r["email"] = pr["email"]
                    r["email_source"] = "Prospeo (enrich)"

        email = (r.get("email") or "").strip()
        if not email:
            r["verify_status"] = ""
            r["verify_provider"] = ""
            r["final_status"] = "no_valid_email_found"
            r["pushable"] = "no"
            return
        v_cached = CACHE.get({"email": email}, "verify")
        if v_cached is not None:
            CACHE.note_hit("verify")
            r["verify_status"] = v_cached.get("status", "")
            r["verify_provider"] = v_cached.get("provider", "cached")
            r["pushable"] = "yes" if r["verify_status"] == "valid" else "no"
            r["final_status"] = ("pushable" if r["pushable"] == "yes"
                                 else "no_valid_email_found")
            return
        d = db.verify_email(email)
        r["verify_status"] = d.get("status") or ""
        r["verify_provider"] = "DeBounce"
        # A DeBounce error returns status "" with a populated error. "" is not
        # in the retry tuple below, so without this the contact would skip the
        # LeadMagic pass and be written off as no_valid_email_found -- a
        # 30-minute outage would silently discard every contact in that window.
        if d.get("error"):
            r["verify_provider"] = "DeBounce(error)"
            r["resolve_status"] = "debounce_error: %s" % str(d["error"])[:60]
        if r["verify_status"] in ("risky", "invalid", "unknown", ""):
            L = lm.validate_email(email)
            r["resolve_status"] = L.get("status") or ""
            if L.get("status") in ("valid", "invalid"):
                r["verify_status"] = L["status"]
                r["verify_provider"] = "DeBounce+LeadMagic"
        r["pushable"] = "yes" if r["verify_status"] == "valid" else "no"
        r["final_status"] = "pushable" if r["pushable"] == "yes" else "no_valid_email_found"
        CACHE.put({"email": email}, "verify", {"status": r["verify_status"],
                                               "provider": r["verify_provider"]})

    done = failed = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fu in as_completed([ex.submit(one, r) for r in rows]):
            try:
                fu.result()
            except Exception as e:
                failed += 1
                print("    verify failed: %s: %s" % (type(e).__name__, str(e)[:70]))
            done += 1
            if done % 25 == 0:
                print("    verified %d/%d" % (done, len(rows)))
    if failed:
        print("    WARNING: %d contact(s) raised during verification" % failed)
    CACHE.save()
    return rows


def _ckpt_path(outdir, stage):
    return os.path.join(outdir, "_ckpt_%s.json" % stage)


def checkpoint(outdir, stage, data):
    """Persist a completed stage so a crash costs wall time, not credits."""
    try:
        with open(_ckpt_path(outdir, stage), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        print("    (checkpoint %s failed: %s)" % (stage, e))


def restore(outdir, stage):
    """Return a completed stage's data, or None. Only consulted with --resume."""
    pth = _ckpt_path(outdir, stage)
    if not os.path.exists(pth):
        return None
    try:
        with open(pth, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main():
    outdir = sys.argv[1]
    n = int(arg("--companies", "200"))
    per = int(arg("--per-company", "5"))
    min_emp = int(arg("--min-employees", "5"))
    seed = int(arg("--seed", "200921"))
    os.makedirs(outdir, exist_ok=True)

    p0 = ps.credits_remaining()
    clay_json = arg("--clay-json")
    resume = "--resume" in sys.argv
    comps = restore(outdir, "companies") if resume else None
    if comps:
        print("[resume] reusing %d companies from checkpoint" % len(comps))
    else:
        comps = pick_companies(n, min_emp, seed)
        checkpoint(outdir, "companies", comps)
    if not comps:
        print("No companies matched the pool filters (pod/contacts/domain/employees). "
              "Nothing to do.")
        return
    with open(os.path.join(outdir, "companies.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(comps[0].keys())); w.writeheader(); w.writerows(comps)

    TITLES = list(dict.fromkeys(list(icp_titles.INCLUDE) +
                                list(icp_titles.CONDITIONAL_INCLUDE)))
    srcs = tuple(x.strip() for x in
                 (arg("--sources", "clay,prospeo,leadmagic")).split(",") if x.strip())
    quarantined, rejected = [], []

    cached = restore(outdir, "discovered") if resume else None
    if cached:
        rows = cached["rows"]
        pstats = collections.Counter(cached["stats"])
        print("[resume] reusing %d discovered contacts from checkpoint" % len(rows))
    else:
        # EVERY source on EVERY company, then union. The old fallback chain ran
        # LeadMagic only where Prospeo had failed, so a source could never
        # contribute at a company another had already "covered" -- which caps
        # the contact count. Maximum contacts means everyone runs everywhere.
        raw, quarantined, pstats = discover_all.run(comps, TITLES, enabled=srcs)
        rows = []
        for r in raw:
            ok, why = icp_titles.title_passes(r.get("job_title", ""),
                                              employee_count=r.get("employees"))
            if ok:
                rows.append(r)
            else:
                rejected.append((r.get("company_domain", ""), r, why))
        print("    passed ICP title gate     : %d of %d" % (len(rows), len(raw)))
        # EMPTY-TITLE RESCUE, last resort only.
        # 249 of 1,025 discovered people (24%) had no title at all, and the gate
        # fails those closed. Rather than lose them, resolve the title through
        # Prospeo -- but ONLY at companies where nothing else was found, so we
        # never spend on a company already covered.
        covered = {r["company_domain"] for r in rows}
        blanks = [(d, r) for d, r, why in rejected
                  if why == "empty title" and d not in covered
                  and (r.get("linkedin_url") or "").strip()]
        if blanks:
            print("    empty-title rescue: %d people at %d companies with nothing else"
                  % (len(blanks), len({d for d, _ in blanks})))
            rescued = 0

            def _resolve_title(pair):
                d, r = pair
                got = pc.resolve_person(linkedin_url=r.get("linkedin_url", "")) or {}
                return d, r, (got.get("title") or "").strip()

            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                for fu in as_completed([ex.submit(_resolve_title, b) for b in blanks]):
                    try:
                        d, r, title = fu.result()
                    except Exception:
                        continue
                    if not title:
                        continue
                    ok2, _why2 = icp_titles.title_passes(
                        title, employee_count=r.get("employees"))
                    if ok2:
                        r["job_title"] = title
                        r["discovery_layer"] = (r.get("discovery_layer", "")
                                                + ",TitleRescue").strip(",")
                        rows.append(r)
                        rescued += 1
            print("    empty-title rescue: recovered %d contacts" % rescued)
            pstats["title_rescued"] = rescued


        pstats["passed_gate"] = len(rows)
        pstats["gate_rejected"] = len(raw) - len(rows)
        checkpoint(outdir, "discovered", {"rows": rows, "stats": dict(pstats)})

    if quarantined:
        with open(os.path.join(outdir, "stale_domain_quarantine.csv"), "w",
                  newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["searched_domain", "actual_domain", "name", "title",
                        "linkedin", "source"])
            for q in quarantined:
                w.writerow([q.get("company_domain", ""), q.get("clay_actual_domain", ""),
                            (q.get("first_name", "") + " " + q.get("last_name", "")).strip(),
                            q.get("job_title", ""), q.get("linkedin_url", ""),
                            q.get("source", "")])

    print("\n[3] HubSpot dedupe on %d contacts" % len(rows))
    rows = dedupe(rows)
    already = [r for r in rows if r["dedupe_state"] != "net-new"]
    rows = [r for r in rows if r["dedupe_state"] == "net-new"]
    print("    net-new %d | already in HubSpot %d (skipped, credits saved)"
          % (len(rows), len(already)))

    if rows:
        resolved = restore(outdir, "resolved") if resume else None
        if resolved:
            rows = resolved
            print("[resume] reusing %d Seamless-resolved contacts (credits already spent)"
                  % len(rows))
        else:
            try:
                rows = resolve_seamless(rows)
            except sc.SeamlessCreditError as e:
                # The EXPECTED end state of a large run. Without this the
                # traceback exits the interpreter before any CSV is written
                # and every credit already spent is lost.
                print()
                print("*** SEAMLESS CREDITS EXHAUSTED -- %s" % str(e)[:160])
                print("*** keeping everything resolved so far and continuing")
            checkpoint(outdir, "resolved", rows)
        rows = finish(rows)
        checkpoint(outdir, "verified", rows)

    for r in rows:
        r.pop("_person_id", None)
    with open(os.path.join(outdir, "contacts.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    with open(os.path.join(outdir, "rejected.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["domain", "name", "title", "employees", "reason"])
        for dom, p, why in rejected:
            nm = p.get("full_name") or (p.get("first_name", "") + " " +
                                        p.get("last_name", "")).strip()
            w.writerow([dom, nm, p.get("job_title", ""),
                        p.get("employee_count") or p.get("employees", ""), why])
    with open(os.path.join(outdir, "no_contact_companies.csv"), "w", newline="",
              encoding="utf-8") as f:
        # `covered` was computed BEFORE the LeadMagic rescue extended rows, so
        # every LeadMagic-found company was mislabelled "no contact found".
        # `already` rows were also excluded, so companies whose only contacts
        # were already in HubSpot showed up here as failures.
        found_any = {r["company_domain"] for r in rows} |                     {r["company_domain"] for r in already}
        reach = {r["company_domain"] for r in rows if r.get("pushable") == "yes"} |                 {r["company_domain"] for r in already}
        w = csv.writer(f)
        w.writerow(["company_id", "company_name", "company_domain", "employees", "reason"])
        for c in comps:
            if c["company_domain"] not in reach:
                w.writerow([c["company_id"], c["company_name"], c["company_domain"],
                            c.get("employees", ""),
                            "no contact found" if c["company_domain"] not in found_any
                            else "contact found, no verified email"])

    push = [r for r in rows if r.get("pushable") == "yes"]
    mob = [r for r in push if (r.get("mobile") or "").strip()]
    s_em = sum(1 for r in rows if (r.get("seamless_email") or "").strip())
    s_mo = sum(1 for r in rows if (r.get("mobile_source") or "") == "Seamless")
    p1 = ps.credits_remaining()
    L = []
    L.append("=" * 70)
    L.append("MAX-CONTACTS RUN  (Prospeo discover -> Seamless resolve)")
    L.append("=" * 70)
    L.append("companies (>=%s employees) : %d" % (min_emp, len(comps)))
    L.append("contacts pursued           : %d" % (len(rows) + len(already)))
    L.append("  already in HubSpot       : %d" % len(already))
    L.append("  net-new processed        : %d" % len(rows))
    L.append("")
    L.append("--- SEAMLESS DATA TEST (ran on 100% of contacts) ---")
    L.append("  email returned           : %d/%d (%.1f%%)"
             % (s_em, len(rows), 100.0 * s_em / len(rows) if rows else 0))
    L.append("  MOBILE returned          : %d/%d (%.1f%%)"
             % (s_mo, len(rows), 100.0 * s_mo / len(rows) if rows else 0))
    md = collections.Counter(r.get("seamless_mobile_status") or "none" for r in rows)
    L.append("  mobile datatype split    : %s" % dict(md))
    L.append("")
    L.append("--- OUTCOME ---")
    L.append("  VERIFIED / pushable      : %d (%.1f%%)"
             % (len(push), 100.0 * len(push) / len(rows) if rows else 0))
    L.append("  pushable WITH a mobile   : %d" % len(mob))
    L.append("  companies reached        : %d/%d (%.0f%%)"
             % (len({r["company_domain"] for r in push}), len(comps),
                (100.0 * len({r["company_domain"] for r in push}) / len(comps))
                if comps else 0))
    L.append("  verified contacts/company: %.2f"
             % (len(push) / len(comps) if comps else 0))
    L.append("")
    L.append("--- DISCOVERY FUNNEL (was stdout-only, now recorded) ---")
    for k in ("people_seen", "passed_gate", "pursued", "intra_dupe",
              "no_verified_email", "companies_with_people", "probe_exception",
              "prospeo_error"):
        if k in pstats:
            L.append("  %-24s %s" % (k, pstats[k]))
    L.append("")
    L.append("--- ATTRIBUTION ---")
    L.append("  discovery : %s" % dict(collections.Counter(
        r.get("discovery_layer") or "?" for r in rows)))
    L.append("  email src : %s" % dict(collections.Counter(
        r.get("email_source") or "none" for r in rows)))
    L.append("  verifier  : %s" % dict(collections.Counter(
        r.get("verify_provider") or "none" for r in rows)))
    L.append("")
    cs = CACHE.summary()
    L.append("--- CONTACT CACHE (cross-run memory) ---")
    L.append("  contacts remembered      : %s" % cs["cached_contacts"])
    L.append("  reused this run          : %s" % cs["reused_this_run"])
    L.append("  credits NOT respent      : %s" % cs["credits_saved_est"])
    L.append("")
    L.append("  prospeo credits used     : %s"
             % ((p0 - p1) if None not in (p0, p1) else "?"))
    L.append("  (seamless == 1 credit per contact resolved, no search spend)")
    L.append("")
    L.append("NOTHING WAS WRITTEN TO HUBSPOT.")
    txt = "\n".join(L)
    print("\n" + txt)
    open(os.path.join(outdir, "AUDIT.txt"), "w", encoding="utf-8").write(txt + "\n")
    json.dump({"companies": len(comps), "net_new": len(rows), "pushable": len(push),
               "with_mobile": len(mob), "seamless_email": s_em, "seamless_mobile": s_mo,
               "prospeo_credits": (p0 - p1) if None not in (p0, p1) else None},
              open(os.path.join(outdir, "summary.json"), "w"), indent=1)
    print("\nartifacts in %s/" % outdir)


if __name__ == "__main__":
    main()
