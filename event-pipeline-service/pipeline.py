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


def normalize_phone(p):
    p = (p or "").strip()
    if not p:
        return None
    return p if p.startswith("+") else "+" + p


def process_row(row):
    """row: dict from the Contact Import sheet tab (template column names).
    Returns a dict of result columns to write back to the sheet."""
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

    # ---- STEP 2: check HubSpot (dedupe) ----
    existing_contact = hubspot_client.find_contact_by_email(email) if email else None
    existing_company = hubspot_client.find_company_by_domain(domain) if domain else None

    # ---- STEP 3: enrich, only now that we know this row is worth it ----
    enrichment_note = ""
    if not email and not existing_contact:
        found_email, confidence = prospeo_client.find_email(first_name, last_name, domain)
        if found_email:
            email = found_email
            enrichment_note = f"Email found via Prospeo ({confidence})"
            # re-check HubSpot now that we have an email we didn't have before
            existing_contact = hubspot_client.find_contact_by_email(email)

    # ---- STEP 4: round robin (only for genuinely new companies without a pod) ----
    company_id = None
    if existing_company:
        company_id = existing_company["id"]
        current_pod = (existing_company["properties"].get("pod") or "").strip()
        if not current_pod:
            pod = hubspot_client.least_loaded_pod()
            owner = hubspot_client.least_loaded_owner_in_pod(pod)
            hubspot_client.update_company(company_id, {"pod": pod, "sdr_owner": owner})
        else:
            owner = (existing_company["properties"].get("sdr_owner") or "").strip()
    elif company_name:
        pod = hubspot_client.least_loaded_pod()
        owner = hubspot_client.least_loaded_owner_in_pod(pod)
        company_id = hubspot_client.create_company(company_name, domain, pod, owner)
    else:
        owner = None

    # ---- STEP 5: push contact ----
    if existing_contact:
        contact_id = existing_contact["id"]
        props = {}
        if not (existing_contact.get("jobtitle") or "").strip() and title:
            props["jobtitle"] = title
        if not (existing_contact.get("phone") or "").strip():
            ph = normalize_phone(phone or mobile)
            if ph:
                props["phone"] = ph
        if not (existing_contact.get("hs_linkedin_url") or "").strip() and linkedin:
            props["hs_linkedin_url"] = linkedin
        if not (existing_contact.get("hs_lead_status") or "").strip():
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

    return {
        "Pipeline Status": "Pushed",
        "ICP Verdict": "PASS",
        "Already in HubSpot?": already_existed,
        "HubSpot Contact ID": contact_id,
        "HubSpot Company ID": company_id or "",
        "Notes": enrichment_note,
    }
