#!/usr/bin/env python3
"""Physical-product screen for a Store Leads Shopify export.

  python scripts/physical_product_run.py <input.csv> <out.jsonl> [--limit N]
                                          [--workers N] [--pass2] [--only-domains f]

Pass 1 judges every row from the export fields. Rows the judge returns
UNRESOLVED for are handled two ways:
  - row is in the NO-EVIDENCE set (thin description AND blank categories AND no
    apps)  -> queued for pass 2 (homepage lookup via ZenRows), then re-judged
  - otherwise                                     -> default-to-PASS

Writes one JSON object per row to the .jsonl checkpoint as results arrive, so
the run is resumable: re-running skips any domain already present.

Nothing is written back to the CSV, the sheet, or HubSpot.
"""
import argparse
import csv
import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)  # so config.load_dotenv() finds .env

csv.field_size_limit(10 ** 8)

import qualify  # noqa: E402

THIN = 40


def g(row, k):
    return (row.get(k) or "").strip()


def is_no_evidence(row):
    """The only rows a homepage lookup can actually add information to."""
    return (
        len(g(row, "description")) < THIN
        and len(g(row, "meta_description")) < THIN
        and not g(row, "categories")
        and not g(row, "installed_apps_names")
    )


def name_from_domain(domain):
    """hoover.com -> Hoover. Used only to backfill a blank name on a PASS."""
    d = domain.lower().strip()
    for pre in ("https://", "http://", "www.", "shop.", "store."):
        if d.startswith(pre):
            d = d[len(pre):]
    d = d.split("/")[0]
    core = d.rsplit(".", 1)[0] if "." in d else d
    core = core.split(".")[-1]
    parts = [p for p in core.replace("_", "-").split("-") if p]
    return " ".join(p.capitalize() for p in parts) or domain


_zen_lock = threading.Semaphore(6)   # concurrency cap on the scrape tier
_zen_calls = Counter()


def homepage_text(domain, cap):
    """Pass-2 lookup. Reuses the existing ZenRows client; returns '' on any
    failure or once the hard cap is reached."""
    if _zen_calls["used"] >= cap:
        _zen_calls["skipped"] += 1
        return ""
    with _zen_lock:
        _zen_calls["used"] += 1
        try:
            import zenrows_client
            return (zenrows_client.company_context(domain) or "")[:6000]
        except Exception as e:
            _zen_calls["error"] += 1
            return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--pass2", action="store_true", help="enable homepage lookups")
    ap.add_argument("--pass2-cap", type=int, default=500)
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--only-domains", default="", help="file of domains to restrict to")
    args = ap.parse_args()

    done = set()
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["domain"])
                except Exception:
                    pass
        print(f"resuming: {len(done):,} rows already judged", flush=True)

    only = None
    if args.only_domains:
        only = {l.strip() for l in open(args.only_domains, encoding="utf-8") if l.strip()}

    rows = []
    with open(args.src, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            d = g(r, "domain")
            if d in done:
                continue
            if only is not None and d not in only:
                continue
            rows.append(r)
            if args.limit and len(rows) >= args.limit:
                break
    print(f"to judge: {len(rows):,} rows | workers={args.workers} "
          f"| pass2={'on' if args.pass2 else 'OFF'} (cap {args.pass2_cap})", flush=True)

    out_lock = threading.Lock()
    fout = open(args.out, "a", encoding="utf-8")
    counts = Counter()
    t0 = time.time()

    def work(row):
        rec = qualify.physical_product_judge(row, model=args.model)
        used_pass2 = False
        if rec["verdict"] == "UNRESOLVED":
            if args.pass2 and is_no_evidence(row):
                txt = homepage_text(g(row, "domain"), args.pass2_cap)
                if txt:
                    used_pass2 = True
                    rec = qualify.physical_product_judge(
                        row, homepage_text=txt, model=args.model
                    )
            if rec["verdict"] == "UNRESOLVED":
                # never emit UNRESOLVED: high-capture default
                rec = {**rec, "verdict": "PASS", "fail_reason": None,
                       "fulfillment": rec.get("fulfillment") or "unknown",
                       "reason": (rec.get("reason") or "") +
                                 " [unresolved -> default PASS]"}
        name = g(row, "title")
        if rec["verdict"] == "PASS" and not name:
            name = name_from_domain(g(row, "domain"))
            rec["name_backfilled"] = True
        return {
            "domain": g(row, "domain"),
            "name": name,
            "categories": g(row, "categories"),
            "verdict": rec["verdict"],
            "fail_reason": rec.get("fail_reason"),
            "evidence": rec.get("evidence"),
            "fulfillment": rec.get("fulfillment"),
            "reason": rec.get("reason"),
            "pass2": used_pass2,
            "no_evidence_row": is_no_evidence(row),
            "error": rec.get("error"),
            "name_backfilled": rec.get("name_backfilled", False),
        }

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, r) for r in rows]
        for i, fu in enumerate(as_completed(futs), 1):
            try:
                rec = fu.result()
            except Exception as e:
                counts["crash"] += 1
                continue
            counts[rec["verdict"]] += 1
            if rec["verdict"] == "FAIL":
                counts["fr:" + str(rec["fail_reason"])] += 1
            if rec["fulfillment"]:
                counts["ff:" + rec["fulfillment"]] += 1
            if rec["error"]:
                counts["api_error"] += 1
            with out_lock:
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if i % 250 == 0:
                    fout.flush()
                    rate = i / max(1e-9, time.time() - t0)
                    eta = (len(rows) - i) / max(1e-9, rate) / 60
                    print(f"  {i:,}/{len(rows):,}  {rate:.1f}/s  eta {eta:.0f}m  "
                          f"{dict(counts)}", flush=True)
    fout.close()
    print("\nDONE", dict(counts))
    print("pass2:", dict(_zen_calls))
    print(f"elapsed {(time.time()-t0)/60:.1f} min")


if __name__ == "__main__":
    main()
