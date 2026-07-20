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
    r"\bMarketplace\s+Brand\s+Manager\b", r"\bE\-Commerce\s+Brand\s+Manager\b", r"\bNational\s+Account\s+Manager\b",
    r"\bInfluencer\s+Partnerships\b", r"\bAmazon\s+Category\s+Manager\b", r"\bRegional\s+Brand\s+Manager\b",
    r"\bMarketplace\s+Operations\b", r"\bNational\s+Brand\s+Manager\b", r"\bBrand\s+Manager\s+\-\s+Amazon\b",
    r"\bPerformance\s+Marketing\b", r"\bEcommerce\s+Operations\b", r"\bAmazon\s+Brand\s+Manager\b",
    r"\bEcommerce\s+Specialist\b", r"\bBusiness\s+Development\b", r"\bInfluencer\s+Marketing\b",
    r"\bMarketplace\s+Manager\b", r"\bKey\s+Account\s+Manager\b", r"\bDigital\s+Marketplace\b",
    r"\bWalmart\s+Marketplace\b", r"\bEcommerce\s+Marketing\b", r"\bAffiliate\s+Marketing\b",
    r"\bAmazon\s+Marketplaces\b", r"\bEcommerce\s+Director\b", r"\bFounding\s+Principal\b",
    r"\bE\-commerce\s+Manager\b", r"\bManaging\s+Principal\b", r"\bEcommerce\s+Manager\b",
    r"\bAffiliate\s+Manager\b", r"\bPartner\s+Marketing\b", r"\bNational\s+Accounts\b",
    r"\bChannel\s+Marketing\b", r"\bAmazon\s+Specialist\b", r"\bCategory\s+Manager\b",
    r"\bGrowth\s+Marketing\b", r"\bDigital\s+Commerce\b", r"\bPerformance\s+Lead\b",
    r"\bAmazon\s+Marketing\b", r"\bManaging\s+Partner\b", r"\bGeneral\s+Manager\b",
    r"\bChannel\s+Manager\b", r"\bE\-commerce\s+Lead\b", r"\bManaging\s+Member\b",
    r"\bWalmart\s+Connect\b", r"\bBrand\s+Marketing\b", r"\bBrand\s+Registry\b",
    r"\bVendor\s+Manager\b", r"\bWalmart\s+Seller\b", r"\bSeller\s+Central\b",
    r"\bBrand\s+Director\b", r"\bGrowth\s+Manager\b", r"\bAmazon\s+Manager\b",
    r"\bVendor\s+Central\b", r"\bEcommerce\s+Lead\b", r"\bAffiliate\s+Lead\b",
    r"\bAmazon\s+Seller\b", r"\bMulti\-Channel\b", r"\bSelf\s+Employed\b",
    r"\bBrand\s+Manager\b", r"\bAmazon\s+Stores\b", r"\bEntrepreneur\b",
    r"\bKey\s+Accounts\b", r"\bAmazon\s+Sales\b", r"\bMarketplaces\b",
    r"\bMultichannel\b", r"\bPartnerships\b", r"\bOmni\-Channel\b",
    r"\bMarketplace\b", r"\bPartnership\b", r"\bGrowth\s+Lead\b",
    r"\bOmnichannel\b", r"\bProprietor\b", r"\bCo\-Founder\b",
    r"\bBrand\s+Lead\b", r"\bE\-Commerce\b", r"\bMarketing\b",
    r"\bPresident\b", r"\bPrincipal\b", r"\beCommerce\b",
    r"\bAffiliate\b", r"\bEcommerce\b", r"\bStrategy\b",
    r"\bRevenue\b", r"\bFounder\b", r"\bChannel\b",
    r"\bWalmart\b", r"\bDigital\b", r"\bGrowth\b",
    r"\bAmazon\b", r"\bOwner\b", r"\bBrand\b",
    r"\bE\-com\b", r"\bEcom\b", r"\beCom\b",
    r"\bCOO\b", r"\bCEO\b", r"\bCMO\b",
    r"\bCRO\b",
]

GENERIC_SENIORITY_PATTERNS = [
    r"\bVice\s+President\b", r"\bSenior\s+Manager\b", r"\bDirector\b",
    r"\bChief\b", r"\bHead\b", r"\bVP\b",
]

