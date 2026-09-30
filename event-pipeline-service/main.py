"""
Entrypoint: polls the Google Sheet's Contact Import tab every
POLL_INTERVAL_SECONDS, runs each unprocessed row through the pipeline, and
writes the outcome back into the sheet. Run as a systemd service or in a
long-lived container -- see README.md for deployment.
"""
import time
import traceback

import config
import enrichment
import sheets_client
import hubspot_client
import pipeline


def run_once():
    ws, rows = sheets_client.get_unprocessed_rows(config.CONTACT_SHEET_NAME)
    if not rows:
        print("No new rows.")
        return

    # read once per cycle, not once per row -- the Company Import tab rarely
    # changes as often as the Contact Import tab does. Both only needed for the
    # full pipeline, not ENRICH_ONLY.
    company_import_by_domain = {} if config.ENRICH_ONLY else sheets_client.get_company_import_by_domain()
    marketing_events_by_name = ({} if (config.ENRICH_ONLY or not config.MARKETING_EVENT_ENABLED)
                                else hubspot_client.list_marketing_events())

    mode = "ENRICH_ONLY" if config.ENRICH_ONLY else "full pipeline"
    print(f"Found {len(rows)} new row(s) to process ({mode}).")
    # Names, not ids: the sheet's SDR Owner column holds display names.
    partnership_names = {hubspot_client.get_owner_name(o) for o in config.PARTNERSHIP_OWNERS}
    partnership_names.discard("")
    to_mirror = []
    for row_number, row_dict, header in rows:
        name = f"{row_dict.get('First Name','')} {row_dict.get('Last Name','')}".strip()
        try:
            if config.ENRICH_ONLY:
                result = pipeline.enrich_row(row_dict)
            else:
                result = pipeline.process_row(row_dict, company_import_by_domain, marketing_events_by_name)
        except Exception as e:
            result = {"Pipeline Status": "Error", "Notes": f"{type(e).__name__}: {e}"}
            print(f"  ERROR on row {row_number} ({name}): {e}")
            traceback.print_exc()
        else:
            print(f"  Row {row_number} ({name}): {result.get('Pipeline Status')}")
        if config.DRY_RUN and result.get("Pipeline Status") == "Pushed":
            result["Pipeline Status"] = "DRY RUN - would push"
        sheets_client.write_result(ws, row_number, header, result)
        # Anything routed to Katerina or Milosh is Partnerships' to work, so
        # mirror it onto their tab. Collected here and appended once at the end
        # of the cycle (see append_partnership_rows).
        if (result.get("SDR Owner") or "").strip() in partnership_names:
            to_mirror.append({**row_dict, **result})

        # Event list push: a lead with a real email and domain belongs in the
        # SDR event list. 7887 is dynamic, so this writes the properties its
        # filter reads rather than adding a membership. Gated by
        # EVENT_LIST_PUSH and deliberately independent of DRY_RUN.
        if config.EVENT_LIST_PUSH:
            merged = {**row_dict, **result}
            email = (merged.get("Email") or "").strip().lower()
            domain = (merged.get("Company Domain") or "").strip()
            event = config.EVENT_NAME_TO_HUBSPOT.get((merged.get("Event Name") or "").strip())
            qualified = (merged.get("Qualification") or "").strip() in ("Qualified", "PASS")
            if "@" in email and enrichment.valid_company_domain(domain) and event and qualified:
                try:
                    outcome = hubspot_client.push_to_event_list(
                        email, event,
                        merged.get("First Name", ""), merged.get("Last Name", ""),
                        merged.get("Job Title", ""))
                    print(f"    event list ({email}): {outcome}")
                except Exception as e:
                    print(f"    event list ({email}) FAILED: {type(e).__name__}: {e}")

    if to_mirror:
        added = sheets_client.append_partnership_rows(to_mirror)
        print(f"  Mirrored {added} row(s) to {config.PARTNERSHIP_SHEET_NAME} "
              f"({len(to_mirror) - added} already there).")


def main():
    mode = "DRY RUN (no HubSpot writes)" if config.DRY_RUN else "LIVE"
    print(f"Levanta event pipeline service starting in {mode} mode. Polling every {config.POLL_INTERVAL_SECONDS}s.")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"Poll cycle failed: {e}")
            traceback.print_exc()
        time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
