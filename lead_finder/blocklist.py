"""
Filters applied to *search results* before we ever scrape them — this is
where most of the junk in a naive pipeline comes from: business directories,
marketplaces, "Top 10" listicle articles, generic platforms, and — as it
turns out — dictionary sites that rank for generic query words like "small"
or "best" that show up in query phrasing but aren't themselves a company.
"""

from __future__ import annotations

import re
from typing import List, Optional

# Directories, marketplaces, review aggregators, and lead-gen/data-broker
# sites that list *other* companies rather than being one themselves.
DIRECTORY_DOMAINS = {
    "bark.com", "designrush.com", "clutch.co", "goodfirms.co", "themanifest.com",
    "techbehemoths.com", "superbcompanies.com", "softwareworld.co", "g2.com",
    "capterra.com", "trustpilot.com", "bbb.org", "aeroleads.com",
    "topconsumerreviews.com", "clearlyrated.com", "yelp.com", "angi.com",
    "thumbtack.com", "homeadvisor.com", "glassdoor.com", "indeed.com",
    "ziprecruiter.com", "ambitionbox.com", "manta.com", "yellowpages.com",
    "expertise.com", "upcity.com", "sortlist.com", "rocketreach.co",
    "taxbuzz.com", "zoominfo.com", "crunchbase.com", "lusha.com",
    "talkinternational.com",
}

# Non-US businesses that showed up under a US-state search despite using a
# .com/.org domain (so the foreign-TLD check in is_foreign_tld doesn't catch
# them) — confirmed foreign via direct lookup, not vertical-specific. Grows
# across whichever vertical/state audit happens to surface one next; the
# underlying gap (no real geography check beyond TLD + phone area code) is
# tracked as a known limitation rather than solved generally — see CLAUDE.md.
FOREIGN_BUSINESS_DOMAINS = {
    "alfaridhospital.com",  # hospital in Qatar
    "confidentalclinic.com",  # dental clinic in Purley, UK
    # found in an Alabama/Alaska Event Management + Floral audit (2026-09-15)
    "azonova.com",  # product engineering studio in New Delhi, India — also
                     # not an event business at all, a doubly-wrong match
    "greenplantflowers.com",  # florist in Sharjah, UAE
    "flowerdeliverydanang.com",  # florist in Da Nang, Vietnam
}

# Freelance/job marketplaces — results are gig listings, not companies.
FREELANCE_DOMAINS = {"upwork.com", "fiverr.com", "freelancer.com", "guru.com", "outsource-work.com"}

# General-purpose platforms that surface for almost any query but are never
# themselves the business you're looking for.
PLATFORM_DOMAINS = {
    "wikipedia.org", "youtube.com", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "linkedin.com", "pinterest.com", "reddit.com",
    "quora.com", "medium.com", "tiktok.com", "google.com", "bing.com",
    "yahoo.com", "duckduckgo.com", "amazon.com", "apple.com", "bestbuy.com",
    "zhihu.com", "heylink.me",
    "archive.org",  # non-profit digital library, not a business of any kind
                    # (found under Event Management/Floral, 2026-09-24 audit)
}

# Local/regional news outlets, magazines, and media/blog sites — general,
# not vertical-specific (a news outlet can rank for "<city> event companies"
# or "<city> florists" just as easily as any other query). Found in a
# 2026-09-24 Event Management + Floral audit across many states; kept
# separate from EVENT_AND_FLORAL_DIRECTORY_DOMAINS since these aren't
# wedding/floral-specific and could turn up under any vertical.
NEWS_AND_MEDIA_DOMAINS = {
    "fox13now.com", "noozhawk.com", "strictlybusinessomaha.com",
    "rimonthly.com", "buckrail.com", "nspiretoday.com", "siouxfalls.business",
    "breckenridgetexan.com",
}

# City/county/state government sites, chambers of commerce, and tourism /
# convention-and-visitors-bureau (CVB) sites. General, not vertical-specific
# — a chamber or a .gov page ranks for "<city> <any service> companies" just
# as readily as a real business would. Found in a 2026-09-24 Event
# Management + Floral audit. See also is_institutional_tld (.gov/.edu) and
# is_chamber_domain / is_tourism_domain below, which generalize this beyond
# a fixed list for future finds.
GOVERNMENT_CHAMBER_TOURISM_DOMAINS = {
    "discoverdunwoody.com", "greenwichchamber.com", "greenvillechamber.com",
    "masontx.org", "tylertexas.com", "visitbeulah.com",
}

