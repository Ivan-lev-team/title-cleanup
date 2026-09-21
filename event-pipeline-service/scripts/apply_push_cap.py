"""
Applies a per-company cap to the dry-run push plan, keeping the most valuable
contacts per company. Still a DRY RUN -- writes nothing to HubSpot.

Ranking per contact (highest first):
  1. Function relevance to Levanta (affiliate/marketplace/ecommerce/Amazon >
     brand/growth/category > marketing/digital)
  2. Seniority (founder/owner > C-level/president > VP > head/director > manager)
  3. Contact completeness (email+mobile > email only > mobile only)

Usage: python apply_push_cap.py <PUSH_DRYRUN_batch1.csv> <outdir> [--cap N]
"""
import csv
import re
import sys
from pathlib import Path

import pandas as pd

FUNC_HIGH = r"affiliate|marketplace|market place|amazon|walmart|e-?comm|ecom\b|dtc|direct[- ]to[- ]consumer|digital commerce|omni-?channel|influencer|partnership"
FUNC_MID = r"\bgrowth\b|\bbrand\b|category|merchandis|\brevenue\b"
FUNC_LOW = r"marketing|digital|\bsales\b"

SEN_FOUNDER = r"founder|co-?founder|owner|proprietor"
SEN_CLEVEL = r"\bceo\b|chief executive|\bpresident\b|\bchief\b|\bcmo\b|\bcoo\b|\bcro\b"
SEN_VP = r"\bevp\b|\bsvp\b|\bvp\b|vice president"
SEN_HEAD = r"head of|\bdirector\b|general manager"
SEN_MGR = r"\bmanager\b|\blead\b|\bspecialist\b"


def func_score(t):
    t = (t or "").lower()
    if re.search(FUNC_HIGH, t):
        return 5
    if re.search(FUNC_MID, t):
        return 3
    if re.search(FUNC_LOW, t):
        return 2
    return 0


def sen_score(t):
    t = (t or "").lower()
    if re.search(SEN_FOUNDER, t):
        return 5
    if re.search(SEN_CLEVEL, t):
        return 4
    if re.search(SEN_VP, t):
        return 3
    if re.search(SEN_HEAD, t):
        return 2
    if re.search(SEN_MGR, t):
        return 1
    return 0


def completeness(r):
    has_e = bool(str(r.get("email", "")).strip())
    has_m = bool(str(r.get("mobile", "")).strip())
    if has_e and has_m:
        return 3
    if has_e:
        return 2
    return 1 if has_m else 0


def main():
    in_path, outdir = sys.argv[1], Path(sys.argv[2])
    cap = 5
    if "--cap" in sys.argv:
        cap = int(sys.argv[sys.argv.index("--cap") + 1])
    outdir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(in_path, dtype=str, keep_default_na=False)
    live = df[df["action"] != "SKIP_NO_EMAIL_NO_MOBILE"].copy()

    live["func_score"] = live["job_title"].map(func_score)
    live["seniority_score"] = live["job_title"].map(sen_score)
    live["completeness"] = live.apply(completeness, axis=1)
    live["rank_score"] = live["func_score"] * 10 + live["seniority_score"] * 3 + live["completeness"]

    # group key: prefer the resolved HubSpot company, fall back to source name
    live["_grp"] = live["hubspot_company_id"].where(live["hubspot_company_id"] != "", live["company_name_source"])

    live = live.sort_values(["_grp", "rank_score"], ascending=[True, False])
    live["rank_in_company"] = live.groupby("_grp").cumcount() + 1

    kept = live[live["rank_in_company"] <= cap].drop(columns=["_grp"])
    cut = live[live["rank_in_company"] > cap].drop(columns=["_grp"])

    kept.to_csv(outdir / f"PUSH_PLAN_capped{cap}.csv", index=False, quoting=csv.QUOTE_MINIMAL)
    cut.to_csv(outdir / f"PUSH_CUT_by_cap{cap}.csv", index=False, quoting=csv.QUOTE_MINIMAL)

    print(f"=========== CAPPED PUSH PLAN (cap {cap}/company, DRY RUN) ===========")
    print(f"  eligible contacts before cap: {len(live)}")
    print(f"  KEPT:                         {len(kept)}")
    print(f"  cut by cap:                   {len(cut)}")
    print(f"  companies:                    {kept['company_name_source'].nunique()}")
    print("\nActions among kept:")
    for a, n in kept["action"].value_counts().items():
        print(f"  {a:26s} {n}")
    print("\nRouting by SDR Owner among kept:")
    for nm, n in kept["route_sdr_owner_name"].replace("", "(no SDR owner)").value_counts().items():
        print(f"  {nm:26s} {n}")
    print("\nBiggest companies after cap:")
    print(kept.groupby("company_name_source").size().sort_values(ascending=False).head(8).to_string())
    print("\nTitle mix of kept (function tier):")
    tier = kept["func_score"].map({5: "high (affil/mktplace/ecom)", 3: "mid (brand/growth/category)",
                                   2: "low (marketing/digital/sales)", 0: "none"})
    for k, n in tier.value_counts().items():
        print(f"  {k:30s} {n}")


if __name__ == "__main__":
    main()
