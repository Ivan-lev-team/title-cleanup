"""
Orchestrates the 5-step pipeline for a single sheet row, in the order
established for this project: qualify -> check HubSpot -> enrich -> round
robin -> push. Enrichment only ever runs on rows that already survived the
first two steps, so no Prospeo credits are spent on something that gets
filtered out anyway.
"""
import qualify
import hubspot_client
import enrichment
import config

normalize_phone = hubspot_client.normalize_phone


def _is_customer(record):
    """True if a matched HubSpot contact/company is already a customer -- we
    skip those rather than re-tag existing customers as fresh event leads."""
    if not record:
        return False
    return (record["properties"].get("lifecyclestage") or "").strip().lower() == "customer"

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


def process_row(row, company_import_by_domain=None, marketing_events_by_name=None):
    """row: dict from the Contact Import sheet tab (template column names).
    company_import_by_domain: {domain: row_dict} from the Company Import
    tab (see sheets_client.get_company_import_by_domain), used to enrich a
    brand-new company beyond the bare name+domain the contact row gives us.
    marketing_events_by_name: {event_name_lower: objectId} from
    hubspot_client.list_marketing_events(), used to record marketing-event
    attendance for attribution. Returns a dict of result columns to write back.
    """
    company_import_by_domain = company_import_by_domain or {}
    marketing_events_by_name = marketing_events_by_name or {}
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

    # Verdict -> funnel + Contact Type. Brand (PASS) goes to the Pod/sales
    # rotation; Agency and Tech Partner both go to the Partnerships team.
    is_brand = company_verdict == "PASS"
    is_agency = company_verdict == "AGENCY"
    is_tech = company_verdict == "TECH"
    is_partner = is_agency or is_tech
    icp_verdict = company_verdict  # PASS / AGENCY / TECH (FAIL already returned above)
    contact_type_value = (config.CONTACT_TYPE_AGENCY if is_agency
                          else config.CONTACT_TYPE_TECH if is_tech
                          else config.CONTACT_TYPE_BRAND)

    # ---- STEP 2: check HubSpot (dedupe) ----
    existing_contact = hubspot_client.find_contact_by_email(email) if email else None
    existing_company = hubspot_client.find_company_by_domain(domain) if domain else None

    # Existing customers are skipped outright -- no update, no event re-tag,
    # and (by returning before Step 3) no enrichment credits spent.
    if _is_customer(existing_contact) or _is_customer(existing_company):
        return {
            "Pipeline Status": "Skipped",
            "ICP Verdict": icp_verdict,
            "Already in HubSpot?": "Yes",
            "Notes": "Skipped: existing HubSpot customer",
        }

    # Companies Sales is already actively working (an open deal) are also
    # skipped -- don't re-tag a live opportunity as a fresh event lead.
    if existing_company and hubspot_client.company_has_open_deal(existing_company["id"]):
        return {
            "Pipeline Status": "Skipped",
            "ICP Verdict": icp_verdict,
            "Already in HubSpot?": "Yes",
            "Notes": "Skipped: company has an open deal",
        }

    full_name = f"{first_name} {last_name}".strip()

    # ---- STEP 3: enrich email (waterfall: LeadMagic -> Prospeo) ----
    # Only for rows with no email and no existing contact. Strict: only a
    # verified email is auto-used; an unverified hit is left for manual review.
    enrichment_note = ""
    unverified_email = ""
    enriched_email = ""   # written back to the sheet's Email column when found
    if not email and not existing_contact:
        found = enrichment.find_email(first_name, last_name, full_name, company_name, domain, linkedin)
        if found["linkedin_url"] and not linkedin:
            linkedin = found["linkedin_url"]
        if found["email"]:
            email = found["email"]
            enriched_email = email
            enrichment_note = f"Email found via {found['provider']} (verified)"
            # re-check HubSpot now that we have an email we didn't have before
            existing_contact = hubspot_client.find_contact_by_email(email)
            if _is_customer(existing_contact):
                return {
                    "Pipeline Status": "Skipped",
                    "ICP Verdict": icp_verdict,
                    "Already in HubSpot?": "Yes",
                    "Notes": "Skipped: existing HubSpot customer (matched on enriched email)",
                }
        elif found["unverified_email"]:
            unverified_email = found["unverified_email"]
            enrichment_note = found["unverified_note"]

    # ---- STEP 3.5: enrich mobile (waterfall: Prospeo -> Forager -> LeadMagic) ----
    # Flag-gated (ENRICH_MOBILE) and only for rows with no phone yet whose
    # matched contact (if any) also lacks a phone -- never spend mobile credits
    # on a contact that already has a number.
    enriched_mobile = ""
    mobile_note = ""
    existing_has_phone = bool(existing_contact and (existing_contact["properties"].get("phone") or "").strip())
    if config.ENRICH_MOBILE and not phone and not mobile and not existing_has_phone:
        mob = enrichment.find_mobile(full_name, first_name, last_name, company_name,
                                     domain, linkedin_url=linkedin, email=email or unverified_email)
        if mob["mobile"]:
            enriched_mobile = mob["mobile"]
            mobile_note = f"Mobile found via {mob['provider']}"

    # ---- STEP 4: round robin ----
    # Partners (AGENCY or TECH verdict) skip the normal Pod rotation entirely --
    # they go to the Partnerships team instead, tracked purely via sdr_owner
    # (pod is intentionally left blank; HubSpot's "pod" enumeration has no
    # Partnerships value). Everyone else follows the existing Pod round-robin,
    # only for genuinely new companies / companies without an owner yet --
    # an already-assigned company (pod OR partnership owner already set) is
    # never reassigned.
    company_id = None
    company_has_pod = False   # used by the Qualification rule below
    company_rev_code = ""     # estimated_annual_revenue band code
    revenue_note = ""

    def _revenue_for_brand():
        # Brand-only, pod-gated: enrich revenue only when we don't already have
        # a band and there's a domain to look up. Sets company_rev_code + note.
        nonlocal company_rev_code, revenue_note
        if not (config.ENRICH_REVENUE and is_brand and domain and not company_rev_code):
            return
        res = enrichment.find_revenue_band(domain)
        if res["code"]:
            company_rev_code = res["code"]
            revenue_note = f"Revenue band {res['code']} via {res['provider']} (~${int(res['dollars']):,}/yr)"

    if existing_company:
        company_id = existing_company["id"]
        current_owner = (existing_company["properties"].get("sdr_owner") or "").strip()
        current_pod = (existing_company["properties"].get("pod") or "").strip()
        company_rev_code = (existing_company["properties"].get("estimated_annual_revenue") or "").strip()
        company_has_pod = bool(current_pod)
        if is_partner:
            if not current_owner:
                owner = hubspot_client.least_loaded_partnership_owner()
                hubspot_client.update_company(company_id, {"sdr_owner": owner})
            else:
                owner = current_owner
        elif current_pod:
            owner = current_owner  # already in a pod => already qualified, no revenue enrich
        else:
            # brand not yet in a pod: enrich revenue, then assign a pod
            _revenue_for_brand()
            pod = hubspot_client.least_loaded_pod()
            owner = hubspot_client.least_loaded_owner_in_pod(pod)
            update = {"pod": pod, "sdr_owner": owner}
            if company_rev_code:
                update["estimated_annual_revenue"] = company_rev_code
            hubspot_client.update_company(company_id, update)
            company_has_pod = True
    elif company_name:
        company_import_row = company_import_by_domain.get(domain)
        props = _build_company_create_props(company_name, domain, company_import_row)
        if is_partner:
            owner = hubspot_client.least_loaded_partnership_owner()
            props["sdr_owner"] = owner
        else:
            # new brand: enrich revenue, then assign a pod
            _revenue_for_brand()
            if company_rev_code:
                props["estimated_annual_revenue"] = company_rev_code
            pod = hubspot_client.least_loaded_pod()
            owner = hubspot_client.least_loaded_owner_in_pod(pod)
            props["pod"] = pod
            # Company Import's own Company Owner (if present) takes priority
            # over the round-robin SDR owner for hubspot_owner_id -- but
            # sdr_owner (the pod-tracking property) always reflects the
            # round-robin result.
            props["sdr_owner"] = owner
            company_has_pod = True
        props.setdefault("hubspot_owner_id", owner)
        company_id = hubspot_client.create_company(props)
    else:
        owner = None

    # Qualification (marketing spec): Qualified if a Brand whose company annual
    # revenue is >= $1M (estimated_annual_revenue codes 3/4), OR the company has
    # a Pod; otherwise Disqualified (the property has no "Unqualified" option).
    qualified = (is_brand and company_rev_code in config.QUALIFIED_REVENUE_CODES) or company_has_pod
    qualification_value = "Qualified" if qualified else "Disqualified"

    # ---- STEP 5: push contact ----
    if existing_contact:
        contact_id = existing_contact["id"]
        props = {}
        if not (existing_contact["properties"].get("jobtitle") or "").strip() and title:
            props["jobtitle"] = title
        if not (existing_contact["properties"].get("phone") or "").strip():
            ph = normalize_phone(phone or mobile or enriched_mobile)
            if ph:
                props["phone"] = ph
            mp = normalize_phone(mobile or enriched_mobile)
            if mp:
                props["mobilephone"] = mp
        if not (existing_contact["properties"].get("hs_linkedin_url") or "").strip() and linkedin:
            props["hs_linkedin_url"] = linkedin
        if not (existing_contact["properties"].get("hs_lead_status") or "").strip():
            props["hs_lead_status"] = "NEW"
        # Lead Source: gap-fill only -- never overwrite a known Lead Source
        # (marketing spec). Parent empty => write parent + drill-down.
        if not (existing_contact["properties"].get("how_did_you_hear_about_us_") or "").strip():
            props["how_did_you_hear_about_us_"] = config.LEAD_SOURCE_VALUE
            if event_name:
                props["how_did_you_hear_about_us___drill_down"] = event_name
        # Contact Type + Qualification: gap-fill (ensure set, don't clobber)
        if not (existing_contact["properties"].get("contact_type") or "").strip():
            props["contact_type"] = contact_type_value
        if not (existing_contact["properties"].get(config.QUALIFICATION_PROPERTY) or "").strip():
            props[config.QUALIFICATION_PROPERTY] = qualification_value
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
        ph = normalize_phone(phone or mobile or enriched_mobile)
        if ph:
            props["phone"] = ph
        mp = normalize_phone(mobile or enriched_mobile)
        if mp:
            props["mobilephone"] = mp
        if linkedin:
            props["hs_linkedin_url"] = linkedin
        props["hs_lead_status"] = "NEW"
        props["how_did_you_hear_about_us_"] = config.LEAD_SOURCE_VALUE
        if event_name:
            props["how_did_you_hear_about_us___drill_down"] = event_name
        props["contact_type"] = contact_type_value
        props[config.QUALIFICATION_PROPERTY] = qualification_value
        if owner:
            props["hubspot_owner_id"] = owner
        contact_id = hubspot_client.create_contact(props)
        already_existed = "No"

    if company_id:
        hubspot_client.associate_contact_to_company(contact_id, company_id)

    # ---- Marketing Event attribution (object 0-54) ----
    # Record the contact's attendance on the HubSpot Marketing Event whose name
    # matches the row's Event Name (this drives marketing attribution). Degrades
    # to a note when the scope is missing (empty map) or no event matches.
    marketing_event_note = ""
    if config.MARKETING_EVENT_ENABLED and event_name:
        oid = marketing_events_by_name.get(event_name.strip().lower())
        if oid:
            hubspot_client.record_marketing_event_attendance(
                oid, config.MARKETING_EVENT_STATE,
                email=email, vid=("" if email else contact_id))
            marketing_event_note = f"Marketing Event: {config.MARKETING_EVENT_STATE.title()} on '{event_name}'"
        elif marketing_events_by_name:
            marketing_event_note = f"No Marketing Event named '{event_name}' found"

    notes = enrichment_note
    if is_partner:
        kind = "Agency" if is_agency else "Tech Partner"
        notes = f"{notes} | {kind} -- routed to Partnerships ({company_reason})".strip(" |")
    if marketing_event_note:
        notes = f"{notes} | {marketing_event_note}".strip(" |")
    if mobile_note:
        notes = f"{notes} | {mobile_note}".strip(" |")
    if revenue_note:
        notes = f"{notes} | {revenue_note}".strip(" |")
    if unverified_email:
        notes = f"{notes} | Unverified email for manual review: {unverified_email}".strip(" |")

    result = {
        "Pipeline Status": "Pushed",
        "ICP Verdict": icp_verdict,
        "Already in HubSpot?": already_existed,
        "HubSpot Contact ID": contact_id,
        "HubSpot Company ID": company_id or "",
        "Notes": notes,
    }
    # Reflect enriched values back onto the sheet's input columns so the sheet
    # shows what we found. write_result only writes header columns that exist,
    # and both of these are standard template columns.
    if enriched_email:
        result["Email"] = enriched_email
    if enriched_mobile:
        result["Mobile Phone Number"] = enriched_mobile
    return result


