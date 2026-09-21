"""
ICP title include/exclude lists for the Seamless flow, as supplied by Ivan
2026-09-18.

Why this file exists separately from classify_titles.py: that module is the
pipeline's own regex gate, tuned over time and used by the live enrichment
path. This one is the Seamless-specific list and is kept dependency-free (no
config, no .env) so any script can import it, same posture as icp_prompt.py.

HOW THESE ARE APPLIED -- important, and not how the spec assumed:

Seamless's search/contacts endpoint caps `jobTitle` at 10 entries and offers
NO title-exclusion parameter at all ("-" exclusion exists only for location
and industry-code filters). So neither list can be handed to the API whole:

  * INCLUDE is chunked into batches of <=10 (SEARCH_BATCHES) and the batches
    are unioned client-side. Search is free, so full coverage costs nothing
    but wall time. Batch DOMAINS too -- companyDomain accepts up to 100 per
    call, so 100 companies x 12 title batches is 12 calls, not 1,200.
  * EXCLUDE is applied locally, by title_passes(), against the `title` field
    that search already returns for free -- i.e. BEFORE any credit is spent
    on research. Filtering after research would mean paying for contacts we
    then discard.

The include/exclude tie-break mirrors classify_titles.py rule 7: when both an
include and an exclude term match, the earlier non-overlapping span wins, so
"Marketplace Operations" passes while "Operations Manager" does not.
"""
import re

INCLUDE = [
    "Founder", "Co-Founder", "Owner", "CEO", "President",
    "Ecommerce", "eCommerce", "E-Commerce", "E-com", "Ecom", "eCom",
    "DTC", "Direct-to-Consumer", "D2C",
    "Growth", "Marketing", "CMO", "Brand", "Digital",
    # Spelled-out C-suite. The matcher is word-boundary anchored, so "CEO"
    # does NOT match "Chief Executive Officer" -- that dropped 8 CEOs/COOs on
    # a 100-company run, each of them the best contact at their company.
    "Chief Executive Officer", "Chief Operating Officer",
    "Chief Marketing Officer", "Chief Revenue Officer",
    "Chief Commercial Officer", "Chief Digital Officer",
    "Chief Brand Officer", "Chief Growth Officer",
    "Managing Director",
    "Affiliate", "Influencer", "Partnership", "Partnerships",
    "Partner Marketing", "Performance Marketing", "Paid Media", "Paid Social",
    "Acquisition", "Online Store",
    "Amazon Sales", "Amazon Marketplaces", "Amazon Marketing", "Amazon Stores",
    "Marketplace", "Marketplaces", "Digital Marketplace",
    "Shopify", "Shopify Plus", "Web Store", "Webstore",
    "Online Sales", "Online Marketing", "Web Marketing",
    "Digital Commerce", "Digital Sales", "Digital Storefront",
    "Marketplace Manager", "Marketplace Operations",
    "Amazon Manager", "Amazon Specialist", "Amazon Seller",
    "Vendor Manager", "Vendor Central", "Seller Central", "Brand Registry",
    "Walmart", "Walmart Marketplace", "Walmart Connect", "Walmart Seller",
    "National Account Manager", "Key Account Manager",
    "National Accounts", "Key Accounts",
    "Category Manager", "Amazon Category Manager", "Amazon Brand Manager",
    "National Brand Manager", "Regional Brand Manager",
    "E-Commerce Brand Manager", "Marketplace Brand Manager",
    "Channel Manager", "Channel Marketing",
    "Multichannel", "Multi-Channel", "Omnichannel", "Omni-Channel",
    "DTC Marketing", "DTC Operations", "DTC Manager",
    "Growth Manager", "Growth Marketing",
    "Ecommerce Manager", "Ecommerce Director", "Ecommerce Operations",
    "Ecommerce Marketing", "E-commerce Manager",
    "Influencer Marketing", "Influencer Partnerships",
    "Affiliate Manager", "Affiliate Marketing",
    "Social Media Marketing", "Social Media Director",
    "Content Marketing", "Content Strategy", "Integrated Marketing",
    "Marketing Operations", "Marketing Manager", "Marketing Director",
    "Brand Manager", "Brand Marketing", "Brand Director", "Brand Lead",
    "Product Marketing", "Go-to-Market", "GTM",
    "Managing Partner", "Principal", "Self Employed", "Entrepreneur",
    "Proprietor", "Managing Principal", "Founding Principal",
]

