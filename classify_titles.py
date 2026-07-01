#!/usr/bin/env python3
"""
Classify job titles for Levanta's affiliate/creator marketing B2B campaign.

Usage:
    python3 classify_titles.py input.csv output.csv

Input CSV must have a "Job Title" column (adjust JOB_TITLE_COL below if different).
Output CSV = input CSV + verdict + reason columns.
"""

import csv
import re
import sys

JOB_TITLE_COL = "Job Title"

# --- Qualifying: specific function/role keywords (checked as whole-word/phrase matches) ---
QUALIFY_PATTERNS = [
    r"co-?founder", r"\bfounder\b", r"\bowner\b", r"\bceo\b", r"\bpresident\b",
    r"managing partner", r"\bproprietor\b", r"\bentrepreneur\b", r"self-?employed",
    r"managing member", r"e-?commerce", r"\bdtc\b", r"\bd2c\b", r"direct-to-consumer",
    r"\bgrowth\b", r"\bmarketing\b", r"\bcmo\b", r"\bbrand\b", r"\bdigital\b",
    r"\baffiliate\b", r"\binfluencer\b", r"partnerships?\b", r"partner marketing",
    r"performance marketing", r"paid media", r"paid social", r"\bacquisition\b",
    r"\bcommerce\b", r"online store", r"\bamazon\b", r"\bmarketplace\b",
    r"\bstrategy\b", r"strategic", r"\brevenue\b", r"\bretention\b", r"\blifecycle\b",
    r"\bcro\b", r"\bcoo\b", r"chief executive officer", r"chief operating officer",
    r"general manager", r"business development",
    r"\bchannel\b", r"\bmedia\b", r"\bshopify\b", r"web store", r"online sales",
    r"digital commerce", r"multichannel", r"omnichannel", r"\bcreator\b",
    r"platform manager", r"email marketing", r"sms marketing", r"demand gen",
    r"customer acquisition", r"social media", r"content marketing", r"content strategy",
    r"marketing operations", r"product marketing", r"go-to-market", r"\bgtm\b",
    r"\bwalmart\b", r"chief revenue", r"chief marketing", r"chief growth",
    r"chief strategy", r"chief commercial", r"\bprincipal\b",
]

# --- Disqualifying: specific function keywords (checked as whole-word/phrase matches) ---
DISQUALIFY_PATTERNS = [
    r"\bfinance\b", r"financial", r"\baccounting\b", r"\bcontroller\b", r"\bcfo\b",
    r"\btax\b", r"\bpayroll\b", r"\baudit", r"\btreasury\b",
    r"information technology", r"\bit\b", r"\bsoftware\b", r"\bengineer", r"\bdeveloper\b",
    r"devops", r"\bsystems?\b", r"\bnetwork\b", r"\bsecurity\b", r"cybersecurity",
    r"human resources", r"\bhr\b", r"\brecruiter\b", r"recruiting", r"\btalent\b",
    r"\blegal\b", r"\bcounsel\b", r"\bcompliance\b", r"\blogistics\b", r"supply chain",
    r"\bwarehouse\b", r"fulfillment", r"procurement", r"purchasing",
    r"customer service", r"customer support", r"call center", r"\bwholesale\b",
    r"retail sales", r"field sales", r"inside sales", r"store manager",
    r"sales associate", r"\bstylist\b", r"\bcashier\b", r"\bdesigner\b", r"\bgraphic\b",
    r"photographer", r"videographer", r"copywriter", r"\beditor", r"\bux\b", r"\bui\b",
    r"manufacturing", r"\bproduction\b", r"r&d", r"research and development",
    r"\bquality\b", r"executive assistant", r"administrative assistant",
    r"\breceptionist\b", r"\bintern\b", r"\btrainee\b", r"apprentice",
    r"brand ambassador", r"\bambassador\b", r"assistant manager", r"\binvestor\b",
    r"board director", r"board member", r"sustainability", r"government relations",
    r"\bcoach\b", r"school principal", r"assistant principal", r"vice principal",
    r"philanthropy", r"advancement", r"major gift", r"donor relations", r"\bdonor\b",
    r"fundrais", r"\bmembership\b", r"\badmissions\b", r"visitor services",
    r"community outreach", r"housekeeping", r"catering", r"culinary", r"\bchef\b",
    r"hospitality", r"food and beverage", r"\bticketing\b", r"corporate communications",
    r"internal communications", r"public relations", r"\bpublicity\b",
    r"\badministration\b", r"\bfacilities\b", r"\bmaintenance\b", r"\btraining\b",
    r"client services", r"client success", r"client relations", r"technical support",
    r"project management", r"program management", r"\bresearch\b", r"construction",
    r"distribution", r"collections", r"technology officer", r"technical officer",
    r"chief technology", r"chief information", r"chief science", r"chief data",
    r"diversity", r"\bequity\b", r"\binclusion\b", r"chief impact officer",
    r"\bimpact officer\b", r"tour guide", r"\bzoning\b", r"\bplumber\b",
    r"\belectrician\b",
]

