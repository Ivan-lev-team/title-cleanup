export const meta = {
  name: 'event-pipeline',
  description: 'Qualify (ICP/title), dedupe against HubSpot, enrich, and round-robin assign Pod/SDR Owner for a CSV of contacts/companies; optionally push to HubSpot',
  whenToUse: 'Use whenever a new CSV of event/tradeshow (or similar) contacts needs to be title-qualified and optionally run all the way through HubSpot dedupe, enrichment, and Pod/SDR round-robin assignment. Pass args.mode = "icp_only" to run just the qualification filter standalone.',
  phases: [
    { title: 'Qualify' },
    { title: 'Dedupe' },
    { title: 'Enrich' },
    { title: 'Assign' },
    { title: 'Push' },
  ],
}

// Same round-robin config validated and used for the Social Commerce Summit 2026 NYC list.
// Pod RevOps intentionally excluded. Update this table if the pod/owner roster changes.
const POD_OWNERS = {
  'Pod 1': ['80046048', '87811820'],
  'Pod 2': ['87755705', '91884999'],
  'Pod 3': ['82954422', '91884994'],
  'Pod 4': ['91884997', '83840015'],
  'Pod 5': ['87811818', '89148921'],
  'Pod 6': ['86070116'],
  'Pod 7': ['87811816', '87811817'],
}
const POD_ORDER = Object.keys(POD_OWNERS)

const csvPath = args.csvPath
if (!csvPath) throw new Error('args.csvPath is required — path to the input CSV')
const mode = args.mode || 'full' // 'full' | 'icp_only'
const suppress = args.suppress || { people: [], companies: [] } // ad-hoc per-run exclusions, e.g. {people:["David Vanderveen"], companies:["I Wanna Sleep"]}
const push = args.push === true // default false: stop at a reviewable output file, don't write to HubSpot
const leadSource = args.leadSource || null // e.g. "Events / Tradeshow"
const leadSourceDrillDown = args.leadSourceDrillDown || null // e.g. "Social Commerce Summit 2026 NYC"

phase('Qualify')
const qualifyResult = await agent(`
You are given a CSV file at ${JSON.stringify(csvPath)}. Columns vary run to run, but you can always count on at least First Name/Full Name, Last Name, and Company Name being present; a Job Title column is used for qualification when present; a Company Domain column should be treated as a near-must-have but may occasionally be missing.

1. Read the CSV (use Python/pandas or the csv module via Bash — whatever is reliable).
2. For each row, classify the Job Title using the EXISTING classify() function in /home/user/title-cleanup/classify_titles.py — import and call it directly, do NOT reimplement or approximate the regex rules yourself. If a row has no Job Title at all, treat it as PASS (matches the existing "empty title, default pass" rule already in that function).
3. Apply this suppression list (case-insensitive exact match): exclude any row whose Company matches one of ${JSON.stringify(suppress.companies)}, or whose Full Name matches one of ${JSON.stringify(suppress.people)}.
4. Pick a scratch directory for this run's output files (e.g. under /tmp/ with a descriptive subfolder name derived from the input filename) and write two JSON files there: one array of qualified rows (title PASS and not suppressed, with ALL original CSV fields preserved), one array of excluded rows (same fields plus an "ExcludeReason" field: "Title FAIL: <reason>", "Brand suppressed", or "Person suppressed").

Report back (structured): total row count, qualified count, excluded count, a breakdown of exclude reasons (title-fail vs brand-suppressed vs person-suppressed counts), and the exact absolute paths of both JSON files you wrote.
`, {
  schema: {
    type: 'object',
    properties: {
      totalRows: { type: 'number' },
      qualifiedCount: { type: 'number' },
      excludedCount: { type: 'number' },
      excludeBreakdown: {
        type: 'object',
        properties: {
          titleFail: { type: 'number' },
          brandSuppressed: { type: 'number' },
          personSuppressed: { type: 'number' },
        },
      },
      qualifiedPath: { type: 'string' },
      excludedPath: { type: 'string' },
    },
    required: ['totalRows', 'qualifiedCount', 'excludedCount', 'qualifiedPath', 'excludedPath'],
  },
})

log(`Qualified ${qualifyResult.qualifiedCount} of ${qualifyResult.totalRows} rows (excluded ${qualifyResult.excludedCount})`)

if (mode === 'icp_only') {
  return qualifyResult
}