EXCLUDE = [
    "Finance", "Accounting", "Accountant", "Controller", "CFO",
    "Chief Financial Officer", "Bookkeeper", "FP&A", "Tax", "Payroll",
    "Audit", "Treasury",
    "IT", "Information Technology", "Software", "Engineer", "Engineering",
    "Developer", "DevOps", "Systems", "Network", "Security", "QA",
    "Digital Transformation", "Digital Capabilities",
    "HR", "HRIS", "HRBP", "Human Resources", "Recruiter", "Recruiting",
    "Recruitment", "Talent", "People",
    "Legal", "Counsel", "Attorney", "Compliance", "Paralegal", "Employer Brand",
    "Logistics", "Supply Chain", "Transportation", "Import-Export",
    "Warehouse", "Fulfillment", "Procurement", "Sourcing", "Inventory",
    "Purchasing",
    "Customer Service", "Customer Support", "Customer Success", "Call Center",
    "Wholesale", "Retail Sales", "Field Sales", "Inside Sales",
    "Store Manager", "Sales Associate", "Stylist", "Cashier",
    "Designer", "Graphic", "Photographer", "Videographer", "Copywriter",
    "Content Writer", "Editor",
    "Product Manager", "Product Director", "VP Product", "Head of Product",
    "AI Product", "Product Management", "UX", "UI",
    "Manufacturing", "Production", "R&D", "Quality", "Formulator",
    "Executive Assistant", "Administrative", "Receptionist", "Office Manager",
    "Operations Manager",
    "Intern", "Trainee", "Apprentice", "Ambassador", "Brand Ambassador",
    "Assistant Manager",
    "Investor", "Board Director", "Board Member", "Independent Director",
    "Non-Executive Director",
    "Sustainability", "Government Relations", "Social Media Warrior", "Coach",
    "Independent Distributor", "Director of Operations",
    "School Principal", "Assistant Principal", "Vice Principal",
    "Philanthropy", "Advancement", "Major Gifts", "Individual Giving",
    "Institutional Advancement", "Donor Relations", "Membership",
    "Admissions", "Visitor Services", "Community Outreach",
    "Community Engagement",
    "Housekeeping", "Catering", "Culinary", "Chef", "Hospitality",
    "Food and Beverage", "Coffee", "Ticketing", "Ticket Sales",
    "Corporate Communications", "Internal Communications",
    "Chief Communications Officer", "External Affairs", "Public Relations",
    "Publicity",
    "Administration", "Facilities", "Maintenance", "Training",
    "Client Services", "Client Success", "Client Relations", "Client Director",
    "Customer Relations", "Customer Business Manager", "Technical Support",
    "Business Intelligence",
    "Project Management", "Program Management", "Program Manager",
    "Special Projects", "Programs", "Programming",
    "Research", "Research and Development", "A&R",
    "Construction", "Implementation", "Distribution", "Collections",
    "Architect",
    "Chief AI Officer", "VP of AI", "Head of AI", "AI Engineer", "AI Lead",
    "Artificial Intelligence", "Machine Learning", "Data Science",
    "Analytics", "Insights",
    "Chief Technology Officer", "CTO", "Chief Product Officer",
    "Chief Diversity Officer", "Diversity and Inclusion",
    "Corporate Development", "Corporate Strategy",
    "Mergers and Acquisitions", "Design Director", "Project Controls",
    "Pricing", "Sales Operations", "SalesOps", "Sales Excellence",
    "Rewards", "Total Rewards", "Compensation and Benefits",
    "Creative Operations", "Business Unit", "Business Units",
    "VP of Sales", "Director of Sales", "Sales Director",
    "Senior Director Sales", "VP Sales",
    "Advisor", "Consultant",
    "Police", "Fire", "Military", "Law Enforcement", "Judge", "Clergy",
    "Chaplain",
]

# Seniority and department vocabularies are Seamless enums, capped at 5 each.
# Department is deliberately EMPTY: verified 2026-09-18 that Seamless files
# owner-operators under department "Other", so a Sales/Marketing/Operations
# filter removes exactly the founders this ICP targets (it took a 15-match
# result to 0). Leave it empty unless that changes.
TARGET_SENIORITY = ["C-Level", "VP", "Director", "Manager"]
TARGET_DEPARTMENT = []

MAX_TITLES_PER_CALL = 10       # Seamless jobTitle cap
MAX_DOMAINS_PER_CALL = 100     # Seamless companyDomain cap


