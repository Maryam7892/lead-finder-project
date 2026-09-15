"""
Final junk pass — runs AFTER every other filter (pre-scrape blocklist,
scrape, extraction, confidence scoring), on the fully-assembled lead rows,
right before they're saved.

Everything upstream judges a *search result* (a URL + title + snippet) or a
*field* (is this phone real). This pass judges the *finished lead*: now that
we have the site's real extracted name, its domain, its emails, and the
vertical it was found under, is this actually a member of the kind of
business we searched for — or is it a directory / media site / a different
service business that slipped through on a keyword?

Conservative by design: a false positive here silently deletes a real lead,
so every rule targets something that is almost never a genuine local target
business. It will not catch everything; it removes the clear cases.

Entry point: `check_lead(row, vertical_keywords) -> (is_junk, reason)`.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from . import blocklist

# Patterns in the company NAME (and, separately, the domain — see
# _OFF_TARGET_DOMAIN_SUBSTRINGS below) that mean "this sells a service TO an
# industry" or "this is media/a directory", not "this is a local business
# of the searched type". These fire regardless of vertical.
_OFF_TARGET_NAME_PATTERNS = [
    # "SEO for Event Planning Companies", "Marketing Agency for Florists"
    re.compile(r"\b(seo|ppc|sem|digital marketing|marketing|advertising|branding|web design|web development|lead generation|lead gen)\b.{0,20}\b(for|agency|services|solutions|company|experts?)\b", re.I),
    # "Free Local Citation Checker" — an SEO/directory-listing tool, not a
    # business of any kind we'd search for.
    re.compile(r"\bcitation (checker|builder|building|management|tool)\b", re.I),
    re.compile(r"\bfor\s+(small businesses|contractors|realtors?|dentists?|florists?|event planners?|restaurants?|[a-z]+\s+(companies|businesses|professionals))\b", re.I),
    # Sanitation / rentals that ride "for your event"
    re.compile(r"\b(porta[\s-]?potty|porta[\s-]?john|portable (toilet|restroom)s?|restroom trailer|dumpster|roll[\s-]?off|junk (removal|hauling))\b", re.I),
    re.compile(r"\bphoto ?booth\b", re.I),
    # Photographers/videographers keep leaking under Event Management
    # because "wedding" (a photographer's own specialty word) is also an
    # Event Management relevance keyword — the vertical-keyword gate that
    # protects other conditional terms doesn't help here. No vertical in
    # this app wants a photography business, so this is unconditional now
    # (previously conditional — see CLAUDE.md change log 2026-09-14).
    re.compile(r"\b(photography|photographer|photograph studio|videography|videographer|cinematography)\b", re.I),
    # A "destination wedding" business is, by definition, not a local
    # vendor in the searched state — the whole pitch is a wedding held
    # somewhere else (often abroad).
    re.compile(r"\bdestination weddings?\b", re.I),
    # Ticket sellers and law firms occasionally match on adjacent keywords
    # ("event" tickets, wedding "planning" for a prenup) but are a
    # different trade entirely.
    re.compile(r"\b(law firm|attorneys?( at law)?|law offices?|legal services)\b", re.I),
    # Bare "Law" as a naming SUFFIX ("Pierview Law", "Smith Law Group") is a
    # common law-firm convention — but only checked at the end of the name,
    # so an incidental "Law" elsewhere (a surname, a street) doesn't false-
    # positive a real target business ("Law Gardens Florist" survives).
    re.compile(r"\blaw(\s+(group|firm|office|offices|pllc|llc|p\.?c\.?))?\s*$", re.I),
    re.compile(r"\b(box office|ticket(ing)? (sales|sellers?)|buy tickets)\b", re.I),
    # Media / reference / directory
    re.compile(r"\b(magazine|blog|podcast|newsletter|wikipedia|fandom|encyclopedia)\b", re.I),
    re.compile(r"\b(vendor directory|business directory|\bdirectory\b|listings?)\b", re.I),
    re.compile(r"\b(chamber of commerce|visitors? bureau|convention (and|&) visitors|tourism (board|bureau)|visitor center|turismo)\b", re.I),
    re.compile(r"^\s*visit\s+\w", re.I),  # "Visit Vacaville", "Visit Bishop" — destination-marketing sites
    re.compile(r"\bfilm\s*&?\s*tv production\b", re.I),
    # Local newspapers — masthead-style naming words that are essentially
    # never a dental/event/floral business name ("The Andalusia Star-News").
    # Bare "news"/"times"/"press" are deliberately excluded — too generic
    # ("Flower Press" is a real kind of floral-business name).
    re.compile(r"\b(star-news|tribune|gazette|chronicle|dispatch|courier|ledger|observer|advertiser)\b", re.I),
    # Professional associations / academic & research institutions —
    # "Academy of General Dentistry", "College of Veterinary Medicine" pass
    # the vertical-relevance check on "dentistry"/"medicine" alone, but
    # they're membership orgs or university departments, not a local
    # practice you'd cold-email. This one pattern catches both real
    # examples found in an audit (see CLAUDE.md change log).
    re.compile(r"\b(academy of|association of|society of|college of|school of|institute of)\b.{0,40}\b(dentistry|dental|medicine|veterinary)\b", re.I),
    # Veterinary/animal care is never a target of any vertical in this app —
    # unconditional (like photography) rather than gated by vertical
    # keywords, since "dentistry" and "veterinary" can coexist in a real
    # name ("veterinary dentistry" is an actual specialty) without making
    # the business a fit for a HUMAN dental-practice search.
    re.compile(r"\bveterinar(y|ian)\b", re.I),
]

# Same idea as above, but for the DOMAIN string — a junk site sometimes
# rebrands its display NAME to something plausible ("USA Rental Experts")
# while the domain still gives away the real business
# ("mrportapottyorlando.com"). Domains have no word boundaries/spaces, so
# these are plain substrings, not the word-bounded regexes used for names.
_OFF_TARGET_DOMAIN_SUBSTRINGS = [
    "portapotty", "porta-potty", "portajohn", "dumpster", "junkremoval",
    "ticketsales", "boxoffice", "lawfirm", "destinationwedding",
]

# Terms that are off-target *unless* the vertical we searched actually wants
# them (checked against vertical_keywords). E.g. "hot springs" is noise for
# Floral Businesses, but if a vertical's own keywords mention it, it's spared.
_CONDITIONAL_OFF_TARGET_TERMS = [
    "wedding dj", "mobile dj", "disc jockey",
    "hot springs", "day spa", "dollar tree", "dollar store", "car wash",
    "urgent care", "emergency room",
    # A general hospital is a different scale/type of institution than a
    # dental *practice* — conditional (not unconditional) because a name
    # that also says "dental"/"dentist" (a real dental hospital/clinic)
    # already passes via the vertical-keyword check below and is kept.
    "hospital",
]

# Emails from a shared, templated "customer care" address are a strong
# signal the site is one of many machine-generated pages in a network
# (e.g. "wecare@pomonasflorist.com", "wecare@sandiegosflorist.com" — same
# prefix, different city-named domains, confirmed by manual audit to be a
# templated florist-delivery network, not independent local shops) rather
# than a distinct, independently-owned business. GENERIC_EMAIL_PREFIXES
# already keeps these un-sendable; this drops the ROW too, since the user
# doesn't want the network's page counted as a "company" at all.
_NETWORK_TEMPLATE_EMAIL_PREFIXES = {"wecare"}

_BROKEN_NAME_TOKENS = {
    "same", "full", "head", "home", "about", "contact", "menu", "welcome",
    "untitled", "index", "default", "null", "none", "n/a", "loading",
    "page not found", "404", "site", "website",
}


def _contains_any(haystack: str, needles) -> Optional[str]:
    for n in needles:
        if n in haystack:
            return n
    return None


def check_lead(row: Dict, vertical_keywords: Optional[List[str]] = None) -> Tuple[bool, str]:
    """
    Returns (is_junk, reason). `vertical_keywords` is the list of positive
    keywords for the vertical this lead was found under (from
    states.py) — used so a conditional off-target term is only a strike
    when the vertical didn't actually want it. Pass None for a service with
    no vertical concept (conditional terms then always apply).
    """
    name = str(row.get("company_name", "") or "").strip()
    domain = str(row.get("domain", "") or "").strip().lower()
    name_l = name.lower()
    kw = [k.lower() for k in (vertical_keywords or [])]
    kw_hit = any(k in name_l for k in kw)

    # 1. Known bad / directory / national-gateway domain (defense in depth —
    #    upstream blocklist should already have skipped it pre-scrape; this
    #    still catches rows collected before a domain was added to the list).
    if domain and not domain.startswith("osm:"):
        if blocklist.is_blocked_domain(domain) or blocklist.is_foreign_tld(domain):
            return True, f"blocked/directory domain ({domain})"

    # 2. Name-quality red flags (listicle title, keyword salad, parked, slug).
    if blocklist.is_low_quality_name(name):
        return True, "low-quality / listicle-style name"

    # 3. Broken extraction — the "name" is a nav label or a bare fragment.
    if name_l.strip(" .|-–—:") in _BROKEN_NAME_TOKENS or not re.search(r"[A-Za-z]", name):
        return True, f"unusable extracted name ({name!r})"

    # 4. Sells-to-an-industry / media / directory / sanitation-rental name.
    for pat in _OFF_TARGET_NAME_PATTERNS:
        if pat.search(name):
            return True, f"off-target business name (matched /{pat.pattern[:40]}.../)"

    # 4a. Same patterns against the DOMAIN — a junk site's display name is
    # sometimes rebranded to something plausible ("USA Rental Experts")
    # while the domain still names the real business
    # ("mrportapottyorlando.com"). Skip osm: pseudo-domains (no real URL).
    if domain and not domain.startswith("osm:"):
        for pat in _OFF_TARGET_NAME_PATTERNS:
            if pat.search(domain):
                return True, f"off-target domain (matched /{pat.pattern[:40]}.../ against {domain})"
        hit = _contains_any(domain, _OFF_TARGET_DOMAIN_SUBSTRINGS)
        if hit:
            return True, f"off-target term '{hit}' in domain ({domain})"

    # 5. Conditional off-target term, and the vertical didn't ask for it.
    if not kw_hit:
        hit = _contains_any(name_l, _CONDITIONAL_OFF_TARGET_TERMS)
        if hit:
            return True, f"off-target term '{hit}' with no matching vertical keyword"

    # 6. Templated-network email prefix (e.g. "wecare@") — a strong signal
    # this is one page in a machine-generated multi-city network, not an
    # independent local business, regardless of how good everything else
    # about the row looks.
    email_prefix = str(row.get("email", "") or "").split("@")[0].strip().lower()
    if email_prefix in _NETWORK_TEMPLATE_EMAIL_PREFIXES:
        return True, f"templated network email prefix ('{email_prefix}@')"

    return False, ""


def partition(rows: List[Dict], vertical_keywords: Optional[List[str]] = None):
    """Split rows into (kept, dropped) — dropped items are (row, reason) tuples."""
    kept, dropped = [], []
    for r in rows:
        is_junk, reason = check_lead(r, vertical_keywords)
        (dropped if is_junk else kept).append((r, reason) if is_junk else r)
    return kept, dropped
