"""
Orchestrates the 5-step pipeline for a single sheet row, in the order
established for this project: qualify -> check HubSpot -> enrich -> round
robin -> push. Enrichment only ever runs on rows that already survived the
first two steps, so no Prospeo credits are spent on something that gets
filtered out anyway.
"""
import qualify
import hubspot_client
import prospeo_client
import config

normalize_phone = hubspot_client.normalize_phone

# Company Import tab -> HubSpot company property. "Company Owner" is handled
# separately (needs email->ID resolution), not a straight copy.
COMPANY_FIELD_MAP = {
    "Website URL": "website",
    "Industry": "industry",
    "Company Type": "type",
    "Number of Employees": "numberofemployees",
    "Annual Revenue": "annualrevenue",
    "City": "city",
    "State/Region": "state",
    "Country/Region": "country",
    "LinkedIn Company Page": "linkedin_company_page",
    "Amazon Storefront URL": "amazon_storefront_url",
}


def _build_company_create_props(company_name, domain, company_import_row):
    """Merges the bare name+domain we always have from the Contact Import
    row with the richer fields from the Company Import tab, when that
    company's domain is present there (Company Domain is that tab's
    required dedupe key, so this is a direct dict lookup, not a search)."""
    props = {"name": company_name}
    if domain:
        props["domain"] = domain
    if not company_import_row:
        return props
    for template_col, hs_prop in COMPANY_FIELD_MAP.items():
        value = company_import_row.get(template_col, "").strip()
        if value:
            props[hs_prop] = value
    owner_email = company_import_row.get("Company Owner", "").strip()
    if owner_email:
        owner_id = hubspot_client.find_owner_id_by_email(owner_email)
        if owner_id:
            props["hubspot_owner_id"] = owner_id
    return props


