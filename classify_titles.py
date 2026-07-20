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
    r"\bMarketplace\s+Brand\s+Manager", r"\bE\-Commerce\s+Brand\s+Manager", r"\bNational\s+Account\s+Manager",
    r"\bInfluencer\s+Partnerships", r"\bAmazon\s+Category\s+Manager", r"\bRegional\s+Brand\s+Manager",
    r"\bMarketplace\s+Operations", r"\bNational\s+Brand\s+Manager", r"\bBrand\s+Manager\s+\-\s+Amazon",
    r"\bPerformance\s+Marketing", r"\bEcommerce\s+Operations", r"\bAmazon\s+Brand\s+Manager",
    r"\bEcommerce\s+Specialist", r"\bBusiness\s+Development", r"\bInfluencer\s+Marketing",
    r"\bMarketplace\s+Manager", r"\bKey\s+Account\s+Manager", r"\bDigital\s+Marketplace",
    r"\bWalmart\s+Marketplace", r"\bEcommerce\s+Marketing", r"\bAffiliate\s+Marketing",
    r"\bAmazon\s+Marketplaces", r"\bEcommerce\s+Director", r"\bFounding\s+Principal",
    r"\bE\-commerce\s+Manager", r"\bManaging\s+Principal", r"\bEcommerce\s+Manager",
    r"\bAffiliate\s+Manager", r"\bPartner\s+Marketing", r"\bNational\s+Accounts",
    r"\bChannel\s+Marketing", r"\bAmazon\s+Specialist", r"\bCategory\s+Manager",
    r"\bGrowth\s+Marketing", r"\bDigital\s+Commerce", r"\bPerformance\s+Lead",
    r"\bAmazon\s+Marketing", r"\bManaging\s+Partner", r"\bGeneral\s+Manager",
    r"\bChannel\s+Manager", r"\bE\-commerce\s+Lead", r"\bManaging\s+Member",
    r"\bWalmart\s+Connect", r"\bBrand\s+Marketing", r"\bBrand\s+Registry",
    r"\bVendor\s+Manager", r"\bWalmart\s+Seller", r"\bSeller\s+Central",
    r"\bBrand\s+Director", r"\bGrowth\s+Manager", r"\bAmazon\s+Manager",
    r"\bVendor\s+Central", r"\bEcommerce\s+Lead", r"\bAffiliate\s+Lead",
    r"\bAmazon\s+Seller", r"\bMulti\-Channel", r"\bSelf\s+Employed",
    r"\bBrand\s+Manager", r"\bAmazon\s+Stores", r"\bEntrepreneur",
    r"\bKey\s+Accounts", r"\bAmazon\s+Sales", r"\bMarketplaces",
    r"\bMultichannel", r"\bPartnerships", r"\bOmni\-Channel",
    r"\bMarketplace", r"\bPartnership", r"\bGrowth\s+Lead",
    r"\bOmnichannel", r"\bProprietor", r"\bCo\-Founder",
    r"\bBrand\s+Lead", r"\bE\-Commerce", r"\bMarketing",
    r"\bPresident", r"\bPrincipal", r"\beCommerce",
    r"\bAffiliate", r"\bEcommerce", r"\bStrategy",
    r"\bRevenue", r"\bFounder", r"\bChannel",
    r"\bWalmart", r"\bDigital", r"\bGrowth",
    r"\bAmazon", r"\bOwner", r"\bBrand",
    r"\bE\-com", r"\bEcom", r"\beCom",
    r"\bCOO", r"\bCEO", r"\bCMO",
    r"\bCRO",
]

# Seniority words that mean nothing on their own (per judgment rule 1) --
# only used to detect that a title has *some* leadership seniority, never
# used to win a tie-break against a specific disqualifying function.
GENERIC_SENIORITY_PATTERNS = [
    r"\bVice\s+President", r"\bSenior\s+Manager", r"\bDirector", r"\bChief", r"\bHead", r"\bVP",
]

