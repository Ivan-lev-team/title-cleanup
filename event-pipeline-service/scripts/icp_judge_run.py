import sys, csv, json, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\Ivan\Projects\levanta-event-pipeline\event-pipeline-service")

# load key from the project's .env without importing config.py (which _requires other vars)
from dotenv import dotenv_values
KEY = dotenv_values(r"C:\Users\Ivan\Projects\levanta-event-pipeline\event-pipeline-service\.env")["ANTHROPIC_API_KEY"]
import anthropic
client = anthropic.Anthropic(api_key=KEY)

# ICP prompt now lives in icp_prompt.py -- single source shared with qualify.py
from icp_prompt import ICP_PROMPT_4WAY as ICP_PROMPT

def judge(company, domain="(no domain provided)"):
    if not company:
        return "FAIL", "no company name"
    prompt = ICP_PROMPT.format(company=company, domain=domain)
    for attempt in range(3):
        try:
            resp = client.messages.create(model="claude-sonnet-5", max_tokens=200,
                messages=[{"role": "user", "content": prompt}])
            text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "").strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.lower().startswith("json"): text = text[4:].strip()
            data = json.loads(text)
            v = data.get("verdict", "").upper(); r = data.get("reason", "")
            if v not in ("PASS", "AGENCY", "TECH", "FAIL"): raise ValueError(v)
            return v, r
        except Exception as e:
            if attempt == 2:
                return "PASS", f"judge error, defaulted PASS: {e}"
            time.sleep(1.5 * (attempt + 1))

def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if len(sys.argv) < 3:
        sys.exit("usage: icp_judge_run.py <limit|0> <input.csv> [output.tsv]")
    src = sys.argv[2]
    rows = list(csv.DictReader(open(src, encoding="utf-8-sig")))
    from collections import Counter
    comp = Counter((x.get("Company") or "").strip() for x in rows)
    uniq = [c for c, _ in comp.most_common()]
    if limit: uniq = uniq[:limit]
    print(f"judging {len(uniq)} unique companies...", flush=True)
    results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(judge, c): c for c in uniq}
        done = 0
        for f in as_completed(futs):
            c = futs[f]; results[c] = f.result(); done += 1
            if done % 50 == 0: print(f"  {done}/{len(uniq)}", flush=True)
    out = sys.argv[3] if len(sys.argv) > 3 else "icp_verdicts.tsv"
    with open(out, "w", encoding="utf-8") as fo:
        fo.write("COUNT\tVERDICT\tCOMPANY\tREASON\n")
        for c in uniq:
            v, r = results[c]; fo.write(f"{comp[c]}\t{v}\t{c}\t{r}\n")
    vc = Counter(v for v, _ in results.values())
    rowc = Counter()
    for c in uniq: rowc[results[c][0]] += comp[c]
    print("VERDICTS (unique companies):", dict(vc))
    print("VERDICTS (rows):", dict(rowc))
    print("wrote", out)

if __name__ == "__main__":
    main()