DISQUALIFY_PATTERNS = [
    r"\bChief\s+Communications\s+Officer\b", r"\bCompensation\s+and\s+Benefits\b", r"\bCustomer\s+Business\s+Manager\b",
    r"\bInstitutional\s+Advancement\b", r"\bResearch\s+and\s+Development\b", r"\bCorporate\s+Communications\b",
    r"\bMergers\s+and\s+Acquisitions\b", r"\bChief\s+Technology\s+Officer\b", r"\bArtificial\s+Intelligence\b",
    r"\bDiversity\s+and\s+Inclusion\b", r"\bIndependent\s+Distributor\b", r"\bChief\s+Financial\s+Officer\b",
    r"\bChief\s+Diversity\s+Officer\b", r"\bInternal\s+Communications\b", r"\bNon\-Executive\s+Director\b",
    r"\bInformation\s+Technology\b", r"\bDirector\s+of\s+Operations\b", r"\bDigital\s+Transformation\b",
    r"\bChief\s+Product\s+Officer\b", r"\bBusiness\s+Intelligence\b", r"\bSenior\s+Director\s+Sales\b",
    r"\bCorporate\s+Development\b", r"\bGovernment\s+Relations\b", r"\bDigital\s+Capabilities\b",
    r"\bSocial\s+Media\s+Warrior\b", r"\bIndependent\s+Director\b", r"\bCommunity\s+Engagement\b",
    r"\bAssistant\s+Principal\b", r"\bCreative\s+Operations\b", r"\bExecutive\s+Assistant\b",
    r"\bProduct\s+Management\b", r"\bCommunity\s+Outreach\b", r"\bCorporate\s+Strategy\b",
    r"\bCustomer\s+Relations\b", r"\bProgram\s+Management\b", r"\bOperations\s+Manager\b",
    r"\bProject\s+Management\b", r"\bFood\s+and\s+Beverage\b", r"\bDirector\s+of\s+Sales\b",
    r"\bAssistant\s+Manager\b", r"\bTechnical\s+Support\b", r"\bIndividual\s+Giving\b",
    r"\bCustomer\s+Support\b", r"\bSales\s+Excellence\b", r"\bSpecial\s+Projects\b",
    r"\bProject\s+Controls\b", r"\bPublic\s+Relations\b", r"\bSales\s+Operations\b",
    r"\bBrand\s+Ambassador\b", r"\bVisitor\s+Services\b", r"\bMachine\s+Learning\b",
    r"\bProduct\s+Director\b", r"\bExternal\s+Affairs\b", r"\bCustomer\s+Service\b",
    r"\bClient\s+Relations\b", r"\bSchool\s+Principal\b", r"\bCustomer\s+Success\b",
    r"\bChief\s+AI\s+Officer\b", r"\bClient\s+Services\b", r"\bSales\s+Associate\b",
    r"\bHuman\s+Resources\b", r"\bHead\s+of\s+Product\b", r"\bDesign\s+Director\b",
    r"\bProgram\s+Manager\b", r"\bDonor\s+Relations\b", r"\bProduct\s+Manager\b",
    r"\bClient\s+Director\b", r"\bAdministration\b", r"\bBoard\s+Director\b",
    r"\bImplementation\b", r"\bSales\s+Director\b", r"\bAdministrative\b",
    r"\bContent\s+Writer\b", r"\bBusiness\s+Units\b", r"\bVice\s+Principal\b",
    r"\bClient\s+Success\b", r"\bTransportation\b", r"\bEmployer\s+Brand\b",
    r"\bOffice\s+Manager\b", r"\bSustainability\b", r"\bTotal\s+Rewards\b",
    r"\bBusiness\s+Unit\b", r"\bManufacturing\b", r"\bStore\s+Manager\b",
    r"\bImport\-Export\b", r"\bConstruction\b", r"\bVideographer\b",
    r"\bPhotographer\b", r"\bBoard\s+Member\b", r"\bReceptionist\b",
    r"\bInside\s+Sales\b", r"\bHousekeeping\b", r"\bData\s+Science\b",
    r"\bRetail\s+Sales\b", r"\bTicket\s+Sales\b", r"\bSupply\s+Chain\b",
    r"\bPhilanthropy\b", r"\bDistribution\b", r"\bMajor\s+Gifts\b",
    r"\bAdvancement\b", r"\bFulfillment\b", r"\bAI\s+Engineer\b",
    r"\bCall\s+Center\b", r"\bProgramming\b", r"\bVP\s+of\s+Sales\b",
    r"\bRecruitment\b", r"\bField\s+Sales\b", r"\bCollections\b",
    r"\bHospitality\b", r"\bMaintenance\b", r"\bProcurement\b",
    r"\bEngineering\b", r"\bProduction\b", r"\bRecruiting\b",
    r"\bConsultant\b", r"\bPurchasing\b", r"\bFacilities\b",
    r"\bController\b", r"\bCopywriter\b", r"\bAccountant\b",
    r"\bAmbassador\b", r"\bAccounting\b", r"\bHead\s+of\s+AI\b",
    r"\bFormulator\b", r"\bMembership\b", r"\bAdmissions\b",
    r"\bBookkeeper\b", r"\bApprentice\b", r"\bAI\s+Product\b",
    r"\bVP\s+Product\b", r"\bCompliance\b", r"\bRecruiter\b",
    r"\bTicketing\b", r"\bDeveloper\b", r"\bWholesale\b",
    r"\bInventory\b", r"\bParalegal\b", r"\bWarehouse\b",
    r"\bArchitect\b", r"\bAnalytics\b", r"\bLogistics\b",
    r"\bPublicity\b", r"\bSoftware\b", r"\bResearch\b",
    r"\bVP\s+of\s+AI\b", r"\bSecurity\b", r"\bSourcing\b",
    r"\bTreasury\b", r"\bDesigner\b", r"\bTraining\b",
    r"\bCulinary\b", r"\bInvestor\b", r"\bInsights\b",
    r"\bSalesOps\b", r"\bCatering\b", r"\bPrograms\b",
    r"\bVP\s+Sales\b", r"\bEngineer\b", r"\bAttorney\b",
    r"\bTrainee\b", r"\bNetwork\b", r"\bPayroll\b",
    r"\bCashier\b", r"\bGraphic\b", r"\bAI\s+Lead\b",
    r"\bRewards\b", r"\bQuality\b", r"\bStylist\b",
    r"\bAdvisor\b", r"\bPricing\b", r"\bFinance\b",
    r"\bSystems\b", r"\bCounsel\b", r"\bCoffee\b",
    r"\bTalent\b", r"\bEditor\b", r"\bIntern\b",
    r"\bPeople\b", r"\bDevOps\b", r"\bAudit\b",
    r"\bCoach\b", r"\bLegal\b", r"\bFP\&A\b",
    r"\bChef\b", r"\bA\&R\b", r"\bTax\b",
    r"\bCFO\b", r"\bR\&D\b", r"\bCTO\b",
    r"\bUX\b", r"\bIT\b", r"\bUI\b",
    r"\bHR\b", r"\bQA\b",
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
