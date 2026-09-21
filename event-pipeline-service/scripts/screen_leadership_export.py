"""
Stage 1 (free) screen for a fresh raw contact export.

Input columns (Clay/Sales-Nav-style export): blank, "Company Table Data",
"First Name", "Last Name", "Full Name", "Job Title", "Location",
"Company Domain", "LinkedIn Profile".

Dedupes to unique companies, then eliminates obvious non-ICP companies for $0
via a name/domain keyword blocklist + CJK-name exclusion, before any paid
Anthropic ICP judge call. Writes:
  - <outdir>/companies_blocked.csv   (unique companies eliminated + why)
  - <outdir>/companies_kept.csv      (unique companies surviving to the next step)
  - <outdir>/contacts_kept.csv       (all contact rows whose company survived)
  - <outdir>/contacts_blocked.csv    (all contact rows whose company was blocked)

Usage: python screen_leadership_export.py <input.csv> <outdir>
"""
import csv
import re
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

# Big tech / platforms / mega-cap non-ICP companies that show up as "Leadership,
# Founders" contacts but are not Levanta's ICP (physical consumer-product brands
# selling on Amazon/Walmart/Shopify). Matched as whole-word-ish substrings against
# a normalized company name, and against the registrable domain.
NAME_BLOCKLIST = [
    # big tech / platforms / software giants
    "microsoft", "alphabet", "google", "amazon.com", "amazon web services", "aws",
    "meta platforms", "facebook", "apple inc", "apple", "salesforce", "oracle",
    "sap ", "ibm", "intel", "cisco", "adobe", "shopify", "hubspot", "servicenow",
    "workday", "atlassian", "snowflake", "palantir", "nvidia", "dell", "hp inc",
    "hewlett packard", "hewlett-packard", "vmware", "twilio", "stripe", "square",
    "paypal", "block, inc", "docusign", "zoom video", "slack technologies",
    "okta", "datadog", "mongodb", "elastic", "cloudflare", "akamai", "vmware",
    "epic games", "ea sports", "electronic arts", "activision", "netflix",
    "spotify", "uber technologies", "lyft", "airbnb", "doordash", "instacart",
    "linkedin", "x corp", "twitter", "tiktok", "bytedance", "snap inc",
    "pinterest", "reddit",
    # mega retail / big box
    "walmart", "target corp", "costco", "kroger", "albertsons", "home depot",
    "lowe's", "lowes companies", "best buy", "macy's", "nordstrom", "tj maxx",
    "tjx companies", "ross stores", "dollar general", "dollar tree", "cvs health",
    "walgreens", "kohl's",
    # mega CPG / conglomerates
    "procter & gamble", "procter and gamble", "unilever", "nestle", "nestlé",
    "coca-cola", "coca cola", "pepsico", "frito-lay", "kraft heinz", "mondelez",
    "general mills", "kellogg", "kellanova", "colgate-palmolive", "colgate palmolive",
    "johnson & johnson", "johnson and johnson", "reckitt", "kimberly-clark",
    "kimberly clark", "l'oreal", "loreal", "estee lauder", "estée lauder",
    "beiersdorf", "henkel", "danone", "mars, incorporated", "mars inc",
    "anheuser-busch", "anheuser busch", "molson coors", "diageo", "constellation brands",
    # industrial / conglomerate / big finance
    "general electric", "honeywell", "siemens", "3m company", "caterpillar",
    "boeing", "lockheed martin", "raytheon", "general motors", "ford motor",
    "stellantis", "toyota", "volkswagen", "goldman sachs", "morgan stanley",
    "jpmorgan", "jp morgan", "bank of america", "wells fargo", "citigroup",
    "american express", "visa inc", "mastercard", "berkshire hathaway",
    "exxonmobil", "exxon mobil", "chevron corporation", "pfizer", "moderna",
    "merck & co", "abbvie", "bristol myers squibb", "bristol-myers squibb",
    "eli lilly", "novartis", "roche", "sanofi", "astrazeneca",
    # telecom / media conglomerates
    "at&t", "verizon", "t-mobile", "comcast", "disney", "warner bros",
    "paramount global", "nbcuniversal", "fox corporation", "sony corporation",
    # big consulting / staffing / agency holding companies (channel partners, not ICP)
    "accenture", "deloitte", "mckinsey", "boston consulting group", "bain & company",
    "pwc", "pricewaterhousecoopers", "kpmg", "ernst & young", "ey ",
    "wpp plc", "omnicom", "publicis", "interpublic group", "dentsu",
    "randstad", "adecco", "manpowergroup",
    # airlines / logistics giants
    "united airlines", "delta air lines", "american airlines", "southwest airlines",
    "fedex", "united parcel service", "ups inc", "dhl",
]

