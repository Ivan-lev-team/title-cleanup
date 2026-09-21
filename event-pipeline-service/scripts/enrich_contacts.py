import sys, os, csv, time
sys.stdout.reconfigure(encoding="utf-8")
SVC = r"C:\Users\Ivan\Projects\levanta-event-pipeline\event-pipeline-service"
os.chdir(SVC)              # so config.load_dotenv() finds .env
sys.path.insert(0, SVC)    # so local modules import regardless of cwd
from concurrent.futures import ThreadPoolExecutor, as_completed
import enrichment

DL = r"C:\Users\Ivan\Downloads"
C = DL + r"\42k Contacts - Maria-CLEANED.csv"
OUT = DL + r"\42k Contacts - Maria-ENRICHED-2k.csv"
N = 2000

rows = list(csv.DictReader(open(C, encoding="utf-8-sig")))
target = [r for r in rows if r.get("Contact in HubSpot?") == "No" and (r.get("Company Domain") or "").strip()][:N]
print(f"enriching {len(target)} net-new-with-domain contacts (email + phone)", flush=True)

NEWCOLS = ["Work Email", "Email Verified", "Email Provider", "Unverified Email", "Mobile Phone", "Mobile Provider"]

def enrich(r):
    fn = (r.get("First Name") or "").strip(); ln = (r.get("Last Name") or "").strip()
    full = (r.get("Full Name") or f"{fn} {ln}").strip()
    co = (r.get("Company") or "").strip(); dom = (r.get("Company Domain") or "").strip()
    li = (r.get("LinkedIn Profile") or "").strip()
    out = dict(r)
    for k in NEWCOLS: out[k] = ""
    try:
        em = enrichment.find_email(fn, ln, full, co, dom, linkedin_url=li)
        out["Work Email"] = em.get("email", "")
        out["Email Verified"] = "Yes" if em.get("email") else ""
        out["Email Provider"] = em.get("provider", "")
        out["Unverified Email"] = em.get("unverified_email", "")
        best_email = em.get("email") or ""
    except Exception as e:
        out["Email Provider"] = f"ERR:{type(e).__name__}"; best_email = ""
    try:
        mob = enrichment.find_mobile(full, fn, ln, co, dom, linkedin_url=li, email=best_email)
        if isinstance(mob, dict):
            out["Mobile Phone"] = mob.get("mobile", ""); out["Mobile Provider"] = mob.get("provider", "")
    except Exception as e:
        out["Mobile Provider"] = f"ERR:{type(e).__name__}"
    return out

results = [None] * len(target)
fields = list(rows[0].keys()) + [c for c in NEWCOLS if c not in rows[0].keys()]

def flush(done_ct):
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=fields); wr.writeheader()
        for r in results:
            if r: wr.writerow({k: r.get(k, "") for k in fields})

done = em_hits = mob_hits = 0
with ThreadPoolExecutor(max_workers=6) as ex:
    futs = {ex.submit(enrich, r): i for i, r in enumerate(target)}
    for fut in as_completed(futs):
        i = futs[fut]; results[i] = fut.result(); done += 1
        if results[i].get("Work Email"): em_hits += 1
        if results[i].get("Mobile Phone"): mob_hits += 1
        if done % 100 == 0:
            print(f"  {done}/{len(target)} | emails={em_hits} mobiles={mob_hits}", flush=True)
            flush(done)
flush(done)
print(f"\nDONE: {done} processed | emails {em_hits} ({em_hits*100//max(done,1)}%) | mobiles {mob_hits} ({mob_hits*100//max(done,1)}%)", flush=True)
print("wrote", OUT, flush=True)
