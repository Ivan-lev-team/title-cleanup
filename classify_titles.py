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

# Specific function/role keywords that qualify a title on their own.
SPECIFIC_QUALIFY_PATTERNS = [
    r"\bMarketplace\s+Coordinator", r"\bInfluencer\s+Partnerships", r"\bMarketplace\s+Operations",
    r"\bMarketplace\s+Specialist", r"\bSocial\s+Media\s+Marketing", r"\bPerformance\s+Marketing",
    r"\bSocial\s+Media\s+Director", r"\bInfluencer\s+Marketing", r"\bIntegrated\s+Marketing",
    r"\bBusiness\s+Development", r"\bEcommerce\s+Operations", r"\bEcommerce\s+Specialist",
    r"\bCreator\s+Partnerships", r"\bMarketing\s+Operations", r"\bSocial\s+Media\s+Manager",
    r"\bCustomer\s+Acquisition", r"\bAffiliate\s+Marketing", r"\bEcommerce\s+Marketing",
    r"\bMarketplace\s+Manager", r"\bMarketplace\s+Analyst", r"\bManaging\s+Principal",
    r"\bMarketing\s+Director", r"\bFounding\s+Principal", r"\bDigital\s+Storefront",
    r"\bE\-commerce\s+Manager", r"\bEcommerce\s+Director", r"\bDirect\-to\-Consumer",
    r"\bChannel\s+Marketing", r"\bDemand\s+Generation", r"\bAffiliate\s+Manager",
    r"\bMarketing\s+Manager", r"\bAmazon\s+Specialist", r"\bContent\s+Marketing",
    r"\bEcommerce\s+Manager", r"\bCreator\s+Marketing", r"\bProduct\s+Marketing",
    r"\bPartner\s+Marketing", r"\bContent\s+Strategy", r"\bManaging\s+Partner",
    r"\bGrowth\s+Marketing", r"\bPlatform\s+Manager", r"\bOnline\s+Marketing",
    r"\bDigital\s+Commerce", r"\bPerformance\s+Lead", r"\bBrand\s+Marketing",
    r"\bGeneral\s+Manager", r"\bManaging\s+Member", r"\bSocial\s+Commerce",
    r"\bChannel\s+Manager", r"\bE\-commerce\s+Lead", r"\bRetention\s+Lead",
    r"\bDTC\s+Operations", r"\bEcommerce\s+Lead", r"\bAmazon\s+Manager",
    r"\bLifecycle\s+Lead", r"\bMarketing\s+Lead", r"\bAffiliate\s+Lead",
    r"\bBrand\s+Director", r"\bGrowth\s+Manager", r"\bMulti\-Channel",
    r"\bDigital\s+Sales", r"\bDTC\s+Marketing", r"\bAmazon\s+Seller",
    r"\bBrand\s+Manager", r"\bWeb\s+Marketing", r"\bSMS\s+Marketing",
    r"\bSelf\s+Employed", r"\bOnline\s+Sales", r"\bRevenue\s+Lead",
    r"\bOmni\-Channel", r"\bMultichannel", r"\bGo\-to\-Market",
    r"\bPartnerships", r"\bOnline\s+Store", r"\bShopify\s+Plus",
    r"\bDigital\s+Lead", r"\bEntrepreneur", r"\bOmnichannel",
    r"\bAcquisition", r"\bDTC\s+Manager", r"\bMarketplace",
    r"\bPartnership", r"\bPaid\s+Social", r"\bGrowth\s+Lead",
    r"\bCo\-Founder", r"\bInfluencer", r"\bBrand\s+Lead",
    r"\bConversion", r"\bE\-Commerce", r"\bPaid\s+Media",
    r"\bProprietor", r"\bDemand\s+Gen", r"\bRetention",
    r"\beCommerce", r"\bAffiliate", r"\bWeb\s+Store",
    r"\bEcommerce", r"\bMarketing", r"\bPrincipal",
    r"\bPresident", r"\bLifecycle", r"\bCommerce",
    r"\bWebstore", r"\bShopping", r"\bStrategy",
    r"\bDigital", r"\bShopify", r"\bCreator",
    r"\bFounder", r"\bRevenue", r"\bWalmart",
    r"\bChannel", r"\bGrowth", r"\bAmazon",
    r"\bE\-com", r"\bOwner", r"\bMedia",
    r"\bBrand", r"\beCom", r"\bEcom",
    r"\bCOO", r"\bCEO", r"\bCRO",
    r"\bGTM", r"\bD2C", r"\bDTC",
    r"\bCMO",
    # Spelled-out C-suite forms not literally in the include list but implied
    # by their abbreviations (CEO, COO, CRO, CMO) being present.
    r"\bChief\s+Executive\s+Officer", r"\bChief\s+Operating\s+Officer",
    r"\bChief\s+Revenue\s+Officer", r"\bChief\s+Marketing\s+Officer",
]

