#!/usr/bin/env python3
"""
One-off maintenance job (NOT run by the polling service): creates (or
reuses) a static HubSpot contact list and adds every contact tagged with a
given event drill-down value. Idempotent-ish -- a list with the same name
is reused and just gets new members added.

Requires the token to have scopes: crm.lists.read, crm.lists.write (plus
the crm.objects.contacts.read scope the rest of this service already uses).

Usage:
    cd event-pipeline-service
    python3 scripts/create_hubspot_list.py "Social Commerce Summit 2026 NYC" "Social Commerce Summit 2026 NYC / Ivan"
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import hubspot_client

DRILLDOWN_PROP = "how_did_you_hear_about_us___drill_down"


def collect_contact_ids(event_name):
    ids, after = [], None
    while True:
        body = {
            "filterGroups": [{"filters": [{"propertyName": DRILLDOWN_PROP, "operator": "EQ", "value": event_name}]}],
            "properties": ["email"],
            "limit": 100,
        }
        if after:
            body["after"] = after
        resp = hubspot_client.request_with_retry(
            "POST", f"{hubspot_client.BASE}/crm/v3/objects/contacts/search", json=body
        )
        d = resp.json()
        ids += [r["id"] for r in d.get("results", [])]
        after = d.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return ids


def find_existing_list(list_name):
    resp = hubspot_client.request_with_retry(
        "POST", f"{hubspot_client.BASE}/crm/v3/lists/search", json={"query": list_name, "count": 50}
    )
    if resp.status_code >= 300:
        return None
    for lst in resp.json().get("lists", []):
        if lst.get("name") == list_name:
            return lst["listId"]
    return None


def chunk(lst, n=200):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def main():
    if len(sys.argv) < 3:
        sys.exit('Usage: python3 scripts/create_hubspot_list.py "Event Name" "List Name"')
    event_name, list_name = sys.argv[1], sys.argv[2]

    ids = collect_contact_ids(event_name)
    print(f"contacts tagged '{event_name}': {len(ids)}")
    if not ids:
        sys.exit("No tagged contacts found -- nothing to add.")

    portal_resp = hubspot_client.request_with_retry("GET", f"{hubspot_client.BASE}/account-info/v3/details")
    portal = portal_resp.json().get("portalId") if portal_resp.status_code < 300 else None

    list_id = find_existing_list(list_name)
    if list_id:
        print(f"reusing existing list '{list_name}' id={list_id}")
    else:
        resp = hubspot_client.request_with_retry(
            "POST", f"{hubspot_client.BASE}/crm/v3/lists",
            json={"name": list_name, "objectTypeId": "0-1", "processingType": "MANUAL"},
        )
        if resp.status_code >= 300:
            sys.exit(f"CREATE LIST error: {resp.status_code} {resp.text[:500]}")
        list_id = resp.json()["list"]["listId"]
        print(f"created static list '{list_name}' id={list_id}")

    added = 0
    for c in chunk(ids, 200):
        resp = hubspot_client.request_with_retry(
            "PUT", f"{hubspot_client.BASE}/crm/v3/lists/{list_id}/memberships/add", json=c
        )
        if resp.status_code >= 300:
            print(f"  add error: {resp.status_code} {resp.text[:300]}")
            continue
        added += len(resp.json().get("recordIdsAdded", c))
        time.sleep(0.3)
    print(f"members added: {added}")

    if portal:
        print(f"\nOpen it here:\n  https://app.hubspot.com/contacts/{portal}/objectLists/{list_id}")


if __name__ == "__main__":
    main()
