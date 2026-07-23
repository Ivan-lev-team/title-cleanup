# Levanta Event Import Pipeline

Polls a Google Sheet (matching the Levanta Event Import Template's Contact
Import tab), and for every new row runs: **qualify -> check HubSpot ->
enrich -> round-robin -> push**. Enrichment (Prospeo) only ever runs on rows
that already survived qualify + dedupe, so no credits are spent on rows
that would be filtered out anyway.

## What you need before deploying

1. **HubSpot Private App token** — Settings -> Integrations -> Private Apps.
   Scopes needed: `crm.objects.contacts.read/write`, `crm.objects.companies.read/write`,
   `crm.schemas.contacts.read`, `crm.schemas.companies.read`.
2. **Anthropic API key** — console.anthropic.com. Used for the company-ICP
   judge (one call per new company, not per row).
3. **Prospeo API key** (`PROSPEO_KEY` in `.env`, starts `pk_...`) — used to
   find missing emails for name-only rows via `/bulk-enrich-person`. Optional:
   if left blank, enrichment is skipped and those rows still get created
   (name + LinkedIn + phone, whatever's present), just without a found email.
   Only Prospeo hits with `status == "VERIFIED"` are auto-written to the
   contact's email; anything else is left out of the push and noted in the
   sheet's `Notes` column for manual review, never auto-used.
4. **Google service account** with a JSON key, and edit access to the sheet
   (share the sheet with the service account's `client_email` address).
5. A **Google Sheet** with a `Contact Import` tab whose header row matches
   the Levanta Event Import Template's Contact Import columns exactly
   (First Name, Last Name, Email, Job Title, Company Name, Company Domain,
   Phone Number, Mobile Phone Number, LinkedIn URL, Event Name, ...).

## First-time setup on the server

```bash
sudo mkdir -p /opt/levanta-event-pipeline
sudo useradd -r -s /bin/false levanta   # if this user doesn't already exist
cd /opt/levanta-event-pipeline

# copy this whole folder's contents here, then:
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env   # fill in every value

# put the Google service account JSON somewhere NOT in this folder's git history, e.g.:
sudo mkdir -p /etc/levanta-event-pipeline
sudo cp /path/to/google_service_account.json /etc/levanta-event-pipeline/
sudo chmod 600 /etc/levanta-event-pipeline/google_service_account.json
# then set GOOGLE_SERVICE_ACCOUNT_JSON in .env to that path

sudo chown -R levanta:levanta /opt/levanta-event-pipeline /etc/levanta-event-pipeline
sudo chmod 600 .env
```

## Run it as a systemd service (recommended for always-on)

```bash
sudo cp levanta-event-pipeline.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now levanta-event-pipeline
sudo systemctl status levanta-event-pipeline
journalctl -u levanta-event-pipeline -f   # live logs
```

## Quick manual test first (before trusting the systemd service)

Set `DRY_RUN=true` in `.env` first. In this mode, every HubSpot *read* (contact/
company search, round-robin counts) still runs live against your real portal,
but every *write* (create/update company or contact, association) is skipped
and logged as `[DRY RUN] would ...` instead of executed — so you can watch the
whole qualify -> dedupe -> enrich -> round-robin flow run against real data
with zero risk of touching production records.

```bash
cd /opt/levanta-event-pipeline
source venv/bin/activate
python3 main.py
```
Add one (or a few) test rows to the `Contact Import` tab, watch the terminal:
you'll see `[DRY RUN]` lines for anything that would be created/updated, and
the sheet gets a `Pipeline Status` of `DRY RUN - would push` (or `Rejected`/
`Error`) plus the `Notes`/`ICP Verdict` columns filled in. A `DRY RUN` status
does NOT count as "already processed" — once you flip `DRY_RUN=false` and
rerun, those same rows will go through for real.

Ctrl+C to stop. Once the dry run looks right, set `DRY_RUN=false`, clear the
`Pipeline Status`/`Notes`/etc. columns on any rows you dry-ran (so they get
reprocessed for real, cleanly), and move to the systemd setup above.

## Optional one-off maintenance scripts

These are NOT run by the polling service (`main.py`) — run them by hand when
needed, from inside `event-pipeline-service/` with the venv active and `.env`
filled in:

- `scripts/enrich_mobiles.py "Event Name"` — backfills mobile numbers for
  already-pushed contacts tagged with that event's drill-down value that are
  still missing a phone. Costs 10 Prospeo credits per number found, which is
  why it's a separate manual step instead of part of the live per-row
  pipeline (the live pipeline only spends the 1-credit email lookup).
- `scripts/create_hubspot_list.py "Event Name" "List Name"` — creates (or
  reuses) a static HubSpot contact list and adds every contact tagged with
  that event's drill-down value. Requires the token to also have
  `crm.lists.read`/`crm.lists.write` scopes.

## Known things worth double-checking before trusting this at scale

- **The Anthropic model name** in `qualify.py` (`claude-sonnet-5`) — confirm
  this matches whatever's current/available on your API plan at deploy time.
- **HubSpot's `domain IN [...]` over-matching bug** (found during the manual
  Social Commerce Summit push) is guarded against in
  `hubspot_client.find_companies_by_domains_bulk`, but this service actually
  looks up one domain at a time per row (`find_company_by_domain`, an exact
  `EQ` match) rather than bulk IN-batches, so it isn't exposed to that bug
  in normal operation. The bulk helper is kept for any future reconciliation
  tooling that processes many rows in a single sweep.
- **Sheet header must match exactly** — `sheets_client.py` reads columns by
  the template's exact header names. If the sheet's headers drift from the
  template, update the `row.get("...")` calls in `pipeline.py` to match.
- **Prospeo schema is now the validated, working one** — `prospeo_client.py`,
  `scripts/enrich_mobiles.py` match the real `/bulk-enrich-person` endpoint
  and payload/response shape from the local `/hubspot-push` tooling that was
  actually run against live HubSpot data, not a guess.
