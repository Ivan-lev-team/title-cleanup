import sys, csv, re, time
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import dotenv_values
import requests

TOKEN = dotenv_values(r"C:\Users\Ivan\Projects\levanta-event-pipeline\event-pipeline-service\.env")["HUBSPOT_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
URL = "https://api.hubapi.com/crm/v3/objects/contacts/search"
DL = r"C:\Users\Ivan\Downloads"
CLEAN = DL + r"\Entrepreneurs,-US-and-Canada,-Ecommerce-Default-CLEANED.csv"

def handle(url):
    m = re.search(r"linkedin\.com/in/([a-z0-9\-_%\.]+)", (url or "").strip().lower())
    return m.group(1).rstrip("/") if m else ""

rows = list(csv.DictReader(open(CLEAN, encoding="utf-8-sig")))
hmap = {}
for i, r in enumerate(rows):
    h = handle(r.get("LinkedIn Profile"))
    if h: hmap.setdefault(h, []).append(i)
handles = sorted(hmap)
print(f"{len(rows)} rows / {len(handles)} unique handles", flush=True)

def search_batch(hs_vals, li_vals):
    matched = {}  # handle -> contact id
    payload = {
        "filterGroups": [
            {"filters": [{"propertyName": "hs_linkedin_url", "operator": "IN", "values": hs_vals}]},
            {"filters": [{"propertyName": "linkedin_personal_url", "operator": "IN", "values": li_vals}]},
        ],
        "properties": ["hs_linkedin_url", "linkedin_personal_url"],
        "limit": 100,
    }
    after = None
    while True:
        if after: payload["after"] = after
        for attempt in range(5):
            r = requests.post(URL, headers=H, json=payload, timeout=60)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1)); continue
            r.raise_for_status(); break
        data = r.json()
        for c in data.get("results", []):
            p = c.get("properties", {})
            h = handle(p.get("hs_linkedin_url")) or handle(p.get("linkedin_personal_url"))
            if h and h not in matched: matched[h] = c["id"]
        after = (data.get("paging", {}).get("next", {}) or {}).get("after")
        if not after: break
        time.sleep(0.25)
    return matched

B = 100
found = {}
batches = [handles[i:i+B] for i in range(0, len(handles), B)]
for bi, b in enumerate(batches):
    hs_vals = [f"https://linkedin.com/in/{h}" for h in b]
    li_vals = [f"https://www.linkedin.com/in/{h}" for h in b]
    m = search_batch(hs_vals, li_vals)
    found.update(m)
    print(f"  batch {bi+1}/{len(batches)}: +{len(m)} matched (running {len(found)})", flush=True)
    time.sleep(0.3)

# annotate
rc = 0
for i, r in enumerate(rows):
    h = handle(r.get("LinkedIn Profile"))
    if h in found:
        r["Contact in HubSpot?"] = "Yes"; r["HubSpot Contact Id"] = found[h]; rc += 1
    else:
        r["Contact in HubSpot?"] = "No"; r["HubSpot Contact Id"] = ""

fields = list(rows[0].keys())
for k in ("Contact in HubSpot?", "HubSpot Contact Id"):
    if k not in fields: fields.append(k)
with open(CLEAN, "w", newline="", encoding="utf-8-sig") as f:
    wr = csv.DictWriter(f, fieldnames=fields); wr.writeheader()
    for r in rows: wr.writerow({k: r.get(k, "") for k in fields})

print(f"\nDONE: {rc}/{len(rows)} contacts already in HubSpot | {len(rows)-rc} net-new")