# Conditional includes: title terms that only qualify below a headcount
# ceiling. Added 2026-09-18 on Ivan's instruction.
#
# "General Manager" at a sub-50-employee brand is usually the operator who
# owns the channel -- Marc Conaway at Research and Design (30 employees) is
# the worked example: DeBounce-valid email, 92%-confidence mobile. At a larger
# company the same words far more often mean a regional or site operations
# role with no say over affiliate, so the term is NOT applied there.
#
# These terms ARE included in the search net (search_batches) because recall
# has to happen at the API, where no headcount-per-title filter exists; the
# headcount gate is then applied locally by title_passes() against the
# employeeCount that search already returns for free.
CONDITIONAL_INCLUDE = {
    "General Manager": 50,     # qualifies only when employeeCount < 50
}


def _compile(terms):
    """Word-boundary, case-insensitive, longest-first so a multi-word term is
    tested before the single word it contains."""
    out = []
    for t in sorted(set(terms), key=len, reverse=True):
        esc = re.escape(t).replace(r"\ ", r"\s+").replace(r"\-", r"[\-\s]?")
        out.append((t, re.compile(r"(?<![A-Za-z0-9])" + esc + r"(?![A-Za-z0-9])",
                                  re.IGNORECASE)))
    return out


_INC = _compile(INCLUDE)
_EXC = _compile(EXCLUDE)
_COND = _compile(list(CONDITIONAL_INCLUDE))


def _first(patterns, title):
    best = None
    for term, rx in patterns:
        m = rx.search(title)
        if m and (best is None or m.start() < best[1].start()):
            best = (term, m)
    return best


def _overlap(a, b):
    return a.start() < b.end() and b.start() < a.end()


def title_passes(title, employee_count=None):
    """Apply the full include/exclude lists to one title string.

    `employee_count` comes from the Seamless search result (free) and gates
    CONDITIONAL_INCLUDE terms. Returns (bool, reason).

    Rules, in order:
      1. a primary INCLUDE term matches      -> candidate
      2. else a CONDITIONAL_INCLUDE term matches AND employee_count is known
         and below that term's ceiling       -> candidate
      3. no candidate                        -> FAIL (not in ICP)
      4. candidate + no EXCLUDE match        -> PASS
      5. both match: earlier non-overlapping span wins, so
         "Marketplace Operations" passes while "Operations Manager" fails
      6. empty title                         -> FAIL, deliberately unlike
         classify_titles.py rule 7. That module defaults an unknown title to
         PASS so a live lead is never silently dropped; this pilot measures
         accuracy, and an unverifiable title would contaminate coverage.

    Unknown headcount does NOT satisfy a conditional term. "Small" is a claim
    we either have evidence for or we don't, and guessing would reintroduce
    exactly the ambiguous General Managers the ceiling exists to exclude.
    """
    t = (title or "").strip()
    if not t:
        return False, "empty title"

    inc = _first(_INC, t)
    cond_note = ""
    if not inc:
        cand = _first(_COND, t)
        if not cand:
            return False, "no ICP title term matched"
        ceiling = CONDITIONAL_INCLUDE[cand[0]]
        if employee_count is None or str(employee_count).strip() == "":
            return False, f"conditional:{cand[0]} needs headcount, none known"
        try:
            n = int(float(employee_count))
        except (TypeError, ValueError):
            return False, f"conditional:{cand[0]} unparseable headcount {employee_count!r}"
        if n >= ceiling:
            return False, f"conditional:{cand[0]} rejected at {n} employees (>= {ceiling})"
        inc = cand
        cond_note = f" [small company: {n} < {ceiling}]"

    exc = _first(_EXC, t)
    if not exc:
        return True, f"include:{inc[0]}{cond_note}"
    # A conditional term is a FALLBACK, so it does not get the span tie-break
    # that primary terms get. Any exclude match beats it. Without this,
    # "General Manager, Logistics" at a small company would pass on span
    # order alone, and a logistics GM is not the channel owner we want.
    if cond_note:
        return False, f"conditional:{inc[0]} loses to exclude:{exc[0]}"
    if inc[1].start() < exc[1].start() and not _overlap(inc[1], exc[1]):
        return True, f"include:{inc[0]} before exclude:{exc[0]}{cond_note}"
    return False, f"exclude:{exc[0]}"


def search_batches():
    """INCLUDE chunked to the API's 10-title ceiling."""
    # Conditional terms go into the SEARCH net so their contacts are retrieved
    # at all; the headcount gate is applied afterwards by title_passes().
    uniq = list(dict.fromkeys(list(INCLUDE) + list(CONDITIONAL_INCLUDE)))
    return [uniq[i:i + MAX_TITLES_PER_CALL]
            for i in range(0, len(uniq), MAX_TITLES_PER_CALL)]
