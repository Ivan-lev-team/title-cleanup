import sys, os, csv, re, time
sys.stdout.reconfigure(encoding="utf-8")
SVC = r"C:\Users\Ivan\Projects\levanta-event-pipeline\event-pipeline-service"
os.chdir(SVC); sys.path.insert(0, SVC)
import requests
from dotenv import dotenv_values
TOKEN = dotenv_values(".env")["HUBSPOT_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
B = "https://api.hubapi.com"
MARIA = "80046048"; LIST_ID = 7519
SRC = r"C:\Users\Ivan\Downloads\42k Contacts - Maria-ENRICHED-2k.csv"
def g(r,k): return (r.get(k) or "").strip()
def post(path, body):
    for a in range(6):
        r=requests.post(f"{B}{path}",headers=H,json=body,timeout=90)
        if r.status_code==429: time.sleep(2*(a+1)); continue
        return r
    return r
rows=list(csv.DictReader(open(SRC,encoding="utf-8-sig")))

def search_in(prop, vals):
    m={}
    for i in range(0,len(vals),100):
        r=post("/crm/v3/objects/contacts/search",
            {"filterGroups":[{"filters":[{"propertyName":prop,"operator":"IN","values":vals[i:i+100]}]}],
             "properties":[prop],"limit":100})
        for rec in r.json().get("results",[]):
            v=(rec["properties"].get(prop) or "")
            m.setdefault(v.lower(), rec["id"])
        time.sleep(0.05)
    return m

# rebuild row -> contact id
emails=sorted({g(r,"Work Email").lower() for r in rows if g(r,"Work Email")})
email2id=search_in("email",emails)
li_vals=sorted({g(r,"LinkedIn Profile") for r in rows if not g(r,"Work Email") and g(r,"LinkedIn Profile")})
li2id=search_in("hs_linkedin_url",li_vals)   # exact stored value
row_cid={}
for idx,r in enumerate(rows):
    em=g(r,"Work Email").lower()
    if em and em in email2id: row_cid[idx]=email2id[em]
    else:
        li=g(r,"LinkedIn Profile")
        if li.lower() in li2id: row_cid[idx]=li2id[li.lower()]
missing=[idx for idx in range(len(rows)) if idx not in row_cid]
print(f"recovered {len(row_cid)} existing contacts | missing (to create): {len(missing)}", flush=True)

def cprops(r):
    p={"firstname":g(r,"First Name"),"lastname":g(r,"Last Name"),"hs_lead_status":"NEW","hubspot_owner_id":MARIA}
    if g(r,"Job Title"): p["jobtitle"]=g(r,"Job Title")
    if g(r,"Work Email"): p["email"]=g(r,"Work Email")
    if g(r,"Mobile Phone"): p["mobilephone"]=g(r,"Mobile Phone")
    if g(r,"LinkedIn Profile"): p["hs_linkedin_url"]=g(r,"LinkedIn Profile")
    return p

# create missing individually with 409 handling
created=recovered409=failed=0
for idx in missing:
    r=post("/crm/v3/objects/contacts",{"properties":cprops(rows[idx])})
    if r.status_code<300:
        row_cid[idx]=r.json()["id"]; created+=1
    elif r.status_code==409:
        m=re.search(r"Existing ID:\s*(\d+)",r.text)
        if m: row_cid[idx]=m.group(1); recovered409+=1
        else: failed+=1; print("409 no id:",r.text[:150],flush=True)
    else:
        failed+=1; print("CREATE ERR",r.status_code,r.text[:150],flush=True)
    if (created+recovered409+failed)%100==0: print(f"  creating... {created} new, {recovered409} conflict-recovered, {failed} failed",flush=True)
print(f"create done: {created} new | {recovered409} conflict-recovered | {failed} failed",flush=True)

# associations (correct endpoint) for ALL rows
pairs=[]
for idx,r in enumerate(rows):
    cid=row_cid.get(idx); comp=g(r,"HubSpot Company Id")
    if cid and comp: pairs.append({"from":{"id":str(cid)},"to":{"id":str(comp)}})
aok=0
for i in range(0,len(pairs),100):
    r=post("/crm/v4/associations/contacts/companies/batch/associate/default",{"inputs":pairs[i:i+100]})
    if r.status_code<300: aok+=len(pairs[i:i+100])
    else: print("ASSOC ERR",r.status_code,r.text[:150],flush=True)
    time.sleep(0.05)
print(f"associations: {aok}/{len(pairs)}",flush=True)

# list add ALL (idempotent)
allids=[str(row_cid[i]) for i in range(len(rows)) if i in row_cid]
added=0
for i in range(0,len(allids),100):
    r=requests.put(f"{B}/crm/v3/lists/{LIST_ID}/memberships/add",headers=H,json=allids[i:i+100],timeout=60)
    if r.status_code<300: added+=len(allids[i:i+100])
    else: print("LIST ERR",r.status_code,r.text[:150],flush=True)
    time.sleep(0.05)
print(f"\nDONE: total contacts mapped {len(allids)}/{len(rows)} | assoc {aok} | list-add calls covered {added}",flush=True)
