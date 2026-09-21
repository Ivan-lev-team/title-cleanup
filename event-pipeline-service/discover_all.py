"""
Combined discovery: run EVERY people-database on EVERY company, then union.

This replaces the fallback chain the pipeline used before, where LeadMagic only
ran on companies Prospeo had failed at. That minimised cost but capped the
contact count, because a source never got to contribute at a company an earlier
source had already "covered" -- even when it knew different people there.

Goal here is maximum contacts, so all three run everywhere and the results are
merged by person identity:

    Clay    ~0.2 credits/company (measured)  -- 223874.6 -> 223872.6 over 10
    Prospeo free search                      -- reveal is the paid step, later
    LeadMagic role-finder  free on a miss, 2 credits on a hit

Seamless is deliberately NOT a discovery source: its /search/contacts bills
~1 credit per 10 results plus a per-call floor (830 of 971 credits in a measured
200-company run, ~15 credits per verified contact against Prospeo's ~1.9). It
earns its place as the RESOLVER instead, where it costs exactly 1 per contact
and returns email AND mobile.

Person identity for the union, in priority order:
  1. LinkedIn slug            -- the only cross-provider stable key
  2. firstname|lastname|domain -- pipe-separated, because bare concatenation
     makes ("Jo","Anne") collide with ("Joan","ne")

Every merged person records WHICH sources found them, so the audit can report
each database's unique contribution and the overlap between them.
"""
import collections
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import clay_layer
import prospeo_search as ps
import leadmagic_client as lm
import ratelimit

# Measured over 32 real hits: "owner" produced 28 of them (87%) and the other
# five roles produced NONE. Each role-finder call costs ~8 seconds, so probing
# six roles per company was 6x the calls for ~0 extra contacts -- it was the
# single reason a 100-company run took 10.8 minutes.
ROLES = ["owner"]


def person_key(first, last, domain, linkedin):
    m = re.search(r"linkedin\.com/in/([a-z0-9\-_%\.]+)", (linkedin or "").lower())
    if m:
        return "li:" + m.group(1).strip("/")
    return "nd:%s|%s|%s" % ((first or "").strip().lower(),
                            (last or "").strip().lower(),
                            (domain or "").strip().lower())


def _clay(c, titles):
    r = clay_layer.search_people(c["company_domain"], job_titles=titles)
    return "Clay", c, r.get("people") or [], r.get("quarantined") or [], r.get("error", "")


def _prospeo(c, titles):
    # unfiltered: Prospeo caps job_title at 50 and the local ICP gate applies
    # the full list anyway, so one call per company beats three.
    r = ps.search_person(c["company_domain"], job_titles=None, seniority=None)
    people = [{"first_name": p["first_name"], "last_name": p["last_name"],
               "full_name": p["full_name"], "job_title": p["job_title"],
               "linkedin_url": p["linkedin_url"],
               "company_domain": c["company_domain"],
               "employee_count": p.get("employee_count"),
               "prospeo_email_status": p.get("email_status", ""),
               "person_id": p.get("person_id", ""), "source": "Prospeo"}
              for p in (r.get("people") or [])]
    return "Prospeo", c, people, [], r.get("error", "")


def _leadmagic(c, titles):
    dom = ps.root_domain(c["company_domain"])
    found = []
    for role in ROLES:
        resp = lm._post("/role-finder", {"company_domain": dom, "job_title": role})
        if resp is None or resp.status_code >= 300:
            continue
        j = ratelimit.safe_json(resp)
        if (j.get("message") or "") == "Role Found" and j.get("first_name"):
            found.append({"first_name": j.get("first_name"),
                          "last_name": j.get("last_name") or "",
                          "full_name": (j.get("name") or "").strip(),
                          "job_title": role,
                          "linkedin_url": j.get("profile_url") or "",
                          "company_domain": c["company_domain"],
                          "source": "LeadMagic"})
            break   # one named decision-maker per company is what it returns
    return "LeadMagic", c, found, [], ""


SOURCES = {"clay": _clay, "prospeo": _prospeo, "leadmagic": _leadmagic}


def run(companies, titles, enabled=("clay", "prospeo", "leadmagic"), workers=24):
    """Run every enabled source on every company and union the people.

    Returns (merged, quarantined, stats) where each merged row carries
    `sources` (list) and `discovery_layer` (comma-joined, for the CSV).
    """
    merged, quarantined = {}, []
    stats = collections.Counter()
    per_source_companies = collections.defaultdict(set)
    lock = threading.Lock()

    jobs = [(name, c) for c in companies for name in enabled if name in SOURCES]
    print("\n[DISCOVERY] %d companies x %d sources = %d probes"
          % (len(companies), len(enabled), len(jobs)))

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(SOURCES[name], c, titles) for name, c in jobs]
        for fu in as_completed(futs):
            try:
                src, c, people, quar, err = fu.result()
            except Exception as e:
                stats["probe_exception"] += 1
                print("    probe crashed: %s: %s" % (type(e).__name__, str(e)[:70]))
                continue
            done += 1
            if err:
                stats["%s_error" % src.lower()] += 1
            with lock:
                quarantined.extend(quar)
                if people:
                    per_source_companies[src].add(c["company_domain"])
                stats["%s_people" % src.lower()] += len(people)
                for p in people:
                    k = person_key(p.get("first_name"), p.get("last_name"),
                                   c["company_domain"], p.get("linkedin_url"))
                    cur = merged.get(k)
                    if cur is None:
                        p = dict(p)
                        p["sources"] = [src]
                        p["company_id"] = c.get("company_id", "")
                        p["company_name"] = c.get("company_name", "")
                        p["employees"] = (p.get("employee_count")
                                          or c.get("employees") or "")
                        merged[k] = p
                    else:
                        if src not in cur["sources"]:
                            cur["sources"].append(src)
                            stats["overlap_%s" % src.lower()] += 1
                        # keep the richest record: prefer a LinkedIn URL and a
                        # non-empty title from whichever source has them
                        if not cur.get("linkedin_url") and p.get("linkedin_url"):
                            cur["linkedin_url"] = p["linkedin_url"]
                        if not cur.get("job_title") and p.get("job_title"):
                            cur["job_title"] = p["job_title"]
                        if not cur.get("person_id") and p.get("person_id"):
                            cur["person_id"] = p["person_id"]
                        if not cur.get("prospeo_email_status") and p.get("prospeo_email_status"):
                            cur["prospeo_email_status"] = p["prospeo_email_status"]
            if done % 60 == 0:
                print("    %d/%d probes | %d unique people so far"
                      % (done, len(jobs), len(merged)))

    rows = []
    for p in merged.values():
        p["discovery_layer"] = ",".join(sorted(p["sources"]))
        p["_person_id"] = p.get("person_id", "")
        rows.append(p)

    print("    unique people after union : %d" % len(rows))
    for src in sorted(per_source_companies):
        print("      %-10s found people at %3d companies, %4d rows"
              % (src, len(per_source_companies[src]),
                 stats["%s_people" % src.lower()]))
    multi = sum(1 for r in rows if len(r["sources"]) > 1)
    print("      found by >1 source        : %d (%.0f%% overlap)"
          % (multi, 100.0 * multi / len(rows) if rows else 0))
    if quarantined:
        print("      stale-domain quarantined  : %d" % len(quarantined))
    stats["unique_people"] = len(rows)
    stats["multi_source"] = multi
    for src in per_source_companies:
        stats["companies_%s" % src.lower()] = len(per_source_companies[src])
    return rows, quarantined, stats
