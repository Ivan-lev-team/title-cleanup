# Levanta Lead Screening + Enrichment + HubSpot Push - PLAYBOOK

The full method for taking a raw list of contacts/companies and turning it into a
clean, ICP-screened, enriched, HubSpot-pushed list. Any new chat can read this and
run the whole thing. The **engine is the code in this folder**; this doc is the
operating manual + the decisions/IDs that aren't obvious from the code.

---

## 0. TL;DR flow
1. **Dedupe to unique companies** (a list of N contacts is usually far fewer companies).
2. **Free company screen** (no API $): keyword blocklist of obvious non-ICP (big tech, industrial, mega-CPG, SaaS, agencies, CJK/unreachable) + match against a HubSpot company export.
3. **Company ICP judge** (Anthropic) on only the *unknown* remaining companies.
4. **Title gate** (free regex) on every contact.
5. **Enrich** the survivors: email + mobile + firmographics (waterfalls below).
6. **Dedupe contacts vs HubSpot** by LinkedIn URL (and email).
7. **Push** net-new to HubSpot (contacts + companies + associations + a static list + owner/pod).
> "Companies-first" is the cost lever: judge/enrich per *unique company*, not per contact.

---

## 1. Where everything lives
- **Code (the engine):** `C:\Users\Ivan\Projects\levanta-event-pipeline\event-pipeline-service`, git branch **`claude/waterfall-enrichment`** (the complete one - has ENRICH_ONLY, firmographics, all waterfalls). The `job-title-classification` branch is older/partial.
- **Keys:** `.env` in that folder (git-ignored). Holds: `ANTHROPIC_API_KEY`, `HUBSPOT_TOKEN`, `LEADMAGIC_KEY`, `PROSPEO_KEY`, `FORAGER_KEY`, `FORAGER_ACCOUNT_ID=1129`, `STORELEADS_KEY`, `ZENROWS_KEY`. (For CSV-only work set `GOOGLE_SERVICE_ACCOUNT_JSON=unused` + `GOOGLE_SHEET_ID=unused` so `config.py` imports.)
- **Server (runs the sheet pipeline automatically):** `178.105.190.88`, `/opt/levanta-event-pipeline/event-pipeline-service`, systemd unit `levanta-event-pipeline`. SSH as `root`. Google service account JSON on server at `/etc/levanta-event-pipeline/google_service_account.json`.
- **Repo:** `github.com/ivan-lev-team/title-cleanup.git` (named "title-cleanup" - easy to lose).

## 2. Providers / APIs (what each is for + cost)
| Provider | Used for | Cost note |
|---|---|---|
| **Anthropic** (`claude-sonnet-5`) | Company ICP judgment | ~$0.0018/company (Sonnet); Haiku ~$0.0006 - use Haiku for big/cheap runs |
| **LeadMagic** (`lm_live_...`) | Email finder, company firmographics, mobile (tier), b2b-profile | email ~1cr, **free on miss**; company-search 1cr/hit; mobile ~10cr |
| **Prospeo** (`pk_...`) | Email, mobile, company enrich, revenue | email ~1cr, **mobile ~10cr**; company enrich 1cr. **Watch credits - runs dry.** |
| **Forager** (`FORAGER_ACCOUNT_ID=1129`) | Mobile tier-2 (needs LinkedIn handle) | per-hit |
| **StoreLeads** | Ecommerce revenue + firmographics (Shopify-native) | per-lookup; best revenue signal for DTC brands |
| **ZenRows** | Last-resort LinkedIn discovery (scrapes DuckDuckGo) | slow (up to 70s/call) - dormant fallback |
Each waterfall tier auto-skips if its key is blank.

## 3. ICP judgment
Two independent gates - FAIL on **either** = drop the contact.
- **Title gate** (`classify_titles.py`, free regex): decision-maker/e-comm functions PASS; HR/IT/finance/eng/support/etc FAIL; empty/ambiguous = **default PASS**.
- **Company ICP judge** (`qualify.py::company_icp_judge`, one Anthropic call): verdict **PASS** (physical consumer-product brand selling Amazon/Walmart/Shopify/DTC), **AGENCY** (Amazon/marketing agency = channel partner), **TECH** (tech provider partner), **FAIL** (SaaS/industrial/finance/events/unidentifiable). Parse-failure defaults to PASS (never silently drop).
> **Confirming "actually sells products" (e.g. Shopify lists):** use **StoreLeads** - a live store with `platform=shopify`, product count, and `estimated_sales` is the hard "yes, they sell products" signal. Combine with the ICP judge for fit.