# Seniority words that mean nothing on their own (per judgment rule 1) --
# only used to detect that a title has *some* leadership seniority, never
# used to win a tie-break against a specific disqualifying function.
GENERIC_SENIORITY_PATTERNS = [
    r"\bSenior\s+Manager", r"\bVice\s+President", r"\bDirector", r"\bChief", r"\bHead", r"\bVP",
]

DISQUALIFY_PATTERNS = [
    r"\bInstitutional\s+Advancement", r"\bCorporate\s+Communications", r"\bResearch\s+and\s+Development",
    r"\bIndependent\s+Distributor", r"\bInternal\s+Communications", r"\bDirector\s+of\s+Operations",
    r"\bInformation\s+Technology", r"\bDigital\s+Transformation", r"\bBusiness\s+Intelligence",
    r"\bSocial\s+Media\s+Warrior", r"\bGovernment\s+Relations", r"\bCommunity\s+Engagement",
    r"\bExecutive\s+Assistant", r"\bAssistant\s+Principal", r"\bCommunity\s+Outreach",
    r"\bProject\s+Management", r"\bCustomer\s+Relations", r"\bProgram\s+Management",
    r"\bTechnical\s+Support", r"\bFood\s+and\s+Beverage", r"\bIndividual\s+Giving",
    r"\bNational\s+Accounts", r"\bAssistant\s+Manager", r"\bVisitor\s+Services",
    r"\bSpecial\s+Projects", r"\bBrand\s+Ambassador", r"\bPublic\s+Relations",
    r"\bCustomer\s+Service", r"\bExternal\s+Affairs", r"\bCustomer\s+Support",
    r"\bCustomer\s+Success", r"\bSchool\s+Principal", r"\bClient\s+Relations",
    r"\bSales\s+Associate", r"\bHuman\s+Resources", r"\bClient\s+Services",
    r"\bProduct\s+Manager", r"\bDonor\s+Relations", r"\bClient\s+Success",
    r"\bEmployer\s+Brand", r"\bContent\s+Writer", r"\bImplementation",
    r"\bOffice\s+Manager", r"\bBoard\s+Director", r"\bVice\s+Principal",
    r"\bAdministration", r"\bAdministrative", r"\bSustainability",
    r"\bManufacturing", r"\bStore\s+Manager", r"\bHousekeeping",
    r"\bPhotographer", r"\bReceptionist", r"\bDistribution",
    r"\bSupply\s+Chain", r"\bTicket\s+Sales", r"\bRetail\s+Sales",
    r"\bKey\s+Accounts", r"\bBoard\s+Member", r"\bConstruction",
    r"\bVideographer", r"\bPhilanthropy", r"\bInside\s+Sales",
    r"\bCollections", r"\bFulfillment", r"\bMajor\s+Gifts",
    r"\bProgramming", r"\bEngineering", r"\bHospitality",
    r"\bField\s+Sales", r"\bMaintenance", r"\bAdvancement",
    r"\bCall\s+Center", r"\bProcurement", r"\bApprentice",
    r"\bPurchasing", r"\bMembership", r"\bAmbassador",
    r"\bController", r"\bAccounting", r"\bFormulator",
    r"\bCopywriter", r"\bRecruiting", r"\bFacilities",
    r"\bBookkeeper", r"\bAccountant", r"\bAdmissions",
    r"\bCompliance", r"\bProduction", r"\bInventory",
    r"\bLogistics", r"\bDeveloper", r"\bWarehouse",
    r"\bPublicity", r"\bRecruiter", r"\bTicketing",
    r"\bParalegal", r"\bWholesale", r"\bPrograms",
    r"\bResearch", r"\bTreasury", r"\bEngineer",
    r"\bCatering", r"\bAttorney", r"\bSecurity",
    r"\bTraining", r"\bCulinary", r"\bSoftware",
    r"\bDesigner", r"\bInvestor", r"\bStylist",
    r"\bTrainee", r"\bCashier", r"\bNetwork",
    r"\bFinance", r"\bPayroll", r"\bQuality",
    r"\bGraphic", r"\bSystems", r"\bCounsel",
    r"\bDevOps", r"\bPeople", r"\bIntern",
    r"\bTalent", r"\bCoffee", r"\bEditor",
    r"\bLegal", r"\bCoach", r"\bAudit",
    r"\bFP\&A", r"\bChef", r"\bTax",
    r"\bCFO", r"\bR\&D", r"\bA\&R",
    r"\bQA", r"\bUI", r"\bIT",
    r"\bHR", r"\bUX",
    # Spelled-out C-suite forms not literally in the exclude list but implied
    # by their abbreviations (CFO, IT, HR) being present.
    r"\bChief\s+Financial\s+Officer", r"\bChief\s+Technology\s+Officer",
    r"\bChief\s+Information\s+Officer", r"\bChief\s+Human\s+Resources\s+Officer",
    r"\bChief\s+People\s+Officer", r"\bChief\s+Talent\s+Officer",
    r"\bChief\s+Legal\s+Officer", r"\bChief\s+Compliance\s+Officer",
    r"\bChief\s+Security\s+Officer",
]

