# Lead Finder

A Streamlit dashboard for finding prospective clients by service and US
state, pulling publicly listed contact info off their websites, drafting
tailored outreach per lead, and - optionally - actually sending it, by email
or text, in a rate-limited batch via your own accounts. Sending is off by
default in both cases: it only activates once you deliberately add
credentials to `.env`.

Currently launched with **Bookkeeping** only (the sidebar restricts the
service dropdown to this on purpose - see "Bookkeeping's target verticals"
below for why this one service covers a lot of ground). Other services
(Accounting, Tax Prep, Payroll, CFO Advisory, Business Consulting, HR
Outsourcing, Dental Billing) have OSM tags and search targeting built, but
are not exposed in the UI and have no hand-written email/SMS template yet -
they would fall back to the generic template. See "Email templates" below.

## How it works

1. **OpenStreetMap first** - for each service + state, it queries the free
   [Overpass API](https://overpass-api.de) for community-mapped businesses
   tagged with that service (e.g. `office=accountant` for Bookkeeping,
   `amenity=dentist` for Dental Billing). This is real place data, not a
   search ranking - a listing means someone confirmed this business exists
   at that address, which is why it's queried first and trusted highly in
   confidence scoring.
2. **Web search fills the gaps** - runs several DuckDuckGo query variants
   (e.g. "bookkeeping companies in Texas", plus a city-level variant like
   "bookkeeping companies in Houston, Texas" for **every city the app knows
   for that state** - the full `MAJOR_CITIES_BY_STATE` list in
   `states.py`, which for large states is hundreds of cities, not just the
   top few) to catch businesses OSM doesn't have mapped. Restricting this
   to the 2 largest cities was tried and didn't surface enough leads, so
   it now fans out across all of them. DuckDuckGo (via the free `ddgs`
   package) is the only web search backend - no paid or metered API keys
   needed for anything in this app. Trade-off: a run with city queries on
   is much longer, since each city adds one more query per vertical (with
   the `SEARCH_QUERY_DELAY` pause between each). Turn off **"Add city-level
   search queries"** in Advanced settings for a fast whole-state-only run.
3. **Filter** - before anything gets scraped, results are checked against a
   blocklist of directories/marketplaces (Bark, Clutch, DesignRush, Upwork...),
   generic platforms (Wikipedia, YouTube, LinkedIn...), "Top 10" listicle
   titles, and foreign-TLD domains (`.ca`, `.uk`, `.au`, etc - this app finds
   US-state leads, so an offshore domain usually means the query matched an
   unrelated foreign business). These aren't real local-company leads and
   are the main source of noisy results - filtering them out before
   scraping is both faster and more accurate than cleaning up after.
4. **Scrape** - for each remaining unique company domain, it fetches the
   homepage and, if needed, a linked contact/about page. Contact info is
   extracted in a trust hierarchy, not by scanning the whole page indiscriminately:
   - `mailto:`/`tel:` links and JSON-LD/`og:site_name` structured data first -
     the site is explicitly declaring this is its own name/contact info.
   - An OpenStreetMap-supplied phone number, if the site's own listing came
     from OSM and the site itself doesn't have a findable one.
   - A regex scan over the page text only as a fallback when nothing more
     reliable is found - and even then, obvious placeholders and fake-looking
     numbers (`222-222-2222`, `555-01xx`, sequential digits, `you@work.com`,
     etc.) are filtered out.
   - It also looks (best-effort) for a named founder/owner/CEO mentioned in
     the site's own text, and any LinkedIn URL the company itself links to
     - see "Targeting owners and decision-makers" below.
5. **Score** - every lead gets a **confidence label** (High/Medium/Low) based
   on how its name/email/phone were sourced, whether the email's domain
   matches the company's own site, whether the company name itself looks
   like a listicle title, and - as a soft signal, not a hard filter - whether
   the phone's area code plausibly belongs to the target state. This directly
   targets the "random companies and emails" problem: a listicle article
   that mentions ten other companies by name no longer donates its *own*
   contact form email to all ten of them - only mailto:/tel:/OSM data can
   produce a "High" score.
6. **Final junk pass** - after scraping and scoring, one more automatic
   filter runs over the finished leads (not the raw search results this
   time): it drops directory/marketplace domains that slipped through, the
   national flower-order gateways and event/venue marketplaces, businesses
   that are clearly a different trade that just matched a keyword
   (photographers, porta-potty rental, SEO agencies under an "event"
   search), and rows whose extracted name is a nav label like "Home" or
   "Contact". The run summary shows how many were dropped and a sample.
7. **Store** - the surviving leads are deduped by domain and written to
   `data/leads.csv`. **Each search replaces the database** - the app shows
   only your most recent search's results, it does not pile every past run
   into one growing file. (A search that finds nothing leaves the previous
   results untouched rather than wiping them.)
8. **Template** - each service gets its own outreach email template *and*
   its own short SMS template, both built around a simple Input → Process →
   Output pitch, and the dashboard auto-switches to the right ones when you
   change the service in the sidebar. Edits save per-service and persist
   across restarts.
9. **Send** - each lead is rendered with *its own* recorded service's
   template automatically, so a mixed-service batch (e.g. some Bookkeeping
   leads, some Dental Billing leads) sends the correct pitch to each one -
   not whatever template happened to be open. Sending (email or text) is
   rate-limited, tracked so the same lead never gets contacted twice on the
   same channel, and checks one suppression list shared across both
   channels before every send.

## Bookkeeping's target verticals

Bookkeeping doesn't search for one kind of prospect - it searches across 8
distinct business verticals, each a plausible reason a business would need
bookkeeping/accounting help:

- **Accounting & Bookkeeping Firms** - a partnership/white-label angle
- **Dental Practices** - same audience Dental Billing targets
- **Event Management**
- **Floral Businesses**
- **Construction & Real Estate**
- **Rental Properties (STR/LTR)**
- **Manufacturing**
- **IT & Software**

Each vertical gets its own **set of search phrasings** (not just one - e.g.
Floral Businesses also searches "flower shops," "flower delivery
services," and "wedding florists," not just "florists," since real
businesses describe the same trade differently; toggle "Use extra search
phrasings per vertical" in Advanced settings to use just the primary
phrasing instead), its own OpenStreetMap tags where a reasonable one
exists, and its own relevance keywords, so a "Dental Practices" search is
judged as relevant against dental terms, not bookkeeping terms. Every
lead gets tagged with which vertical it came from (the **Target Type**
column in Lead Database), so you can filter or export by vertical.

Because this multiplies the search space by 8, **"Max companies to scrape
per state" applies per vertical, not per state as a whole** (only relevant
when "uncapped" is off). A full run across all 8 verticals - especially
with extra search phrasings and city-level queries both on - takes
proportionally longer than a single-target search did; that's expected,
not a hang.