DISQUALIFY_PATTERNS = [
    r"\bChief\s+Communications\s+Officer", r"\bCompensation\s+and\s+Benefits", r"\bCustomer\s+Business\s+Manager",
    r"\bInstitutional\s+Advancement", r"\bResearch\s+and\s+Development", r"\bCorporate\s+Communications",
    r"\bMergers\s+and\s+Acquisitions", r"\bChief\s+Technology\s+Officer", r"\bArtificial\s+Intelligence",
    r"\bDiversity\s+and\s+Inclusion", r"\bIndependent\s+Distributor", r"\bChief\s+Financial\s+Officer",
    r"\bChief\s+Diversity\s+Officer", r"\bInternal\s+Communications", r"\bNon\-Executive\s+Director",
    r"\bInformation\s+Technology", r"\bDirector\s+of\s+Operations", r"\bDigital\s+Transformation",
    r"\bChief\s+Product\s+Officer", r"\bBusiness\s+Intelligence", r"\bSenior\s+Director\s+Sales",
    r"\bCorporate\s+Development", r"\bGovernment\s+Relations", r"\bDigital\s+Capabilities",
    r"\bSocial\s+Media\s+Warrior", r"\bIndependent\s+Director", r"\bCommunity\s+Engagement",
    r"\bAssistant\s+Principal", r"\bCreative\s+Operations", r"\bExecutive\s+Assistant",
    r"\bProduct\s+Management", r"\bCommunity\s+Outreach", r"\bCorporate\s+Strategy",
    r"\bCustomer\s+Relations", r"\bProgram\s+Management", r"\bOperations\s+Manager",
    r"\bProject\s+Management", r"\bFood\s+and\s+Beverage", r"\bDirector\s+of\s+Sales",
    r"\bAssistant\s+Manager", r"\bTechnical\s+Support", r"\bIndividual\s+Giving",
    r"\bCustomer\s+Support", r"\bSales\s+Excellence", r"\bSpecial\s+Projects",
    r"\bProject\s+Controls", r"\bPublic\s+Relations", r"\bSales\s+Operations",
    r"\bBrand\s+Ambassador", r"\bVisitor\s+Services", r"\bMachine\s+Learning",
    r"\bProduct\s+Director", r"\bExternal\s+Affairs", r"\bCustomer\s+Service",
    r"\bClient\s+Relations", r"\bSchool\s+Principal", r"\bCustomer\s+Success",
    r"\bChief\s+AI\s+Officer", r"\bClient\s+Services", r"\bSales\s+Associate",
    r"\bHuman\s+Resources", r"\bHead\s+of\s+Product", r"\bDesign\s+Director",
    r"\bProgram\s+Manager", r"\bDonor\s+Relations", r"\bProduct\s+Manager",
    r"\bClient\s+Director", r"\bAdministration", r"\bBoard\s+Director",
    r"\bImplementation", r"\bSales\s+Director", r"\bAdministrative",
    r"\bContent\s+Writer", r"\bBusiness\s+Units", r"\bVice\s+Principal",
    r"\bClient\s+Success", r"\bTransportation", r"\bEmployer\s+Brand",
    r"\bOffice\s+Manager", r"\bSustainability", r"\bTotal\s+Rewards",
    r"\bBusiness\s+Unit", r"\bManufacturing", r"\bStore\s+Manager",
    r"\bImport\-Export", r"\bConstruction", r"\bVideographer",
    r"\bPhotographer", r"\bBoard\s+Member", r"\bReceptionist",
    r"\bInside\s+Sales", r"\bHousekeeping", r"\bData\s+Science",
    r"\bRetail\s+Sales", r"\bTicket\s+Sales", r"\bSupply\s+Chain",
    r"\bPhilanthropy", r"\bDistribution", r"\bMajor\s+Gifts",
    r"\bAdvancement", r"\bFulfillment", r"\bAI\s+Engineer",
    r"\bCall\s+Center", r"\bProgramming", r"\bVP\s+of\s+Sales",
    r"\bRecruitment", r"\bField\s+Sales", r"\bCollections",
    r"\bHospitality", r"\bMaintenance", r"\bProcurement",
    r"\bEngineering", r"\bProduction", r"\bRecruiting",
    r"\bConsultant", r"\bPurchasing", r"\bFacilities",
    r"\bController", r"\bCopywriter", r"\bAccountant",
    r"\bAmbassador", r"\bAccounting", r"\bHead\s+of\s+AI",
    r"\bFormulator", r"\bMembership", r"\bAdmissions",
    r"\bBookkeeper", r"\bApprentice", r"\bAI\s+Product",
    r"\bVP\s+Product", r"\bCompliance", r"\bRecruiter",
    r"\bTicketing", r"\bDeveloper", r"\bWholesale",
    r"\bInventory", r"\bParalegal", r"\bWarehouse",
    r"\bArchitect", r"\bAnalytics", r"\bLogistics",
    r"\bPublicity", r"\bSoftware", r"\bResearch",
    r"\bVP\s+of\s+AI", r"\bSecurity", r"\bSourcing",
    r"\bTreasury", r"\bDesigner", r"\bTraining",
    r"\bCulinary", r"\bInvestor", r"\bInsights",
    r"\bSalesOps", r"\bCatering", r"\bPrograms",
    r"\bVP\s+Sales", r"\bEngineer", r"\bAttorney",
    r"\bTrainee", r"\bNetwork", r"\bPayroll",
    r"\bCashier", r"\bGraphic", r"\bAI\s+Lead",
    r"\bRewards", r"\bQuality", r"\bStylist",
    r"\bAdvisor", r"\bPricing", r"\bFinance",
    r"\bSystems", r"\bCounsel", r"\bCoffee",
    r"\bTalent", r"\bEditor", r"\bIntern",
    r"\bPeople", r"\bDevOps", r"\bAudit",
    r"\bCoach", r"\bLegal", r"\bFP\&A",
    r"\bChef", r"\bA\&R", r"\bTax",
    r"\bCFO", r"\bR\&D", r"\bCTO",
    r"\bUX", r"\bIT", r"\bUI",
    r"\bHR", r"\bQA",
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
        # function (this also covers cases like "Director of Sales" or
        # "Digital Transformation" where a qualifying word is either a
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