def process_row(row, company_import_by_domain=None):
    """row: dict from the Contact Import sheet tab (template column names).
    company_import_by_domain: {domain: row_dict} from the Company Import
    tab (see sheets_client.get_company_import_by_domain), used to enrich a
    brand-new company beyond the bare name+domain the contact row gives us.
    Returns a dict of result columns to write back to the sheet."""
    company_import_by_domain = company_import_by_domain or {}
    first_name = row.get("First Name", "").strip()
    last_name = row.get("Last Name", "").strip()
    email = row.get("Email", "").strip().lower()
    title = row.get("Job Title", "").strip()
    company_name = row.get("Company Name", "").strip()
    domain = row.get("Company Domain", "").strip().lower()
    linkedin = row.get("LinkedIn URL", "").strip()
    phone = row.get("Phone Number", "").strip()
    mobile = row.get("Mobile Phone Number", "").strip()
    event_name = row.get("Event Name", "").strip()

    # ---- STEP 1: qualify (no network cost beyond one Anthropic call) ----
    title_verdict, title_reason = qualify.title_qualify(title)
    company_verdict, company_reason = qualify.company_icp_judge(company_name, domain)

    if title_verdict == "FAIL" or company_verdict == "FAIL":
        reason = title_reason if title_verdict == "FAIL" else company_reason
        return {
            "Pipeline Status": "Rejected",
            "ICP Verdict": "FAIL",
            "Notes": f"Excluded: {reason}",
        }

    is_agency = company_verdict == "AGENCY"

    # ---- STEP 2: check HubSpot (dedupe + exclude already-known accounts) ----
    existing_contact = hubspot_client.find_contact_by_email(email) if email else None
    existing_company = (
        hubspot_client.find_company_by_domain(domain, company_name) if (domain or company_name) else None
    )

    dedupe_note = ""
    if existing_company and existing_company["properties"].get("_dedupe_method") == "name_exact":
        dedupe_note = (
            f"Matched existing company \"{existing_company['properties'].get('name', '')}\" by name only "
            "-- its domain field didn't match, worth checking for a data-quality issue"
        )

    contact_stage = (existing_contact or {}).get("properties", {}).get("lifecyclestage", "")
    company_stage = (existing_company or {}).get("properties", {}).get("lifecyclestage", "")
    if contact_stage in config.EXCLUDED_LIFECYCLE_STAGES or company_stage in config.EXCLUDED_LIFECYCLE_STAGES:
        stage = contact_stage if contact_stage in config.EXCLUDED_LIFECYCLE_STAGES else company_stage
        return {
            "Pipeline Status": "Skipped",
            "ICP Verdict": "AGENCY" if is_agency else "PASS",
            "Already in HubSpot?": "Yes",
            "Notes": f"Skipped: already an engaged HubSpot account (lifecyclestage={stage}), not treated as a fresh event lead",
        }

    # ---- STEP 3: enrich, only now that we know this row is worth it ----
    enrichment_note = ""
    unverified_email = ""
    if not email and not existing_contact:
        full_name = f"{first_name} {last_name}".strip()
        found = prospeo_client.enrich_person(full_name, company_name, domain, linkedin_url=linkedin)
        if found.get("linkedin_url") and not linkedin:
            linkedin = found["linkedin_url"]
        if found.get("email"):
            if found["status"] == "VERIFIED":
                email = found["email"]
                enrichment_note = "Email found via Prospeo (VERIFIED)"
                # re-check HubSpot now that we have an email we didn't have before
                existing_contact = hubspot_client.find_contact_by_email(email)
            else:
                unverified_email = found["email"]
                enrichment_note = f"Prospeo found an UNVERIFIED email ({found['status'] or 'unverified'}) -- not auto-used"

    # ---- STEP 4: round robin ----
    # Agencies (AGENCY verdict) skip the normal Pod rotation entirely -- they
    # go to the Partnerships team instead, tracked purely via sdr_owner
    # (pod is intentionally left blank; HubSpot's "pod" enumeration has no
    # Partnerships value). Everyone else follows the existing Pod round-robin,
    # only for genuinely new companies / companies without an owner yet --
    # an already-assigned company (pod OR partnership owner already set) is
    # never reassigned.
    company_id = None
    if existing_company:
        company_id = existing_company["id"]
        current_owner = (existing_company["properties"].get("sdr_owner") or "").strip()
        current_pod = (existing_company["properties"].get("pod") or "").strip()
        if is_agency:
            if not current_owner:
                owner = hubspot_client.least_loaded_partnership_owner()
                hubspot_client.update_company(company_id, {"sdr_owner": owner})
            else:
                owner = current_owner
        elif not current_pod:
            pod = hubspot_client.least_loaded_pod()
            owner = hubspot_client.least_loaded_owner_in_pod(pod)
            hubspot_client.update_company(company_id, {"pod": pod, "sdr_owner": owner})
        else:
            owner = current_owner
    elif company_name:
        company_import_row = company_import_by_domain.get(domain)
        props = _build_company_create_props(company_name, domain, company_import_row)
        if is_agency:
            owner = hubspot_client.least_loaded_partnership_owner()
            props["sdr_owner"] = owner
        else:
            pod = hubspot_client.least_loaded_pod()
            owner = hubspot_client.least_loaded_owner_in_pod(pod)
            props["pod"] = pod
            # Company Import's own Company Owner (if present) takes priority
            # over the round-robin SDR owner for hubspot_owner_id -- but
            # sdr_owner (the pod-tracking property) always reflects the
            # round-robin result.
            props["sdr_owner"] = owner
        props.setdefault("hubspot_owner_id", owner)
        company_id = hubspot_client.create_company(props)
    else:
        owner = None

    # ---- STEP 5: push contact ----
    if existing_contact:
        contact_id = existing_contact["id"]
        existing_props = existing_contact.get("properties", {})
        props = {}
        if not (existing_props.get("jobtitle") or "").strip() and title:
            props["jobtitle"] = title
        if not (existing_props.get("phone") or "").strip():
            ph = normalize_phone(phone or mobile)
            if ph:
                props["phone"] = ph
        if not (existing_props.get("hs_linkedin_url") or "").strip() and linkedin:
            props["hs_linkedin_url"] = linkedin
        if not (existing_props.get("hs_lead_status") or "").strip():
            props["hs_lead_status"] = "NEW"
        props["how_did_you_hear_about_us_"] = config.LEAD_SOURCE_VALUE
        if event_name:
            props["how_did_you_hear_about_us___drill_down"] = event_name
        if owner:
            props["hubspot_owner_id"] = owner
        hubspot_client.update_contact(contact_id, props)
        already_existed = "Yes"
    else:
        props = {"firstname": first_name, "lastname": last_name}
        if title:
            props["jobtitle"] = title
        if email:
            props["email"] = email
        ph = normalize_phone(phone or mobile)
        if ph:
            props["phone"] = ph
        if linkedin:
            props["hs_linkedin_url"] = linkedin
        props["hs_lead_status"] = "NEW"
        props["how_did_you_hear_about_us_"] = config.LEAD_SOURCE_VALUE
        if event_name:
            props["how_did_you_hear_about_us___drill_down"] = event_name
        if owner:
            props["hubspot_owner_id"] = owner
        contact_id = hubspot_client.create_contact(props)
        already_existed = "No"

    if company_id:
        hubspot_client.associate_contact_to_company(contact_id, company_id)

    notes = enrichment_note
    if dedupe_note:
        notes = f"{notes} | {dedupe_note}".strip(" |")
    if is_agency:
        notes = f"{notes} | Agency -- routed to Partnerships ({company_reason})".strip(" |")
    if unverified_email:
        notes = f"{notes} | Unverified email for manual review: {unverified_email}".strip(" |")

    return {
        "Pipeline Status": "Pushed",
        "ICP Verdict": "AGENCY" if is_agency else "PASS",
        "Already in HubSpot?": already_existed,
        "HubSpot Contact ID": contact_id,
        "HubSpot Company ID": company_id or "",
        "Notes": notes,
    }