BLUE_COLLAR_OPERATOR = re.compile(
    r"\b(machine|forklift|cnc|press|plant)\s+operator\b", re.IGNORECASE
)

QUALIFY_RE = re.compile("|".join(QUALIFY_PATTERNS), re.IGNORECASE)
DISQUALIFY_RE = re.compile("|".join(DISQUALIFY_PATTERNS), re.IGNORECASE)
BUSINESS_DEV_RE = re.compile(r"business development", re.IGNORECASE)
MARKETING_COMMS_RE = re.compile(r"marketing communications?", re.IGNORECASE)
SCHOOL_PRINCIPAL_RE = re.compile(
    r"(school|middle school|high school|assistant principal|vice principal)", re.IGNORECASE
)


def classify(title):
    if not title or not title.strip():
        return "PASS", "Empty/unknown title, default pass"

    t = title.strip()

    if BLUE_COLLAR_OPERATOR.search(t):
        return "FAIL", "Blue-collar operator role"

    if "principal" in t.lower() and SCHOOL_PRINCIPAL_RE.search(t):
        return "FAIL", "School principal, education context"

    # Marketing Communications overrides the Communications-family disqualifiers
    if MARKETING_COMMS_RE.search(t):
        return "PASS", "Marketing Communications, marketing function"

    qualify_match = QUALIFY_RE.search(t)
    disqualify_match = DISQUALIFY_RE.search(t)

    if disqualify_match and not qualify_match:
        return "FAIL", f"'{disqualify_match.group(0)}' disqualifying function"

    if disqualify_match and qualify_match:
        # Primary-function tie-break: whichever keyword appears first in the
        # title wins, since that's usually the lead/primary role descriptor.
        if qualify_match.start() <= disqualify_match.start():
            return "PASS", f"'{qualify_match.group(0)}' qualifying function (primary)"
        return "FAIL", f"'{disqualify_match.group(0)}' disqualifying function (primary)"

    if qualify_match:
        return "PASS", f"'{qualify_match.group(0)}' qualifying function"

    return "PASS", "Ambiguous title, default pass per rule 7"


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 classify_titles.py input.csv output.csv")
        sys.exit(1)

    in_path, out_path = sys.argv[1], sys.argv[2]

    with open(in_path, newline="", encoding="utf-8") as f_in:
        reader = csv.DictReader(f_in)
        fieldnames = list(reader.fieldnames) + ["verdict", "reason"]
        rows = list(reader)

    with open(out_path, "w", newline="", encoding="utf-8") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            verdict, reason = classify(row.get(JOB_TITLE_COL, ""))
            row["verdict"] = verdict
            row["reason"] = reason
            writer.writerow(row)

    print(f"Classified {len(rows)} rows -> {out_path}")


if __name__ == "__main__":
    main()