# Dictionary/reference sites — these rank for generic query words like
# "small" or "best" that appear in query phrasing (e.g. "best bookkeeping
# companies", "small business bookkeeping services") even though they have
# nothing to do with the actual service being searched for.
REFERENCE_DOMAINS = {
    "merriam-webster.com", "dictionary.com", "thefreedictionary.com",
    "cambridge.org", "wordreference.com", "wiktionary.org", "yourdictionary.com",
    "lovetoknow.com", "vocabulary.com", "urbandictionary.com", "britannica.com",
    "lingojam.com", "collinsdictionary.com", "thesaurus.com",
}

# Accounting/bookkeeping *software* vendors and unrelated tool vendors that
# collide on generic keywords — relevant-looking, but not a services company.
SOFTWARE_VENDOR_DOMAINS = {
    "xero.com", "quickbooks.intuit.com", "intuit.com", "freshbooks.com",
    "wave.apps.com", "waveapps.com", "zoho.com", "sage.com", "netsuite.com",
    "gemini.com", "stripe.com", "paypal.com", "smallpdf.com", "smallpdf.us",
}

# News/PR wire services — the "company" in the result is often just the PR
# distributor, not the business the article is about.
NEWSWIRE_DOMAINS = {
    "einnews.com", "prnewswire.com", "businesswire.com", "globenewswire.com",
    "fintechworldpost.com",
}

# Academic/journal publishers and exam-prep sites — occasionally collide on
# service keywords but are never a company you'd cold-email as a lead.
PUBLISHER_DOMAINS = {"wiley.com", "onlinelibrary.wiley.com", "practicetestgeeks.com"}

# Offshore/national outsourced accounting BPO providers. These are real
# companies, but they SEO-optimize pages like "bookkeeping companies in
# Texas" or "bookkeeping services in [every state]" while actually being a
# single national or offshore (usually India-based) provider — the opposite
# of the "authentic local company" this tool is trying to find. Confirmed
# via direct research, not guessed, since getting a domain wrong here would
# wrongly block an unrelated real business.
OFFSHORE_BPO_DOMAINS = {
    "ibntech.com", "outbooks.com", "outbooks.co.uk", "invedus.com",
    "aviaanaccounting.com", "clarigro.com", "datamatics.com", "qxaccounting.com",
    "cogneesol.com", "analytix.com", "safebooks.ai",
}

# National directory/franchise-style sites that generate templated per-city
# pages ("Bookkeepers in [any city]") rather than being one local firm, plus
# SaaS/platform tools that showed up matching bookkeeping-adjacent keywords.
SAAS_AND_TEMPLATED_DIRECTORY_DOMAINS = {
    "unclekam.com", "sulekha.com", "bench.co", "bookkeeper360.com",
    "birdeye.com", "clio.com",
}