def enrich_row(row):
    """ENRICH_ONLY flow (config.ENRICH_ONLY). Order is deliberate and strict:
    HubSpot checkup FIRST (free reads -- dedup, customer, open deal, and the
    company/revenue/pod HubSpot already has, including a matched contact's
    associated company), then classify (ICP), then ENRICH LAST and strictly
    gated -- identity resolution only when no company is known anywhere; revenue
    only when HubSpot doesn't already have it; work email + mobile ONLY for
    Qualified prospects. No HubSpot writes; results are written back to the sheet.
    """
    first_name = row.get("First Name", "").strip()
    last_name = row.get("Last Name", "").strip()
    full_name = f"{first_name} {last_name}".strip()
    email = row.get("Email", "").strip().lower()
    company_name = row.get("Company Name", "").strip()
    domain = row.get("Company Domain", "").strip().lower()
    linkedin = row.get("LinkedIn URL", "").strip()
    phone = (row.get("Phone Number", "").strip() or row.get("Mobile Phone Number", "").strip())

    # Domain policy: explicit domain; else derive from a CORPORATE email; never
    # treat a free provider (gmail/yahoo/...) as a company domain.
    email_domain = email.split("@", 1)[1].strip() if "@" in email else ""
    if not domain and email_domain and not enrichment.is_free_email_domain(email_domain):
        domain = email_domain
    domain = enrichment.clean_domain(domain)

    # ---- 1) HubSpot checkup (free reads) ----
    existing_contact = hubspot_client.find_contact_by_email(email) if email else None
    existing_company = hubspot_client.find_company_by_domain(domain) if domain else None
    if existing_contact and not existing_company:
        existing_company = hubspot_client.get_company_for_contact(existing_contact["id"])
    hs_status = "Yes" if (existing_contact or existing_company) else "No"

    if _is_customer(existing_contact) or _is_customer(existing_company):
        return {"Pipeline Status": "Skipped", "Already in HubSpot?": "Yes",
                "Notes": "Existing HubSpot customer -- not enriched"}
    if existing_company and hubspot_client.company_has_open_deal(existing_company["id"]):
        return {"Pipeline Status": "Skipped", "Already in HubSpot?": "Yes",
                "Notes": "Company has an open deal -- not enriched"}

    ep = (existing_company or {}).get("properties", {})
    company_name = company_name or (ep.get("name") or "").strip()
    domain = domain or (ep.get("domain") or "").strip().lower()
    hs_rev_code = (ep.get("estimated_annual_revenue") or "").strip()
    hs_pod = (ep.get("pod") or "").strip()

    # ---- 2) Resolve identity ONLY if no company is known anywhere ----
    resolve_note = ""
    if not company_name and not domain and email:
        res = enrichment.resolve_identity(full_name, email, linkedin)
        linkedin = linkedin or res.get("linkedin", "")
        company_name = res.get("company_name", "")
        domain = res.get("domain", "")
        resolve_note = (f"Resolved company via {res['provider']}"
                        if (company_name or domain) else "Unresolved -- no company from personal email")
    if not company_name and not domain:
        return {"Pipeline Status": "Unresolved", "ICP Verdict": "", "Already in HubSpot?": hs_status,
                "LinkedIn URL": linkedin, "Notes": resolve_note or "No company/domain -- cannot classify"}

    # ---- 3) ICP ----
    company_verdict, company_reason = qualify.company_icp_judge(company_name, domain)
    if company_verdict == "FAIL":
        return {"Pipeline Status": "Rejected", "ICP Verdict": "FAIL", "Already in HubSpot?": hs_status,
                "Company Name": company_name, "Company Domain": domain, "LinkedIn URL": linkedin,
                "Notes": f"Excluded: {company_reason}"}
    is_brand = company_verdict == "PASS"
    is_agency = company_verdict == "AGENCY"
    is_tech = company_verdict == "TECH"
    is_partner = is_agency or is_tech
    contact_type_value = (config.CONTACT_TYPE_AGENCY if is_agency
                          else config.CONTACT_TYPE_TECH if is_tech else config.CONTACT_TYPE_BRAND)

    # ---- 4) Revenue: HubSpot first, enrich only if missing ----
    rev_code = hs_rev_code
    revenue_note = ""
    if config.ENRICH_REVENUE and is_brand and domain and not rev_code and not hs_pod:
        rr = enrichment.find_revenue_band(domain)
        if rr["code"]:
            rev_code = rr["code"]
            revenue_note = f"Revenue {rr['code']} via {rr['provider']} (~${int(rr['dollars']):,}/yr)"

    # ---- 5) Qualification ----
    qualified = (is_brand and rev_code in config.QUALIFIED_REVENUE_CODES) or bool(hs_pod)

    # ---- 6) Enrich contact details LAST, only for Qualified prospects ----
    work_email = ""
    mobile = ""
    mobile_note = ""
    if qualified:
        if (not email) or enrichment.is_free_email_domain(email_domain):
            fe = enrichment.find_email(first_name, last_name, full_name, company_name, domain, linkedin)
            if fe["email"]:
                work_email = fe["email"]
            if fe["linkedin_url"] and not linkedin:
                linkedin = fe["linkedin_url"]
        if config.ENRICH_MOBILE and not phone:
            mb = enrichment.find_mobile(full_name, first_name, last_name, company_name,
                                        domain, linkedin_url=linkedin, email=work_email or email)
            if mb["mobile"]:
                mobile = mb["mobile"]
                mobile_note = f"Mobile via {mb['provider']}"

    parts = []
    if resolve_note:
        parts.append(resolve_note)
    if is_partner:
        parts.append(f"{'Agency' if is_agency else 'Tech Partner'} (Partnerships)")
    if revenue_note:
        parts.append(revenue_note)
    if work_email:
        parts.append("Work email found")
    if mobile_note:
        parts.append(mobile_note)
    if not qualified:
        parts.append("Not qualified -- contact enrichment skipped")

    result = {
        "Pipeline Status": "Enriched",
        "ICP Verdict": company_verdict,
        "Contact Type": contact_type_value,
        "Qualification": "Qualified" if qualified else "Disqualified",
        "Already in HubSpot?": hs_status,
        "Notes": " | ".join(parts),
    }
    if company_name:
        result["Company Name"] = company_name
    if domain:
        result["Company Domain"] = domain
    if linkedin:
        result["LinkedIn URL"] = linkedin
    if work_email:
        result["Work Email"] = work_email      # kept separate -- never overwrites the row's Email
    if mobile:
        result["Mobile Phone Number"] = mobile
    if rev_code:
        result["Estimated Annual Revenue"] = rev_code
    return result
