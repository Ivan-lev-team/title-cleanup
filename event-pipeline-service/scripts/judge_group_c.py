#!/usr/bin/env python3
"""Group C: judge each ambiguous subdomain -- genuinely distinct business, or
another face of the parent? DRY RUN, writes nothing to HubSpot."""
import json, os, re, sys, threading, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(HERE)
from dotenv import dotenv_values
import anthropic
client = anthropic.Anthropic(api_key=dotenv_values(".env")["ANTHROPIC_API_KEY"])

SYSTEM = """You decide whether a Shopify store on a SUBDOMAIN is a genuinely
DISTINCT business from the company that owns the root domain, or just another
face of that same company.

DISTINCT ("distinct") -- it should exist as its own workable company record:
- a different company operating a store under a partner's domain
  (costco.logobrands.com = Logo Brands' Costco-specific channel business)
- a separate product line or brand with its own customers
- a wholesale/B2B arm that sells to a completely different buyer than the
  consumer parent, with its own commercial relationship
- a merch/licensing business attached to a non-commerce parent
  (shop.<newspaper>.com selling branded goods is a distinct merch operation)

SAME ("same") -- collapse it into the parent:
- the parent's own online store on a subdomain (shop./buy./store.)
- a landing, campaign, checkout, returns, help or support surface
- a duplicate of the parent's catalogue with no separate business identity
- a regional storefront of the same brand

Judge from what is actually sold and to whom. When the subdomain is simply the
brand's own shop, that is SAME even if the label looks commercial. Reserve
"distinct" for a real separate commercial entity or a genuinely separate
customer base. If it is a close call, prefer "same" ONLY when the evidence
points that way -- do not use "same" as a default for missing evidence; use
your judgement and say so in the reason.

Respond with ONLY a JSON array, same order and count as the input:
[{"i":1,"verdict":"distinct"|"same","reason":"one short clause"}]"""


def ask(batch):
    lines = []
    for i, b in enumerate(batch):
        e = b.get("export") or {}
        lines.append(
            f'{i+1}. child={b["child_domain"]} label="{b["label"]}" '
            f'child_name="{b["child_name"][:40]}" | parent="{b["parent_name"][:40]}" '
            f'({b["parent_domain"]}) | cats={(e.get("categories") or "")[:60]} '
            f'| desc={(e.get("description") or "")[:150]}')
    for attempt in range(3):
        try:
            r = client.messages.create(
                model="claude-sonnet-5", max_tokens=1600,
                system=[{"type": "text", "text": SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": "\n".join(lines)}])
            t = next((c.text for c in r.content if getattr(c, "type", None) == "text"), "").strip()
            if t.startswith("```"):
                t = t.strip("`")
                if t.lower().startswith("json"):
                    t = t[4:].strip()
            data = json.loads(t)
            if len(data) != len(batch):
                raise ValueError("count mismatch")
            out = {}
            for o in data:
                idx = int(o["i"]) - 1
                v = str(o.get("verdict", "")).lower()
                if 0 <= idx < len(batch) and v in ("distinct", "same"):
                    out[batch[idx]["child_domain"]] = (v, str(o.get("reason", ""))[:160])
            return out
        except Exception:
            if attempt == 2:
                return {}
            time.sleep(2 * (attempt + 1))


def main():
    gdir = sys.argv[1]
    C = json.load(open(os.path.join(gdir, "group_C.json"), encoding="utf-8"))
    print(f"judging {len(C):,} ambiguous subdomains")
    batches = [C[i:i+10] for i in range(0, len(C), 10)]
    res, lock, t0 = {}, threading.Lock(), time.time()
    with ThreadPoolExecutor(max_workers=28) as ex:
        futs = [ex.submit(ask, b) for b in batches]
        for n, f in enumerate(as_completed(futs), 1):
            with lock:
                res.update(f.result() or {})
                if n % 40 == 0:
                    print(f"  {n}/{len(batches)} batches  {(time.time()-t0)/60:.1f}m", flush=True)
    unresolved = 0
    for r in C:
        hit = res.get(r["child_domain"])
        if hit:
            r["verdict"], r["reason"] = hit
        else:
            r["verdict"], r["reason"] = "same", "judge failed; defaulted to collapse"
            unresolved += 1
    vc = Counter(r["verdict"] for r in C)
    print(f"\nverdicts: {dict(vc)}  (judge failures defaulted to 'same': {unresolved})")
    print(f"  distinct -> import as NET-NEW with a parent link: {vc['distinct']:,}")
    print(f"  same     -> collapse into parent (enrich-only)  : {vc['same']:,}")
    lab = Counter((r["label"], r["verdict"]) for r in C)
    print("\nverdict by label (top 24):")
    for (l, v), c in lab.most_common(24):
        print(f"   {l[:22]:22} {v:8} {c:5,}")
    json.dump(C, open(os.path.join(gdir, "group_C_judged.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    with open(os.path.join(gdir, "group_C_review.csv"), "w", encoding="utf-8-sig", newline="") as f:
        import csv as _csv
        w = _csv.writer(f)
        w.writerow(["child_domain", "label", "child_name", "parent_name", "parent_domain",
                    "parent_pod", "verdict", "reason"])
        for r in sorted(C, key=lambda x: (x["verdict"], x["label"])):
            w.writerow([r["child_domain"], r["label"], r["child_name"], r["parent_name"],
                        r["parent_domain"], r["parent_pod"], r["verdict"], r["reason"]])
    print(f"\nwrote group_C_judged.json + group_C_review.csv to {gdir}")


if __name__ == "__main__":
    main()
