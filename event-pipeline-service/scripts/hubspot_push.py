import sys, os, csv, time
sys.stdout.reconfigure(encoding="utf-8")
SVC = r"C:\Users\Ivan\Projects\levanta-event-pipeline\event-pipeline-service"
os.chdir(SVC); sys.path.insert(0, SVC)
import requests
from dotenv import dotenv_values
TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
B = "https://api.hubapi.com"
MARIA = "80046048"; POD = "Pod 1"; LIST_ID = 7519
SRC = r"C:\Users\Ivan\Downloads\42k Contacts - Maria-ENRICHED-2k.csv"

def g(r,k): return (r.get(k) or "").strip()
def post(path, body):
    for a in range(6):
        r = requests.post(f"{B}{path}", headers=H, json=body, timeout=90)
        if r.status_code == 429: time.sleep(2*(a+1)); continue
        return r
    return r

rows = list(csv.DictReader(open(SRC, encoding="utf-8-sig")))

# --- existing contacts by email ---
emails = sorted({g(r,"Work Email").lower() for r in rows if g(r,"Work Email")})
email2id = {}
for i in range(0,len(emails),100):
    r = post("/crm/v3/objects/contacts/search",
        {"filterGroups":[{"filters":[{"propertyName":"email","operator":"IN","values":emails[i:i+100]}]}],
         "properties":["email"],"limit":100})
    for rec in r.json().get("results",[]):
        email2id[(rec["properties"].get("email") or "").lower()] = rec["id"]
    time.sleep(0.1)

def cprops(r, create):
    p = {"firstname":g(r,"First Name"), "lastname":g(r,"Last Name")}
    if g(r,"Job Title"): p["jobtitle"]=g(r,"Job Title")
    if g(r,"Work Email"): p["email"]=g(r,"Work Email")
    if g(r,"Mobile Phone"): p["mobilephone"]=g(r,"Mobile Phone")
    if g(r,"LinkedIn Profile"): p["hs_linkedin_url"]=g(r,"LinkedIn Profile")
    if create:
        p["hs_lead_status"]="NEW"; p["hubspot_owner_id"]=MARIA
    return p

# --- split create/update, keep row->contactId ---
row_cid = {}
updates=[]; creates=[]
for idx,r in enumerate(rows):
    em=g(r,"Work Email").lower()
    if em and em in email2id:
        cid=email2id[em]; row_cid[idx]=cid
        updates.append({"id":cid,"properties":cprops(r,False)})
    else:
        creates.append((idx,r))

# batch update existing contacts
for i in range(0,len(updates),100):
    r=post("/crm/v3/objects/contacts/batch/update",{"inputs":updates[i:i+100]})
    print(f"contact update batch {i//100+1}: {r.status_code}", flush=True)
    time.sleep(0.1)

# batch create new contacts (preserve order -> map ids back)
created=0
for i in range(0,len(creates),100):
    chunk=creates[i:i+100]
    r=post("/crm/v3/objects/contacts/batch/create",{"inputs":[{"properties":cprops(rr,True)} for _,rr in chunk]})
    if r.status_code<300:
        res=r.json().get("results",[])
        for (idx,_),rec in zip(chunk,res): row_cid[idx]=rec["id"]
        created+=len(res)
    else:
        print("CREATE ERR",r.status_code,r.text[:200], flush=True)
    print(f"contact create batch {i//100+1}: {r.status_code} (created {created})", flush=True)
    time.sleep(0.1)

# --- companies: current owner/pod, preserve conflicts ---
comp_ids=sorted({g(r,"HubSpot Company Id") for r in rows if g(r,"HubSpot Company Id")})
cur={}
for i in range(0,len(comp_ids),100):
    ch=[x for x in comp_ids[i:i+100] if x.isdigit()]
    r=post("/crm/v3/objects/companies/batch/read",
        {"properties":["sdr_owner","pod","hubspot_owner_id"],"inputs":[{"id":x} for x in ch]})
    for rec in r.json().get("results",[]): cur[rec["id"]]=rec.get("properties",{})
    time.sleep(0.1)
comp_dom={}; comp_amz={}
for r in rows:
    cid=g(r,"HubSpot Company Id")
    if not cid: continue
    if g(r,"Company Domain"): comp_dom.setdefault(cid,g(r,"Company Domain"))
    if g(r,"Amazon Storefront URL"): comp_amz.setdefault(cid,g(r,"Amazon Storefront URL"))
cupdates=[]
for cid in comp_ids:
    p={}; c=cur.get(cid,{})
    if comp_dom.get(cid): p["domain"]=comp_dom[cid]
    if comp_amz.get(cid): p["amazon_storefront_url"]=comp_amz[cid]
    so=(c.get("sdr_owner") or "").strip(); pd=(c.get("pod") or "").strip(); ho=(c.get("hubspot_owner_id") or "").strip()
    if not so: p["sdr_owner"]=MARIA
    if not pd: p["pod"]=POD
    if not ho: p["hubspot_owner_id"]=MARIA
    if p: cupdates.append({"id":cid,"properties":p})
for i in range(0,len(cupdates),100):
    r=post("/crm/v3/objects/companies/batch/update",{"inputs":cupdates[i:i+100]})
    print(f"company update batch {i//100+1}: {r.status_code}", flush=True)
    time.sleep(0.1)

# --- associations contact<->company (default) ---
assoc=[]
for idx,r in enumerate(rows):
    cid=row_cid.get(idx); comp=g(r,"HubSpot Company Id")
    if cid and comp: assoc.append({"from":{"id":cid},"to":{"id":comp}})
aok=0
for i in range(0,len(assoc),100):
    r=post("/crm/v4/associations/contacts/companies/batch/create/default",{"inputs":assoc[i:i+100]})
    if r.status_code<300: aok+=len(assoc[i:i+100])
    else: print("ASSOC ERR",r.status_code,r.text[:200], flush=True)
    time.sleep(0.1)
print(f"associations created: {aok}/{len(assoc)}", flush=True)

# --- add to list 7519 ---
all_ids=[row_cid[i] for i in range(len(rows)) if i in row_cid]
added=0
for i in range(0,len(all_ids),100):
    r=requests.put(f"{B}/crm/v3/lists/{LIST_ID}/memberships/add",headers=H,json=all_ids[i:i+100],timeout=60)
    if r.status_code<300: added+=len(all_ids[i:i+100])
    else: print("LIST ERR",r.status_code,r.text[:200], flush=True)
    time.sleep(0.1)
print(f"\nDONE: contacts total {len(all_ids)} (created {created}, updated {len(updates)}) | companies updated {len(cupdates)} | assoc {aok} | added to list {added}", flush=True)
