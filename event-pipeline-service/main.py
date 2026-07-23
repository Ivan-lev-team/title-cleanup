"""
Entrypoint: polls the Google Sheet's Contact Import tab every
POLL_INTERVAL_SECONDS, runs each unprocessed row through the pipeline, and
writes the outcome back into the sheet. Run as a systemd service or in a
long-lived container -- see README.md for deployment.
"""
import time
import traceback

import config
import sheets_client
import pipeline


def run_once():
    ws, rows = sheets_client.get_unprocessed_rows(config.CONTACT_SHEET_NAME)
    if not rows:
        print("No new rows.")
        return

    # read once per cycle, not once per row -- the Company Import tab rarely
    # changes as often as the Contact Import tab does
    company_import_by_domain = sheets_client.get_company_import_by_domain()

    print(f"Found {len(rows)} new row(s) to process.")
    for row_number, row_dict, header in rows:
        name = f"{row_dict.get('First Name','')} {row_dict.get('Last Name','')}".strip()
        try:
            result = pipeline.process_row(row_dict, company_import_by_domain)
        except Exception as e:
            result = {"Pipeline Status": "Error", "Notes": f"{type(e).__name__}: {e}"}
            print(f"  ERROR on row {row_number} ({name}): {e}")
            traceback.print_exc()
        else:
            print(f"  Row {row_number} ({name}): {result.get('Pipeline Status')}")
        if config.DRY_RUN and result.get("Pipeline Status") == "Pushed":
            result["Pipeline Status"] = "DRY RUN - would push"
        sheets_client.write_result(ws, row_number, header, result)


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
