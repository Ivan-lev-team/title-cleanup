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
3. **Prospeo API key** — used to find missing emails for name-only rows.
   Optional: if left blank, enrichment is skipped and those rows still get
   created (name + LinkedIn + phone, whatever's present), just without a
   found email.
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

```bash
cd /opt/levanta-event-pipeline
source venv/bin/activate
python3 main.py
```
Add one test row to the sheet, watch it get picked up and processed, check
the sheet gets a `Pipeline Status` value and (if pushed) real HubSpot IDs.
Ctrl+C to stop, then move to the systemd setup above once it looks right.

## Known things worth double-checking before trusting this at scale

- **Prospeo's exact request/response schema** in `prospeo_client.py` was
  written from general knowledge of their API, not verified against live
  docs from this session (no network access here to test it). Check
  Prospeo's current API reference and adjust the endpoint URL / field names
  in `prospeo_client.py` if the first real enrichment call errors out.
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