phase('Dedupe')
const dedupeResult = await agent(`
Read the qualified rows from ${JSON.stringify(qualifyResult.qualifiedPath)}.

For each row, determine dedupe status against HubSpot:
1. Contacts: if the row has an email, batch-search HubSpot contacts (mcp__HubSpot__search_crm_objects, objectType "contacts") by exact email match using the IN operator, batching ~40-50 emails per call to stay within response size limits. Record contactExisting=true/false and existingContactId when found. If the row has NO email, set contactExisting="unknown" — do not attempt to match by name alone, that's too unreliable and risks false-positive merges.
2. Companies: if the row has a Company Domain, batch-search HubSpot companies by exact domain match the same way (IN operator, batches of ~40-50), fetching properties ["name","domain","pod","sdr_owner"]. Record companyExisting=true/false, existingCompanyId, currentPod, currentSdrOwner when found. If the row has NO domain, set companyExisting="unknown" and leave currentPod/currentSdrOwner blank — do not guess.
3. IMPORTANT sanity check before trusting a large batch of "not found" results: if an entire batch of company-domain searches comes back with zero matches, verify the search mechanism works at all by testing it against 2-3 domains you already know exist in HubSpot from an earlier successful lookup in this same run. If the mechanism itself is broken, fix your query approach before concluding companies don't exist.

Write ALL rows (every field from the input preserved) plus the new fields above to a new JSON file next to the input. Report back: counts for each contactExisting/companyExisting status combination, and the file's absolute path.
`, {
  schema: {
    type: 'object',
    properties: {
      dedupePath: { type: 'string' },
      contactsExisting: { type: 'number' },
      contactsNew: { type: 'number' },
      contactsUnknown: { type: 'number' },
      companiesExisting: { type: 'number' },
      companiesNew: { type: 'number' },
      companiesUnknown: { type: 'number' },
    },
    required: ['dedupePath'],
  },
})

log(`Dedupe: ${dedupeResult.contactsExisting || 0} contacts already exist, ${dedupeResult.contactsNew || 0} new; ${dedupeResult.companiesExisting || 0} companies already exist, ${dedupeResult.companiesNew || 0} new`)

phase('Enrich')
let enrichedPath = dedupeResult.dedupePath
const enrichResult = await agent(`
Read ${JSON.stringify(dedupeResult.dedupePath)}. This is best-effort enrichment ONLY — never fabricate data.

For any row missing a Company Domain (and only Company Domain — do not attempt to invent emails, phone numbers, or LinkedIn URLs), try a web search on the company name to find its real, official domain. Only fill it in if you're genuinely confident it's the right company (watch out for common-word company names that collide with unrelated businesses). Mark any row where you filled in a domain this way with "domainInferred": true so it can be spot-checked later. Leave it blank if uncertain.

Write the full updated row-set (every existing field preserved, plus domainInferred where applicable) to a new JSON file. Report back: how many rows got a domain filled in, how many you left blank due to low confidence, and the file's absolute path.
`, {
  schema: {
    type: 'object',
    properties: {
      enrichedPath: { type: 'string' },
      domainsFilledIn: { type: 'number' },
      domainsLeftBlank: { type: 'number' },
    },
    required: ['enrichedPath'],
  },
})
enrichedPath = enrichResult.enrichedPath
log(`Enrichment: filled in ${enrichResult.domainsFilledIn || 0} missing domains, left ${enrichResult.domainsLeftBlank || 0} blank (low confidence)`)

