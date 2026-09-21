"""
Second-pass filter applied ON TOP OF the standard company_icp_judge PASS
verdicts, for this list only. The production ICP prompt (icp_prompt.py) was
deliberately broadened by marketing to pass ANY physical-product company
(including B2B/industrial and retailers) -- this script adds a stricter
consumer-brand-only cut for this specific screening request, without
modifying the shared production prompt.

Classifies each PASS-verdict company as:
  CONSUMER  - a brand selling its OWN physical consumer products, primarily to
              end consumers, via Amazon/Walmart/Shopify/DTC/retail-consumer
              channels. KEEP.
  B2B       - sells primarily to other businesses / industrial / enterprise
              customers (components, equipment, raw materials, enterprise
              hardware), not a consumer-facing brand. EXCLUDE.
  RETAILER  - a retailer/reseller/distributor selling OTHER brands' products
              (department store, office-supply chain, big-box, marketplace),
              not a manufacturer of its own product line. EXCLUDE.

Defaults to CONSUMER (keep) on any parse failure -- never silently drop.

Usage: python tighten_b2b_retail_filter.py <companies_icp_judged.csv> <outdir> [--workers N]
"""
import csv
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import anthropic  # noqa: E402

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

PROMPT = """A company already passed a broad screen confirming it sells physical products. Now classify it more precisely for a DTC/consumer-brand affiliate marketing platform (Levanta) whose actual customers are brands selling directly to CONSUMERS on Amazon, Walmart, and/or Shopify/DTC ecommerce -- not B2B suppliers and not retailers reselling other brands.

Company name: {company}
Domain: {domain}

Classify as exactly one of:
- "CONSUMER": the company designs/makes/owns physical products under its OWN brand and sells primarily to end consumers (any category: beauty, food & beverage, apparel, home goods, pet, toys, consumer electronics, supplements, etc.), via retail, Amazon, Walmart, Shopify/DTC, or similar consumer channels. Company SIZE does not matter -- a large, well-known consumer brand is still CONSUMER.
- "B2B": the company's core customers are OTHER BUSINESSES, not consumers -- e.g. industrial components/equipment (bearings, pumps, semiconductors, sensors), enterprise hardware (barcode scanners, printers, servers), medical devices sold to clinics/hospitals, raw materials, or B2B supply chains. Sold via distributors/reps to businesses, not to a consumer directly.
- "RETAILER": the company is a store chain, marketplace, or distributor that sells a MIX of OTHER companies' branded products, rather than manufacturing/owning its own product brand (e.g. an office-supply chain, department store, big-box retailer, wholesale distributor).

If genuinely unsure or the company sells its own branded product both to consumers AND businesses with consumer being a real channel, choose CONSUMER.

Respond with ONLY a JSON object, no other text: {{"classification": "CONSUMER" or "B2B" or "RETAILER", "reason": "one short sentence"}}"""


def classify_one(row):
    try:
        resp = _client.messages.create(
            model="claude-sonnet-5",
            max_tokens=150,
            messages=[{"role": "user", "content": PROMPT.format(
                company=row["company_name"], domain=row.get("domain", "") or "(no domain)"
            )}],
        )
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        data = json.loads(text)
        cls = (data.get("classification") or "").upper()
        if cls not in ("CONSUMER", "B2B", "RETAILER"):
            raise ValueError(f"bad classification: {cls!r}")
        return row["company_name"], row.get("domain", ""), row["contact_count"], cls, data.get("reason", "")
    except Exception as e:
        return row["company_name"], row.get("domain", ""), row["contact_count"], "CONSUMER", f"parse failure, defaulted to keep ({e})"


def main():
    if len(sys.argv) < 3:
        print("Usage: python tighten_b2b_retail_filter.py <companies_icp_judged.csv> <outdir> [--workers N]")
        sys.exit(1)
    in_path, outdir = sys.argv[1], Path(sys.argv[2])
    workers = 8
    if "--workers" in sys.argv:
        workers = int(sys.argv[sys.argv.index("--workers") + 1])
    outdir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(in_path, dtype=str, keep_default_na=False)
    df = df[df["verdict"] == "PASS"].copy()
    df["contact_count"] = df["contact_count"].astype(int)
    rows = df.to_dict("records")

    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(classify_one, r): r for r in rows}
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            if done % 50 == 0 or done == len(rows):
                print(f"{done}/{len(rows)} classified", flush=True)

    out_df = pd.DataFrame(results, columns=["company_name", "domain", "contact_count", "classification", "reason"])
    out_df = out_df.sort_values("contact_count", ascending=False)
    out_df.to_csv(outdir / "companies_consumer_b2b_retail.csv", index=False, quoting=csv.QUOTE_MINIMAL)

    counts = Counter(out_df["classification"])
    contacts_by_class = out_df.groupby("classification")["contact_count"].sum()
    print("\nClassification counts (companies):", dict(counts))
    print("Classification counts (contacts):", contacts_by_class.to_dict())


if __name__ == "__main__":
    main()
