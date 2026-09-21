"""
Simple gap-fill for the 227 corporate-domain leads in
levanta_triggered_leads_20260813.csv:
  - pod set, sdr_owner blank  -> fill sdr_owner with least_loaded_owner_in_pod(pod)
  - sdr_owner set, pod blank  -> fill pod from config.POD_OWNERS reverse lookup
  - neither set               -> least_loaded_pod() + least_loaded_owner_in_pod()
  - both already set          -> leave alone
No exclusions, no reassignment of existing values. Never touch hubspot_owner_id.

Usage: python push_trigger_batch_20260813.py <csv> <outdir> [--live]
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hubspot_client  # noqa: E402
import config  # noqa: E402

FREE_DOMAINS = {"gmail.com", "yahoo.com", "icloud.com", "me.com", "msn.com",
                 "hotmail.com", "outlook.com", "aol.com", "gmx.ch"}


def pod_for_owner(owner_id):
    for pod, owners in config.POD_OWNERS.items():
        if owner_id in owners:
            return pod
    return None


def main():
    if len(sys.argv) < 3:
        print("Usage: python push_trigger_batch_20260813.py <csv> <outdir> [--live]")
        sys.exit(1)
    in_path, outdir = sys.argv[1], Path(sys.argv[2])
    live = "--live" in sys.argv[3:]
    outdir.mkdir(parents=True, exist_ok=True)
    config.DRY_RUN = not live

    with open(in_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    domains = sorted(set(
        r["lead_email"].strip().lower().split("@")[-1] for r in rows
    ) - set())
    domains = [d for d in domains if d not in FREE_DOMAINS]
    print(f"{len(rows)} rows, {len(domains)} unique corporate domains", flush=True)

    companies = hubspot_client.find_companies_by_domains_bulk(domains)
    print(f"{len(companies)} of {len(domains)} domains matched a HubSpot company", flush=True)

    out_rows = []
    for domain in domains:
        rec = companies.get(domain)
        if rec is None:
            out_rows.append((domain, "", "no_company_record", "", ""))
            continue
        cid = rec["id"]
        pod = (rec["properties"].get("pod") or "").strip()
        sdr = (rec["properties"].get("sdr_owner") or "").strip()

        if pod and sdr:
            out_rows.append((domain, cid, "already_set", pod, sdr))
        elif pod and not sdr:
            new_sdr = hubspot_client.least_loaded_owner_in_pod(pod) if pod in config.POD_OWNERS else ""
            out_rows.append((domain, cid, "fill_sdr_owner", pod, new_sdr))
        elif sdr and not pod:
            new_pod = pod_for_owner(sdr)
            out_rows.append((domain, cid, "fill_pod", new_pod or "", sdr))
        else:
            new_pod = hubspot_client.least_loaded_pod()
            new_sdr = hubspot_client.least_loaded_owner_in_pod(new_pod)
            out_rows.append((domain, cid, "fill_both", new_pod, new_sdr))

    manifest_path = outdir / "TRIGGER_gapfill.csv"
    with open(manifest_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["domain", "hs_object_id", "action", "pod", "sdr_owner_id"])
        w.writerows(out_rows)
    print(f"Wrote {manifest_path}", flush=True)

    from collections import Counter
    print("\n--- by action ---")
    for action, n in Counter(r[2] for r in out_rows).most_common():
        print(f"  {action:20s} {n}")

    if not live:
        print("\nDRY RUN -- no writes made. Re-run with --live to push.")
        return

    print("\nLIVE -- writing to HubSpot...", flush=True)
    updated, failed = 0, 0
    for domain, cid, action, pod, sdr in out_rows:
        if action in ("already_set", "no_company_record") or not cid:
            continue
        try:
            hubspot_client.update_company(cid, {"pod": pod, "sdr_owner": sdr})
            updated += 1
        except Exception as e:
            print(f"  FAILED {domain} ({cid}): {e}")
            failed += 1
    print(f"\nDone. updated={updated} failed={failed}")


if __name__ == "__main__":
    main()