phase('Assign')
const assignResult = await agent(`
Read ${JSON.stringify(enrichedPath)}. Each row has companyExisting/currentPod/currentSdrOwner fields from the dedupe step.

Build the list of unique companies referenced (group by Company Domain when present, otherwise by exact Company Name). For any unique company whose currentPod is empty/missing (this includes ALL companies where companyExisting is false, i.e. not yet in HubSpot, as well as existing companies that simply have no pod set) assign a NEW pod + sdr_owner using this exact round-robin rule:
- Sort the unique companies needing assignment by domain (or company name if no domain) alphabetically, for a stable/reproducible order.
- Cycle through this pod order, wrapping around as needed: ${JSON.stringify(POD_ORDER)}.
- Each pod has these owner ID(s), listed in alternation order — every time a given pod comes up again in the cycle, use the NEXT owner in its list (wrapping around); if a pod has only one owner, always use it: ${JSON.stringify(POD_OWNERS)}.
- Companies that ALREADY have a currentPod must NOT be reassigned — leave their existing pod/owner completely untouched in the output.
- Companies with no resolvable identity at all (no domain AND no usable company name) get no assignment — flag them clearly instead of guessing.
- Resolve numeric owner IDs to human names for the report using mcp__HubSpot__search_owners (do not trust only the sdr_owner property's dropdown "options" list for names — some valid owner IDs are absent from that list but are still real, active owners; search_owners is authoritative).

Write the final per-row output (all original fields plus: newPod, newSdrOwner, newSdrOwnerName when assigned) to a new JSON file, and also write a flat CSV version of the same data (one row per contact) for human review, with these columns: whatever identity/contact fields were in the input, plus "Company HS Id (existing)", "Company Found in HubSpot", "Current Pod", "Current SDR Owner Name", "New Pod Assigned", "New SDR Owner Name", "SDR Owner Assigned (Yes/No — Yes means it already had one before this run)", "Company Created (Yes/No — always No at this stage)", "Tags Applied (Yes/No — always No at this stage)".

Report back: how many unique companies got a new assignment, the resulting distribution across pods (object mapping pod name to count), how many were left unassigned due to no resolvable identity, and the absolute paths of both output files (JSON and CSV).
`, {
  schema: {
    type: 'object',
    properties: {
      assignedPath: { type: 'string' },
      assignedCsvPath: { type: 'string' },
      companiesAssigned: { type: 'number' },
      companiesUnresolved: { type: 'number' },
      podDistribution: { type: 'object' },
    },
    required: ['assignedPath', 'assignedCsvPath', 'companiesAssigned'],
  },
})

log(`Assigned pod/owner to ${assignResult.companiesAssigned} companies (${assignResult.companiesUnresolved || 0} left unresolved)`)

phase('Push')
if (!push) {
  log(`push=false — stopping before writing to HubSpot. Review file: ${assignResult.assignedCsvPath}`)
  return { ...qualifyResult, ...dedupeResult, ...assignResult, pushed: false }
}

const pushResult = await agent(`
Read ${JSON.stringify(assignResult.assignedPath)}.

Before writing anything, look up the ACTUAL internal HubSpot property names for "Lead Source" and "Lead Source Drill Down" on the contacts object via mcp__HubSpot__search_properties (objectType "contacts", keywords like ["lead source","lead_source","drill down"]) — do not assume a property name, verify it. The values to set are: Lead Source = ${JSON.stringify(leadSource)}, Lead Source Drill Down = ${JSON.stringify(leadSourceDrillDown)} (skip setting these two if either is null/not provided).

For every row, in batches of at most 10 objects per mcp__HubSpot__manage_crm_objects call (HubSpot's hard cap), confirmationStatus "CONFIRMED":
1. If companyExisting is false: create a new Company (name, domain if known) with pod=newPod and sdr_owner=newSdrOwner set at creation time.
2. If companyExisting is true but currentPod was empty: update that existing company's pod=newPod, sdr_owner=newSdrOwner.
3. If companyExisting is true and currentPod was already set: do not touch that company at all.
4. If contactExisting is false: create a new Contact using whichever of firstname/lastname/jobtitle/email/phone/linkedin are present in the row, tagged with hs_lead_status=NEW plus the verified Lead Source properties above (if provided), and associate it to its company record (existing ID, or the ID just created in step 1/2).
5. If contactExisting is true: only fill genuinely BLANK fields on that existing contact (never overwrite populated ones), still additively apply the Lead Source tags if provided, and add the company association if the existing contact doesn't already have one.

Track every error individually (do not silently swallow partial-batch failures). Report back final totals: contacts created, contacts updated, companies created, companies updated, and a list of any errors encountered with enough detail to act on them.
`, {
  schema: {
    type: 'object',
    properties: {
      contactsCreated: { type: 'number' },
      contactsUpdated: { type: 'number' },
      companiesCreated: { type: 'number' },
      companiesUpdated: { type: 'number' },
      errors: { type: 'array', items: { type: 'string' } },
    },
    required: ['contactsCreated', 'contactsUpdated', 'companiesCreated', 'companiesUpdated'],
  },
})

log(`Pushed: ${pushResult.contactsCreated} contacts created, ${pushResult.contactsUpdated} updated; ${pushResult.companiesCreated} companies created, ${pushResult.companiesUpdated} updated`)

return { ...qualifyResult, ...dedupeResult, ...assignResult, ...pushResult, pushed: true }