# Event / wedding / venue marketplaces, wedding media, and national
# flower-order gateways. Same shape as the offshore-BPO problem: they rank
# for "florists in <city>" / "wedding venues in <state>" but are a
# marketplace or a national order-router, not a local business. Found in
# real result data — grow this list as more turn up.
EVENT_AND_FLORAL_DIRECTORY_DOMAINS = {
    # wedding / event / venue marketplaces + media
    "zola.com", "theknot.com", "weddingwire.com", "tagvenue.com",
    "fresha.com", "receptionhalls.com", "recommendedcompany.com",
    "cityscoop.us", "eventective.com", "peerspace.com", "giggster.com",
    "splacer.co", "partyslate.com", "wedding-spot.com", "venuereport.com",
    "realweddingsmag.com", "realweddingsmagazine.com", "equallywed.com",
    "mywed.com", "theacme.com", "greenweddingshoes.com", "wedding.report",
    "gethoneybun.com",
    # local-news / municipal / tourism sites that rank on "<city> CA" + a keyword
    "patch.com", "ocregister.com", "visitcamarillo.com", "bishopvisitor.com",
    # national flower-order gateways (not a local florist)
    "1800flowers.com", "1800flowersalhambra.com", "ftd.com", "teleflora.com",
    "floristone.com", "fromyouflowers.com", "flowershopnetwork.com",
    "bloomnation.com", "proflowers.com", "avasflowers.com", "avasflowers.net",
    "sendflowers.com", "flowerdelivery.com", "urbanstems.com",
    "ode-a-la-rose.com", "kremp.com", "flowersin4hours.com",
    # national wedding-vendor booking services (serve "all 50 states" /
    # destination weddings, not a business local to the state searched)
    "indian-wedding-dj.com", "djmehul.com", "poshweddingplanners.com",
    # unrelated national retail chains seen in results
    "dollartree.com", "nothingbundtcakes.com", "walmart.com", "target.com",
    "costco.com", "instacart.com",
    # SEO/citation tools, directories, and misc non-target domains found in
    # a manual audit of real results (2026-09-14) — an SEO tool, two
    # directory sites posing as a specific business, a portable-toilet
    # company whose display name ("USA Rental Experts") hid the domain's
    # own giveaway, an Australian/Indian destination-wedding site, an
    # Italian tourism board, and two local-news/blog sites
    "freelocalcitationchecker.com", "localareafinds.com",
    "local-service-close-by.com", "mrportapottyorlando.com",
    "thegrandindianwedding.com", "turismotorino.org", "benitolink.com",
    "inmenlo.com", "bucketlistbums.com", "sunnylands.org",
    # found in an Alabama/Alaska Event Management + Floral audit
    # (2026-09-15): a tourism-board site whose "Host an Event" subpage got
    # scraped and named "Sitka Weddings and Events" (misleading — the site
    # is actually a Sitka, AK visitor guide, not a wedding business), a
    # travel/food blog, and a gambling/casino content blog with no
    # relation to flowers at all.
    "travelsitka.com", "travelcookeat.com", "techntrip.com",
    # found in a nationwide (all-states) Event Management + Floral audit
    # (2026-09-24): national wedding-planner/media pages repeatedly
    # scraped as if local across many different states.
    "weddingelegance.nyc", "orchidblossomevents.com", "junebugweddings.com",
    "stylemepretty.com", "weddingforward.com", "postalpetals.com",
    "socialtables.com", "herecomestheguide.com", "yellowpagesdirectory.com",
    # national flower-delivery aggregators — same shape as the existing
    # national flower-order gateways above, just a later batch found in
    # the same audit.
    "terrysflorist.com", "flowersbyeva.com", "odealarose.com",
    "bloomypresents.com", "cityflowershop.com", "flowersezgo.com",
    "theflowershop.com", "quickflowerdelivery.com",
    # confirmed real domains behind two of the _KNOWN_JUNK_COMPANY_NAMES
    # entries in junk_filter.py (found by retroactively re-running the new
    # rules against real data, not guessed) — a POI/business-data API
    # provider and a Yellow Pages reseller.
    "poidata.io", "ypages.us",
}

# A business still on a website-builder's free default subdomain (never
# migrated to its own domain) is almost never the deployed site of an
# established local business — overwhelmingly a demo, in-progress, or
# abandoned page. Distinct from PLATFORM_DOMAINS (which are platforms that
# ARE the destination, e.g. Wikipedia) — these are meant to be a stepping
# stone off of.
WEBSITE_BUILDER_DEFAULT_DOMAINS = {
    "squarespace.com", "wixsite.com", "weebly.com", "godaddysites.com",
    "business.site", "wordpress.com", "sites.google.com", "webs.com",
    "yolasite.com", "jimdofree.com", "strikingly.com", "carrd.co",
}

ALL_BLOCKED_DOMAINS = (
    DIRECTORY_DOMAINS | FREELANCE_DOMAINS | PLATFORM_DOMAINS | REFERENCE_DOMAINS
    | SOFTWARE_VENDOR_DOMAINS | NEWSWIRE_DOMAINS | PUBLISHER_DOMAINS
    | OFFSHORE_BPO_DOMAINS | SAAS_AND_TEMPLATED_DIRECTORY_DOMAINS
    | EVENT_AND_FLORAL_DIRECTORY_DOMAINS | WEBSITE_BUILDER_DEFAULT_DOMAINS
    | FOREIGN_BUSINESS_DOMAINS | NEWS_AND_MEDIA_DOMAINS
    | GOVERNMENT_CHAMBER_TOURISM_DOMAINS
)

# Title patterns that strongly indicate a "Top N" / "Best of" listicle
# rather than a single company's own page.
_LISTICLE_PATTERNS = [
    re.compile(r"^\s*(top|best)\s*\d+", re.I),
    re.compile(r"\btop\s*\d+\b", re.I),          # catches "...: Top 10 Picks" mid-title
    re.compile(r"\d+\s*\+?\s*(best|top)\b", re.I),
    re.compile(r"\bthe\s+best\b", re.I),
    re.compile(r"^\s*(top|best)\b", re.I),        # "Best Virtual Bookkeeping Companies in Houston" — no digit, no "the"
    re.compile(r"\bvs\.?\s", re.I),
    re.compile(r"\bcompar(e|ison)\b", re.I),
    re.compile(r"\bguide\s+to\b", re.I),
    re.compile(r"^\s*(how to|what is|what are|why)\b", re.I),
    re.compile(r"\breview(s)?\b", re.I),
    re.compile(r"\b(companies|firms|services|providers)\s+(in|near)\b.*\b(usa|us|united states)\b", re.I),
    re.compile(r"\b(leading|top-rated|best-rated)\s+(bookkeeping|accounting|tax|payroll|dental)\b", re.I),
]

