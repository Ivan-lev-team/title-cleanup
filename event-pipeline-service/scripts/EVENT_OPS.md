# Event lead operations

One entry point for the bulk event-lead flow:

```
Clay -> "Contact Import" sheet -> enrich -> assign owner/pod -> bulk push to HubSpot
```

The auto-enrichment poller fills the sheet. Everything after that is run by
hand, on purpose: `EVENT_LIST_PUSH` stays `false` so the service never writes
to HubSpot on its own.

## Running it

```bash
cd event-pipeline-service
export GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/sa.json   # scp from the server
python scripts/event_ops.py audit                     # always safe
python scripts/event_ops.py assign-routing --mode test
python scripts/event_ops.py assign-routing --mode apply
```

The service account file lives on the server at
`/etc/levanta-event-pipeline/google_service_account.json`. `GOOGLE_SHEET_ID`
and `HUBSPOT_TOKEN` come from the service `.env`.

## Modes

| `--mode` | effect |
| --- | --- |
| `dry` (default) | reports what it would do, writes nothing |
| `test` | writes the first **5** records only |
| `apply` | writes everything |

Always run `dry`, then `test`, then `apply`. A wrong mapping then costs 5
records instead of 700.

**Every write fills blanks only.** A value already in HubSpot, or already in
a sheet cell, is never overwritten: anything a human has touched is the system
of record. Existing customers and open-deal accounts are skipped throughout.

## Commands

| command | what it does |
| --- | --- |
| `audit` | coverage across both sheet tabs and both HubSpot lists |
| `assign-routing` | fills blank POD / SDR Owner on Contact Import |
| `mirror-partnership` | appends Partnership-owned rows to the Agencies/Tech tab |
| `push-event-list` | makes rows qualify for the dynamic event list (7887) |
| `push-partnership-list` | sets membership of the manual partnership list (8037) |
| `associate` | links contacts with no company to the company for their domain |
| `complete` | fills every blank HubSpot field the sheet can answer |
| `industry` | maps sheet industry text onto HubSpot's enum and writes it |
| `hygiene` | repairs malformed values already in HubSpot |
| `all` | the whole sequence in dependency order |

Useful flags: `--with-disqualified` (route sub-$1M brands too), `--industry`
(fold industry into `complete`), `--prune` (drop non-Partnership members from
8037), `--list-id` (which list `hygiene` repairs).

## Things that will bite you

**The two lists work differently.** 7887 is **DYNAMIC**: you cannot add
members, and `memberships/add` does nothing useful. A contact joins by
satisfying its filter, which reads `how_did_you_hear_about_us___drill_down`
and `gold___ent__qualification`. 8037 is **MANUAL**, so membership is set
explicitly. `push-event-list` and `push-partnership-list` are not variants of
each other for this reason.

**7887's filter enumerates event names.** A new event will not appear in the
list until its value is added to that filter in the HubSpot UI, no matter what
the pipeline writes.

**Event names differ between the sheet and HubSpot** (`Q3Y26 Amazon Accelerate`
vs `Q326 Amazon Accelerate`). `config.EVENT_NAME_TO_HUBSPOT` maps them.
HubSpot silently drops a value that is not a real property option, so an
unmapped event looks like a successful write while nothing joins the list.
**Add new events to that map**, or they are skipped.

**`industry` is a 148-option enum**, and free-text industry values are dropped
the same silent way. `INDUSTRY_ALIASES` plus a strict fuzzy match handles the
current corpus; anything below the cutoff is reported as unmapped and left
blank rather than guessed onto a real account. Add new values to the alias
table.

**`pod` is a company property.** There is no `contacts.pod` in this portal, so
a contact with no associated company has no pod. That is what `associate` is
for. The enum has no bare `Partnership` value, only `Pod Partnership`.

**`sdr_owner` is an owner reference**, so it takes an owner id. The sheet
holds display names; the script translates. Owner `87811820`
(Milosh Mihajlovikj) is **archived** and is excluded from new assignment.

**Batch create dies on the first conflict.** `push-event-list` falls back to
per-record creates and recovers the existing id from the 409 message.

**Sheets allows 60 writes/minute.** All sheet writes are batched; do not
change them to per-cell updates.
