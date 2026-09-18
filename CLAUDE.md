# CLAUDE.md — Lead Finder: rules, behavior, and system reference

This file is the **single source of truth** for how Lead Finder behaves:
every heuristic, threshold, filter list, safety rule, and deliberate
non-goal that is currently implemented in the code. It exists so the
product can be run and evolved by an agent (or a new contributor) without
re-deriving all of this from the source each time.

**Maintenance protocol — read before changing anything:**

1. Any change to a rule, threshold, list, filter, template, env var, data
   schema, or safety gate **must** update the matching section here in the
   same change. A code change that contradicts this file is a bug in one of
   the two.
2. When a rule changes, keep a one-line note in **Change log** at the
   bottom (date + what changed + why).
3. If you add a new module, service, channel, or data file, add it to the
   relevant section *and* to `README.md`'s user-facing description.
4. Keep this file factual and specific (names, numbers, file paths). It is
   not marketing copy — that's `README.md`.
5. If code and this file disagree and you can't tell which is right, stop
   and flag it rather than guessing.

---

## 1. Product

**What it is:** a Streamlit dashboard that finds prospective B2B clients by
service + US state, scrapes their publicly listed contact info, scores how
trustworthy that info is, drafts tailored outreach per lead, and can send
it by email (SMTP), SMS (8x8), or both together per lead (Bulk Send, §14).

**Direction:** currently a single-user local tool; being turned into a
product, and ultimately into an **agent-driven** system where searching,
scoring, template upkeep, and sending are orchestrated rather than clicked
through manually. This file is the contract that makes that possible.

**Active scope right now:**
- One active service: **Bookkeeping** (`states.ACTIVE_SERVICES`).
- Bookkeeping fans out across **8 target verticals** (see §4).
- Email sending: configured and live. SMS: configured and live (8x8
  credentials added 2026-09-15).

**Guiding principles baked into the code:**
- *Precision over recall* for anything that feeds real outreach (owner
  names, "High" confidence, sendable emails). A wrong result here emails
  the wrong person.
- *Filter before scrape.* Junk is removed from search results before any
  site is fetched — faster and more accurate than cleaning up after.
- *Trust the source, not the string.* How a piece of data was obtained
  (mailto: link vs. structured data vs. bare regex) matters more than
  whether it exists.
- *Nothing sends by accident.* Sending is off until credentials are added;
  every batch needs an explicit confirmation checkbox; test-send first.

---

## 2. Stack, layout, how to run

- Python 3.11, Streamlit 1.38, pandas 2.2, openpyxl 3.1 (Excel export).
  Deps: `environment.yml` (conda, canonical) / `requirements.txt` (pip
  fallback).
- Run: `streamlit run app.py` → `http://localhost:8501`.
- Entry point: [`app.py`](app.py) — all UI, 6 tabs (Find Leads, Lead
  Database, Email Template, Send Emails, Send Texts, Bulk Send). ~2,125 lines.
- Logic package: [`lead_finder/`](lead_finder/).
- All persistent state is flat files in [`data/`](data/) (see §12).
- Theme: `.streamlit/config.toml` (dark theme + `server.headless`).
- `.env` holds all secrets and is gitignored. `.env.example` is the
  template. Git repo since 2026-09-15, pushed to a public GitHub remote.

### Module responsibilities

| Module | Responsibility | Must NOT |
|---|---|---|
| `app.py` | All UI, wiring, background-thread orchestration for sends | Contain business rules that aren't mirrored here |
| `states.py` | Static data: states, `ACTIVE_SERVICES`, `SERVICE_PRESETS`, target-vertical definitions (incl. `query_terms`), `MAJOR_CITIES_BY_STATE`, `EXTRA_METRO_AREAS_BY_STATE`, search-target mapping | Do any I/O |
| `osm_source.py` | OpenStreetMap Overpass API queries, one call per `(k,v)` tag | Raise on failure (returns `[]`) |
| `search.py` | `ddgs` web search + query-variant builder | Use any paid/keyed backend |
| `blocklist.py` | Pre-scrape result filtering + name-quality checks + email-prefix screening + relevance check + blocked-domain sets | Fetch anything |
| `junk_filter.py` | **Final** post-scrape/post-score pass on finished rows: off-target business types, directory domains, broken names (§6a) | Do network I/O; be aggressive enough to drop real leads |
| `scrape.py` | Fetch homepage (+ one contact page), orchestrate extraction, normalize the company name, assemble the lead record, call scorer | Contain extraction regexes (those live in `extract.py`) |
| `extract.py` | Pull name/email/phone/owner/LinkedIn from HTML; authenticity checks; `score_lead` | Do network I/O |
| `browser_scrape.py` | Playwright re-scrape fallback for JS-rendered sites; reuses `scrape.scrape_company` via `html_override` | Duplicate extraction logic |
| `area_codes.py` | Area-code → state map; `phone_matches_state` (soft signal) | Hard-reject a lead |
| `storage.py` | `leads.csv` load/`save_leads` (default: replace with this run only; `replace=False` merges), dedupe by domain, empty-value normalization, `rescan_confidence`; per-run Excel archive to `data/exports/` (`save_export_snapshot`/`list_export_snapshots`/`load_export_snapshots`/`build_export_filename`); reading user-uploaded Excel/CSV files (`load_uploaded_snapshots`); blank-domain-safe dedup (`dedupe_keep_last`) | Accumulate across runs by default (`leads.csv` itself — the exports archive is deliberately the one place that DOES accumulate) |
| `email_template.py` | Per-service + per-vertical email templates, SMS templates, merge-field render to text/HTML | Send anything |
| `mailer.py` | SMTP send, rate-limited batch, sent-log, shared suppression list, HTML signature builder | Send without an explicit call |
| `texter.py` | 8x8 Connect SMS send, rate-limited batch, sent-text-log, E.164 formatting | Send without an explicit call |

---

## 3. The pipeline (Find Leads)

For each **state × target** (a "target" is a vertical when the service has
verticals, otherwise the service itself):