## Targeting owners and decision-makers

Every scraped page is also checked for a named founder/owner/CEO in its own
text (best-effort regex + structured-data matching - small business sites
often don't name an individual at all, so coverage is partial) and any
LinkedIn URL the company itself links to from its own site.

When an owner's name is found, email/SMS templates automatically greet them
by first name ("Hi Jane,") instead of the generic "Hi Acme Bookkeeping
team,". The **Owner/Founder** and **LinkedIn** columns in Lead Database show
what was found.

**What this deliberately doesn't do:** automate LinkedIn itself - logging
in, searching profiles, or pulling emails from LinkedIn. That violates
LinkedIn's terms of service and is aggressively technically blocked besides
(rate limits, CAPTCHAs, and real risk of your own account being suspended if
you're logged in while it happens). The LinkedIn URL captured here is meant
for a human to click and review manually, not for further automation. If
you want automated decision-maker contact data at scale, that's what
legitimate paid data providers (Apollo.io, Hunter.io, ZoomInfo) are built
for - they have licensing agreements for exactly this, which a scraper does
not.

## Setup (conda)

```bash
# from the project folder
conda env create -f environment.yml
conda activate lead_finder

# optional - only needed for email/text sending (see below)
cp .env.example .env
# then edit .env and add the values for whichever you'll use
```

If you later add or change a dependency in `environment.yml`, apply it with:

```bash
conda env update -f environment.yml --prune
```

## Run

```bash
streamlit run app.py
```

This opens the dashboard in your browser (usually `http://localhost:8501`).

## Auto-activating the environment in VS Code

Do this once per machine:

1. Open this folder in VS Code.
2. Run **Python: Select Interpreter** from the Command Palette (`Ctrl+Shift+P` /
   `Cmd+Shift+P`), and pick the `lead_finder` conda environment from the list.
3. That's it. VS Code saves this choice into `.vscode/settings.json` as
   `python.defaultInterpreterPath`, and from then on it activates
   `lead_finder` automatically in every new integrated terminal you open in
   this folder - no manual `conda activate` needed.

If the `lead_finder` env doesn't show up in the picker, make sure you ran
`conda env create -f environment.yml` first and that VS Code's Python
extension is installed - it may need a reload (**Developer: Reload Window**)
to pick up a newly created env.

## Using it

- **Find Leads tab** - pick a service (or type a custom one), pick one or
  more states (or check **Search all 50 states + DC**), adjust search depth,
  and click **Find companies**. Progress bars show search and scrape status live.
- **Lead Database tab** - browse the results of your **most recent search**
  (each search replaces the database), filter by state, target type and
  confidence, and download as CSV or Excel (.xlsx) - same option on the
  purpose-built email-ready/SMS-ready exports too. (No service filter here
  on purpose - you pick the service when *searching*.)
- **Email Template tab** - shows the template for whatever service is
  currently selected in the sidebar; switching the service dropdown
  auto-switches the template shown here too. Edit it, click **Save
  template** to persist that service's version to disk (it reloads
  automatically next time you pick this service), or **Reset to default**
  to go back. Preview against any saved lead and export rendered versions.
- **Send Emails tab** - see [Sending emails](#sending-emails) below.

## Email templates - Input, Process, Output

**Bookkeeping** is the only active service, and it ships with a
hand-written template: a catchy subject line and a body structured around
three beats:

- **Input** - the specific pain the prospect is likely dealing with right now
- **Process** - what you actually do about it
- **Output** - the concrete result they walk away with

On top of the service-level template, some **target verticals** have their
own hand-written pitch (currently Event Management and Floral Businesses) -
a lead found under that vertical gets the vertical template instead of the
generic Bookkeeping one, because a pitch that names vendor payments and
client deposits converts better than a generic one. Vertical templates are
built-in and not yet editable from the Email Template tab.

Typing a custom service (via "Custom..." in the sidebar) falls back to a
generic Input/Process/Output template built from that service name - still
structured the same way, just without the hand-written specifics. The other
seven services (Accounting, Tax Prep, Payroll, CFO Advisory, Business
Consulting, HR Outsourcing, Dental Billing) have search targeting and OSM
tags built but are **not exposed in the UI** and have no hand-written
template of their own yet - deliberately, after an incident where a stale
non-Bookkeeping lead picked up the wrong service's pitch. Add a service to
`ACTIVE_SERVICES` and give it a template in the same change, never before.

Each service's template is edited and saved independently (to
`data/custom_email_templates.json`), so editing one never touches another.

**Dental Billing is designed to work differently.** Every other service
searches for *other companies offering that service* (e.g. "Bookkeeping
companies in Texas") - useful if you're pitching a partnership/outsourcing
angle. Dental Billing instead searches for **dental practices** (the actual
prospects who'd need billing help). The search queries, relevance
filtering, and email pitch are all built around that distinction.

## Confidence scoring - how to trust the data

Every lead gets a **Confidence** badge in the Lead Database tab. "High" is a
hard gate, not just a weighted score - a lead only reaches High if *all* of
these pass, regardless of how well it scores elsewhere:

- Email, if present, must either match the company's own domain, **or have
  come from a `mailto:` link pointing at a well-known free email provider**
  (Gmail, Yahoo, Outlook, iCloud, ...) - very common for small local
  businesses (event planners, florists, single-location practices) to just
  use a personal inbox, and there's nothing for that address to plausibly
  "match" in the first place. A `mailto:` to some OTHER custom domain
  doesn't get this pass - a real audit found one pointing at a differently
  -worded domain than the business's own site, most likely a stale
  rebrand address, and requiring domain-match there is what correctly
  caps it at Medium instead of High. A regex-scraped email that doesn't
  match the site's domain is excluded outright - that's the "some other
  address that happened to be on the page" case this check exists for
- Phone, if present, must be a structurally valid number (not `(178)` or
  `(113)` - North American area codes can't start with 0 or 1) and not
  confirmed to belong to a different state or a non-US region (Canada,
  Caribbean)
- The company name mustn't look like a listicle title ("Best Bookkeeping
  Companies in Houston...")
- The domain mustn't be a known directory, SaaS platform, or offshore/
  national outsourcing provider (see the blocklist below)

Earlier versions of this scoring used a pure weighted ratio, which let leads
that failed one of these basic checks - a mismatched email domain, an
invalid phone - still reach "High" if enough *other* signals looked good.
That's now fixed: any one of these failing caps the lead at Medium (or Low,
for a bad domain/listicle name), no matter what else checks out.

| Label | What it means |
|---|---|
| 🟢 **High** | Passes every hard gate above, and the overall sourcing quality (structured name, `mailto:`/`tel:`/OSM-sourced contact info) is strong. |
| 🟡 **Medium** | Decent sourcing, but fails one hard gate (e.g. email domain mismatch) or relies on a regex fallback for a field. |
| 🔴 **Low** | A listicle-style name, a known bad domain, or contact info sourced entirely from unverified regex matches. |
| ⚪ **Unrated** | Only appears on leads imported straight into `data/leads.csv` from outside the app (a lead collected through a search always gets scored). |

Confidence is scored with the **current** rules every time a search runs,
so there's no separate "re-check" step to remember - update the app, run a
search, and new leads reflect the latest blocklist/scoring. By default
(**"Automatically discard non-High-confidence leads"** in Advanced
settings, on) only High-confidence leads are even saved, so there's no
backlog of stale Medium/Low rows sitting in the database waiting to be
re-judged. The `storage.rescan_confidence()` helper still exists for
re-scoring an existing `leads.csv` in place (e.g. after importing an
external CSV), but it's not surfaced as a dashboard button.

A phone number whose area code belongs to a *different* state than the one
you searched - or to Canada/the Caribbean - blocks the High label but
doesn't discard the lead entirely; it's still visible at Medium/Low, since
businesses do legitimately keep old area codes after moving or use VOIP
numbers.

Use the **Filter by confidence** dropdown in the Lead Database tab to narrow
down to High (and optionally Medium) before exporting for outreach. None of
this is a legal or factual guarantee - it's a heuristic based on *how* the
data was sourced - so spot-check a sample before a large send either way.

## Result quality filtering

The sidebar's **"Filter out directories, listicles & platforms"** toggle
(on by default) skips known non-company results before they're ever scraped.
It catches things like:

- Directories/marketplaces/data brokers: Bark, Clutch, GoodFirms, DesignRush,
  Upwork, Yelp, RocketReach, TaxBuzz, Sulekha, UncleKam (a templated
  franchise-directory site that generates a page per city)...
- **Offshore/national outsourced accounting providers** - real companies,
  but ones that SEO-optimize pages like "bookkeeping companies in Texas"
  while actually being a single India/UK-based BPO serving clients
  nationwide (IBN Technologies, Outbooks, Invedus, Aviaan Accounting,
  Clarigro, and others) - the opposite of the local business this tool is
  trying to find.
- Generic platforms and SaaS tools: Wikipedia, YouTube, LinkedIn, Pinterest,
  Facebook, Bench, Bookkeeper360, Birdeye, Clio...
- Dictionary/reference sites that rank for generic query words like "best"
  or "small" (Merriam-Webster, Dictionary.com, Wiktionary...) - these showed
  up specifically because query phrasing includes words like "small business"
  and "best" for coverage, and some search backends will happily return a
  dictionary definition of "best" alongside real results.
- "Top 10" / "Best of" listicle article titles - including plain ones like
  "Best Virtual Bookkeeping Companies in Houston" that don't include a
  number or the word "the," which earlier versions of this check missed -
  and bot-verification pages
- Ad-click redirect URLs (e.g. `bing.com/aclick?...`)
- **Foreign-TLD domains** (`.ca`, `.uk`, `.com.au`, `.ae`, etc.) - this app
  searches for US-state leads, so an offshore domain almost always means the
  query matched an unrelated foreign business rather than a real local one.
- **Relevance mismatches** - results whose title/snippet never actually
  mentions the service (or a close synonym) at all, even after passing every
  other check. This is what catches things like a PDF-editing tool matching
  on the word "small" in "small business bookkeeping services."

You can turn it off if you specifically want the raw, unfiltered search
output - but the confidence scoring still applies either way, so low-quality
results just show up labeled Low instead of being skipped outright.

### The final junk pass (always on)

The pre-scrape filter above works on a search *result* (a URL, a title, a
snippet). A second filter runs **after** scraping and scoring, on the
finished lead - now that we know the site's real name, its domain, and
which business type it was found under. It's always on and catches what
slips past the first pass:

- **Directory / marketplace / media domains** not on the pre-scrape list -
  event & venue marketplaces (Zola, The Knot, WeddingWire, Tagvenue,
  Peerspace, PartySlate), wedding media (Green Wedding Shoes, Real Weddings
  Magazine, Equally Wed), local-news and tourism sites.
- **National flower-order gateways** - 1-800-FLOWERS, FTD, Teleflora,
  FloristOne, FromYouFlowers, BloomNation and similar. These rank for
  "florist in \<city\>" everywhere but are an order-routing service, not a
  local shop (the floral version of the offshore-BPO problem).
- **Wrong trade** - photographers/videographers, photo-booth rental,
  porta-potty/dumpster rental, SEO/citation-checker tools, law firms,
  ticket sellers, destination-wedding businesses, tourism boards, general
  hospitals (a real dental hospital/clinic is still kept, only one with no
  dental branding at all gets dropped), and veterinary/animal care
  (veterinary dentistry is a real specialty, so a vet school can otherwise
  slip past a Dental Practices relevance check on that word alone) -
  showing up under an Event Management, Floral, or Dental Practices search.
- **Professional associations, academies, and university departments** -
  "Academy of General Dentistry" or "College of Veterinary Medicine" are
  membership organizations or academic departments, not a practice you'd
  cold-email, even though they pass a relevance check on "dentistry" easily.
- **Local newspapers** - masthead-style names (Star-News, Tribune, Gazette,
  Chronicle, ...) rather than generic words like "news" or "press" (a real
  florist can legitimately be named "Flower Press").
- **Foreign businesses on a plain `.com`/`.org` domain** - a hospital in
  Qatar and a dental clinic in the UK both turned up under an Alabama
  search with no foreign-TLD signal to catch them. There's no general
  geography check for this (a known, documented gap, not silently missed)
  - each one gets hardcoded into the blocklist once spotted.
- **A junk site's domain, even when its display name was rebranded** -
  e.g. a portable-toilet company calling itself "USA Rental Experts" is
  still caught because its domain (`mrportapottyorlando.com`) gives it away.
- **A business still on a website-builder's free default subdomain**
  (Squarespace, Wix, Weebly, WordPress.com, ...) rather than its own
  domain - almost always a template/demo page or a stale duplicate of a
  business already captured under its real domain.
- **Templated "florist network" pages** - a shared, non-personal contact
  address (like `wecare@`) reused across many different city-named domains
  is a strong sign of a machine-generated multi-city network, not an
  independently owned local shop.
- **Broken extractions** - the company name came out as a nav label
  ("Home", "About", "Contact"), the bare domain, or a page of SEO keywords.

The run summary reports how many rows this removed and shows a sample so
you can see what was cut and add a false-positive back by hand if needed.
This list grows over time from real audits - if you spot another
recurring pattern of junk, that's a signal to add it here rather than
filtering it out by hand every run.

## Sources considered but not built

State CPA Society directories, BBB.org, YellowPages, and State Secretary of
State registries would all add real value, but weren't built into this tool:

- **State CPA Society sites** each have a different structure per state -
  reliably scraping 50 different sites is a much bigger undertaking than
  fits here, and a half-working version across some-but-not-all states would
  be more confusing than useful.
- **BBB.org and YellowPages** both have active anti-bot protection. A
  scraper for these would need to be built and tuned against their live,
  defended sites to have any confidence it actually works - not something
  that can be done reliably without testing against them directly.
- **State SoS registries** are useful for verification (confirming a
  business is legally registered and active) but rarely have phone/email at
  all, so they're a cross-check step rather than a lead source, and again
  differ by state.

OpenStreetMap was the one source from this list that could be built with
real confidence: it's a single, documented, stable API with no anti-bot
concerns. If you want the others, the cleanest path is usually exporting a
CSV from each site manually for your target state/city and importing it
alongside what this tool finds, rather than a fragile scraper.

## Data sources - all free, no keys required

Everything in this app is free and needs zero API keys to find leads:

| Source | What it gives you | Cost/setup |
|---|---|---|
| **OpenStreetMap (Overpass API)** | Name, phone, website, city for community-mapped businesses | Free, no key, no signup |
| **DuckDuckGo** (`ddgs` package) | General web search results | Free, no key, no signup |

Brave Search, Google Programmable Search, SerpAPI, and (evaluated but
rejected - see below) **Yelp's API** were left out - they need paid or
metered access, and OSM + DuckDuckGo together cover the same ground for
free. Yelp specifically: it's no longer a free API (only a one-time 30-day/
5,000-call trial, then $229+/month with a *minimum* 1,000 calls billed per
day regardless of use), searches by city/address rather than a whole state
anyway, generally doesn't return a business's own website, and its terms
describe it as built for consumer-facing display of Yelp data rather than
compiling an outreach list - not a fit here.

Each OpenStreetMap query is now issued **once per tag** rather than one
combined query for a vertical's whole tag list - a big state (CA, TX) can
return enough elements to time out or get truncated in one mega-query;
splitting means one tag's data loss doesn't take the others down with it.
It also now runs **in the background while the web search step runs**,
since the two hit unrelated services with no dependency between them -
free wall-clock time saved on every run, no change in what's found.

**OpenStreetMap** is queried first and is the more trustworthy of the two -
it's real, community-confirmed place data (someone mapped that business at
that address), not a search ranking. Coverage varies by area though: dense
urban areas tend to be well-mapped, rural/small-town businesses less
consistently so. **DuckDuckGo** fills in whatever OSM doesn't have, subject
to the filtering described above. Toggle either off in the sidebar if you
want to isolate one source, though running both together is the default and
generally gives the widest, most accurate coverage.

## Search depth & the "uncapped" option

- **"Fetch everything the search finds (uncapped)"** - **on by default.**
  Pulls every unique company the backend returns across all query variants,
  with no per-state/per-vertical cap. This is the "get all of them" mode.
  It takes proportionally longer since every site still gets fetched
  individually to pull contact info - for a big state this can run for
  several minutes, which is expected, not a hang. **Uncheck it** to cap the
  run with the max-companies slider below. It no longer affects the
  results-per-query slider below (see next bullet).
- **Search results per query** - **default 40**, applies whether or not
  you're uncapped. Used to be forced to 200 whenever "uncapped" was on,
  which likely made the search backend paginate the results page several
  times for a single query. Results ranked past the first ~40 for a
  hyper-local query are overwhelmingly directories/noise anyway, so this
  cuts a lot of wasted requests without losing much real signal. Raise it
  if you suspect genuine results are being missed.
- **Max companies to scrape per state / vertical** - only used when
  "uncapped" is unchecked; raise it to widen a capped run.
- **Concurrent site scrapes** - how many company sites are fetched in
  parallel during the scrape step. **Default 25** (range 4–50); raise it to
  speed large runs up, lower it if you're getting a lot of scrape timeouts.
- **"Stop early after N queries with no new companies"** - **default 15.**
  Per state + business type: once this many search queries *in a row* find
  zero new unique companies, the app stops issuing further query variants
  for it instead of grinding through the rest of the city list for no
  gain. Adaptive, not a fixed cutoff - a state/vertical that keeps finding
  new companies keeps getting searched for as long as it keeps paying off;
  this only ever cuts in once coverage is genuinely exhausted.
- **"Use extra search phrasings per vertical"** - **on by default.** Each
  vertical searches several synonym phrasings, not just one (see
  "Bookkeeping's target verticals" above). These extra phrasings only run
  as whole-state queries by default - see the next control for the setting
  that actually controls total query count.
- **"Phrasings expanded per city"** - **default 1.** How many of a
  vertical's phrasings get repeated for every single city. Only the first
  run of this feature multiplied *every* phrasing by *every* city - on
  California that meant ~4,400 queries and a multi-hour run for two
  verticals. Left at 1, city-level search still runs (just with the
  primary phrasing), and every phrasing still gets its full whole-state
  coverage - raise this only if you specifically want every phrasing
  repeated per city, and expect the multiplier that comes with it.
- **"Also search well-known metro neighborhoods"** - off by default. Adds
  named-neighborhood queries (Wynwood, Williamsburg, Koreatown, ...) on top
  of the city list, currently only for California, Texas, Florida, and New
  York.
- **"Auto-retry no-contact leads with a real browser"** - on by default
  when Playwright is installed. Right after scraping, any lead with a real
  website but no email/phone found gets one more try with an actual
  headless browser (catches JS-rendered contact info), **before** the
  High-only filter runs - so a recovered contact can still make a lead
  High instead of it being discarded first. Capped by **"Max browser
  retries per run"** (default 150) since each retry takes real seconds;
  the manual "Retry with a real browser" button in the Lead Database tab
  covers whatever the cap left out. This step now launches **one browser
  for the whole batch** instead of one per site (a full browser launch was
  the dominant cost, not the page load) and waits for the page to be
  parsed plus a short 1.5s settle rather than for it to go fully idle -
  both changes only affect speed, not what gets extracted.

Every filter in the Advanced expander has a toggle - nothing here filters
or slows a run down silently. Before any query is sent, the app also shows
an **estimated query count** (and a floor time from it) whenever a run
would issue more than ~300 queries, so a combinatorial blowup from
stacking these settings is visible upfront instead of discovered after
hours.

### When searches keep timing out

The free search engines (`ddgs` scrapes Google/Bing/Brave/Yahoo/Yandex) go
through phases of timing out or rate-limiting a given IP. When that
happens you'll get **one summary warning** at the end of a run
("N search queries returned nothing…") instead of a wall of red boxes, and
the run falls back on whatever OpenStreetMap and the successful queries
returned.

**Don't pin `SEARCH_ENGINE_BACKEND` to a single engine** to "fix" this -
that was tried and made things worse: one run's errors turned out to be
bing.com's own DNS lookup failing on that network, and with only one
engine configured there was nothing left to fall back to, so *every* query
died. `ddgs`'s multi-engine list already falls through to the next engine
within a single query when one fails - that's the actual safety net, and a
single-engine pin throws it away. Keep `SEARCH_ENGINE_BACKEND` blank
(the multi-engine default) unless you've separately confirmed one engine
is reliably reachable from your network. Things that do help:

- **Re-run later** - these blocks are usually temporary.
- Raise **`SEARCH_TIMEOUT`** (default 25s, currently `40` in this
  project's `.env`) and **`SEARCH_QUERY_DELAY`** (default 2.5s).
- The app already retries a fully-failed query twice with a backoff
  (`SEARCH_RETRIES`, default 2) before giving up on it.

## Notes on reliability

- Not every company site publishes an email or phone number in a
  scrapable place (some hide them behind a contact form or JS-rendered
  page). Expect a `no_contact_found` status on a meaningful fraction of
  results - that's the extraction working correctly, not a bug.
- Scraping is polite by default (small delays, limited concurrency) but
  you're still fetching other people's sites - keep volumes reasonable
  and check a site's `robots.txt`/terms if you plan to scale this up.

## Sending emails

The **Send Emails** tab sends real emails through your own SMTP account
(Gmail, Outlook, or any provider) - there's no third-party sending service
or paid API involved, and nothing sends until you explicitly configure it.

**Setup:** copy `.env.example` to `.env` and fill in the `SMTP_*`, `FROM_*`,
and `SENDER_*` values. For Gmail, use an
[App Password](https://myaccount.google.com/apppasswords) - Google blocks
plain-password SMTP logins. Restart the app after editing `.env`.

**Scraping and sending don't have to happen in the same sitting.** Every
completed search run auto-saves an Excel snapshot to `data/exports/`, named
`date_businesstypes_states.xlsx` (e.g.
`2026-09-15_EventManagement+FloralBusinesses_Alabama+Alaska.xlsx` - all
50 states + DC collapses to `...AllStates.xlsx` rather than listing every
one). Open the **"Send from a saved run instead of the current database"**
expander at the top of Send Emails (or Send Texts) to pick one or more of
these - their leads are combined and used for that send instead of
whatever's currently in the Lead Database tab, so you can run several
searches across different days/states/business types and then send from
any of them (or several combined) later, without re-scraping anything.

**How it decides who's eligible to send to:**
- Has an email address (obviously)
- **High confidence only** and **matching the currently selected service** -
  two fixed safety filters applied on top of whatever the Lead Database
  tab's filter is set to, not adjustable from the Send tab
- Not a generic/shared-inbox address - `support@`, `info@`, `admin@`,
  `noreply@`, `billing@`, `hi@`, `contact@`, `wecare@`, etc. are screened
  out (they route to ticketing systems and auto-responders; this is what
  generated auto-reply support tickets in the past). A personal-looking
  address like `erica@` or `jsmith@` is **not** screened out - only the
  known shared-inbox prefixes in `blocklist.GENERIC_EMAIL_PREFIXES` are.
  There is no allowlist of "approved" prefixes; anything that isn't a
  known bad one is fair game. This list is exact-match, not pattern-based
  - a variant that isn't spelled out (e.g. a future `weluvu@`) can still
  slip through until it's added.
- Not already emailed - a persistent send log (`data/sent_log.csv`) tracks
  every successful send by domain, so re-running a search and hitting
  **Send** again won't double-email anyone, unless you uncheck "Skip leads
  already emailed"
- Not on the suppression list (`data/suppression_list.csv`) - manage this
  from the expander at the bottom of the tab; add anyone who unsubscribes
  or asks not to be contacted

**Each lead gets its own template automatically.** If your database has a
mix of services (some Bookkeeping leads, some Dental Billing leads), each
one is rendered with *that lead's own* service's saved template - not
whatever template happens to be open in the Email Template tab. This is
what makes the output genuinely tailored per company rather than one
generic blast.

**Before sending to real companies:**
1. Use **Send a test email** first - this verifies your SMTP credentials
   work and lets you see exactly what a recipient will get, without
   contacting anyone. If your current leads span more than one target
   type (e.g. both Event Management and Floral Businesses), you'll get a
   separate row - and a separate template preview - for each one, since
   each target type has its own pitch.
2. Check the delay between sends. A few seconds per email is normal
   outbound practice and reduces the chance of your account getting
   rate-limited or flagged as spam - don't set this to zero for a large batch.
3. Read the confirmation checkbox before checking it. It's there on purpose.

**Compliance - this matters, read it:**
- **CAN-SPAM Act (US)** requires accurate sender info, a non-deceptive
  subject line, your real physical mailing address in the email, and a
  working opt-out that you actually honor. `SENDER_ADDRESS` in `.env` fills
  the address into every template's footer - don't leave it blank before a
  real send.
- If any recipients are in the EU/UK, **GDPR/PECR** rules on unsolicited
  B2B email are stricter and vary by country - worth checking before a
  wide send.
- The suppression list is manual - if someone replies "unsubscribe," you
  need to add them yourself (paste their email into the tab's suppression
  box). This app doesn't parse inbound replies.

None of the above is legal advice - if outbound volume grows, it's worth a
quick pass by someone who knows email marketing compliance in your
jurisdiction.

## Sending texts

The **Send Texts** tab sends real SMS through your own **8x8 Connect**
account - same "off until you configure it" pattern as email, and defaults
to texting only leads that **don't have an email** (uncheck "Only leads
with no email" to widen that).

**Setup:** copy `.env.example` to `.env` and fill in `EIGHTXEIGHT_API_KEY`
and `EIGHTXEIGHT_SUBACCOUNT_ID` from the
[8x8 Developer Portal](https://developer.8x8.com/connect) (under your
existing 8x8 account - an API key and a subaccount ID, both shown there,
not something to invent). `EIGHTXEIGHT_SOURCE` is optional - an
alphanumeric/numeric Sender ID shown as the "from" on the recipient's
phone; leave it blank to use the account's default sender. Restart the app
after editing. Both the API key *and* the subaccount ID must be present -
if either is missing the tab stays in the "not configured" state.

**Two things that will trip you up if you skip them:**

1. **Sender ID / number provisioning** is done in the 8x8 account, not
   here. Whatever number or alphanumeric sender the subaccount is set up
   with is what recipients see; this app only supplies the destination and
   message text.
2. **Real US business texting needs A2P 10DLC registration** - you register
   your business and message type with the carriers through 8x8. This is a
   multi-day compliance process, not a config value, and unregistered
   traffic gets aggressively filtered by carriers once volume looks like
   business messaging. International numbers (outside the US) don't need
   this - it's specifically a US carrier requirement - but cost more per
   message.

**SMS templates are intentionally short.** Carriers charge/segment by
length and a text is read at a glance, not skimmed like an email - the
default Bookkeeping SMS template is well under 200 characters. Each lead
still gets rendered with *its own* service's SMS template, same as email.

**Before sending to real numbers:** use **Send test text to this number**
first (enter your own number directly in the tab - there's no env var for a
personal test destination). Same delay/confirmation pattern as email
applies here too.

**Phone numbers are converted to E.164 format** (`+15125550101`) before
sending, since that's what 8x8's API requires - this happens
automatically for both US numbers (a bare 10-digit number gets `+1`
prepended) and already-international numbers (a `+`-prefixed number is
passed through as-is).

## Project structure

```
lead_finder_project/
├── app.py                      # Streamlit dashboard (entry point)
├── lead_finder/
│   ├── search.py                # DuckDuckGo search, query building (state + city level)
│   ├── osm_source.py             # OpenStreetMap Overpass API integration
│   ├── blocklist.py              # directory/platform/listicle/TLD/relevance filtering (pre-scrape)
│   ├── scrape.py                # site fetching + name normalization + contact extraction pipeline
│   ├── extract.py                # mailto/tel/JSON-LD/owner/LinkedIn extraction + confidence scoring
│   ├── junk_filter.py            # final post-scrape/post-score junk pass (off-target biz, directories, broken names)
│   ├── area_codes.py             # area-code-to-state data for phone plausibility checks
│   ├── storage.py                # CSV-backed lead database (one run at a time), dedupe, normalization
│   ├── email_template.py         # per-service email + SMS template rendering + save/load
│   ├── mailer.py                 # SMTP sending, sent-log, shared suppression list
│   ├── texter.py                 # 8x8 Connect SMS sending, sent-log, E.164 formatting
│   ├── hubspot_crm.py            # push leads to HubSpot as Contacts
│   ├── browser_scrape.py         # Playwright fallback re-scrape for JS-rendered sites
│   └── states.py                 # US states + service presets + target verticals + major cities
├── data/
│   ├── leads.csv                    # your most recent search's results - replaced each run (gitignored)
│   ├── custom_email_templates.json  # your saved per-service email templates, if any (gitignored)
│   ├── custom_sms_templates.json    # your saved per-service SMS templates, if any (gitignored)
│   ├── sent_log.csv                 # history of every email send attempt (gitignored)
│   ├── sent_text_log.csv            # history of every text send attempt (gitignored)
│   └── suppression_list.csv         # emails/phones/domains to never contact (gitignored)
├── .streamlit/
│   └── config.toml              # pinned dark theme (fixes it varying by OS/browser)
├── .vscode/
│   └── settings.json            # enables auto-activation in VS Code terminals
├── environment.yml              # conda environment definition
├── requirements.txt             # pip fallback, if you'd rather not use conda
├── .env.example
└── README.md
```