CJK_RE = re.compile(r"[一-鿿぀-ヿ가-힣]")

# domains that map 1:1 to a blocklisted company but might not string-match the name
DOMAIN_BLOCKLIST = {
    "shopify.com", "microsoft.com", "google.com", "amazon.com", "apple.com",
    "meta.com", "facebook.com", "walmart.com", "target.com", "costco.com",
    "pepsico.com", "coca-colacompany.com", "coca-cola.com", "pg.com",
    "unilever.com", "nestle.com", "jnj.com", "loreal.com", "esteelauder.com",
    "accenture.com", "deloitte.com", "mckinsey.com", "pwc.com", "kpmg.com",
    "ey.com", "salesforce.com", "oracle.com", "sap.com", "ibm.com", "intel.com",
    "cisco.com", "adobe.com", "hubspot.com", "servicenow.com", "workday.com",
    "atlassian.com", "snowflake.com", "palantir.com", "nvidia.com", "dell.com",
    "hp.com", "vmware.com", "twilio.com", "stripe.com", "squareup.com",
    "paypal.com", "docusign.com", "zoom.us", "slack.com", "okta.com",
    "datadoghq.com", "mongodb.com", "elastic.co", "cloudflare.com", "akamai.com",
}


def normalize(name):
    return re.sub(r"[^a-z0-9&' ]", " ", (name or "").lower()).strip()


def is_blocked(company_name, domain):
    norm = normalize(company_name)
    if CJK_RE.search(company_name or "") or CJK_RE.search(domain or ""):
        return True, "cjk_name_or_domain"
    dom = (domain or "").lower().strip()
    if dom in DOMAIN_BLOCKLIST:
        return True, f"domain_blocklist:{dom}"
    for kw in NAME_BLOCKLIST:
        if kw in f" {norm} ":
            return True, f"name_blocklist:{kw.strip()}"
    return False, ""


def main():
    if len(sys.argv) != 3:
        print("Usage: python screen_leadership_export.py <input.csv> <outdir>")
        sys.exit(1)

    in_path, outdir = sys.argv[1], Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(in_path, dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]
    company_col = "Company Table Data"
    domain_col = "Company Domain"

    total_contacts = len(df)

    # unique companies keyed by normalized name (fall back to domain if name blank)
    df["_norm_company"] = df[company_col].map(normalize)
    df["_company_key"] = df["_norm_company"].where(
        df["_norm_company"] != "", df[domain_col].str.lower()
    )

    companies = (
        df.groupby("_company_key")
        .agg(
            company_name=(company_col, "first"),
            domain=(domain_col, "first"),
            contact_count=(company_col, "size"),
        )
        .reset_index()
    )

    verdicts = companies.apply(
        lambda r: is_blocked(r["company_name"], r["domain"]), axis=1
    )
    companies["blocked"] = [v[0] for v in verdicts]
    companies["block_reason"] = [v[1] for v in verdicts]

    blocked_keys = set(companies.loc[companies["blocked"], "_company_key"])
    companies_blocked = companies[companies["blocked"]].drop(columns=["_company_key"])
    companies_kept = companies[~companies["blocked"]].drop(columns=["_company_key", "blocked", "block_reason"])

    contacts_blocked = df[df["_company_key"].isin(blocked_keys)].drop(columns=["_norm_company", "_company_key"])
    contacts_kept = df[~df["_company_key"].isin(blocked_keys)].drop(columns=["_norm_company", "_company_key"])

    companies_blocked.sort_values("contact_count", ascending=False).to_csv(
        outdir / "companies_blocked.csv", index=False, quoting=csv.QUOTE_MINIMAL
    )
    companies_kept.sort_values("contact_count", ascending=False).to_csv(
        outdir / "companies_kept.csv", index=False, quoting=csv.QUOTE_MINIMAL
    )
    contacts_blocked.to_csv(outdir / "contacts_blocked.csv", index=False, quoting=csv.QUOTE_MINIMAL)
    contacts_kept.to_csv(outdir / "contacts_kept.csv", index=False, quoting=csv.QUOTE_MINIMAL)

    reason_counts = Counter(
        r.split(":")[0] for r in companies.loc[companies["blocked"], "block_reason"]
    )

    print(f"Total contacts in:        {total_contacts}")
    print(f"Unique companies:         {len(companies)}")
    print(f"Companies blocked:        {len(companies_blocked)}  ({reason_counts.get('name_blocklist', 0)} name, "
          f"{reason_counts.get('domain_blocklist', 0)} domain, {reason_counts.get('cjk_name_or_domain', 0)} cjk)")
    print(f"Companies kept:           {len(companies_kept)}")
    print(f"Contacts blocked:         {len(contacts_blocked)}")
    print(f"Contacts kept:            {len(contacts_kept)}")


if __name__ == "__main__":
    main()