1. **OpenStreetMap, fetched in the BACKGROUND while web search (step 2) runs**
   (`use_osm`, default on; concurrency added 2026-09-14). `app.py` starts a
   daemon thread that calls `osm_source.query_osm_by_tags(tags, state_abbr)`
   — which itself issues **one Overpass call per `(k,v)` tag** (not one
   combined call for all of a vertical's tags — see §4a), each:
   `area["ISO3166-2"="US-<abbr>"]["admin_level"="4"]`, then `node`/`way` for
   that one tag — and stores the raw result list in a holder dict. The
   thread does ONLY the network fetch; nothing is turned into candidate
   rows until after `.join()`, which happens in the main thread right after
   the web-search loop (step 2) finishes. This keeps it race-free — no two
   threads ever write to `candidate_urls`/`osm_only_rows` at the same time
   — while overlapping OSM's wall-clock cost with the much longer search
   phase instead of paying for it up front.
   - Businesses **with a website** → merged into the scrape candidate pool
     (keyed by domain) **after** web search's own findings are already
     collected (see step 2) — OSM still wins a domain tie over web search
     (preserves the original OSM-first precedence, including its phone as
     a fallback for that domain, even though it's processed second now).
   - Businesses **without a website but with a name** → become
     `osm_only_rows`, scored on the spot, domain stored as
     `osm:<lowercased name>`.
   - Results are deduped across tags by `(name, website)`. Any one tag's
     call erroring → `[]` for that tag only; the others still run.
2. **Web search** (`search.build_queries` → `search.run_search`), running
   into its OWN dict (`web_candidate_urls`) while the OSM thread above
   fetches in the background. Query variants per target (see §5) —
   potentially **several search terms per vertical**, each expanded to its
   own state+city query set. Each result:
   - Dropped if no URL or contains `__ERROR__` (error surfaced to user).
   - Dropped if `filter_directories` on (default) **and**
     `blocklist.should_skip_result(...)` returns True (see §6).
   - Otherwise its domain is added to `web_candidate_urls` (first URL seen
     for a domain wins within web search; OSM can still override after
     the merge in step 1).
   - Stops adding once the pool hits `effective_max` (per target, not per
     state — see §4).
   - **Adaptive early-stop** (`no_new_streak_limit`, default 15, added
     2026-09-14): after each query, if it added zero new unique domains,
     increment a per-target streak counter; reset to 0 on any query that
     does add something. Once the streak hits the configured limit, stop
     issuing further query variants for this state+target — city-level
     queries in particular tend to keep re-finding the same businesses
     once a metro's real coverage is exhausted, and there's no reason to
     keep paying `SEARCH_QUERY_DELAY` + request time for queries that
     provably aren't finding anything new. A target that keeps finding new
     companies is never cut short by this — it only fires on genuine
     saturation.
   - **Search-failure circuit breaker** (`SEARCH_FAILURE_CIRCUIT_BREAKER =
     8`, hardcoded in `app.py`, added 2026-09-18) — a different signal
     than the streak above: a query that returns a single `__ERROR__` row
     means *every* configured engine failed *and* every `SEARCH_RETRIES`
     retry also failed — a total loss for that query, not just an
     unproductive one. A `consecutive_search_failures` counter (reset to
     0 by any query that returns anything else, success or legitimately
     empty) is tracked **across the whole run**, not per target. Once it
     hits 8 in a row, `web_search_disabled_due_to_failures` is set and
     stays set for the rest of the run: every subsequent state/target
     unit skips building/issuing web-search queries entirely
     (`queries = []`) and relies on OSM alone, which is unaffected by
     this (OSM is a separate Overpass API call, not a `ddgs` search).
     Exists because 8 fully-failed queries in a row is strong evidence
     the search backend is unreachable outright (most commonly a DNS
     resolution failure — `ddgs` hitting `search.yahoo.com`,
     `search.brave.com`, `www.bing.com`, etc. all fail with the identical
     "DNS error: Server Failure" pattern, which points at this machine's/
     network's DNS resolver, not per-engine rate-limiting), in which case
     grinding through the rest of a multi-hundred-query run — paying the
     full retry+backoff delay on every single guaranteed-to-fail query —
     wastes real time for zero benefit. Fires a `st.warning` the moment
     it trips (distinct message when the sampled errors mention "DNS
     error" — steers toward checking network/DNS rather than the usual
     "pin to one engine" advice, which doesn't help when every engine is
     failing identically) and again, more briefly, in the end-of-run
     summary. Verified in isolation (not by breaking real DNS): fed the
     exact counting logic a simulated sequence of error/success results
     and confirmed it resets correctly on any success and trips exactly
     on the 8th consecutive failure, not the 7th or 9th.
3. **Scrape** the candidate pool: `scrape.scrape_many(inputs, max_workers=concurrency)`
   (thread pool, `concurrency` = "Concurrent site scrapes", 4–50, **default
   25**). Results are returned in input order. Each site →
   `scrape.scrape_company` (see §7).
4. **Score** every scraped + OSM-only row (`extract.score_lead`, see §8).
5. **Junk pass** (`junk_filter.partition`, on by default, toggle: "Final
   junk filter" — see §6a): the final filter, on the finished rows, using
   the target vertical's own keywords. Drops directory/marketplace domains
   that slipped through, off-target business types (photographers/
   porta-potty/SEO agencies under an event search), and rows whose
   extracted name is a nav label. Dropped rows are counted and a sample is
   shown in the run summary. Off → every scraped row (per target) is kept.
6. **Auto browser-retry** (`auto_browser_retry`, default on **iff**
   Playwright + its browser binary are installed — checked once per
   session via `st.session_state.browser_available`, not on every rerun).
   Runs on `all_new_rows` **after every target has been scraped, before
   the High-only purge in step 7** — order matters: a lead only reaches
   High once the browser recovers an email/phone, so purging first would
   discard it before it got the chance. Candidates: `status ==
   "no_contact_found"`, a real (non-`osm:`) website, capped at
   `browser_retry_cap` (default 150, slider 0–1000) to bound worst-case
   runtime. Re-scrapes with `browser_scrape.rescrape_many_with_browser`
   (same extraction pipeline, rendered HTML) and overwrites those rows
   in place — a lead that gains contact info also gets re-scored.
7. **Auto-purge** (`auto_purge_low_confidence`, default **on**): drop every
   row whose `confidence_label != "High"`. Off → Medium/Low kept for review.
   High-only is intended — phone-only "High" rows (a real, in-state phone,
   no email) are wanted; do **not** add an email requirement to the High
   gate.
7a. **Stop button + incremental saving** (added 2026-09-18) — a
   "🛑 Stop search" button is rendered once, before the per-(state,
   business type) loop starts, and stays visible/clickable for the whole
   run (`app.py`, `run_clicked` block). Clicking it doesn't call any stop
   logic of its own — any Streamlit widget interaction during a running
   script aborts that script and starts a fresh one, and on that fresh
   run `run_clicked` (tied to the Find Companies button) is False, so the
   search block is simply never re-entered. What makes this a *good*
   stop rather than a data-losing one: after every unit fully completes
   (right where step 9's `st.write("✅ ...")` log line fires), whatever's
   in `all_new_rows` so far is immediately persisted —
   `storage.save_leads(rows_so_far, replace=True)`, with the same
   High-confidence purge applied inline if `auto_purge_low_confidence` is
   on — not just once at the very end. So an abort (Stop, or literally
   any other interruption — browser closed, network drop) loses at most
   the ONE unit that was still in flight; every unit that finished stays
   saved and immediately usable from Lead Database/Send Emails/Send
   Texts. Skipped by a stop: the browser-retry pass (step 6, only runs
   after the whole loop) and the auto-archived Excel snapshot (step 8
   below, also only on full completion) — both have manual equivalents
   already in the UI ("Retry with a real browser" / "Download as Excel"
   in Lead Database) if wanted after a partial run. Verified live: ran a
   real 2-vertical search, let unit 1 finish (18 kept, 5 after the
   High-only purge), clicked Stop mid-way through unit 2's first query,
   and confirmed — in a brand new browser session reading straight from
   disk — that exactly those 5 rows were there and nothing from the
   incomplete unit was.
8. **Store — REPLACE**: `storage.save_leads(kept, replace=True)` writes
   `data/leads.csv` to contain **exactly this run's kept rows**. Any
   previous run's data is discarded. The app never accumulates leads across
   runs. Dedupe within the run is by `domain` (`keep="last"`). If a run
   keeps **zero** rows, the previous file is left untouched and a warning
   is shown (so a failed/empty search doesn't wipe good data). (This is
   the same save the incremental step above already does per-unit — this
   is just the final call, now additionally covering the browser-retry
   pass's recovered leads.)
   **Also archives** (added 2026-09-15, non-critical — a failure here
   doesn't affect the save above): `storage.save_export_snapshot` writes
   the same kept rows as a standalone `.xlsx` into `data/exports/`, named
   `storage.build_export_filename(date, business_types, states)` →
   `date_businesstypes_states.xlsx` (e.g.
   `2026-09-15_EventManagement+FloralBusinesses_Alabama+Alaska.xlsx`;
   `all_states=True` collapses the states part to `AllStates` instead of
   listing all 51). Unlike `leads.csv`, this folder **does** accumulate —
   one file per run — so scraping and sending don't have to happen in the
   same session: Send Emails/Send Texts can load leads from any past
   run's snapshot instead of only whatever the most recent search left in
   `leads.csv` (see §14).
9. Run stats + `last_run_domains` are stored in session state. Since the DB
   is now always exactly one run, `last_run_domains` == every domain in the
   table, and every tab simply shows the whole table.

### Search-depth controls (Advanced expander)

Every filter in this expander is user-adjustable — nothing filters silently.
Laid out as 3 columns: **Volume**, **Sources & filters**, **Contact-info
recovery**.

| Control | Range / default | Effect |
|---|---|---|
| Fetch everything (uncapped) | **on** (default) | Ignores per-target candidate-pool cap (`effective_max = 100_000`). Uncheck to cap a run with the max-to-scrape slider. **No longer touches results-per-query** (that used to be forced to 200 — see the next row and the 2026-09-14 change log entry). |
| Search results per query | 10–200, **default 40** | `max_results` per query — applies **regardless of uncapped**. Lowered from 200: asking for that many likely made the backend paginate the results page several times per query, and results past ~40 for a hyper-local query are overwhelmingly noise anyway. |
| Max companies to scrape per state / vertical | 10–2000, default 500 | Per-target candidate-pool cap (only when not uncapped) |
| Concurrent site scrapes | 4–50, **default 25** | Scrape thread-pool size |
| Add city-level search queries | on | Toggles per-city query variants (see §5) |
| Stop early after N queries with no new companies | 5–50, **default 15** | `no_new_streak_limit` — per state+target, once this many CONSECUTIVE queries add zero new unique domains, stop issuing further query variants for it. Adaptive: a target that keeps finding new companies keeps getting searched regardless of how many queries that takes. |
| Use extra search phrasings per vertical | **on** | Toggles `query_terms` (all synonyms) vs. `query_terms[:1]` (primary only) — see §4a |
| Phrasings expanded per city | 1–5, **default 1** | `city_term_limit` — how many terms get city-level fan-out (the real query-count lever, not the toggle above) — see §4a |
| Also search well-known metro neighborhoods | **off** | Appends `states.get_extra_metro_areas(state)` to the city list (CA/TX/FL/NY only — see §4b) |
| Include OpenStreetMap business listings | on | Toggles the OSM step |
| Pre-scrape filter: directories, listicles & platforms | on | Toggles `blocklist.should_skip_result` (step 2) |
| Final junk filter (after scrape & scoring) | on | Toggles `junk_filter.partition` (step 5 / §6a) |
| Keep only High-confidence leads | on | Toggles the auto-purge (step 7) |
| Auto-retry no-contact leads with a real browser | on iff Playwright installed | Toggles step 6; disabled+off automatically if unavailable |
| Max browser retries per run | 0–1000, default 150 | Caps step 6's candidate list |

---

## 4. Target verticals

Defined in `states.BOOKKEEPING_TARGET_VERTICALS`, mapped via
`SERVICE_TARGET_VERTICALS`. `get_target_verticals(service)` returns the list
or `None`.

A service with verticals searches **once per selected vertical**, so the
per-target scrape cap is per vertical, not per state — a full 8-vertical
run is ~8× the work of a single search.

Each vertical dict: `label`, `query_terms` (**list** of noun-phrase
synonyms — see §4a), `osm_tags` (list of `(k,v)`; empty = web-search-only),
`keywords` (relevance terms that replace the service-name relevance check
for that vertical).

Current Bookkeeping verticals (label — OSM tags — # query synonyms):

1. **Accounting & Bookkeeping Firms** — `office=accountant`, `office=tax_advisor` — 4 terms
2. **Dental Practices** — `amenity=dentist` — 4 terms
3. **Event Management** — `office=event_management` (sparsely used in OSM; not fixable by inventing a tag) — 4 terms
4. **Floral Businesses** — `shop=florist`, `craft=florist`, `shop=garden_centre` — 5 terms
5. **Construction & Real Estate** — `office=estate_agent`, `office=construction_company` — 4 terms
6. **Rental Properties (STR/LTR)** — `office=property_management` — 4 terms
7. **Manufacturing** — `man_made=works` (factory/production-plant tag; narrow OSM coverage) — 4 terms
8. **IT & Software** — `office=it`, `office=software` — 4 terms

Every lead is tagged with its `target_vertical` (the **Target Type** column).

`SERVICE_SEARCH_TARGETS` maps a service to *what its queries should look
for* when that differs from the service name. Only entry: `Dental Billing →
"dental practices"` (the prospects need billing help; they aren't other
billing companies). `get_search_target(service)` falls back to the service
name.

### 4a. Extra query phrasings (`use_extra_query_terms`, `city_phrasing_limit`)

Each vertical's `query_terms` is a list of synonym phrasings for the same
target — e.g. Floral Businesses: `["florists and floral design businesses",
"flower shops", "flower delivery services", "wedding florists", "event
florists"]`. This exists because a search that only ever says "florist"
misses real results that only say "flower shop" or "flower delivery" —
different businesses phrase what they do differently.

**City-level fan-out is capped separately from state-level, on purpose —
this is the actual query-count control, not the on/off toggle.**
`search.build_queries`'s `city_term_limit` param: every term always gets
its 6 cheap state-level variants, but only the **first `city_term_limit`
terms** get expanded across every city (see §5). A first version expanded
ALL terms across ALL cities — on California (485 cities) that meant
**2,445 queries for Floral + 1,956 for Event Management = 4,401 queries for
one state**, a multi-hour run for ~270 kept leads. `city_term_limit=1`
(the shipped default) drops that same two-vertical CA run to **1,020
queries (4.3× fewer)** while keeping full synonym coverage at the
state level.

`app.py` wires two controls together:
- **"Use extra search phrasings per vertical"** (default **on**) — off
  reproduces the old single-phrasing behavior everywhere (`query_terms[:1]`,
  `city_term_limit=1` is moot since there's only one term).
- **"Phrasings expanded per city"** (`city_phrasing_limit`, slider 1–5,
  default **1**) — only meaningful when the toggle above is on; raising it
  reproduces the old "every term × every city" behavior at whatever
  multiplier you choose. Passed into each target's `city_term_limit`.

`app.py` also computes a **pre-run estimated query count** (pure string
building via `search.build_queries`, no network calls — cheap to run
upfront) summed across every state × target, and shows a `st.warning`
whenever it exceeds 300 — so a combinatorial blowup is visible **before**
committing to a run, not discovered after it finishes. The time figure
next to it is a genuine estimate of real runtime (`~{total} min` =
`~{search_minutes} min searching + ~{scrape_minutes} min scraping`), not
just `est_queries × SEARCH_QUERY_DELAY` — **fixed 2026-09-18** after that
floor-only number turned out to badly undersell actual runtime (it also
had a real display bug: the literal `⏱️` in the warning text plus
`st.warning(..., icon="⏱️")` rendered the icon **twice**; the text-embedded
one was removed). `search_minutes` now adds a per-query request-time
constant (`AVG_QUERY_REQUEST_SECONDS = 3.0`, an observed average, not just
the artificial delay) on top of `SEARCH_QUERY_DELAY`; `scrape_minutes` is
new entirely — `total_units × (candidates_per_unit / concurrency) ×
AVG_SCRAPE_SECONDS_PER_SITE`, where `candidates_per_unit =
min(effective_max, TYPICAL_CANDIDATES_PER_UNIT=150)` (150 is an observed
real-run yield ceiling, not the theoretical/uncapped max — using the
theoretical max here would make the estimate absurd in uncapped mode).
Both `AVG_*` constants are hardcoded in `app.py`, not `.env`-configurable
— revisit them if real runs consistently over/undershoot this estimate by
a lot.

### 4b. Metro neighborhoods (`include_metro_neighborhoods`)

`states.EXTRA_METRO_AREAS_BY_STATE` / `get_extra_metro_areas(state)` — a
**curated, non-exhaustive** list of well-known neighborhoods/districts
inside the biggest metro(s) of **CA, TX, FL, NY only** (e.g. Wynwood,
Williamsburg, Koreatown, Deep Ellum). When `include_metro_neighborhoods` is
on (default **off**) and city queries are on, these are appended to the
`cities` list for that state before `build_queries` runs — same query
templates, just one more "city-like" place name. Scoped to 4 states
deliberately: dense enough business coverage there for neighborhood-level
search to add real signal instead of just multiplying query count.

---

## 5. Query building (`search.build_queries`)

Inputs: `service`, `state`, `search_target` (**string OR list of strings**
— a vertical's `query_terms`, or a single service name), `cities`,
`extra_terms`, `city_term_limit` (default `None` = no cap, every term gets
city-level expansion — callers pass an explicit int to bound it, see §4a).

**State-level ("base") variants are built for EVERY term** in
`search_target`, always — cheap regardless of term count:
- If the term differs from `service` (target ≠ self): 6 variants —
  `"{t} in {state}"`, `"{t} near {state}"`, `"local {t} {state}"`,
  `"find {t} {state}"`, `"{t} directory {state}"`, `"{t} near me {state}"`.
- Else (searching for peers of the service): 6 variants —
  `"{t} companies in {state}"`, `"{t} firms {state}"`,
  `"small business {t} services {state}"`, `"best {t} companies {state}"`,
  `"local {t} providers {state}"`, `"{t} services near me {state}"`.
- `+ "{t} {term} {state}"` for each `extra_terms` entry (none wired
  currently).

**City-level variants are built only for `search_target[:city_term_limit]`**
(all terms if `city_term_limit` is `None`) — one per city in `cities`:
`"{t} companies in {city}, {state}"` (or `"{t} in {city}, {state}"` for the
target≠self branch). This is the split that bounds combinatorial blowup —
see §4a for the concrete before/after query counts.

All queries are combined into one list, deduped case-insensitively across
terms (so overlapping phrasings don't double a query). Order: every term's
state-level variants first, then the allowed terms' city-level variants.

**Cities = ALL cities the app knows for the state** (+ metro neighborhoods
if §4b is on). `app.py` passes `states.get_major_cities(state_name)` — the
full `MAJOR_CITIES_BY_STATE` list, which for large states (CA, TX) is
**hundreds** of cities. This is deliberate: restricting it to the 2 largest
cities was tried and didn't surface enough leads. Consequence: with city
queries on, a run issues `6 × N_terms + N_cities × city_term_limit` queries
per vertical per state — with the shipped `city_term_limit=1` default,
that's `6 × N_terms + N_cities`, not the far larger `(6 + N_cities) ×
N_terms` an earlier version issued (see §4a for the CA numbers: 4,401 →
1,020 queries for a 2-vertical run). Each query is preceded by a
`SEARCH_QUERY_DELAY` pause (default 2.5s). A big multi-vertical run is
still long by design at higher settings — not a hang. Turn off "Add
city-level search queries" for a fast
whole-state-only pass.

### Search backend (`search.search_ddgs`)

- Library: `ddgs` (multi-engine metasearch; NOT literally DuckDuckGo).
- Engines used (default `DEFAULT_BACKEND`): `google,bing,brave,yahoo,yandex`
  — tried left-to-right. `mojeek` and `wikipedia` were **removed from the
  default** (slow, frequent timeouts, thin results); `mullvad_*` excluded
  (DNS-fail on many networks). Override with `SEARCH_ENGINE_BACKEND` (comma
  list, `auto` for all 9, or a single engine to pin).
- Per-engine HTTP timeout = `SEARCH_TIMEOUT` (default 25s), passed as
  `DDGS(timeout=...)`.
- **Retry**: if *every* engine fails a query, retry up to `SEARCH_RETRIES`
  times (default 2) with a `2·attempt` s backoff. Most failures are
  transient timeouts / momentary rate-limits, so a retry usually clears
  them.
- A legitimately-empty result set is returned as-is (no retry).
- `region="us-en"`. Per-query pause before each call = `SEARCH_QUERY_DELAY`
  (default 2.5s).
- Only if every attempt fails → one row with
  `snippet="__ERROR__:All search engines failed after N tries — ..."`.
  `app.py` **counts** these across the run and shows ONE summary warning at
  the end (not one red box per failed query), with the first 3 messages and
  a hint to raise `SEARCH_TIMEOUT` / pin `SEARCH_ENGINE_BACKEND`.

---

## 6. Pre-scrape filtering (`blocklist.should_skip_result`)

Returns True (skip, don't scrape) if **any** of:

- `is_ad_or_tracking_url(url)` — contains `bing.com/aclick`,
  `googleadservices`, `doubleclick.net`, `/aclk?`.
- `is_blocked_domain(domain)` — domain in `ALL_BLOCKED_DOMAINS` (union of
  the sets below).
- `is_foreign_tld(domain)` — ends with one of `_FOREIGN_TLDS` (`.ca`,
  `.co.uk`, `.uk`, `.com.au`, `.au`, `.ae`, `.co.in`, `.in`, `.de`, `.fr`,
  `.nz`, `.ph`, `.pk`, `.sg`, `.hk`, `.cn`, `.mx`, `.br`, `.co.za`, `.ie`,
  `.eu`, `.it`, `.es`, `.nl`, `.jp`, `.ru`, `.pl`, `.id`, `.vn`, `.ng`,
  `.ma`, and `.com.*` variants). US-state search → offshore domain almost
  always = wrong business.
- `looks_like_listicle(title)` — matches any `_LISTICLE_PATTERNS`: `^top/best N`,
  `top N` mid-title, `N best/top`, `the best`, leading `top`/`best`, `vs`,
  `compare/comparison`, `guide to`, `how to / what is / what are / why`,
  `review(s)`, `(companies|firms|services|providers) (in|near) ... (usa|us|united states)`,
  `(leading|top-rated|best-rated) (bookkeeping|accounting|tax|payroll|dental)`.
- `looks_like_bot_check(title)` — "not a bot", "verify you are human",
  "checking your browser", "attention required", "are you a robot".
- `not is_relevant(title+snippet, service, keywords)` — the combined text
  must contain the service (or a synonym), or the vertical's `keywords`
  when provided. Generic query words (`services`, `company`, `best`, `top`,
  `small`, `local`, ...) are stopwords and don't count as relevance.
  `_SERVICE_SYNONYMS` gives a generous synonym map (bookkeeping ↔
  accounting, tax ↔ cpa, etc.).

### Blocked-domain sets (`blocklist.py`)

- `DIRECTORY_DOMAINS` — Bark, Clutch, GoodFirms, DesignRush, G2, Capterra,
  Trustpilot, BBB, Yelp, Angi, Thumbtack, HomeAdvisor, Glassdoor, Indeed,
  ZipRecruiter, Manta, YellowPages, Expertise, UpCity, Sortlist,
  RocketReach, TaxBuzz, ZoomInfo, Crunchbase, Lusha, talkinternational.com
  (added 2026-09-14, a Dental Practices audit — a "listing@" address and a
  generic forum-sounding domain name gave it away as a directory), ...
- `FREELANCE_DOMAINS` — Upwork, Fiverr, Freelancer, Guru, ...
- `PLATFORM_DOMAINS` — Wikipedia, YouTube, Facebook, Instagram, X/Twitter,
  LinkedIn, Pinterest, Reddit, Quora, Medium, TikTok, Google, Bing, Yahoo,
  Amazon, Apple, ...
- `REFERENCE_DOMAINS` — Merriam-Webster, Dictionary.com, Wiktionary,
  Britannica, Thesaurus.com, ... (rank for "best"/"small" in query
  phrasing).
- `SOFTWARE_VENDOR_DOMAINS` — Xero, QuickBooks/Intuit, FreshBooks,
  WaveApps, Zoho, Sage, NetSuite, Stripe, PayPal, smallpdf, ...
- `NEWSWIRE_DOMAINS` — EIN Presswire, PRNewswire, BusinessWire,
  GlobeNewswire, ...
- `PUBLISHER_DOMAINS` — Wiley, practicetestgeeks, ...
- `OFFSHORE_BPO_DOMAINS` — IBN Technologies, Outbooks, Invedus, Aviaan
  Accounting, Clarigro, Datamatics, QX Accounting, Cogneesol, Analytix,
  safebooks.ai. **Real companies** that SEO-target "bookkeeping companies
  in <state>" while being a single national/offshore BPO — the opposite of
  a local business. Only add a domain here after confirming it, since a
  wrong entry blocks a real business.
- `SAAS_AND_TEMPLATED_DIRECTORY_DOMAINS` — UncleKam, Sulekha, Bench.co,
  Bookkeeper360, Birdeye, Clio.
- `EVENT_AND_FLORAL_DIRECTORY_DOMAINS` — event/wedding/venue marketplaces &
  media (Zola, The Knot, WeddingWire, Tagvenue, Fresha, Peerspace,
  PartySlate, Green Wedding Shoes, Real Weddings Mag, Equally Wed,
  wedding.report, cityscoop.us, gethoneybun.com), local-news / tourism
  sites (patch.com, ocregister.com, visitcamarillo.com, benitolink.com,
  inmenlo.com), **national flower-order gateways** (1800flowers, FTD,
  Teleflora, FloristOne, FromYouFlowers, BloomNation, ProFlowers,
  UrbanStems — the floral version of the offshore-BPO problem), unrelated
  retail chains (Dollar Tree, Nothing Bundt Cakes, Walmart, Target), and
  (added 2026-09-14 from a manual audit) an SEO tool
  (freelocalcitationchecker.com), two directory sites posing as a named
  business (localareafinds.com, local-service-close-by.com), a portable-
  toilet company whose *display name* ("USA Rental Experts") hid the
  domain's own giveaway (mrportapottyorlando.com), an Australian/Indian
  destination-wedding site (thegrandindianwedding.com), an Italian tourism
  board (turismotorino.org), a food/travel blog (bucketlistbums.com), and
  an estate/garden venue mistagged as a florist (sunnylands.org), and
  (added 2026-09-15 from an Alabama/Alaska Event Management + Floral
  audit) three more national order/booking networks masquerading as local
  businesses — **Kremp Florist** (kremp.com), **Avas Flowers**
  (avasflowers.net — avasflowers.**com** was already listed; the same
  network also operates on the .net TLD), and a same-day delivery network
  (flowersin4hours.com) — plus two national wedding-vendor booking
  services that explicitly advertise "nationwide"/"all 50 states"
  coverage (indian-wedding-dj.com and its same-operator alias
  djmehul.com, poshweddingplanners.com), a tourism-board site whose "Host
  an Event" subpage got scraped under the misleading name "Sitka Weddings
  and Events" (travelsitka.com — a *different*, real local business,
  "Sitka Weddings & Events LLC" at sitkaweddings.com, survived this audit
  and was NOT blocked), a travel/food blog (travelcookeat.com), and a
  gambling/casino content blog with zero relation to flowers
  (techntrip.com). Grow this list as more turn up in real result data.
- `FOREIGN_BUSINESS_DOMAINS` (added 2026-09-14, extended 2026-09-15) —
  `alfaridhospital.com` (a hospital in Qatar), `confidentalclinic.com` (a
  dental clinic in Purley, UK) — both surfaced under an Alabama Dental
  Practices search — plus, from the 2026-09-15 audit, `azonova.com` (a
  software engineering studio in New Delhi, India — not even an event
  business; a doubly-wrong match), `greenplantflowers.com` (a florist in
  Sharjah, UAE), and `flowerdeliverydanang.com` (a florist in Da Nang,
  Vietnam — the business's own name said "Da Nang," but nothing upstream
  checks a business name for a foreign place name). All confirmed via
  direct lookup of the live site, not guessed — despite a plain `.com`
  TLD, so `is_foreign_tld` never had a chance to catch them. Same
  underlying gap as the Australian destination-wedding site and Italian
  tourism board found earlier: **there is no real geography check beyond
  TLD + phone area code**, so a foreign business on a `.com`/`.org` domain
  is only ever caught by getting hardcoded here after the fact. Not
  solved generally on purpose — see §15.
- `WEBSITE_BUILDER_DEFAULT_DOMAINS` (added 2026-09-14) — squarespace.com,
  wixsite.com, weebly.com, godaddysites.com, business.site, wordpress.com,
  sites.google.com, webs.com, yolasite.com, jimdofree.com, strikingly.com,
  carrd.co. A business still on a website-builder's **free default
  subdomain** (never migrated to its own domain) is almost never an
  established local business's real site — verified against a real hit
  (`allensflowermarket.wordpress.com`, a stale legacy blog subdomain of a
  business already correctly captured under its real `allensflowermarket.com`
  domain elsewhere in the same run — confirmed duplicate, not a missed
  unique lead). Distinct from `PLATFORM_DOMAINS` (platforms that ARE the
  destination, e.g. Wikipedia) — these are meant to be stepped off of.

---

## 6a. Final junk pass (`junk_filter.check_lead` / `.partition`)

Runs in `app.py` **after** scrape + score, per target, with that vertical's
`keywords`. Returns `(is_junk, reason)`. Deliberately conservative — a false
positive silently deletes a real lead. Drops a row if **any**:

1. `blocklist.is_blocked_domain(domain)` or `is_foreign_tld(domain)` —
   backstop for the pre-scrape filter, and catches domains added to the
   blocklist after this row was collected. (Skipped for `osm:` rows.)
2. `blocklist.is_low_quality_name(name)` — listicle / keyword-salad / parked
   / unspaced-slug name (§10).
3. Name is a nav-label fragment (`Home`, `About`, `Same`, `Full`, `Menu`,
   `404`, ...) or has no letters.
4. **Name** matches `_OFF_TARGET_NAME_PATTERNS` — "sells a service **to** an
   industry" or media/directory: `SEO/marketing/web-design … for/agency`,
   `citation checker/builder`, `… for <industry> companies`, `porta potty /
   portable toilet / dumpster / junk removal`, `photo booth`,
   **`photography/photographer/videography/videographer/cinematography`
   (unconditional as of 2026-09-14 — see below)**, `destination wedding(s)`,
   `law firm / attorney / law offices / legal services`, bare `Law` as a
   naming **suffix** only (`\blaw(\s+(group|firm|office|...))?\s*$` — so
   "Pierview Law" is dropped but "Law Gardens Florist" survives, since
   "Law" isn't the last word there), `box office / ticket sales`,
   `magazine / blog / podcast / directory`, `chamber of commerce / visitors
   bureau / tourism board / turismo`, a name starting with `Visit
   <Placename>` (destination-marketing sites), `film & tv production`,
   newspaper-masthead words (`star-news/tribune/gazette/chronicle/...`),
   professional-association/academic-department names (`academy of /
   association of / society of / college of / school of / institute of ...
   dentistry/dental/medicine/veterinary`), bare `veterinar(y|ian)`, and
   (added 2026-09-15, from a real Alabama nail salon literally named
   "Florist Nails & Spa" that passed Floral Businesses relevance on its own
   name) `nails? ... salon/bar/spa/studio/lounge` — nail salons are never a
   target of any vertical here, unconditional like photography/veterinary.
4a. **Domain** is checked against the *same* `_OFF_TARGET_NAME_PATTERNS`
   list, plus a separate plain-substring list `_OFF_TARGET_DOMAIN_SUBSTRINGS`
   (`portapotty`, `dumpster`, `ticketsales`, `boxoffice`, `lawfirm`,
   `destinationwedding`, ...) — a junk site's *display name* is sometimes
   rebranded to something plausible ("USA Rental Experts") while the
   *domain* still gives away the real business
   (`mrportapottyorlando.com`). Domain strings have no word boundaries
   (`mrportapotty` is one token), so the domain-substring list is checked
   with plain `in`, not the word-bounded regexes used for names.
5. Name contains a `_CONDITIONAL_OFF_TARGET_TERM` (`mobile dj`, `hot
   springs`, `dollar tree`, `urgent care`, `hospital` (added 2026-09-14 —
   caught both a foreign general hospital AND, retroactively applied to a
   real database, an unrelated veterinary "Animal Hospital" under Dental
   Practices), ...) **and** none of the vertical's `keywords` appears in
   the name — so a genuine dental hospital/clinic (name also says
   "dental"/"dentist") is spared via the keyword check, only a hospital
   with no dental branding at all gets dropped.
6. **Email prefix is in `_NETWORK_TEMPLATE_EMAIL_PREFIXES`** (`wecare`) —
   a shared, templated "customer care" address seen across many different
   city-named domains (`wecare@pomonasflorist.com`,
   `wecare@perrisflowers.com`, ...) is a strong signal of a machine-
   generated multi-city network page, not an independent local business —
   confirmed by a manual audit. `GENERIC_EMAIL_PREFIXES` already keeps
   `wecare@` un-sendable (§14 step 4); this drops the **row**, since these
   shouldn't count as a distinct "company" at all.

> **Photographer fix (2026-09-14):** this was the #5 known-soft-edge for
> months — "wedding" is an Event Management keyword, so the conditional-
> term gate always spared a "Wedding Photography" business. A manual audit
> confirmed real, repeated leakage (a wedding photographer, "True
> Photography & Video," etc.), so the photography terms moved to the
> **unconditional** list (rule 4) — no vertical in this app wants a
> photography business, so there's no real precision cost.

Not exhaustive by design: it removes the clear cases, not every marginal
one. New patterns/domains land here or in `blocklist.py` as they show up.

---

## 7. Scrape + extraction (`scrape.scrape_company`)

Fetch: `requests.get`, 20s timeout, desktop Chrome UA, only `text/html` +
HTTP 200 accepted. On fetch failure: if an OSM fallback phone exists, build
a minimal record from it; otherwise a `failed` record. `html_override`
(from `browser_scrape`) bypasses the fetch and runs the same extraction on
supplied HTML.

**Name** — trust order: JSON-LD org name → `og:site_name` → cleaned
`<title>` → domain-based (`"acme-books.com" → "Acme Books"`). `name_source`
records which. Then `_normalize_company_name(name, domain)` tidies it:
- trims a trailing tagline/location at a whitespace-padded `| :: : – — / -`
  separator ("Boulevard Florist :: Wholesale Flowers in LA" → "Boulevard
  Florist"); unspaced `:` is left alone so "24:01 Floral Design" survives
- ALL-CAPS → Title Case ("CARLSTROM PRODUCTIONS" → "Carlstrom Productions")
- collapses spaced-out initials ("J E N N Y & J U N E" → "Jenny & June")
- if the value is a nav label (`Home`, `About`, `Same`, `Full`, ...), a
  bare domain slug (`zola.com`, `myeventpod`), >120 chars (SEO keyword
  salad), or has no letters → falls back to the domain-based name **and**
  sets `name_source = "fallback"` so scoring doesn't reward it.
  A multi-word name that merely concatenates to its own domain
  ("everbloomfloraldesign.com" ↔ "Everbloom Floral Design") is **kept**.

**Owner / decision-maker** (`extract.extract_owner_name`) — best-effort,
**site's own text only**, never linkedin.com. Patterns require a `founder /
co-founder / ceo` title word next to exactly two capitalized words (each ≥3
chars). Match is rejected if either word is in `_NON_NAME_WORDS` (a long
denylist of nav/UI/generic terms built from real false positives). Precision
>> recall by design — the name goes straight into "Hi {name}," in outreach.
`extract.revalidate_owner_name` re-checks a stored name against current
rules (for retroactive cleanup) and blanks it if it no longer passes.

**Self-published LinkedIn URL** (`extract.extract_self_published_linkedin_url`)
— a linkedin.com URL the site links to itself. Prefers `/in/` (personal)
over `/company/`. For a human to click, not for further automation.

**Contact info** — trust order:
1. `mailto:` / `tel:` links.
2. If the homepage has neither, fetch **one** linked contact/about page
   (href/label contains `contact`, `about`, `get-in-touch`, `reach-us`) and
   retry.
3. Regex over page text — fallback only.

Email handling:
- **Every `mailto:` candidate is validated against `EMAIL_RE.fullmatch`
  before acceptance** (added 2026-09-14) — a `mailto:` href is "the site
  owner put this here," not "this is a syntactically valid address." A
  real site had `href="mailto:emailto:@domain.com"` (a page-builder typo,
  no real local part) which was previously accepted verbatim and stored as
  the lead's email. A `mailto:` with comma-separated recipients is split
  and each candidate checked independently, so one malformed entry doesn't
  lose a valid one next to it.
- **Lowercased on capture** (`extract_mailto_emails`, `extract_emails`) —
  so `Info@X.com` and `info@x.com` can't both slip past the suppression
  list / sent-log dedup.
- `mailto:` and regex emails both filtered for placeholders
  (`is_placeholder_email`: local part in `you/your/name/test/...`, domain
  in `work.com/company.com/...`; also, **added 2026-09-15**, a 32-hex-char
  local part (an MD5/UUID shape no human types) or a domain whose first
  label is `sentry` — found via a real case where a site's own
  `sentry.<their-domain>` error-tracking address leaked into the page text
  and was stored as the lead's contact email) and blocked domains
  (`email_domain_is_blocked`: the email's own domain is a blocklisted /
  foreign-TLD site — a leaked/syndicated widget contact).
- **Generic role prefixes** (`blocklist.GENERIC_EMAIL_PREFIXES`) are dropped
  here, at scrape time — so `info@ / support@ / sales@ / billing@ / ...`
  never even reach the database. Personal-looking prefixes are kept.
- **Regex emails must match the site's own domain.** A regex email on a
  different domain = someone else mentioned on the page → discarded. (A
  `mailto:` on a different domain is kept — an explicit choice by the site
  owner, e.g. a small biz using a personal Gmail.)
- `best_email` picks: an `info@/contact@/hello@/office@/sales@/admin@`
  address if present (priority order), else the first. `email_source` =
  `mailto` or `regex`. `email_domain_match` recorded.

Phone handling:
- `tel:` first, then domain-matching regex phones, then the OSM fallback
  phone. All filtered by `is_suspicious_phone` (see §9). `phone_source` =
  `tel` / `regex` / `osm`.

`status` = `ok` if email or phone found, else `no_contact_found`, else
`failed` (fetch failed with no fallback).

`scrape_many` fills results back into their **input position** (not
completion order), so `app.py` can `zip(scraped, scrape_inputs)` to attach
the correct `search_query` / `target_vertical` / `state` to each row.

---

## 8. Confidence scoring (`extract.score_lead`)

Returns `(ratio, label)`. `ratio = score / max_score`, rounded to 2 dp.

**Points** (each `if` also adds to `max_score`):

| Signal | +score | +max |
|---|---|---|
| `name_source` in {jsonld, og, osm} | +1 | +1 (always) |
| name is NOT a low-quality name | +1 | +1 (always) |
| email present: `email_source == "mailto"` | +1 | +2 |
| email present: `email_domain_match` | +1 | — |
| phone present: `phone_source` in {tel, osm} | +1 | +2 |
| phone present: NOT suspicious | +1 | — |
| `location_match is not None`: it's True | +1 | +1 |

**Hard gates** — ALL required for "High", regardless of ratio:
- name is NOT a low-quality name (`blocklist.is_low_quality_name`, see §10)
- domain is NOT blocked and NOT a foreign TLD
- `email_ok` = no email, OR email domain matches the site, **OR the email
  came from a `mailto:` link pointing at a well-known free CONSUMER email
  provider** (`extract.CONSUMER_EMAIL_DOMAINS` — gmail.com, yahoo.com,
  outlook.com, hotmail.com, icloud.com, aol.com, protonmail.com, live.com,
  msn.com, mail.com, ymail.com — see below)
- `phone_ok` = no phone, OR (not suspicious AND `location_match is not False`)
- `has_contact_info` = at least one of email/phone present

> **`email_ok`'s mailto: exemption (added 2026-09-11, NARROWED
> 2026-09-14):** domain-match exists to catch an email that isn't actually
> this business's own — either a REGEX-sourced one (an article mentions
> ten other companies, one's email ends up on the page; stays hard-gated,
> since `scrape.py` only ever *chooses* a regex email when it already
> matches the site's domain), or a `mailto:` that happens to point at a
> **different custom business domain**. The original version of this
> exemption spared ANY mailto:, reasoning "the site owner explicitly
> published it" — but that let through a real case found in a manual
> audit: a "Madison Dental Care" site (own domain `madisondentalcare.com`)
> with a mailto: to `md@dentalcaremadison.com` — a different domain
> entirely, most likely a rebrand leftover or a mixed-up similarly-named
> business, not confirmably *this* business's address. Narrowed so the
> exemption applies **only** when the mailto: domain is a known free
> consumer provider (unambiguously a personal inbox, nothing for it to
> "match" in the first place) — a mailto: to any other custom domain is
> held to the same domain-match requirement as everything else. Do not
> revert either the regex-path domain enforcement or the consumer-domain
> narrowing without re-confirming both closed cases stay closed.

**Label:**
- `Low` if bad domain, or low-quality name, or no contact info.
- `High` if all hard gates pass AND `ratio >= 0.75`.
- `Medium` if `ratio >= 0.4`.
- `Low` otherwise.

`Unrated` is assigned by `storage._normalize` only when `confidence_label`
is empty on load — i.e. a row imported straight into `leads.csv` from
outside the app. A lead collected through a search is always scored.

Confidence is recomputed with **current** rules on every search run. There
is no dashboard "re-check" button. `storage.rescan_confidence(df)` /
`rescan_and_save()` still exist to re-score an existing `leads.csv` in place
(re-judges email validity, domain match, phone validity, location, name
quality, blocklist; also drops a now-bad chosen email and recovers a
same-domain replacement from `all_emails_found` if one exists; revalidates
`owner_name`). Not wired to the UI.

---

## 9. Phone validity + location

`extract.is_suspicious_phone(phone)` → True (reject) if:
- not exactly 10 digits (after stripping a leading `1`)
- area code or exchange code starts with `0` or `1` (NANP structural rule)
- ≤ 2 distinct digits (all-same, all-2s placeholder)
- `NXX-555-01XX` reserved fictional range
- straight ascending/descending run (`1234567890` / `0123456789`)

`area_codes.phone_matches_state(phone, state_abbr)` → **soft** signal:
- `True` — area code is known to belong to this state
- `False` — area code belongs to a *different* US state, OR to a non-US
  NANP region (`NON_US_NANP_CODES`: Canada + Caribbean). Only `False`
  blocks the High label (via `phone_ok`).
- `None` — toll-free (`800/833/844/855/866/877/888`), or an area code not
  in `AREA_CODE_TO_STATE`. Treated as neutral, never a strike.

A wrong-state / foreign area code caps the lead at Medium/Low but does not
discard it (businesses keep old area codes, use VOIP, etc.).

---

## 10. Name-quality checks (`blocklist.is_low_quality_name`)

True if any of:
- `looks_like_listicle(name)` — same `_LISTICLE_PATTERNS` as §6.
- `looks_like_keyword_salad(name)` — ≥3 meaningful words (legal suffixes
  `llc/inc/co/corp/ltd/pllc/pc` ignored) and **every** one is a generic
  industry term (`bookkeeping`, `tax`, `dental`, `services`, `solutions`,
  ...). "Elite Accounting & Tax Services LLC" passes (has "Elite"); "Law
  Firm Bookkeeping Quickbooks" fails.
- `looks_like_unspaced_slug(name)` — no spaces and length > 15 (domain-slug
  artifact).
- `looks_like_parked_domain(name)` — "expired domain", "domain for sale /
  parking", "buy this domain", "this web page is parked".

---

## 11. Templates (`email_template.py`)

**Resolution order for a lead's email template** (`load_template(service, target_vertical)`):
1. `VERTICAL_TEMPLATES[target_vertical]` if that vertical has one. Built-in
   only, not UI-editable. Currently: **Event Management**, **Floral
   Businesses**.
2. `data/custom_email_templates.json[service]` if non-empty (saved from the
   Email Template tab).
3. `SERVICE_TEMPLATES[service]` built-in. **Only "Bookkeeping" exists.**
4. `GENERIC_TEMPLATE` (Input/Process/Output built from the service name) —
   for custom services.

> `SERVICE_TEMPLATES["Bookkeeping"]` must stay **word-for-word identical**
> to the `"Bookkeeping"` entry in `data/custom_email_templates.json` — the
> JSON wins whenever present, so a drift changes what real companies
> receive. Only add a service to `SERVICE_TEMPLATES` in the same change
> that adds it to `states.ACTIVE_SERVICES` — a stale lead with a
> non-Bookkeeping `service` value falling through to that service's
> template caused a real mis-send incident.

**SMS templates** — same idea, simpler: `SMS_TEMPLATES` (Bookkeeping only) /
`GENERIC_SMS_TEMPLATE`, overrides in `data/custom_sms_templates.json`. No
subject line, no HTML, no signature — plain text, kept short (carriers
segment by length).

**SMS A/B/C variants** (`SMS_TEMPLATE_VARIANTS`, added 2026-09-18) — a
second axis alongside service, only for SMS: `{service: {variant_name:
template_text}}`. Bookkeeping currently has 4 — `"Original"` (the
pre-existing default above) plus 3 the client supplied verbatim to test
against each other across different states/business types and see which
gets more replies (`"Variant 1 (profit vs. bank balance)"`, `"Variant 2
(backlog anxiety)"`, `"Variant 3 (receipts mess)"`). Only their own
`"[Name]"` placeholder was touched, swapped for `{greeting_name}` (same
role as everywhere else in this file) — **the wording itself must not be
edited**, that's the whole point of an A/B/C test. `load_sms_template`/
`save_sms_template`/`reset_sms_template`/`get_default_sms_for_service`
all take an optional `variant` param (default `"Original"`, i.e. every
one of these functions behaves exactly as before if a caller doesn't
pass one). `data/custom_sms_templates.json`'s schema changed from
`{service: text}` to `{service: {variant: text}}` — `_load_sms_overrides`
transparently migrates an old flat-format file by treating that string as
the `"Original"` variant's override, so an already-customized template
isn't lost. Send Texts tab: a "Message variant to use for this batch"
picker selects which variant renders for the preview, the test text, and
the real send — all three use the same one. Running the actual A/B/C
test across states means: filter Lead Database to a state, pick a
variant, send; repeat with a different state/variant. `render_sms_for_lead`
also takes a `variant` param now (default `"Original"`) and
`texter.record_sent_text`/`SENT_TEXT_LOG_COLUMNS` gained a `variant`
column so a later reply can be traced back to which message earned it —
without this the whole point of the test (comparing reply rates) would be
unmeasurable once time passes. `_ensure_sent_text_log` migrates an
existing log file missing that column before any append, the same
"don't lose old data, don't misalign the CSV" concern as the templates
file above.

**Merge fields** (`_merge_fields`): `{company_name}` (→ "there" if missing),
`{service}`, `{service_lower}`, `{state}` (→ "your area"), `{phone}`,
`{email}`, `{website}`, `{owner_name}`, `{greeting_name}`. SMS templates
also get `{sender_name}`, `{sender_company}`, etc. from `mailer.get_sender_info()`.

`{greeting_name}` = owner's **first name** if an owner was found, else
`"{company_name} team"`. This is how "Hi Jane," vs "Hi Acme Books team,"
happens automatically.

**Rendering:**
- `render_for_lead(lead)` → `(subject, plain_body)`. Subject is the first
  line if it starts `Subject:`, else `"A note for {company}"`. `**bold**`
  markers stripped for plain text. Appends `\n\nBest,\n{signature}`.
- `render_html_for_lead(lead)` → HTML: `**bold**` → `<strong>`, paragraphs
  wrapped in `<p>`, HTML signature (with `cid:sig_logo` image ref).
- `render_html_preview_for_lead(lead)` → same but signature image inlined
  as base64 data URI (Streamlit can't resolve `cid:`).
- **No compliance/opt-out line** (removed 2026-09-15, explicit user
  request — see change log). Every template used to have
  `\'re receiving this because {company_name} is publicly listed in
  {state}. Reply "unsubscribe" and we won't contact you again.` appended
  after the signature, unconditionally and not editable from the UI, as
  this app's only in-email CAN-SPAM opt-out mechanism. **It is gone now,
  from every send path (real and test), for every service/vertical/state
  — there is no opt-out language anywhere in a sent email**, and the
  user was explicitly told this before confirming. Do not silently
  re-add it; if compliance needs it back, that's a deliberate decision
  for the user to make, not an assumed default.
- **Signature** is built at render time from `.env` (`mailer.build_signature_text` /
  `build_signature_html`) — name, title, company, phone, WhatsApp (same
  number, real WhatsApp icon — no text label as of 2026-09-15, previously
  read "WhatsApp: {phone}"), website (always shown `www.`-prefixed),
  address. Any field left blank / on a `[placeholder]` default is omitted.
  It cannot be edited away in the template editor — only the core message
  is stored/edited. Two images are embedded inline (`cid:sig_logo`,
  `cid:wa_icon`, both from `lead_finder/assets/`) via
  `mailer.get_signature_inline_images()`. `signature_logo.png` was tightly
  cropped to its visible content on 2026-09-15 (the original had ~30% dead
  whitespace padding, which — combined with the logo `<td>`/`<img>` only
  setting CSS `width` and relying on `height: auto` — made the logo cell
  taller than the text column next to it in clients like Outlook that
  don't reliably honor `height: auto`; fixed by cropping the asset AND
  giving both the `<td>` and `<img>` explicit HTML `width`/`height`
  attributes plus `valign="middle"`, not just CSS). `whatsapp_icon.png`
  (the real WhatsApp glyph, green circle + white icon) is a new asset
  added the same day, replacing a generic speech-bubble emoji badge.

---

## 12. Data files (`data/`)

| File | Written by | Schema / notes | Gitignored |
|---|---|---|---|
| `leads.csv` | `storage.save_leads` | Columns in `storage.COLUMNS`: company_name, website, domain, phone, email, all_emails_found, status, service, target_vertical, state, search_query, name_source, email_source, email_domain_match, phone_source, confidence_score, confidence_label, owner_name, linkedin_url. **Holds one run only** — a search run rewrites the whole file (`replace=True`). Deduped by `domain`, keep last. Empty cells normalized (`nan`/`none`/NaN → `""`). | yes |
| `custom_email_templates.json` | Email Template tab | `{service: template_string}` | yes (via `data/leads.csv` line only — check `.gitignore`) |
| `custom_sms_templates.json` | Send Texts tab | `{service: {variant: template_string}}` (changed 2026-09-18 for A/B/C variants — see §11; an old flat `{service: template_string}` file is auto-migrated on read) | — |
| `sent_log.csv` | `mailer.record_sent` | domain, email, company_name, service, subject, status, error, sent_at. **Dedup key = domain.** | yes |
| `sent_text_log.csv` | `texter.record_sent_text` | phone, company_name, service, **variant** (added 2026-09-18, blank for pre-existing rows), status, error, sent_at. **Dedup key = phone (E.164 string).** `dtype=str` on load is essential (a `+1512...` parses as int otherwise). | yes |
| `suppression_list.csv` | `mailer.add_to_suppression` | value, added_at. One list, **shared across email + SMS.** Matches on exact email, exact domain, or normalized phone digits. | — |
| `sent_records/<vertical>/<YYYY-MM-DD>.csv` | `app._write_daily_sent_records_csv` | Human-readable per-vertical, per-day export of email sends. Additive — `sent_log.csv` is still the dedup source. Vertical folder name is sanitized; leads with no vertical → `General`. | — |
| `exports/<date>_<businesstypes>_<states>.xlsx` | `storage.save_export_snapshot`, once per completed search run | Same columns as `leads.csv`, frozen at that run's end. **Accumulates** — one file per run, never overwritten by the next search (unlike `leads.csv`). Read back by `storage.load_export_snapshots` when Send Emails/Send Texts picks one or more saved runs to send from instead of the live database. | yes |

`.gitignore` lists `.env`, `__pycache__/`, `*.pyc`, `.venv/`, `venv/`,
`.DS_Store`, and every file in `data/` (`leads.csv`, `sent_log.csv`,
`sent_text_log.csv`, `suppression_list.csv`, `custom_email_templates.json`,
`custom_sms_templates.json`, `sent_records/`, `exports/`) — all
per-run/per-machine state or real scraped contact data, not source (§16
item 1, resolved 2026-09-15).

---

## 13. Config / environment (`.env`)

All optional for finding/scraping. Required only for the feature named.

| Var | For | Notes |
|---|---|---|
| `SEARCH_ENGINE_BACKEND` | search | Comma list of engines, or `auto`. Default (blank) `google,bing,brave,yahoo,yandex` — **keep this multi-engine**; pinning to a single engine removes ddgs's own in-call fallback and one engine's DNS/rate-limit trouble then fails every query outright (hit in practice — see change log). |
| `SEARCH_QUERY_DELAY` | search | Seconds between queries. Default `2.5`. |
| `SEARCH_TIMEOUT` | search | Per-engine HTTP timeout, seconds. Default `25`. Raise on a network that times out a lot. |
| `SEARCH_RETRIES` | search | Extra attempts when every engine fails a query. Default `2`. |
| `OSM_TIMEOUT` | OSM | Per-tag Overpass HTTP timeout, seconds. Default `120`. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` | email | All four required or `get_smtp_config()` returns None. Gmail: `smtp.gmail.com:587` + an **App Password**. |
| `FROM_EMAIL` | email | Defaults to `SMTP_USERNAME`. |
| `FROM_NAME` | email | Also the signature's name line. Person's name > company name for reply rates. |
| `REPLY_TO_EMAIL` | email | Optional `Reply-To` header. |
| `SENDER_TITLE` / `SENDER_COMPANY` / `SENDER_PHONE` / `SENDER_WEBSITE` / `SENDER_ADDRESS` | email | Signature + `{sender_*}` merge fields. **`SENDER_ADDRESS` is legally required in commercial email (CAN-SPAM)** — don't send with it blank. |
| `TEST_EMAIL_ADDRESSES` | email | Optional comma-separated list for the "Send test email to" dropdown (Send Emails tab). Blank = only "Custom..." is offered. Moved out of `app.py` 2026-09-15 so real addresses aren't hardcoded into public source. |
| `EIGHTXEIGHT_API_KEY` + `EIGHTXEIGHT_SUBACCOUNT_ID` | SMS | **Both** required or `get_active_texting_config()` returns None. From developer.8x8.com/connect. |
| `EIGHTXEIGHT_SOURCE` | SMS | Optional Sender ID shown as "from". Blank = account default. |
| `TEST_TEXT_NUMBERS` | SMS | Optional comma-separated list for the "Send test text to" dropdown (Send Texts tab), same pattern as `TEST_EMAIL_ADDRESSES`. Blank = only "Custom..." is offered. Added 2026-09-15. |

`app.py` calls `load_dotenv(override=True)` — `.env` beats a stray shell
export of the same name. Restart the app after editing `.env`.

---

## 14. Sending

### Common shape

Long sends run in a detached `threading.Thread` (daemon), because
Streamlit reruns/abandons the script on any interaction. Progress is a
plain dict mutated from the thread; **no `st.*` call happens off the main
thread**. Every send tab: test-send control → eligibility funnel with a
metric row → 10-row preview → confirmation checkbox → send button →
background thread → progress + results (persist after `eligible` empties).

### Email (`Send Emails` tab, `mailer.py`)

**Eligibility funnel** (each step shown as a metric):
0. **Optional: send from a saved run and/or an uploaded file instead of
   the live database** (added 2026-09-15, extended same day with the
   upload half) — an expander lists every archived snapshot
   (`storage.list_export_snapshots`, newest first) AND offers a
   multi-file uploader (`st.file_uploader`, `.xlsx`/`.csv`) for files
   from anywhere on the user's own machine — not just ones still sitting
   in `data/exports/` on this one. Picking snapshot(s) and/or uploading
   file(s) loads and combines all of them (`storage.load_export_snapshots`
   + `storage.load_uploaded_snapshots`, unioned and deduped via
   `storage.dedupe_keep_last`) and that becomes the starting point for
   everything below, bypassing the Lead Database tab's live filter
   entirely. Leave both empty to fall back to step 1's normal behavior.
   The fixed safety filters in step 2 still apply either way — a saved
   snapshot or uploaded file gets no exemption from them, which in
   practice means an uploaded file that ISN'T one of this app's own
   exports (no `service`/`confidence_label` columns) normalizes to
   `service=""`/`confidence_label="Unrated"` and produces **zero**
   eligible leads — deliberate, not a bug (see §16 note on this if one
   gets added). A bad/corrupt uploaded file is skipped with its error
   shown, not silently dropped or a hard crash.
1. Start from the Lead Database tab's current filtered view
   (`st.session_state.lead_db_view`), or the full DB if none.
2. **Fixed safety filters** (not adjustable in this tab, added after real
   incidents): `service == <currently selected service>` **AND**
   `confidence_label == "High"`.
3. Has a non-empty email.
4. `blocklist.is_targetable_email(email)` — **screens OUT generic/shared
   inboxes only** (`GENERIC_EMAIL_PREFIXES`: support, info, hello, **hi**,
   **hey**, **contact**, sales, admin, billing, noreply, hr, careers,
   legal, it, care, **wecare**, customerservice, **custservice**, ...). Any
   other prefix — including a personal name like `erica@` or `jsmith@` —
   passes. There is **no allowlist** of approved prefixes;
   `TARGETABLE_EMAIL_PREFIXES` (the old reference/priority list) was
   **removed 2026-09-14** — it listed "contact" as a wanted prefix while
   the blocklist above now excludes it, which was actively contradictory,
   and nothing in the code read it any more.
   This check is exact-prefix matching, not substring — a variant not
   spelled out in the set (e.g. `wecare` before it was added) slips
   through even though it's semantically identical to a listed one (`care`).
   `hi`/`contact`/`wecare`/`custservice` were added after showing up
   unfiltered in real collected leads; if another variant turns up, add it
   here rather than trying to generalize the match.
5. Not suppressed (`mailer.is_suppressed(email, domain)`).
6. If "Skip leads already emailed" (default on): domain not in
   `mailer.already_sent_domains()` (status `sent`).

**Send:** `mailer.send_batch(smtp_config, jobs, delay_seconds)` — one
`send_email` per job, `time.sleep(delay_seconds)` between (UI: 1–30s,
default 4.0). Multipart (plain + HTML) when `html_body` given; signature
logo attached inline as `cid:sig_logo`. Each job is rendered with **its own
lead's** service/vertical template. On each result the progress callback
calls `mailer.record_sent(...)`; after the batch,
`_write_daily_sent_records_csv`.

**Test email:** one row **per target vertical currently present** in the
working set for the active service (in the service's own canonical
vertical order — `states.get_target_verticals`), not a single generic
send. A service with sub-verticals (Bookkeeping) has a *different*
template per vertical (`email_template.VERTICAL_TEMPLATES`), so testing
with no vertical set only ever exercised whatever `load_template()` falls
back to and never showed what, say, a Floral Businesses lead would
actually receive — fixed 2026-09-15. Each row: its own "send to" dropdown
(known test addresses from `TEST_EMAIL_ADDRESSES` + Custom) and its own
"Send" button, so e.g. Event Management and Floral Businesses can each be
tested independently. Falls back to a single row (no vertical set) for a
service with no sub-verticals at all. Builds a synthetic lead per row
(`target_vertical` set to that row's vertical), subject prefixed `[TEST]`.
Verifies SMTP before any real send.

### SMS (`Send Texts` tab, `texter.py`)

Same optional "send from a saved run instead of the live database" picker
as Send Emails (step 0 above), independent state (its own multiselect key)
but identical behavior.

**Eligibility:** same fixed safety filters (service + High). Then: has a
phone; if "Only leads with no email" (default on) also require empty email;
not suppressed by phone; not in `texter.already_texted_numbers()` (compared
as E.164).

**Send:** `texter.send_text_batch(config, jobs, delay_seconds)` (UI 1–30s,
default 3.0). `send_text` → `POST {EIGHTXEIGHT_API_BASE}/subaccounts/<id>/messages`
with `Bearer` auth, body `{destination, text, [source]}`. HTTP 200 = success;
anything else surfaces `HTTP <code>: <body>`. Phone → E.164 via
`texter.to_e164` (10-digit → `+1`; `+`-prefixed passed through;
8–15 digits → `+<digits>`; else None → skipped).

**Test text:** dropdown of known numbers (`TEST_TEXT_NUMBERS` + Custom),
mirroring Send Emails' test-address dropdown — added 2026-09-15, replacing
a plain always-Custom text input. Unlike email, this is a **single** row
regardless of how many target verticals are in the working set: SMS
templates are service-level only (`email_template.load_sms_template(service,
variant)`, no `target_vertical` parameter — see §11), so there's no
per-vertical template to test separately — what email covers with
per-vertical rows, SMS covers with the variant picker (§11) instead,
which the test send also respects. Message prefixed `[TEST]`.

### Bulk Send (`Bulk Send` tab, added 2026-09-18)

User request: "row by row, email if the company has an email, text if it
doesn't, both if it has both, until the document ends." The standalone
Send Emails/Send Texts tabs can't do this on their own — Send Emails only
ever looks at rows *with* an email, and Send Texts defaults to *excluding*
rows that have one (`"Only leads with no email"`, on by default — it's
built as email's fallback channel, not an equal partner). Bulk Send is a
third tab, not a replacement for either: same source picker (§ below),
same two hard safety filters (service match + High confidence), but
**two independent per-row eligibility masks computed side by side**
instead of one funnel:

- `email_ok` — identical rules to Send Emails' own funnel (non-empty +
  `blocklist.is_targetable_email` + not suppressed + not already emailed
  if "Skip leads already emailed" is on). `False` for every row if SMTP
  isn't configured.
- `text_ok` — identical rules to Send Texts' own funnel **minus** the
  "only if no email" restriction (non-empty phone + not suppressed + not
  already texted if "Skip leads already texted" is on). Deliberately does
  **not** additionally check `extract.is_suspicious_phone` here, matching
  Send Texts' own existing behavior: an unparseable/suspicious number is
  silently skipped later at job-build time via `texter.to_e164` returning
  `None`, not filtered out of the eligibility count — kept consistent
  with the tab this was copied from rather than introducing a stricter
  check that would make the two tabs disagree on the same row. `False`
  for every row if 8x8 isn't configured.

A row's channels = whichever mask(s) it passes — `email_ok & ~text_ok` →
email only, `text_ok & ~email_ok` → text only, both → both, neither →
excluded. Shown as 4 metrics (Email only / Text only / Both / Excluded)
plus a combined preview table with a "Channels" column. An SMS variant
picker (same variants as §11, edited from the Send Texts tab — Bulk Send
only *selects* one, no separate editor) applies to every text this batch
sends.

**Send mechanics**: not a genuinely interleaved single pass — clicking
"Send bulk batch" builds two separate job lists (`email_jobs`, `text_jobs`)
from the same eligible rows and runs them **sequentially** in the
background (`_run_bulk_batch_in_background`): the whole email batch via
`mailer.send_batch` first, then the whole text batch via
`texter.send_text_batch`, each through the exact same functions (and the
exact same sent-log recording — `mailer.record_sent`,
`texter.record_sent_text` with `variant`) as their standalone tabs. Net
effect for the document is identical to true row-by-row interleaving
(every row gets every channel it qualifies for, exactly once); doing it
as two passes over two already-thoroughly-tested primitives was judged
safer than a third, novel interleaved sender. Progress is tracked as two
independent counters (`email_done`/`email_total`, `text_done`/`text_total`)
shown as two separate progress bars, since the two phases don't run at
the same time. Each channel gets its own delay-between-sends control
(defaults matching each standalone tab: 4.0s email, 3.0s text).

Verified end-to-end against a live 466-row production database (real
Dental Practices leads across several states): 12 email-only, 393
text-only, 61 both, 0 excluded — `12+61=73` emails and `393+61=454` texts
computed and displayed correctly, confirmed against the confirmation
checkbox's own arithmetic. The actual send was **not** triggered during
verification (466 real companies) — only computed/rendered state was
checked.

### Browser re-scrape (`browser_scrape.py`) — automatic + manual

For leads with a real website (not `osm:`) but no email/phone. Runs in two
places on the same underlying function (`rescrape_many_with_browser`):

- **Automatically**, once per search run — pipeline step 6 (§3). Capped by
  `browser_retry_cap`, and only when Playwright is available.
- **Manually**, from the Lead Database tab's "Retry with a real browser"
  button — covers whatever the automatic pass's cap left out, or a batch
  from an older CSV that predates the automatic step.

`is_available()` checks Playwright + chromium binary by launching a
throwaway browser — genuinely slow, so `app.py` calls it **once per
session** and caches the result in `st.session_state.browser_available`,
not on every script rerun.

`rescrape_many_with_browser` launches **exactly ONE browser for the whole
batch** (added 2026-09-14 — it previously launched-and-closed a fresh
browser per lead, sometimes twice if a contact-page fetch was also
needed). One page/tab is opened once and reused, `page.goto()`-navigated to
each lead's URL in turn; `rescrape_with_browser` and `_fetch_rendered_html`
both take that `page` as a parameter rather than launching their own — a
full Chromium launch is the expensive part of this pipeline, far more than
loading one more URL in an already-running browser, so this cuts N launches
down to 1 for a batch of N. If Playwright itself fails to launch at all,
whatever was already processed is returned plus the untouched remainder —
same "never worse than the original" guarantee as a single-lead failure.

Wait strategy also changed 2026-09-14: `domcontentloaded` + a fixed
`JS_SETTLE_MS` (1500ms) pause, not `wait_until="networkidle"`. Full
`networkidle` (no network activity for 500ms) can hold the wait open for
the *entire* 70s timeout on sites with persistent background connections
(chat widgets, analytics, ad trackers — common on small-business sites)
even though the actual contact info rendered almost immediately;
`domcontentloaded` returns as soon as the DOM is parsed, and the short
fixed settle covers typical JS-driven content (lazy footers, cookie-gated
sections) without paying for the page's slowest network connection.
Trade-off: content that renders even later than 1.5s after DOM-load could
still be missed — the same class of risk `networkidle` already had for
pages that never go fully idle, just a different failure mode, and much
faster on the common case.

Runs the **same** `scrape.scrape_company` on the rendered HTML either way,
merges any newly-found non-empty fields.

---

## 15. Deliberate non-goals / constraints

- **No LinkedIn automation** — no login, profile scraping, or email
  extraction from LinkedIn. Violates their ToS, is aggressively blocked,
  and risks account suspension. Only URLs a company self-publishes are
  captured, for a human to review.
- **No paid/keyed data or search APIs** for lead finding — OSM + `ddgs`
  only. Brave / Google PSE / SerpAPI were removed on purpose. **Yelp was
  evaluated (2026-09-11) and rejected on the same grounds**: Yelp ended
  free Fusion API access — it's now "Yelp Places API," a 30-day/5,000-call
  trial then $229+/month (with a *minimum* 1,000 calls billed per day
  regardless of usage). Business Search is also location-string based (city/
  address/lat-long, not a whole state — same per-city looping this app
  already does) and its terms describe it as built for "consumer-facing"
  display of Yelp data, not for compiling an outreach list. If a paid
  source is ever approved, Google Places (Text Search) is the better fit —
  it returns the business's own `website` field directly, which Yelp
  generally doesn't.
- **No inbound-reply parsing** — unsubscribes are handled manually via the
  suppression list.
- **No general geography/country detection beyond TLD + phone area code**
  (gap, not a designed-around limitation — noted here so it isn't
  "discovered" again). A foreign business on a `.com`/`.org` domain (a
  Qatar hospital, a UK dental clinic, an Italian tourism board, an
  Australian destination-wedding site — all found in real audits) is only
  ever caught by hardcoding its domain into the blocklist after someone
  spots it. A real fix would need either a country/state-name check
  against the scraped page text or address, or an authoritative
  address-verification step — meaningfully bigger than a blocklist entry,
  and not built. If foreign leakage keeps recurring at volume, this is the
  next real investment, not another one-off domain add.
- **No CPA-society / BBB / YellowPages / SoS-registry scrapers** — per-state
  structure differences and anti-bot defenses make them unreliable;
  import a manual CSV instead.
- **Owner-name extraction stays high-precision / low-recall** — do not
  loosen the patterns or trim `_NON_NAME_WORDS` to "get more names".
- **Sending stays opt-in and confirmed** — never remove the confirmation
  checkbox, the test-send step, or the inter-send delay floor.
- **OSM notice:** `osm_source.py` header says it was never exercised
  against the live Overpass API in the build environment — validate a
  single-state run before trusting a large batch.

---

## 16. Known issues / tech debt

1. ~~`.gitignore` coverage~~ — **resolved 2026-09-15**: all of `data/`
   (send logs, suppression list, templates, `leads.csv`) is now
   gitignored, since the repo went public on GitHub. `.env` is ignored
   (good) but currently contains live credentials on disk — rotate any
   credential that may have been exposed before this was in place, and
   never commit `.env` itself.
2. **`storage.rescan_confidence` is unreachable from the UI** — fine while
   the DB only holds one freshly-scored run; an imported external CSV still
   can't be re-scored without code.
3. ~~`TARGETABLE_EMAIL_PREFIXES` is dead as a gate~~ — **removed 2026-09-14**
   (it contradicted the blocklist by listing "contact" as wanted).
4. **Two Streamlit-rerun workarounds** (background threads, progress dicts)
   are load-bearing; an agent refactor of `app.py` must preserve them.
5. ~~Junk filter — photographers under Event Management~~ — **fixed
   2026-09-14**: photography/videography terms moved from the conditional
   list to the unconditional `_OFF_TARGET_NAME_PATTERNS` (§6a rule 4).
6. **Name normalization doesn't trim comma-tails** — "Memories by Design,
   Wedding Planner Orange County CA" keeps the trailing location. Commas
   are too risky to split on blindly ("Smith, Jones & Co").
7. **A run that keeps zero rows leaves the old file** (intentional, so a
   failed search doesn't wipe data) — but that means the DB can briefly
   disagree with "the last search". The UI warns when this happens.
8. **Per-city OSM bounding-box queries were considered and NOT implemented**
   (2026-09-11) — true per-city Overpass queries would need geocoding each
   city (Nominatim, with its own strict 1 req/sec policy) and could mean
   hundreds of Overpass calls per state, risking Overpass's public-instance
   usage policy. Implemented instead: per-*tag* Overpass calls (§3 step 1),
   which reduces per-request size/timeout risk without hammering Overpass
   per city. Revisit if per-city OSM granularity is still wanted — it's a
   bigger, separate piece of work (geocoding + its own rate limiting).
9. **The Send Emails/Send Texts file-upload feature (§14 step 0) only
   works for files shaped like this app's own exports** — a genuine
   external contact list (just name/email/phone, no `service`/
   `confidence_label` columns) normalizes to `service=""`/
   `confidence_label="Unrated"` and is filtered down to zero eligible
   leads by the hard safety gate, by design. If sending from a truly
   arbitrary external list is ever wanted, that's a deliberate scope
   change to the safety model (bypassing confidence scoring entirely),
   not a bug fix — don't loosen the gate to "fix" this without the user
   explicitly asking for it.

---

## Change log

- **2026-09-18 (search-failure circuit breaker + DNS-aware error message)**
  — User hit a real run where 36 queries failed with the identical
  pattern across 3 different engines (`search.yahoo.com`,
  `search.brave.com`, `www.bing.com` all returning "DNS error: Server
  Failure"), which the existing warning's advice ("pin to one reliable
  engine") wouldn't have fixed, since the DNS resolver itself was the
  problem, not any one engine. Two fixes, see §3 step 2's new bullet for
  the full mechanism: (1) a `consecutive_search_failures` counter tracked
  across the whole run (not per target) trips a circuit breaker at 8
  consecutive total-failure queries, disabling web search for the rest
  of the run (OSM keeps working — separate API) instead of grinding
  through every remaining query at full retry-backoff cost for a
  guaranteed failure; (2) both the trip-time warning and the end-of-run
  summary detect when the sampled errors mention "DNS error" and steer
  toward checking network/DNS instead of repeating the single-engine-pin
  advice, which is actively unhelpful for a DNS-wide failure. Verified
  the counting logic in isolation (couldn't reproduce real DNS failure
  on demand): fed it a simulated success/failure sequence and confirmed
  it resets on any success and trips on exactly the 8th consecutive
  failure.
- **2026-09-18 (Bulk Send tab)** — User request: a combined send that
  goes row by row through a document and emails or texts (or both) each
  company depending on what contact info it actually has, instead of
  running Send Emails and Send Texts as two separately-filtered passes
  (Send Texts' own "no email only" default would otherwise skip texting
  any row that also has an email). New `Bulk Send` tab — see §14 for the
  full mechanism. Reused the source picker and both channels' existing
  eligibility rules and send primitives verbatim rather than writing a
  new interleaved sender; the only new logic is computing the two
  eligibility masks side by side instead of one funnel excluding the
  other. Verified against a real 466-row database (73 emails + 454 texts
  computed correctly across email-only/text-only/both buckets) without
  triggering the actual send.
- **2026-09-18 (pre-run time estimate fixed; double icon fixed)** — Two
  bugs in the Find Leads pre-run warning, both user-reported: (1) the
  warning showed `⏱️` twice — the icon was both hardcoded into the
  message text AND passed as `st.warning(..., icon="⏱️")`; removed the
  text-embedded one. (2) The time estimate itself was a bare floor
  (`est_queries × SEARCH_QUERY_DELAY`), explicit about excluding request
  time, scraping, and retries — technically honest but badly undersold
  real runtime, since those excluded costs are usually bigger than the
  delay itself. Replaced with a real estimate that adds a per-query
  request-time constant on top of the delay, plus a new, separate
  scrape-phase estimate (`candidates_per_unit / concurrency ×
  AVG_SCRAPE_SECONDS_PER_SITE`, candidates capped at an observed-typical
  150/unit rather than the theoretical/uncapped max). See the "pre-run
  estimated query count" note earlier in §3/§5 area for the exact
  formula and constants.
- **2026-09-18 (Stop button + incremental saving)** — User picked this up
  from a standing suggestions list. See §3 step 7a for the full mechanism.
  Short version: a Stop button that works by letting Streamlit's own
  abort-on-any-interaction behavior do the actual stopping, made safe by
  saving every fully-completed (state, business type) unit's leads to
  `leads.csv` as it goes rather than only once at the end — so stopping
  early (or any other interruption) never loses more than one in-flight
  unit. Verified against a live 2-vertical run: stopped mid-way through
  unit 2, confirmed exactly unit 1's High-confidence leads survived in a
  fresh browser session reading from disk.
- **2026-09-18 (SMS A/B/C variants)** — Client supplied 3 alternate SMS
  bodies to test against the existing default across different states/
  business types and see which gets more replies. See §11 for the full
  mechanism (`SMS_TEMPLATE_VARIANTS`, migrated `custom_sms_templates.json`
  schema, variant picker in Send Texts, new `variant` column on
  `sent_text_log.csv`). Wording is the client's own, verbatim — only their
  `[Name]` placeholder was mapped to this app's existing `{greeting_name}`
  merge field; do not edit the copy itself without the client asking.
  Verified end-to-end against a live app instance: all 4 variants
  (Original + 3 new) appear in the picker, switching variants correctly
  swaps the editable template text, and the existing custom "Original"
  template (already saved from a prior session) survived the schema
  migration intact.
- **2026-09-18 (detailed Find Leads progress UI)** — User request: the old
  progress feedback (one bar advancing once per STATE, a text line per
  query) could sit visually frozen for a long time on a multi-vertical run
  — indistinguishable from actually being stuck. Replaced with the whole
  search+scrape loop wrapped in one `st.status(..., expanded=True)`
  container (`app.py`, `run_clicked` block):
  - Overall progress now advances once per **(state, business type) unit**
    (`total_units = total_states * len(targets)`), not once per state —
    the actual bug behind "looks frozen" on an 8-vertical run.
  - A running elapsed-time readout (`_format_elapsed`, new helper),
    refreshed on every single query and every scrape completion — this
    IS the "still moving" signal, so it's deliberately not throttled.
  - The status label itself shows "Searching {state} — {vertical}
    (N/total)"; the body keeps a query-level sub-count ("query 4/17") and,
    after each unit finishes, a permanent log line ("✅ Alabama — Floral
    Businesses: kept 12 lead(s)") that stays visible in the scrollback for
    the rest of the run — so past progress is inspectable, not just
    current status. Collapses to one line with a checkmark
    ("✅ Search complete — 3m 12s total") when done.
  - **Real gotcha hit and fixed while building this, worth knowing before
    touching this code again**: `StatusContainer.update()`'s `expanded`
    parameter defaults to `None` ("don't change it" per the docstring),
    but omitting it actually **clears** that field on the delta protobuf
    message rather than leaving it alone — confirmed by reading the
    pinned Streamlit 1.38.0's own `mutable_status_container.py` — and the
    frontend renders a cleared bool field as `False`. Net effect: calling
    `.update(label=...)` without also passing `expanded=True` **silently
    re-collapses the box** on every update, which is exactly what
    happened here (one `.update()` call per state×target unit, i.e.
    constantly) until caught by an actual browser screenshot showing the
    box stuck collapsed with no visible content for 30+ seconds on a live
    run — reading the code alone would not have caught this. Fix: pass
    `expanded=True` explicitly on every `.update()` call that should stay
    open, not just the initial `st.status(...)` call.
  - Verified against a live app instance end-to-end: a real small search
    (Delaware, Manufacturing, OSM/city-queries/extra-phrasings off for
    speed) confirmed the box stays expanded and the elapsed time and
    query counter tick correctly throughout, then correctly collapses to
    "✅ Search complete — 39s total" with real results, an archived
    snapshot, and the usual summary metrics below it.
  - Also fixed a false alarm from this same investigation: initially
    misread a *different* Python environment's Streamlit (1.44.1, not the
    pinned/actually-running 1.38.0) while debugging, which looked like a
    dependency-pin drift worth "fixing" — reverted that edit once the
    correct environment (`/home/ubuntu/miniconda3/envs/lead_finder/bin/python3`)
    confirmed `requirements.txt`/`environment.yml`'s `streamlit==1.38.0`
    pin was already accurate. Always check the project's actual conda env
    (not bare `python3`) when inspecting installed package versions here.
- **2026-09-18 (HubSpot removed entirely)** — User request: `hubspot_crm.py`
  deleted, its `app.py` UI section (Lead Database tab "HubSpot CRM" block,
  the `hubspot_crm` import, `hubspot_push_state` session-state init) and
  the `HUBSPOT_ACCESS_TOKEN` line in `.env` all removed. CLAUDE.md and
  README updated to match (module table, env var table, §14 HubSpot
  subsection, project-tree listing, §1's product description). Historical
  changelog entries that mention `hubspot_crm.py` were left as-is — they're
  a record of what happened at the time, not living documentation. Also
  corrected a stale line while in §1: SMS was still described as "code
  complete, needs 8x8 credentials" even though the 2026-09-15 entry above
  already recorded those credentials being added and the tab going live.
- **2026-09-15 (upload leads from your own computer to send from)** —
  Extended the same-day "send from a saved run" feature: Send Emails and
  Send Texts each gained a multi-file uploader (`st.file_uploader`,
  `.xlsx`/`.csv`) in the same expander as the saved-run picker, for files
  the user has moved, hand-edited, or is bringing from another machine —
  not just ones still sitting in `data/exports/` on this one. New
  `storage.load_uploaded_snapshots(files) -> (df, errors)` reads and
  combines them (CSV via `pd.read_csv`, Excel via `pd.read_excel`),
  skipping and reporting (not silently dropping) any file that fails to
  parse. New `storage.dedupe_keep_last` factors out a dedup-by-domain
  that's **safe for a blank domain column** — found while designing this:
  a hand-made sheet with no domain/website column would normalize every
  row's domain to `""`, and the existing `drop_duplicates(subset=["domain"])`
  pattern used elsewhere would silently collapse ALL such rows down to
  just the last one. `load_export_snapshots` was also switched to this
  safer helper. Picked snapshots and uploaded files combine into one
  pool (unioned, then deduped). The two hard safety filters (service
  match + High confidence) apply to this combined pool exactly as they
  do to the live database — an uploaded file that isn't shaped like one
  of this app's own exports (no `service`/`confidence_label` columns)
  correctly produces zero eligible leads rather than bypassing scoring;
  see §16 item 9. Verified end-to-end against a live app instance: built
  a real `.xlsx`, uploaded it through the browser, and confirmed the
  eligibility banner and lead count updated correctly; separately
  confirmed the blank-domain case (2 hand-made rows, no domain column)
  keeps both rows instead of collapsing to 1, and that a corrupted upload
  is reported as an error rather than crashing the page or silently
  vanishing.
- **2026-09-15 (8x8 subaccount ID added — texting now live)** — User
  supplied `EIGHTXEIGHT_SUBACCOUNT_ID`, completing `texter.get_eightxeight_config`'s
  two required vars (`EIGHTXEIGHT_API_KEY` was already set) — the Send
  Texts tab is no longer gated behind the "not configured yet" message.
  Verified: `get_active_texting_config()` now returns a real config, and
  the tab renders its full eligibility funnel against live data. Also
  brought the "send test text" flow up to the same pattern as email's
  (added earlier the same day): new `TEST_TEXT_NUMBERS` env var (comma-
  separated, `.env`/`.env.example`) feeding a "Send test text to"
  dropdown + Custom, replacing the old plain always-Custom text input.
  Deliberately still **one** test row (not per-vertical like email) since
  SMS templates don't vary by vertical — see §11/§14. `TEST_TEXT_NUMBERS`
  is left blank pending the user supplying actual test numbers; nothing
  has been sent yet.
- **2026-09-15 (compliance/opt-out line removed, explicit user request)**
  — `email_template._compliance_line` and both its call sites
  (`render_for_lead`, `render_html_for_lead`) deleted outright. Every
  email this app sends used to end with an unconditional, non-editable
  line: `You're receiving this because {company_name} is publicly listed
  in {state}. Reply "unsubscribe" and we won't contact you again.` — this
  was the app's only in-email CAN-SPAM opt-out mechanism. The user asked
  for it gone "permanently... from everywhere... applies to all mails (all
  businesses, all states)" — before implementing, they were told
  explicitly that this line was the only opt-out language in the email
  and asked to confirm full removal with no replacement (as opposed to
  keeping a shorter opt-out line); they confirmed full removal. Verified
  by rendering a test lead through both `render_for_lead` and
  `render_html_for_lead` and grepping the output for "publicly listed"/
  "unsubscribe" — absent from both. This is a real compliance trade-off,
  not just a copy change — see the note in §11 and the updated
  "Compliance" section in README.md. Do not re-add this line as a "fix"
  without the user asking for it back.
- **2026-09-15 (decouple scraping from sending: run archive + picker)** —
  User request: be able to download each run's results and later send
  email/text from a chosen past run (or several combined) without having
  to re-run the search first. Implemented as a per-run archive rather
  than a manual upload flow:
  - New `storage.save_export_snapshot`/`list_export_snapshots`/
    `load_export_snapshots`/`build_export_filename`. Every completed
    search run (right after `save_leads(replace=True)`) now ALSO writes
    a standalone `.xlsx` to the new `data/exports/` folder (added to
    `.gitignore` — same real-contact-data sensitivity as the rest of
    `data/`), named `date_businesstypes_states.xlsx` per the user's
    requested convention (e.g.
    `2026-09-15_EventManagement+FloralBusinesses_Alabama+Alaska.xlsx`).
    Unlike `leads.csv`, this folder is deliberately append-only — one
    file per run, never overwritten by the next search — since the whole
    point is to keep past runs reachable after `leads.csv` moves on.
    "Search all 50 states + DC" collapses the states part of the
    filename to `AllStates` rather than listing all 51.
  - Send Emails and Send Texts tabs each gained a "Send from a saved run
    instead of the current database" expander: a multiselect of archived
    filenames (newest first); picking one or more loads and combines
    them (deduped by domain) as the starting point for that tab's
    eligibility funnel, in place of the live Lead Database view. The two
    fixed safety filters (service match + High confidence only) still
    apply unconditionally either way — picking an old snapshot is not an
    exemption from them.
  - Also fixed a small leftover from the earlier per-vertical test-email
    change: `SERVICE_PRESETS` was imported in `app.py` but no longer
    used anywhere after that change removed its only call site — deleted
    rather than left as dead import weight.
- **2026-09-15 (Excel exports, signature fixes, per-vertical test email)**
  — Three user-requested changes:
  1. **Excel (.xlsx) export** added alongside every CSV download in the
     Lead Database tab (main leads, email-ready, SMS-ready) via a new
     `app._to_xlsx_bytes()` helper (`pandas.ExcelWriter`, `openpyxl`
     engine — already a listed but previously unused dependency).
  2. **Email signature fixes** (`mailer.py`): removed the "WhatsApp: "
     text label (the row now shows just the phone number); replaced the
     generic speech-bubble emoji badge with the real WhatsApp icon
     (`lead_finder/assets/whatsapp_icon.png`, new asset — official
     simple-icons glyph rendered white on a `#25D366` circle via
     `cairosvg`), embedded inline as `cid:wa_icon` alongside the existing
     `cid:sig_logo`; fixed the logo visually overflowing below the
     signature text by (a) tightly cropping `signature_logo.png` to its
     visible content — the original had roughly 30% dead whitespace
     padding around the mark, which was making the logo cell taller than
     the text column next to it — and (b) giving the logo `<td>`/`<img>`
     explicit HTML `width`/`height` attributes and `valign="middle"` in
     addition to the existing CSS, since Outlook's rendering engine often
     ignores `height: auto` and falls back to an image's native pixel
     size. Verified by rendering the actual signature HTML through
     Playwright (already a project dependency) and inspecting the
     screenshot, not just reading the markup.
  3. **Per-vertical test email** (Send Emails tab): "Send a test email"
     now renders one row — its own address dropdown, its own "Send"
     button — **per target vertical present in the current working set**
     for the active service, instead of a single generic test send. Also
     fixes a real pre-existing bug found while implementing this: the old
     single test lead never set `target_vertical` at all, so a test send
     could never actually exercise a vertical-specific template
     (`email_template.VERTICAL_TEMPLATES`, e.g. Event Management or
     Floral Businesses) — it silently always rendered whatever
     `load_template()` falls back to. Verified against live data (a real
     78-row Alabama database spanning both Event Management and Floral
     Businesses) that the two rows render distinct subjects/bodies.
- **2026-09-15 (Event Management + Floral audit — Alabama & Alaska)** — A
  manual audit across two states/verticals reported 16 suspected junk
  leads. Investigated each via direct site lookup rather than assuming;
  confirmed 13 as real junk (root causes: 3 foreign businesses on plain
  `.com` domains — a New Delhi software studio, a UAE florist, a Vietnam
  florist; 3 national florist/delivery networks — Kremp, Avas Flowers'
  `.net`, flowersin4hours.com; 3 national wedding-vendor booking services
  advertising "all 50 states" coverage; a tourism-board site scraped under
  a misleading "Weddings and Events" name; a gambling blog and a
  travel/food blog with zero relation to the vertical; a nail salon named
  "Florist Nails"). Fixed:
  - `FOREIGN_BUSINESS_DOMAINS` +3 (azonova.com, greenplantflowers.com,
    flowerdeliverydanang.com); `EVENT_AND_FLORAL_DIRECTORY_DOMAINS` +9
    (kremp.com, avasflowers.net, flowersin4hours.com,
    indian-wedding-dj.com + its same-operator alias djmehul.com,
    poshweddingplanners.com, travelsitka.com, travelcookeat.com,
    techntrip.com).
  - New unconditional `junk_filter` name pattern for nail salons (§6a
    rule 4), same precision-safe reasoning as the photography/veterinary
    additions.
  - New general placeholder-email pattern in `extract.is_placeholder_email`
    (§7): a 32-hex-char local part or a `sentry.*` domain — found because
    Avas Flowers' stored "email" was literally a Sentry.io error-tracking
    auto-address (`<hash>@sentry.avasflowers.net`) that had leaked into
    the page's scraped text. General, not domain-specific — protects
    against the same class of bug on any site using client-side error
    tracking.
  - **Found and fixed a related gap while implementing the above**:
    `storage.rescan_confidence`'s email re-validation never called
    `is_placeholder_email` at all, so *no* placeholder rule (old or new)
    was retroactively applied to an existing CSV — only scrape-time
    filtering benefited. Added the missing check.
  - The remaining 3 reported leads (Picture Perfect Wedding Florals /
    perfectweddingevents.net, Frond & Twig, Sitka Weddings & Events LLC /
    sitkaweddings.com) were investigated and found to be **real local
    businesses**, not junk — verified via direct site lookup (named
    owners, local phone numbers, non-templated content). Deliberately
    NOT blocklisted. Picture Perfect's site does have a real phone number
    that the current pipeline never captured (only the Gmail mailto was
    found) — a data-quality gap, not a junk-filter gap; not fixed here,
    noted for a future extraction-quality pass.
  - A fourth category (several other reported leads, e.g. Magnolias
    Flower Farm, Luxx Weddings and Events LLC) were flagged by the audit
    only as "Gmail only" with no other stated problem. Spot-checking one
    of these (Frond & Twig) found a genuine local business, so these were
    **not** blocklisted — flagging "Gmail-only" as inherently too weak to
    trust would mean revisiting the 2026-09-11 mailto:-consumer-domain
    exemption, which that change log entry explicitly says not to revert
    without re-confirming its closed cases stay closed. Left as an open
    question for the user rather than acted on unilaterally.
  **Applied retroactively** to the live `data/leads.csv` (119 rows,
  Alabama + Alaska, Event Management + Floral Businesses): rescanned,
  re-ran the junk pass per vertical, re-purged to High-only → 106 rows,
  18 with email (was 119 / 29) — all 13 confirmed-junk cases dropped by
  name, none of the 3 confirmed-real businesses were touched.
- **2026-09-15 (git init, repo made public)** — Project put under git and
  pushed to a public GitHub repo. `.gitignore` expanded to cover every
  file in `data/` (previously only `leads.csv`) — send logs, suppression
  list, and templates are per-machine runtime state / real scraped
  contact data, not source, and shouldn't be in a public history. `.env`
  was already ignored and was not committed. Resolves known issue #1.
  Also found and scrubbed two pieces of real PII that were about to ship
  in source/docs, not just data files: `.env.example`'s "example" sender
  values were actually the live SMTP account's real name/company/phone/
  address (replaced with generic placeholders), and `app.py`'s "Send test
  email to" dropdown hardcoded real personal/business email addresses
  (moved to a new `TEST_EMAIL_ADDRESSES` env var, kept in the local
  gitignored `.env`, blank in `.env.example`).
- **2026-09-14 (Dental Practices audit — Alabama, round 2)** — 3 more
  non-target businesses reported, all reaching High because they pass the
  Dental Practices relevance keywords (`dental/dentist/dentistry/orthodont`)
  on a technicality:
  - **Auburn University College of Veterinary Medicine** — "dentistry"
    isn't in the name, but veterinary dentistry is a real specialty, so
    something on the page matched. **Fixed**: new unconditional pattern
    `\bveterinar(y|ian)\b` in `junk_filter._OFF_TARGET_NAME_PATTERNS` — no
    vertical in this app wants a veterinary business, so (like
    photography) this isn't gated by vertical keywords, closing the
    hypothetical hole where a name could contain both "veterinary" and
    "dental" together.
  - **The Andalusia Star-News** — a local newspaper. New pattern for
    newspaper-masthead words (`star-news`, `tribune`, `gazette`,
    `chronicle`, `dispatch`, `courier`, `ledger`, `observer`,
    `advertiser`) — deliberately excludes bare `news`/`times`/`press`
    (too generic; "Flower Press" is a real floral-business name shape,
    tested as a control and confirmed to survive).
  - **Agd (Academy of General Dentistry)** — a professional association,
    not a practice. New pattern:
    `\b(academy of|association of|society of|college of|school of|
    institute of)\b.{0,40}\b(dentistry|dental|medicine|veterinary)\b` —
    one pattern that also independently catches the vet-school case above
    (via "College of ... Medicine"), verified both trigger it.
  All three verified against the exact reported cases plus a control set
  (real dental/event/floral businesses, including "City Dental Hospital"
  and "Flower Press Studio" as adversarial checks) — no regressions.
  **Applied retroactively** to the live `data/leads.csv`: 133 → 130 rows
  (dropped the vet school, the newspaper, and a previously-seen "Animal
  Hospital" re-added by a fresh run since the last cleanup — confirms the
  existing `hospital` rule is still catching that one correctly), 19 → 17
  with email.
- **2026-09-14 (Dental Practices audit — Alabama)** — A manual audit found
  3 non-target businesses that reached High confidence and one real
  precision gap in the mailto: exemption added 2026-09-11:
  - `email_ok`'s mailto: domain-match exemption **narrowed to consumer
    email providers only** (`extract.CONSUMER_EMAIL_DOMAINS`) — the
    unconditional version let a mailto: to `md@dentalcaremadison.com`
    (a different custom domain than the site's own `madisondentalcare.com`)
    reach High. Verified: Gmail-sourced mailto still reaches High
    (unaffected — the case the exemption was built for); the mismatched-
    custom-domain case now correctly caps at Medium; matching-domain and
    regex-path behavior unaffected.
  - New `blocklist.FOREIGN_BUSINESS_DOMAINS` (`alfaridhospital.com` — Qatar
    hospital, `confidentalclinic.com` — UK dental clinic) and
    `talkinternational.com` added to `DIRECTORY_DOMAINS` — a directory
    site posing as "Holistic Dentists". Logged the underlying gap (no
    geography check beyond TLD/area code) explicitly in §15 rather than
    treating each foreign hit as a one-off.
  - New conditional off-target term `hospital` in `junk_filter.py` —
    dropped both the reported case AND, when applied retroactively, an
    unrelated "Animal Hospital" veterinary clinic that was also sitting in
    the database under Dental Practices (confirms the rule generalizes,
    not just pattern-matching the one reported example). Genuine dental
    hospitals/clinics are spared via the existing vertical-keyword check
    (tested: "City Dental Hospital" survives).
  **Applied retroactively** to the live `data/leads.csv` (Alabama, Dental
  Practices, 132 rows): rescanned + re-junk-filtered + re-purged to
  High-only → 127 rows, 12 with email (was 16 — the mismatched-domain lead
  and 3 confirmed-junk businesses' emails are the difference).
- **2026-09-14 (scrape-time reduction)** — Implemented 5 of the levers from
  a "how do we cut scraping time without losing results" analysis:
  1. **`results_per_query` no longer forced to 200 in uncapped mode** —
     "uncapped" now only removes the candidate-pool ceiling; the per-query
     result count is always whatever the slider says, default lowered
     200 → **40**. Long-tail SERP results past ~40 for a hyper-local query
     are overwhelmingly noise, and asking for 200 likely meant `ddgs`
     paginating the engine's results page several times per single query.
  2. **Adaptive per-target early-stop** — new `no_new_streak_limit`
     (default 15): after N consecutive queries add zero new unique
     domains to a state+target's pool, stop issuing further query variants
     for it. Resets on any query that does add something, so it only ever
     fires on genuine saturation, never on a target that's still finding
     new companies.
  5. **OSM fetch runs in the background while web search runs** — a daemon
     thread does ONLY the raw Overpass fetch; all processing into
     candidate rows happens single-threaded after `.join()`, right after
     the web-search loop, preserving the exact original OSM-wins-a-tie
     precedence (verified with a standalone test) with zero race risk.
  7. **`rescrape_many_with_browser` launches one browser for the whole
     batch**, not one per lead (verified: a 3-lead batch now launches
     exactly 1 browser, confirmed via a mocked-Playwright test) — a full
     Chromium launch was the dominant cost, not the page load itself.
  9. **Playwright wait strategy: `domcontentloaded` + a fixed 1.5s settle**,
     not `wait_until="networkidle"` — avoids the entire 70s timeout being
     eaten by a page with persistent background connections (chat/
     analytics widgets) that never goes fully idle.
  (Items 3, 4, 6, 8 from the same analysis — trimming query templates by
  measured yield, caching repeated queries across runs, parallelizing
  search across targets, and parallelizing the browser-retry batch itself
  — were not requested and remain undone; #4/#6 need real telemetry first,
  #8 was explicitly flagged as a resource-cost trade-off, and parallel
  search across targets was flagged as risky given past rate-limit/DNS
  incidents on this project.)
- **2026-09-14 (junk-filter expansion)** — A manual, row-by-row audit of a
  real Event Management + Floral CA run (~74 + 84 rows) found ~25-35 noise
  rows the filters missed: an SEO tool, directory sites impersonating a
  named business, a portable-toilet company hiding behind a generic
  display name, foreign (Australian/Italian) sites, news/local-blog sites,
  a photographer (the long-known soft edge, now actually fixed), a law
  firm, a ticket seller, a Squarespace/WordPress template page, and several
  `wecare@`-prefixed "florist network" pages the user identified as
  templated, non-independent listings. Fixed in `junk_filter.py` +
  `blocklist.py`:
  - Photography/videography moved from conditional to **unconditional**
    off-target (§6a) — closes known issue #5.
  - New unconditional name patterns: `citation checker/builder`,
    `destination wedding(s)`, `law firm/attorney/legal services` + bare
    `Law` as a naming *suffix* only, `box office/ticket sales`, `visit
    <Placename>`, `turismo`.
  - **Domain is now checked too**, against the same name-pattern list plus
    a new plain-substring list (`_OFF_TARGET_DOMAIN_SUBSTRINGS`) — catches
    a junk site whose *display name* was rebranded but whose domain still
    gives it away (`mrportapottyorlando.com` under "USA Rental Experts").
  - New `_NETWORK_TEMPLATE_EMAIL_PREFIXES = {"wecare"}` — a row is now
    dropped entirely (not just made un-sendable) when its email has this
    prefix, since it signals a templated multi-city network page.
  - `blocklist.py`: 10 specific bad domains added to
    `EVENT_AND_FLORAL_DIRECTORY_DOMAINS`; new
    `WEBSITE_BUILDER_DEFAULT_DOMAINS` set (Squarespace, Wix, Weebly,
    WordPress.com, ...) added to `ALL_BLOCKED_DOMAINS` — verified against a
    real hit (a stale `allensflowermarket.wordpress.com` page, correctly
    dropped as a duplicate of the same business's real
    `allensflowermarket.com` listing elsewhere in the same run).
  All new rules tested against the reported cases (all correctly dropped)
  and a control set of real, legitimate leads from prior runs (all
  correctly survived, including adversarial checks like "Flawless Weddings
  and Events" against the new `law` pattern). **Applied retroactively** to
  the live `data/leads.csv` (post the same-day email-quality cleanup
  below): 556 → 537 rows, 19 dropped.
- **2026-09-14** — Two data-quality bugs reported directly from the
  collected leads: (1) `emailto:@rmfloralstudio.com` in the database — the
  site's own `mailto:` href was a page-builder typo
  (`mailto:emailto:@rmfloralstudio.com`, no real local part), accepted
  verbatim because nothing validated a mailto:-sourced address was even a
  syntactically real email. **Fixed**: `extract.extract_mailto_emails` now
  validates every candidate against `EMAIL_RE.fullmatch` (comma-separated
  multi-recipient mailto:s are split and checked individually first).
  (2) `hi@`, `contact@` (and, found while checking, `wecare@` — 24
  occurrences from the templated florist-network sites — and
  `custservice@`) present and sendable despite the generic-inbox filter.
  Root cause: `blocklist.GENERIC_EMAIL_PREFIXES` never listed these exact
  strings (it's exact-match, not substring — "wecare" ≠ "care"). **Fixed**:
  added `hi`, `hey`, `contact`, `wecare`, `custservice` to the set. Also
  removed `TARGETABLE_EMAIL_PREFIXES` (dead code that self-contradicted the
  fix by listing "contact" as wanted — see §16 item 3).
  `storage.rescan_confidence` was extended to also reject a stored email
  that's syntactically invalid or now has a generic prefix (previously it
  only re-checked domain-block/domain-match), so an existing CSV benefits
  retroactively. **Ran it once against the live `data/leads.csv`**
  (560 rows) and re-applied the High-only purge: 32 bad emails removed (31
  generic-prefix + 1 syntactically invalid), 4 rows dropped to below High
  entirely (they had no other qualifying signal once the email was
  removed) — 556 rows / 126 with-email remain, all still High under
  current rules.
- **2026-09-11** — Follow-up run (CA, same 2 verticals, after the query-count
  fix): 2,271 scraped in reasonable time (search reliability + query-count
  fixes both confirmed working — no engine-timeout warning this time), but
  still only 662 kept (1,493 dropped as non-High) and 102 of those with an
  email. Root cause identified: `extract.score_lead`'s `email_ok` hard gate
  required domain-match even for a `mailto:`-sourced email, capping a very
  common real pattern (small event/floral businesses publishing a personal
  Gmail/Yahoo mailto: link) at Medium. **Fixed**: mailto:-sourced emails are
  now exempt from the domain-match requirement (regex-sourced ones are not
  — see §8). Tested: a mailto:+Gmail lead with a structured name now scores
  High (was Medium); a same-shape lead with only a `<title>`-sourced name
  correctly stays Medium (the gate removal doesn't bypass the ratio
  threshold); a regex-sourced mismatched email is confirmed still excluded.
  Volume guidance also given: with runtime now controlled, running more
  states (the existing "Search all 50 states + DC" checkbox) and all 8
  verticals rather than 2 is the direct lever for hitting four-figure daily
  volume — the per-state/vertical yield is now known-ish (~330 High leads,
  ~50 emails per vertical in CA) rather than a mystery.
- **2026-09-11 (correction)** — Two problems surfaced by a real run (CA,
  Event Management + Floral, ~4,400 queries, took hours, 274 kept / only
  21 with email):
  1. **`SEARCH_ENGINE_BACKEND=bing` (set earlier the same day) was wrong** —
     bing.com's own DNS lookup was failing on this network ("DNS error:
     Server Failure"), and pinning to one engine removes ddgs's in-call
     fallback to the next engine, so every query using it died outright
     (199 in that run). **Reverted to blank** (`.env`), restoring the
     multi-engine default (§13) — do not pin to a single engine again
     without separately confirming it's reliably reachable.
  2. **Combining "use extra phrasings" with "all cities" multiplied into a
     combinatorial blowup** — every synonym term was being expanded across
     every city (`N_terms × (6 + N_cities)`), not just the state-level
     variants. Added `city_term_limit` to `search.build_queries`: state-
     level queries still use every term, but city-level fan-out is capped
     to the first `city_term_limit` terms (default **1**, UI slider
     "Phrasings expanded per city", 1–5). Cuts the CA/Floral+Event example
     from 4,401 → 1,020 queries (4.3×) with full phrasing coverage kept at
     the state level. Also added a **pre-run query-count estimate**
     (`st.warning` above 300 estimated queries, with a floor-time figure)
     so a blowup is visible before running, not after hours.
  The low email yield (21 of 274) was a downstream symptom of #1: with web
  search mostly dead, most kept leads came from OSM, which never has an
  email. Fixing #1 should recover most of that.
- **2026-09-11** — Yield/coverage pass: (a) per-vertical `query_terms`
  lists (3-5 synonym phrasings each, `search.build_queries` now accepts a
  list) behind "Use extra search phrasings per vertical" (default on); (b)
  `states.EXTRA_METRO_AREAS_BY_STATE` / `get_extra_metro_areas` — curated
  metro neighborhoods for CA/TX/FL/NY, behind "Also search well-known metro
  neighborhoods" (default off); (c) `osm_source.query_osm_by_tags` now
  issues one Overpass call per tag instead of one combined call, deduped by
  (name, website) — partial-failure tolerant on dense states; added
  fallback OSM tags (`craft=florist`, `shop=garden_centre` for Floral;
  `man_made=works` for Manufacturing, which had none before); `OSM_TIMEOUT`
  env; (d) automatic browser-retry pass on `no_contact_found` leads,
  running BEFORE the High-only purge so a recovered contact can still make
  High (`auto_browser_retry` + `browser_retry_cap`, default on/150 when
  Playwright is installed); (e) `browser_scrape.is_available()` now
  cached per-session instead of re-checked (relaunches a real browser) on
  every rerun; (f) Advanced expander reorganized into 3 columns (Volume /
  Sources & filters / Contact-info recovery); (g) `.env` pinned to
  `SEARCH_ENGINE_BACKEND=bing`, `SEARCH_TIMEOUT=40` after a run showed
  every multi-engine query timing out. (h) Evaluated Yelp Fusion/Places API
  as an additional lead source — rejected for now (no longer free, ToS
  scoped to consumer display not outreach lists, no direct website field) —
  see §15.
- **2026-09-10** — Search reliability: `DDGS(timeout=SEARCH_TIMEOUT)` (default
  25s), retry-on-total-failure (`SEARCH_RETRIES`, default 2, with backoff),
  default engine list trimmed to `google,bing,brave,yahoo,yandex` (dropped
  mojeek/wikipedia — slow/timeout-prone). `app.py` now shows ONE summary
  warning for failed queries instead of one red box each.
- **2026-09-10** — Advanced-settings changes: concurrency slider now 4–50
  **default 25**; scrape cap slider now 10–2000; added a **"Final junk
  filter"** on/off toggle (`run_junk_filter`, wired into the junk pass);
  removed the "...applies to *each* of these, a full run will take longer"
  sentence from the vertical-targeting info box.
- **2026-09-10** — Database is now **per-run**: `storage.save_leads` gained
  `replace=` and the search flow calls it with `replace=True`, so each
  search rewrites `data/leads.csv` to contain only that run's kept rows
  (browser-retry still merges with `replace=False`). Removed the "show full
  history" toggle and all `last_run_domains` scoping from the tabs. Existing
  10.5k-row `leads.csv` cleared. *Fixes the "leads accumulate / target type
  shows all" confusion.*
- **2026-09-10** — New `lead_finder/junk_filter.py`: a final automatic pass
  after scrape + score (§6a), wired into `app.py` per target. Added
  `blocklist.EVENT_AND_FLORAL_DIRECTORY_DOMAINS` (event/venue marketplaces,
  wedding media, national flower gateways, retail chains) to
  `ALL_BLOCKED_DOMAINS`.
- **2026-09-10** — `scrape.scrape_many` now returns results in **input
  order**; `app.py` zips against `scrape_inputs` (which carries the query).
  Fixes the `search_query` mis-attribution.
- **2026-09-10** — `scrape._normalize_company_name`: trims tagline/location
  separators (incl. `:` / `::`), Title-cases ALL-CAPS, de-spaces initials,
  falls back to the domain name (and `name_source="fallback"`) for nav
  labels / bare slugs / >120-char keyword salad.
- **2026-09-10** — Emails lowercased on capture in `extract.py`.
- **2026-09-10** — "Fetch everything (uncapped)" checkbox now defaults **on**.
- **2026-09-10** — `config.toml` moved to `.streamlit/config.toml`.
- **2026-09-10** — Email targetability loosened: `is_targetable_email` now
  screens out only `GENERIC_EMAIL_PREFIXES` (shared inboxes) instead of
  requiring membership in `TARGETABLE_EMAIL_PREFIXES`. Personal-name
  addresses (`erica@`, `jsmith@`) are now sendable. Send-tab metric
  relabelled "Generic/shared inbox address". README + this file updated.
- **2026-09-10** — README corrected: city queries fan out across **all**
  known cities per state (not the 2 largest — that under-performed);
  removed the "Re-check confidence" button section (no such button;
  confidence is scored with current rules on every run and only High leads
  are stored by default); Send Texts section corrected from Twilio to 8x8;
  email-template section corrected to "Bookkeeping only + vertical
  templates"; project tree updated with `hubspot_crm.py` / `browser_scrape.py`.
- **2026-09-10** — CLAUDE.md created (this file).