# Bot-check / interstitial pages that occasionally get indexed and returned
# by search backends instead of the real page content.
_BOT_CHECK_PATTERNS = [
    re.compile(r"\bnot a bot\b", re.I),
    re.compile(r"\bverify you are human\b", re.I),
    re.compile(r"\bchecking your browser\b", re.I),
    re.compile(r"\battention required\b", re.I),
    re.compile(r"\bare you a robot\b", re.I),
]

# Words too generic to signal relevance on their own (appear in almost any
# services query — filtering on them alone caused the dictionary-site noise).
_RELEVANCE_STOPWORDS = {
    "services", "service", "company", "companies", "business", "businesses",
    "providers", "provider", "firms", "firm", "outsourcing", "near", "best",
    "top", "small", "local",
}

# Light synonym map so relevance checking isn't just an exact-string match.
# Deliberately a little generous with overlap between closely related fields
# (bookkeeping/accounting, tax/cpa) — being too strict here risks dropping
# genuinely good leads whose site emphasizes the adjacent term.
_SERVICE_SYNONYMS = {
    "bookkeeping": ["bookkeeping", "bookkeeper", "bookkeepers", "accounting", "accountant"],
    "accounting": ["accounting", "accountant", "accountants", "cpa", "bookkeeping"],
    "tax preparation": ["tax", "taxes", "irs", "cpa", "accounting"],
    "payroll services": ["payroll"],
    "cfo advisory": ["cfo", "finance", "financial", "fractional"],
    "business consulting": ["consulting", "consultant", "advisory", "advisor"],
    "hr outsourcing": ["hr", "human resources", "peo", "payroll"],
    "dental billing": ["dental", "dentist", "dentistry", "orthodont"],
}


# Generic role-based prefixes that route to shared inboxes, support/ticketing
# systems, or automated responders rather than a specific decision-maker.
# Sending to these is what generated auto-reply support tickets in practice
# (a real incident: an email to a company whose public address feeds an IT
# helpdesk ticketing system got auto-converted into a support ticket).
GENERIC_EMAIL_PREFIXES = {
    "support", "help", "feedback", "sales", "info", "hello", "hi", "hey",
    "contact", "enquiries", "inquiries", "customerservice", "custservice",
    "care", "wecare", "success", "services", "admin", "reception",
    "marketing", "media", "press", "pr", "hr", "jobs", "careers",
    "recruiting", "complaints", "returns", "billing", "notifications",
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer",
    "newsletter", "updates", "alerts", "webmaster", "security", "abuse",
    "legal", "privacy", "compliance", "compliances", "it", "tech",
    "technical", "developer", "asap", "urgent",
    # government / chamber / tourism shared-role addresses (added
    # 2026-09-24, same nationwide Event Management + Floral audit that
    # added the chamber/tourism domain checks above) — "citymanager@",
    # "chamberinfo@" etc. are role inboxes the same way "support@" is.
    "chamber", "chamberinfo", "citymanager", "cvb", "tourism",
}
# "hi" (Squarespace/Wix default), "contact" (near-universal generic-form
# address), "wecare"/"custservice" (templated support-inbox variants seen
# in real data, e.g. wecare@<florist-network-site>.com) were added
# 2026-09-14 after showing up unfiltered in real leads — this set is
# exact-prefix matching, not substring, so a variant has to be listed
# explicitly to be caught.


def _email_prefix(email: str) -> str:
    return (email or "").split("@")[0].strip().lower()


def is_targetable_email(email: str) -> bool:
    """
    True unless the email's local part (before @) is one of the
    generic/shared-inbox prefixes that routes to a support system, ticketing
    queue, or auto-responder (support@, info@, noreply@, ...). Any other
    prefix — including a personal name like "erica@" or "jsmith@" — is
    considered targetable. We only screen OUT the known-bad shared inboxes;
    we don't require membership in an allowlist of "approved" prefixes.
    """
    if not email or "@" not in email:
        return False
    prefix = _email_prefix(email)
    return prefix not in GENERIC_EMAIL_PREFIXES