SPECIFIC_QUALIFY_RE = re.compile("|".join(SPECIFIC_QUALIFY_PATTERNS), re.IGNORECASE)
GENERIC_SENIORITY_RE = re.compile("|".join(GENERIC_SENIORITY_PATTERNS), re.IGNORECASE)
DISQUALIFY_RE = re.compile("|".join(DISQUALIFY_PATTERNS), re.IGNORECASE)


def spans_overlap(a, b):
    return a.start() < b.end() and b.start() < a.end()


def classify(title):
    if not title or not title.strip():
        return "PASS", "Empty/unknown title, default pass"

    t = title.strip()

    specific_match = SPECIFIC_QUALIFY_RE.search(t)
    disqualify_match = DISQUALIFY_RE.search(t)
    generic_match = GENERIC_SENIORITY_RE.search(t)

    if disqualify_match:
        # A specific qualifying function only wins the tie if it appears
        # earlier than (and doesn't overlap) the disqualifying phrase --
        # otherwise the disqualifying phrase is treated as the primary
        # function (this also covers cases like "Director of Operations"
        # or "Digital Transformation" where a qualifying word is either a
        # generic seniority word or textually swallowed by the exclude
        # phrase).
        if (
            specific_match
            and specific_match.start() < disqualify_match.start()
            and not spans_overlap(specific_match, disqualify_match)
        ):
            return "PASS", f"'{specific_match.group(0)}' qualifying function (primary)"
        return "FAIL", f"'{disqualify_match.group(0)}' disqualifying function"

    if specific_match:
        return "PASS", f"'{specific_match.group(0)}' qualifying function"

    if generic_match:
        return "PASS", f"'{generic_match.group(0)}' seniority, no disqualifying function"

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