## 4. Free company screen (do BEFORE spending on the judge)
- **Keyword blocklist** (see the `SUB`/`WORD` dicts in the screening scripts): HP/Flex/Pentair/IBM/Adobe/Microsoft/Intel/Bayer, mega-CPG (Kraft/Pepsi/Frito/Coca-Cola), SaaS (Highspot/EvenUp), distributors (Essendant/United Stationers), agencies (Nebo), events (IRONMAN), consulting, etc. Free, instant.
- **CJK exclusion**: drop rows whose company/name contain Chinese/Japanese/Korean chars (unreachable).
- **HubSpot company export match**: match unique companies (normalized name, and domain) against a HubSpot companies CSV export to (a) mark already-in-HubSpot, (b) pull domain/industry/pod/owner. Only the *still-unknown* companies go to the paid judge.

## 5. Enrichment waterfalls (`enrichment.py`)
- **Email:** `find_email` = **LeadMagic -> Prospeo**, stop at first VERIFIED. Only strictly-valid emails auto-used; catch-all/unknown go to Notes. LeadMagic first because it's free-on-miss.
- **Mobile:** `find_mobile` = **Prospeo -> Forager -> LeadMagic**, stop at first hit. Gated by `ENRICH_MOBILE`. Mobiles ~10x the cost of emails - enrich phones only on a priority subset.
- **Firmographics:** `find_firmographics(domain, hs_rev_code, company_name)` = **StoreLeads + LeadMagic merged** for every field, **Prospeo only to fill a leftover gap**, then a **Claude last-resort tier** (`zenrows_client.company_context` scrapes the company's own site -> `enrichment.llm_fill_firmographics` extracts HIGH-CONFIDENCE values only, null if unsure -- never guesses). Fires only on remaining gaps. Fills: Estimated Revenue (USD), Employee Count, Industry, Company LinkedIn, Founded, City/State/Country. Works even with Prospeo dry. ZenRows escalates plain->premium->premium+JS proxy to beat anti-bot 422s.
- **Revenue = MAX across all sources** (StoreLeads $ + LeadMagic band + Prospeo + any HubSpot figure). Rationale: a brand may sell across Shopify + Amazon + Walmart, so no single channel should understate it.
- **Domain policy:** explicit domain, else derive from a CORPORATE email (never gmail/yahoo), else identity-resolution (reverse email->LinkedIn->company, ZenRows last).

## 6. Qualification (lenient - don't over-disqualify)
`qualified = has_pod OR (is_brand AND (revenue unknown OR revenue >= $1M))`.
- Codes (HubSpot `estimated_annual_revenue`): 0=$0-10k,1=$10k-100k,2=$100k-1M,3=$1M-10M,4=$10M+. `QUALIFIED_REVENUE_CODES = {3,4}`.
- **Disqualify a Brand ONLY when revenue is confidently < $1M across every source.** Unknown revenue = keep (multi-marketplace brands are invisible to single sources).
- Contact-detail enrichment (work email + mobile) runs only for Qualified rows; firmographics run for all classified rows with a domain.

## 7. HubSpot dedupe (avoid re-adding/re-enriching)
- **Contact-level:** match by LinkedIn URL. Props: `hs_linkedin_url` (stored `https://linkedin.com/in/<handle>`, no www) and `linkedin_personal_url` (`https://www.linkedin.com/in/<handle>`, www). Batch with `IN` operator, ~100 handles/batch, query both props (OR via two filterGroups). Also match by `email`.
- **Company-level:** match the company against HubSpot companies (by normalized name / domain).

## 8. HubSpot push (contacts + companies + list)
- **Contacts:** create new; if the enriched email already exists -> update instead (dedupe on email). Batch create fails the WHOLE batch of 100 if one record conflicts -> on conflict, fall back to per-record create and parse `Existing ID:` from the 409.
- **Companies:** update `domain`, `amazon_storefront_url`; set `sdr_owner`/`pod`/`hubspot_owner_id` only where blank or already the target (preserve other SDRs' assignments unless told to overwrite).
- **NEVER write a domain unverified** (see §8a). A marketplace seller name matching a domain is not evidence; that assumption is what put `asrhealthbenefits.com` on PhysiciansCare and `amazon.com` on 14 companies in list 7565.

## 8a. Domain verification gate (`scripts/fix_list_domains.py`)
Repairs domain/storefront fields on a company list. Also the reference implementation of the verification gate any future domain write should reuse.
- **Sanitize first, free:** a previous import wrote the literal string `"True"` into `amazon_storefront_url` (98 records) and `website` (95). Treat `true/false/none/null/n-a` as junk, not values. Also: Walmart/Etsy URLs land in `amazon_storefront_url` (route Walmart -> `walmart_storefront_url`), and one generic Amazon `/page/<GUID>` gets reused across unrelated brands - detect a page GUID claimed by >1 company and clear it.
- **Waterfall is ZenRows-first** (credits are stacked, and scraping beats paid lookups here): storefront scrape -> search-result scrape -> slug guesses -> StoreLeads -> Clay -> LeadMagic/Prospeo.
- **Rank candidates by SOURCE tier before slug match.** Slug guesses trivially contain the brand name, so ranking on slug match alone starves real search hits and the right answer never gets evaluated.
- **Verifier (Anthropic, fails CLOSED):** verdict `MATCH`/`RESELLER`/`MISMATCH`/`PARKED` + confidence; auto-write only `MATCH` >= 80. Three rules earned from live misfires:
  1. Tell it every brand on the list is a *physical consumer-product* seller - otherwise a same-name software company (`vet-one.com`) or safari agency (`kusini-safaris.com`) scores MATCH 90+.
  2. Downgrade a MATCH on a **subdomain whose registrable name is a third party** (`vetone.vetsfirstchoice.com.au`) - the model reads distributor pages as official. Apex parent-company sites (`acmeunited.com` for PhysiciansCare) are legitimate and must NOT be downgraded.
  3. With no storefront text there is no product context to corroborate a name match, so require the homepage itself to prove the brand.
- **Never clear an existing domain you could not fetch.** ZenRows fails transiently on live sites (`labcharge.com` returned 0 chars mid-run, 6000 on retry). Clear a domain only when the verifier actually returned a negative verdict *for that domain*; otherwise keep it and flag. Do not cache empty fetches - a cached miss disqualifies a good domain forever.
- **Discovery is a search-engine job, not an LLM job.** Measured: one Claude `web_search` call costs **18.4s per company** and was 93% of per-company latency; a ZenRows SERP scrape costs **1.4s**. Discovery runs on ZenRows by default; the Claude-search functions stay behind `--llm-search` for re-running an unresolved tail. Keep the Anthropic **verifier** either way -- it is the correctness gate and only ~$5 per 565 companies.
- **Search engines: DuckDuckGo primary, Bing fallback, Google unusable.** Google returns 0 bytes through ZenRows (blocked). Bing does **not** beat DDG on the hard cryptic brands -- identical for WKONCLDY, worse for DECYOOL (Brazilian classifieds instead of `decool.store`) -- so it runs only when DDG yields <2 candidates. Querying both everywhere doubles ZenRows calls for almost no new candidates.
- **Chris's method, automated:** search the brand name TOGETHER WITH a product from its Amazon storefront. This is the single biggest recall win and it needs the chrome-stripping above to work. It surfaced `wkoncldystore.com`, `sewantausa.com` and `cornstick.com`, all of which the brand-name-only query missed.
- **Blocklist aggregators aggressively.** `manuals.plus`, `manuals.ca`, `cherrypicksreviews.com`, `yellowpages.com`, Bing SERP chrome (`live.com`, `msn.com`) and regional classifieds (`olx.com*`) rank well for obscure brand names. The verifier rejects them correctly, so they cause no bad writes -- blocking them just stops paying a verify call to find that out. Adding them lifted the hit rate from 82.5% to 90%.
- **Bound the ZenRows fetch.** `zenrows_client._fetch` escalates through 3 proxy variants at a **70s timeout each**; wrapping it in a retry made one dead domain cost up to 420s, which stalled a 565-company run at company 12. Use a bounded fetch (one plain attempt, one premium, ~15-25s each) and never escalate to `js_render` for bulk probing.
- **Use `as_completed`, never `pool.map`.** `map` delivers results in submission order, so one slow company head-of-line blocks the progress log, the cache save AND the incremental flush -- the run looks frozen while workers are busy, and a crash discards everything since the last save.
- **Flush results incrementally** so an interrupted run keeps its progress and a re-run costs nothing for what already completed.
- **No verified site:** leave `domain` blank, set `marketplaces` from observed Amazon/Walmart/Shopify presence, and attach a note listing what was checked. Many of these sellers genuinely have no brand site; that is the correct outcome, not a failure.

## 8b. StoreLeads enrichment (`scripts/enrich_shopify_storeleads.py`)
Runs AFTER the domain repair, because StoreLeads is keyed by domain - a wrong domain returns another company's firmographics.
- **`_get_store()` returning a store does NOT mean Shopify.** StoreLeads tracks WooCommerce, Magento, BigCommerce, Wix, Squarespace and custom carts too. Always gate on `store["platform"] == "shopify"`. Checking only `bool(store)` over-tagged 44 of 149 companies as Shopify on list 7565.
- **Affiliate buying signal:** `store["apps"]` lists installed Shopify apps, so a store on GoAffPro/UpPromote/Refersion/Snowball is already running affiliates in-house (`affiliate_history` = `Yes - Managed InHouse`), and one on Awin/ShareASale/impact.com/AvantLink/FlexOffers is on a network (`Yes - Large Affiliate Networks`). 28 of 105 Shopify stores on list 7565 already run a program.
- **Revenue field mapping:** banded -> `estimated_annual_revenue` (codes 0-4), exact -> `annualrevenue`. **Never** write it to `est__monthly_revenue` - that field is "Amazon - Estimated MRR", and StoreLeads only measures the SHOPIFY channel. Because it excludes Amazon it can understate these brands, so fill blanks only and never overwrite (consistent with the MAX-across-sources rule in §6).
- `store["sales_channel_urls"]["Amazon"]` fills Amazon storefront gaps the domain pass could not find.
- HubSpot `industry` is a LinkedIn-style enum, not retail categories. Map StoreLeads category leaves explicitly and leave unmapped ones blank - a wrong industry is worse than an empty one.
- **Associations:** `POST /crm/v4/associations/contacts/companies/batch/associate/default` (NOT `.../batch/create/default` - that 404s). Body: `{"inputs":[{"from":{"id":cid},"to":{"id":companyId}}]}`.
- **Static list add:** `PUT /crm/v3/lists/{listId}/memberships/add` with a JSON array of contact ids.
- **Owner/pod:** `pod` is an enum (`Pod 1`..`Pod 7`, `Pod RevOps`); `sdr_owner` = an owner id string.

## 9. The server sheet-pipeline (runs on its own)
- Polls the Google Sheet **Contact Import** tab every 300s; enriches new rows in place and writes back.
- **Mode flags** in server `.env`: `ENRICH_ONLY=true` (enrich to sheet, no HubSpot push, no routing), `DRY_RUN=true`, `ENRICH_MOBILE=false`, `ENRICH_FIRMOGRAPHICS=true`. To make it push to HubSpot + get phones: `ENRICH_ONLY=false`, `DRY_RUN=false`, `ENRICH_MOBILE=true`, then restart.
- Sheet: `Event Import Template - Levanta`, id `1J9lg8KZhVVr5yaVQn_545I4ofG-tlUf9EY-Dw2O1c24`, tab `Contact Import`. Bulk lists go through the manual scripts, NOT this trickle poller.
- `write_result` batches all of a row's cells into ONE Sheets write (Google caps 60 writes/min/user - per-cell writes 429-crash the cycle).

## 10. Key IDs / facts
- **Maria = Maria Medel**, owner id **`80046048`**, in **Pod 1**. (mariaceleste@ = Celeste, different person.)
- Static list **`7519`** = "Maria - Latest Enriched Contacts" (MANUAL/static, contacts).
- Sheet id + tab: see section 9.

## 11. Reusable scripts (this folder's `scripts/` and past session scratchpads)
- Company screen + judge (dedupe -> keyword filter -> HubSpot-ref match -> Sonnet judge on unknowns).
- LinkedIn contact dedupe vs HubSpot (batched IN on hs_linkedin_url + linkedin_personal_url).
- Enrichment runner (email + mobile via `enrichment.find_email`/`find_mobile`, threaded ~6 workers).
- HubSpot push (create/update contacts, update companies, associate, list add).
> These import the engine (`enrichment`, `qualify`, `hubspot_client`, provider clients). Run from the service folder (or `os.chdir` to it) so `config.load_dotenv()` finds `.env`.

## 12. Lessons / pitfalls (learned the hard way)
- **Test any bulk HubSpot/Sheets write on 5 rows first** - validate the exact endpoint + conflict handling before firing thousands.
- **Prospeo runs out of credits** - firmographics/mobile degrade silently (`INSUFFICIENT_CREDITS` swallowed as empty). StoreLeads+LeadMagic firmographics avoid this dependency.
- **Sheets 60-writes/min** - always batch per-row writes.
- **HubSpot batch-create** dies on the first conflict in a batch - handle per-record fallback.
- **`pod` / `sdr_owner` / `estimated_annual_revenue` are silently dropped on company CREATE** (`POST /crm/v3/objects/companies`) - the call returns 201 and `hubspot_owner_id` sticks, but those three come back null. They persist fine on `PATCH`. Always create the company, then PATCH routing props, then verify. (Hit on the ZonGuru July 23 webinar push: 10 of 13 created companies came back with pod=null.)
- **Company duplicates**: a contact's `associatedcompanyid` (primary company) is often an older Amazon-storefront record (`domain=amazon.com`), not the brand-domain company the CSV routed. Read ALL associations (`GET /crm/v4/objects/contacts/{id}/associations/companies`), not just the primary, when verifying pod/owner coverage.
- **`pod` now has a `Pod Partnership` option** (companies, fieldType=checkbox) - agencies/tech partners no longer need pod left blank; map the sheet's "Partnership" to it.
- **Cost:** dedupe to unique companies + free keyword pre-filter does 80% of the work for $0; only unknown companies hit the paid judge; email is cheap, mobile is ~10x.
- **Model:** Sonnet for accuracy on small runs; Haiku for large runs (validate a sample).
- Ask the user for exact file paths (don't scan the filesystem).