def is_blocked_domain(domain: str) -> bool:
    return domain.lower() in ALL_BLOCKED_DOMAINS


def looks_like_listicle(title: str) -> bool:
    if not title:
        return False
    return any(p.search(title) for p in _LISTICLE_PATTERNS)


# Generic industry/service words that, stuffed together with nothing else,
# read as SEO keyword salad rather than an actual business name — e.g. "Law
# Firm Bookkeeping Quickbooks" as an og:site_name value (real, seen in
# practice: some sites keyword-stuff meta tags for search ranking rather
# than using their actual name there).
_GENERIC_NAME_KEYWORDS = {
    "law", "firm", "bookkeeping", "bookkeeper", "accounting", "accountant",
    "quickbooks", "tax", "taxes", "payroll", "dental", "dentist", "dentistry",
    "construction", "realty", "rental", "rentals", "manufacturing", "software",
    "services", "solutions", "consulting", "company", "companies",
}
_LEGAL_SUFFIXES = {"llc", "inc", "co", "corp", "ltd", "pllc", "pc"}


def looks_like_keyword_salad(name: str) -> bool:
    """
    Every meaningful word is a generic industry term, with nothing
    distinguishing at all — not just "contains some generic words", since
    real business names legitimately contain 1-2 (e.g. "Elite Accounting &
    Tax Services LLC" has "Elite" as its distinguishing term and should NOT
    be flagged; "Law Firm Bookkeeping Quickbooks" has nothing else at all).
    """
    if not name:
        return False
    words = [w.lower() for w in re.findall(r"[a-zA-Z]+", name) if w.lower() not in _LEGAL_SUFFIXES]
    if len(words) < 3:
        return False
    return all(w in _GENERIC_NAME_KEYWORDS for w in words)


def looks_like_unspaced_slug(name: str) -> bool:
    """A long, spaceless name is very likely a domain-slug artifact, not a real business name."""
    if not name:
        return False
    return " " not in name.strip() and len(name.strip()) > 15


# Domain-parking/expired-domain pages sometimes get indexed with the parking
# service's own branding as the site title/og:site_name (e.g. "awrightco.com"
# turning up "ExpiredDomains.com" as its extracted name) — the domain isn't
# a live business at all.
_PARKED_DOMAIN_PATTERNS = [
    re.compile(r"expired\s*domain", re.I),
    re.compile(r"domain\s+(?:for\s+sale|parking|is\s+parked)", re.I),
    re.compile(r"buy\s+this\s+domain", re.I),
    re.compile(r"this\s+web\s*page\s+is\s+parked", re.I),
]


def looks_like_parked_domain(name: str) -> bool:
    if not name:
        return False
    return any(p.search(name) for p in _PARKED_DOMAIN_PATTERNS)


def is_low_quality_name(name: str) -> bool:
    """Single entry point covering every name-quality red flag."""
    return (
        looks_like_listicle(name)
        or looks_like_keyword_salad(name)
        or looks_like_unspaced_slug(name)
        or looks_like_parked_domain(name)
    )


def looks_like_bot_check(title: str) -> bool:
    if not title:
        return False
    return any(p.search(title) for p in _BOT_CHECK_PATTERNS)


def is_ad_or_tracking_url(url: str) -> bool:
    low = url.lower()
    return any(marker in low for marker in ("bing.com/aclick", "googleadservices", "doubleclick.net", "/aclk?"))


def _service_keywords(service: str) -> List[str]:
    key = service.strip().lower()
    if key in _SERVICE_SYNONYMS:
        return _SERVICE_SYNONYMS[key]
    words = [
        w for w in re.findall(r"[a-zA-Z]+", key)
        if len(w) >= 4 and w not in _RELEVANCE_STOPWORDS
    ]
    return words or [key]


def is_relevant(text: str, service: str, keywords: Optional[List[str]] = None) -> bool:
    """
    Loose relevance check: does the search result actually mention the
    service (or a close synonym) anywhere in its title/snippet? Filters out
    results that only matched on generic query words ("small", "best") —
    dictionary sites, unrelated tools, etc.

    keywords: an explicit override list, used when searching for a target
    vertical rather than the service itself (e.g. service="Bookkeeping" but
    the actual query targeted "Dental Practices" — relevance should be
    judged against dental keywords, not bookkeeping ones).
    """
    if not text:
        return True  # nothing to judge against — don't punish
    low = text.lower()
    kw_list = keywords if keywords is not None else (_service_keywords(service) if service else [])
    if not kw_list:
        return True
    return any(kw in low for kw in kw_list)


