#!/usr/bin/env python3
"""
Full provider attribution for an enrichment run: who did what, at every step,
including the steps that failed.

Two outputs, from one run directory:

  provider_ledger.csv     LONG format, one row per (contact, step, provider).
                          Every step a contact passed through, in order, with
                          the provider that handled it and what it returned.
                          This is the raw ledger -- join or pivot it however
                          you like.

  provider_stats.csv      WIDE summary, one row per (step, provider): attempts,
                          successes, failures, success rate. This is the
                          "what did each provider actually contribute" table.

  no_contact_companies.csv  Companies where NOTHING was found, with full
                            firmographics, for handing to a separate agent.

  python scripts/provider_ledger.py <rundir>
"""
import csv, json, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)

# The pipeline's steps, in execution order. Each entry says how to read the
# outcome of that step off a contact row.
STEPS = [
    ("1_discovery",      "discovery provider found the person"),
    ("2_email_supplied", "provider that supplied the original address"),
    ("3_verify",         "DeBounce first-pass verification"),
    ("4_resolve",        "LeadMagic resolution of risky/invalid"),
    ("5_replace",        "Prospeo search for a replacement address"),
    ("6_verify_repair",  "LeadMagic verification of the replacement"),
    ("7_mobile",         "provider that supplied the mobile"),
]


def ledger_rows(r):
    """Yield (step, provider, outcome, detail) for one contact."""
    layer = (r.get("discovery_layer") or "").strip() or "Seamless"
    key = (r.get("email") or r.get("linkedin_url") or "").lower()

    yield ("1_discovery", layer, "found", r.get("job_title", ""))

    src = (r.get("email_source") or "").strip()
    orig_src = "Prospeo (discovery)" if layer == "Prospeo" else "Seamless"
    yield ("2_email_supplied", orig_src,
           "supplied" if (r.get("email_before_repair") or r.get("email")) else "none",
           r.get("email_seamless_validity", ""))

    # DeBounce always runs when there is an address
    prov = (r.get("email_verify_provider") or "")
    deb = ""
    if r.get("email") or r.get("email_before_repair"):
        # first-pass DeBounce verdict: recoverable from resolve/raw fields
        if "DeBounce" in prov and "LeadMagic" not in prov:
            deb = r.get("email_verify_status", "")
        elif r.get("email_resolve_status"):
            deb = "risky_or_invalid"      # it fell through to LeadMagic
        else:
            deb = r.get("email_verify_status", "")
        yield ("3_verify", "DeBounce",
               "valid" if deb == "valid" else "not_valid", deb)

    if r.get("email_resolve_provider"):
        rs = r.get("email_resolve_status", "")
        yield ("4_resolve", "LeadMagic",
               "resolved_valid" if rs == "valid" else "resolved_" + (rs or "none"), rs)

    rep = (r.get("email_replacement_source") or "")
    if rep:
        if rep.startswith("Prospeo (no"):
            yield ("5_replace", "Prospeo", "no_replacement_found", "")
        else:
            yield ("5_replace", "Prospeo", "replacement_found", r.get("email", ""))
            yield ("6_verify_repair", "LeadMagic (repair pass)",
                   "valid" if r.get("email_verify_status") == "valid" else "not_valid",
                   r.get("email_verify_status", ""))

    msrc = (r.get("mobile_source") or "").strip()
    if (r.get("mobile") or "").strip():
        yield ("7_mobile", msrc or "unknown", "mobile_found", r.get("mobile_datatype", ""))
    else:
        dt = (r.get("mobile_datatype") or "").strip()
        yield ("7_mobile", "Seamless",
               "main_only" if dt == "main" else "no_number", dt)


def main():
    rundir = sys.argv[1]
    cpath = os.path.join(rundir, "pilot_contacts.csv")
    rows = list(csv.DictReader(open(cpath, encoding="utf-8")))

    # ---- long ledger
    lpath = os.path.join(rundir, "provider_ledger.csv")
    n = 0
    with open(lpath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["company_domain", "company_name", "contact", "job_title",
                    "final_status", "step", "provider", "outcome", "detail"])
        for r in rows:
            who = (r.get("first_name", "") + " " + r.get("last_name", "")).strip()
            for step, prov, outcome, detail in ledger_rows(r):
                w.writerow([r.get("company_domain", ""), r.get("company_name", ""),
                            who, r.get("job_title", ""), r.get("final_status", ""),
                            step, prov, outcome, detail])
                n += 1
    print("wrote %s  (%d ledger rows from %d contacts)" % (lpath, n, len(rows)))

    # ---- wide per-provider stats
    agg = {}
    for r in rows:
        for step, prov, outcome, _ in ledger_rows(r):
            k = (step, prov)
            a = agg.setdefault(k, {"attempts": 0, "success": 0, "outcomes": {}})
            a["attempts"] += 1
            a["outcomes"][outcome] = a["outcomes"].get(outcome, 0) + 1
            if outcome in ("found", "supplied", "valid", "resolved_valid",
                           "replacement_found", "mobile_found"):
                a["success"] += 1

    spath = os.path.join(rundir, "provider_stats.csv")
    with open(spath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["step", "step_description", "provider", "attempts",
                    "successes", "success_rate_pct", "outcome_breakdown"])
        desc = dict(STEPS)
        for (step, prov), a in sorted(agg.items()):
            rate = (100.0 * a["success"] / a["attempts"]) if a["attempts"] else 0
            w.writerow([step, desc.get(step, ""), prov, a["attempts"], a["success"],
                        round(rate, 1),
                        "; ".join("%s=%d" % kv for kv in
                                  sorted(a["outcomes"].items(), key=lambda x: -x[1]))])
    print("wrote %s" % spath)

    print()
    print("%-17s %-28s %8s %9s %7s" % ("STEP", "PROVIDER", "ATTEMPTS", "SUCCESS", "RATE"))
    for (step, prov), a in sorted(agg.items()):
        rate = (100.0 * a["success"] / a["attempts"]) if a["attempts"] else 0
        print("%-17s %-28s %8d %9d %6.1f%%" % (step, prov[:28], a["attempts"],
                                               a["success"], rate))

    # ---- companies where nothing was found, for a separate agent
    comp = list(csv.DictReader(open(os.path.join(rundir, "pilot_companies.csv"),
                                    encoding="utf-8")))
    reached = {r["company_domain"] for r in rows}
    pushed = {r["company_domain"] for r in rows if r.get("pushable") == "yes"}
    npath = os.path.join(rundir, "no_contact_companies.csv")
    with open(npath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["company_id", "company_name", "company_domain", "annualrevenue",
                    "country", "hs_employees", "outcome", "note"])
        n_none = n_novalid = 0
        for c in comp:
            d = c["company_domain"]
            if d not in reached:
                outcome, note = "no_contact_found", \
                    "no person returned by Seamless OR Prospeo"
                n_none += 1
            elif d not in pushed:
                outcome, note = "contact_but_no_valid_email", \
                    "person(s) found, no verified email after full waterfall"
                n_novalid += 1
            else:
                continue
            w.writerow([c.get("company_id", ""), c.get("company_name", ""), d,
                        c.get("annualrevenue", ""), c.get("country", ""),
                        c.get("hs_employees", ""), outcome, note])
    print()
    print("wrote %s" % npath)
    print("   no_contact_found            : %d (neither provider found anyone)" % n_none)
    print("   contact_but_no_valid_email  : %d (found someone, no usable address)" % n_novalid)
    print("   total handoff rows          : %d of %d companies" % (n_none + n_novalid, len(comp)))


if __name__ == "__main__":
    main()