# Foreign country-code TLDs — this tool searches for US-state leads, so an
# offshore domain is a strong signal the "local company" search matched an
# unrelated foreign business rather than a real local one. Not exhaustive;
# aimed at the TLDs that actually show up in practice for this kind of query.
_FOREIGN_TLDS = (
    ".ca", ".co.uk", ".uk", ".com.au", ".au", ".ae", ".co.in", ".in",
    ".de", ".fr", ".nz", ".co.nz", ".ph", ".pk", ".sg", ".hk", ".cn",
    ".mx", ".com.mx", ".br", ".com.br", ".co.za", ".za", ".ie", ".eu",
    ".it", ".es", ".nl", ".jp", ".ru", ".pl", ".id", ".vn", ".ng", ".ma",
)


def is_foreign_tld(domain: str) -> bool:
    if not domain:
        return False
    low = domain.lower()
    return any(low.endswith(tld) for tld in _FOREIGN_TLDS)


# .gov and .edu are structurally never a small business we'd cold-email —
# a city/county/state government office or a university, not a bookkeeping/
# event/floral/etc. client. Added 2026-09-24 after "Kelley School of
# Business" (iu.edu) and "City Of Stockdale" (stockdaletx.gov) turned up in
# a nationwide Event Management + Floral audit. Safe and general on
# purpose — unlike foreign TLDs, which merely correlate with "wrong
# business," a .gov/.edu domain essentially can't belong to a genuine
# private-sector lead at all.
_INSTITUTIONAL_TLDS = (".gov", ".edu")


def is_institutional_tld(domain: str) -> bool:
    if not domain:
        return False
    low = domain.lower()
    return any(low.endswith(tld) for tld in _INSTITUTIONAL_TLDS)


# A chamber of commerce's domain name varies per city ("greenwichchamber.com",
# "greenvillechamber.com", ...) so no fixed list keeps up — this anchors on
# the domain ending in "chamber.<tld>" (the actual observed naming
# convention) rather than a bare "chamber" substring, which would also
# false-positive a real business legitimately named "Chambers Florist" or
# similar. Added 2026-09-24 from the same audit.
_CHAMBER_DOMAIN_RE = re.compile(r"chamber\.(com|org|net|us|info)$", re.I)


def is_chamber_domain(domain: str) -> bool:
    if not domain:
        return False
    return bool(_CHAMBER_DOMAIN_RE.search(domain.lower()))


# Tourism-board / convention-and-visitors-bureau sites very often name their
# domain "visit<place>.com" / "discover<place>.com" / "explore<place>.com"
# with no space (so the equivalent NAME-based pattern in junk_filter.py,
# which requires "Visit <Placename>" as separate words, doesn't catch it) —
# e.g. "visitbeulah.com", scraped under the unrelated extracted name
# "Bloom 'n House". Anchored to the START of the domain's first label only,
# not a bare substring anywhere, so a real business domain that merely
# CONTAINS one of these words elsewhere isn't caught. Added 2026-09-24.
_TOURISM_DOMAIN_PREFIX_RE = re.compile(r"^(visit|discover|explore)[a-z]", re.I)


def is_tourism_domain(domain: str) -> bool:
    if not domain:
        return False
    first_label = domain.lower().split(".")[0]
    return bool(_TOURISM_DOMAIN_PREFIX_RE.match(first_label))


def should_skip_result(url: str, domain: str, title: str, snippet: str = "", service: str = "",
                        keywords: Optional[List[str]] = None) -> bool:
    """Single entry point: True if this search result shouldn't be scraped as a company."""
    # is_tourism_domain is deliberately NOT included here — a "visit"-
    # prefixed domain can also just be a real business's own marketing
    # language ("visitworldofsmiles.com", a genuine pediatric dental
    # practice), and this pre-scrape stage doesn't yet have the real
    # extracted company name to check that safely against the vertical's
    # keywords the way junk_filter.check_lead's conditional check does
    # post-scrape. See junk_filter.py step 5a.
    combined_text = f"{title} {snippet}"
    return (
        is_ad_or_tracking_url(url)
        or is_blocked_domain(domain)
        or is_foreign_tld(domain)
        or is_institutional_tld(domain)
        or is_chamber_domain(domain)
        or looks_like_listicle(title)
        or looks_like_bot_check(title)
        or not is_relevant(combined_text, service, keywords)
    )