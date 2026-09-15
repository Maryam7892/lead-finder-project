"""
Extraction helpers: pulling a company's own name, email, and phone out of a
page — and telling the difference between "this business's real contact
info" and "some email/phone-shaped text that happened to appear on the
page" (tracking scripts, other companies mentioned in a listicle, etc).

Priority order, most trustworthy first:
  1. mailto:/tel: links       — the site is explicitly offering this contact
  2. JSON-LD / og:site_name   — structured, author-declared business identity
  3. Raw regex over page text — anything on the page that looks email/phone
                                  shaped; used only as a fallback, and never
                                  fully trusted (see score_lead below).
"""

from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple
from urllib.parse import unquote

from bs4 import BeautifulSoup

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9][a-zA-Z0-9._%+\-]*@[a-zA-Z0-9][a-zA-Z0-9.\-]*\.[a-zA-Z]{2,}"
)

# Matches common US phone formats: (123) 456-7890, 123-456-7890, 123.456.7890, +1 123 456 7890
PHONE_RE = re.compile(
    r"(?:\+?1[\s.\-]?)?\(?\b\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b"
)

JUNK_EMAIL_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")

JUNK_EMAIL_DOMAINS = (
    "example.com", "sentry.io", "wixpress.com", "godaddy.com",
    "schema.org", "yoursite.com", "domain.com", "yourdomain.com",
)

# Obvious placeholder local-parts that show up in templates/boilerplate code,
# not real contact addresses (e.g. "you@work.com", "name@company.com").
PLACEHOLDER_LOCAL_PARTS = {
    "you", "your", "yourname", "name", "user", "email", "test", "someone",
    "firstname.lastname", "youremail",
}
PLACEHOLDER_EMAIL_DOMAINS = {"work.com", "company.com", "website.com", "email.com"}

# A 32-hex-char local part (an MD5-shaped hash/UUID) is never something a
# human typed as their own address — it's an auto-generated identifier, most
# often a Sentry/error-tracking DSN or webhook address that leaked into the
# page's visible text. Found via a real case: a site's own error-tracking
# subdomain (`sentry.<their-domain>`) was scraped and stored as the lead's
# contact email (added 2026-09-15).
_HEX_HASH_LOCAL_PART_RE = re.compile(r"^[0-9a-f]{32}$")

# Well-known FREE CONSUMER email providers. Used only by score_lead's
# mailto: domain-match exemption (see there) — a mailto: pointing at one of
# these is unambiguously the site owner's own personal inbox, never
# "someone else's business." Deliberately NOT used to relax anything else
# (blocklist screening, best_email choice, etc.).
CONSUMER_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "aol.com", "protonmail.com", "live.com", "msn.com", "mail.com", "ymail.com",
}

ORG_SCHEMA_TYPES = {
    "organization", "localbusiness", "professionalservice", "accountingservice",
    "corporation", "financialservice",
}


# ------------------------------------------------------------- mailto/tel --
def extract_mailto_emails(html: str) -> List[str]:
    """
    mailto: links are the highest-trust email source (see module docstring)
    — but "highest trust" means "the site owner put this here", not "this
    is a syntactically valid email". A real site was found with a page
    builder typo of `href="mailto:emailto:@rmfloralstudio.com"` — no local
    part at all, just a mistyped literal "emailto:" — which was accepted
    verbatim and stored as the lead's email. Every candidate is now checked
    against EMAIL_RE (the same pattern used for the regex fallback) with
    `fullmatch`, so a malformed href can't pass through just because it
    came from a mailto: link. A `mailto:` can also list multiple
    comma-separated recipients — each is checked separately so one bad
    entry doesn't lose a valid one next to it.
    """
    soup = BeautifulSoup(html, "html.parser")
    found = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            raw = href[7:].split("?")[0].strip()
            raw = unquote(raw)
            for candidate in raw.split(","):
                candidate = candidate.strip()
                if candidate and EMAIL_RE.fullmatch(candidate):
                    # Normalize to lowercase on capture — mail hosts treat
                    # addresses case-insensitively in practice, and keeping
                    # a consistent form means the suppression list and
                    # sent-log dedup can't be defeated by "Info@x.com" vs
                    # "info@x.com".
                    found.add(candidate.lower())
    return sorted(found)


def extract_tel_phones(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    found = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("tel:"):
            raw = href[4:].strip()
            formatted = _format_phone(raw)
            if formatted:
                found.add(formatted)
    return sorted(found)


# --------------------------------------------------------- regex fallback --
def extract_emails(text: str) -> List[str]:
    found = set(EMAIL_RE.findall(text or ""))
    cleaned = set()
    for e in found:
        low = e.lower()
        if low.endswith(JUNK_EMAIL_EXT):
            continue
        if any(low.endswith("@" + d) or low.endswith("." + d) for d in JUNK_EMAIL_DOMAINS):
            continue
        cleaned.add(low)  # store lowercase — consistent form for dedup/suppression
    return sorted(cleaned)


def _format_phone(raw_digits_or_text: str) -> Optional[str]:
    digits = re.sub(r"\D", "", raw_digits_or_text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"


def extract_phones(text: str) -> List[str]:
    found = set(PHONE_RE.findall(text or ""))
    cleaned = set()
    for p in found:
        formatted = _format_phone(p)
        if formatted:
            cleaned.add(formatted)
    return sorted(cleaned)


# -------------------------------------------------------- authenticity checks --
def is_placeholder_email(email: str) -> bool:
    if not email or "@" not in email:
        return True
    local, _, domain = email.lower().partition("@")
    if local in PLACEHOLDER_LOCAL_PARTS:
        return True
    if domain in PLACEHOLDER_EMAIL_DOMAINS:
        return True
    if _HEX_HASH_LOCAL_PART_RE.match(local):
        return True
    if domain.split(".")[0] == "sentry":
        return True
    return False


def is_suspicious_phone(phone: str) -> bool:
    """Flag obviously fake/placeholder-looking numbers."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) != 10:
        return True
    # NANP structural rule: area codes and exchange codes can't start with
    # 0 or 1 — (178) / (113) / etc are not valid area codes at all, no
    # matter how plausible the rest of the number looks.
    if digits[0] in "01" or digits[3] in "01":
        return True
    # All same digit, or the classic all-2s/placeholder pattern.
    if len(set(digits)) <= 2:
        return True
    # Reserved fictional numbers: NXX-555-01XX
    if digits[3:6] == "555" and digits[6:8] == "01":
        return True
    # Simple ascending/descending runs like 1234567890 / 0123456789.
    ascending = "0123456789"
    if digits in ascending or digits in ascending[::-1]:
        return True
    return False


def email_domain_matches(email: str, site_domain: str) -> bool:
    if not email or "@" not in email or not site_domain:
        return False
    email_domain = email.lower().split("@")[-1]
    site_domain = site_domain.lower()
    return email_domain == site_domain or email_domain.endswith("." + site_domain)


def email_domain_is_blocked(email: str) -> bool:
    """
    True if the email's OWN domain is a known directory/SaaS/offshore-BPO
    site — catches leaked/syndicated contact info, e.g. a page on an
    unrelated site whose mailto: link points to a blocklisted BPO's sales
    address (embedded widget, sponsored content, ad syndication). This is a
    stronger signal than a plain domain mismatch: the email doesn't just
    fail to match the site, it actively belongs to a known bad actor.
    """
    if not email or "@" not in email:
        return False
    from . import blocklist  # local import: blocklist has no reverse dependency on this module
    email_domain = email.lower().split("@")[-1]
    return blocklist.is_blocked_domain(email_domain) or blocklist.is_foreign_tld(email_domain)


def best_email(emails: List[str]) -> str:
    """Prefer generic business inboxes over personal-looking ones for a first-touch template."""
    if not emails:
        return ""
    priority_prefixes = ("info@", "contact@", "hello@", "office@", "sales@", "admin@")
    for prefix in priority_prefixes:
        for e in emails:
            if e.lower().startswith(prefix):
                return e
    return emails[0]


# --------------------------------------------------------- name extraction --
def extract_jsonld_org_name(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            type_str = " ".join(item_type) if isinstance(item_type, list) else str(item_type)
            if type_str.lower() in ORG_SCHEMA_TYPES or "business" in type_str.lower():
                name = item.get("name")
                if name:
                    return str(name).strip()
    return None


def extract_og_site_name(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("meta", attrs={"property": "og:site_name"})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


# ------------------------------------------------------ owner / decision-maker --
# These read only the company's OWN website — never linkedin.com itself.
# Automating LinkedIn profile access violates its terms of service and is
# aggressively technically blocked besides, so this stays limited to what a
# company voluntarily publishes about itself: a named founder/CEO in its own
# page text, or a link to its own LinkedIn page. Coverage is intentionally
# narrow — precision matters far more than recall here, since a wrong result
# feeds directly into "Hi {name}," in real outreach. An early, looser version
# of this (including "owner" as a trigger word, and allowing 1-3 word
# matches) produced garbage on ~80% of real pages — "Owner Portal", "Please
# Select Owner", "George Bush Turnpike" — because business sites are full of
# capitalized UI text/nav labels near generic title words. This version
# trades a lot of recall for much higher precision.
_OWNER_TITLE_WORDS = r"(?:founder|co-founder|ceo)"
# Exactly two capitalized words (first + last name), each at least 3
# characters — long enough to exclude boundary artifacts like "By"/"Us"/"At"
# that a looser pattern picked up.
_NAME_PATTERN = r"([A-Z][a-zA-Z'\-]{2,}\s+[A-Z][a-zA-Z'\-]{2,})"

# Case-insensitivity is scoped to just the keyword portions via (?i:...) —
# the name-capturing group deliberately stays case-sensitive, since matching
# capitalization is exactly the signal used to recognize a proper name.
_OWNER_NAME_PATTERNS = [
    re.compile(rf"(?i:founded\s+by)\s+{_NAME_PATTERN}"),
    re.compile(rf"\b{_NAME_PATTERN},?\s+(?i:{_OWNER_TITLE_WORDS})\b"),
    re.compile(rf"(?i:{_OWNER_TITLE_WORDS})(?:\s*(?:&|and)\s*\w+)?[,:]?\s+{_NAME_PATTERN}"),
    re.compile(rf"(?i:meet\s+(?:the\s+)?founder)[,:]?\s+{_NAME_PATTERN}"),
]

# Common non-name words that showed up in real false-positive matches —
# nav labels, form text, section headings, generic business nouns. If
# either captured word is in here (case-insensitive), the match is rejected.
_NON_NAME_WORDS = {
    "systems", "integrator", "sun", "life", "group", "primewest", "health",
    "rsvp", "meet", "our", "about", "company", "leadership", "executive",
    "vice", "relations", "contact", "app", "your", "saas", "please", "select",
    "free", "rental", "rentals", "report", "reports", "reporting", "maui",
    "beaches", "management", "date", "message", "send", "events", "event",
    "statements", "seamless", "process", "short", "term", "instagram",
    "vendor", "application", "trusted", "property", "properties", "managers",
    "administration", "business", "party", "wedding", "meeting", "vacancy",
    "calculator", "owners", "owner", "managed", "homes", "financial",
    "services", "service", "story", "leading", "snow", "tree", "experience",
    "unique", "board", "portal", "overview", "areas", "manufacturers",
    "guide", "accountant", "near", "reviews", "investment", "marketplace",
    "small", "credits", "every", "restaurant", "community", "featured",
    "all", "law", "enforcement", "guardian", "dental", "ameritas", "land",
    "real", "estate", "investors", "clients", "new", "orleans", "sale",
    "pottery", "barn", "cottage", "working", "for", "the", "discount",
    "from", "turnpike", "tollway", "united", "concordia", "communication",
    "when", "specialists", "serving", "houston", "either", "testimonials",
    "guides", "development", "login", "us", "carter", "guarantees", "tenant",
    "access", "criteria", "benchmarking", "competitor", "comparison",
    "advanced", "memory", "solutions", "maintenance", "request", "title",
    "fee", "office", "staff", "resources", "construction", "data",
    "vacation", "modern", "experiences", "tesla", "verified", "companies",
    "remember", "guest", "dds", "pc", "realtor", "agent", "lead", "bio",
    "read", "dentist", "team", "staff", "portal", "compare", "download",
}


def _is_valid_owner_name(name: str) -> bool:
    words = name.split()
    if len(words) != 2:
        return False
    # Each word needs at least 3 characters — excludes short artifacts like
    # "Hi"/"By"/"Us" that could appear in already-stored data captured under
    # an older, looser extraction rule (fresh extraction's regex already
    # enforces this at match time, but retroactive cleanup of old stored
    # strings needs its own check here).
    if any(len(w) < 3 for w in words):
        return False
    return not any(w.lower() in _NON_NAME_WORDS for w in words)


def extract_owner_name(html: str) -> Optional[str]:
    """Best-effort: a named founder/CEO mentioned in the page's own text."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    for pattern in _OWNER_NAME_PATTERNS:
        match = pattern.search(text)
        if match:
            name = match.group(1).strip()
            if _is_valid_owner_name(name):
                return name
    return None


def revalidate_owner_name(stored_name: str) -> str:
    """
    For cleaning up already-stored data retroactively (no HTML available,
    can't re-extract) — re-checks a previously-stored owner_name against the
    current validation rules and blanks it out if it no longer passes. Can't
    recover a better name without re-scraping, but can at least stop
    surfacing known-bad ones like "Systems Integrator" or "Owner Portal".
    """
    stored_name = (stored_name or "").strip()
    if not stored_name:
        return ""
    return stored_name if _is_valid_owner_name(stored_name) else ""


def extract_self_published_linkedin_url(html: str) -> Optional[str]:
    """
    A LinkedIn URL the company itself links to from its own site — e.g. a
    footer "Follow us" link. This is just reading a URL string off the
    target's own page, not accessing linkedin.com; follow-up on that link is
    a manual step for a human, not something this tool automates further.
    Prefers a personal profile (/in/) over a company page if both appear.
    """
    soup = BeautifulSoup(html, "html.parser")
    company_url = None
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "linkedin.com/in/" in href.lower():
            return href
        if "linkedin.com/company/" in href.lower() and not company_url:
            company_url = href
    return company_url


# --------------------------------------------------------------- scoring --
def score_lead(name_source: str, company_name: str, email: str, email_source: str, email_domain_match: bool,
               phone: str, phone_source: str, phone_suspicious: bool,
               location_match: Optional[bool] = None, domain: str = "") -> Tuple[float, str]:
    """
    Rough authenticity score based on *how* each field was obtained, not just
    whether it exists. Structured/explicit signals (mailto:, tel:, JSON-LD,
    OpenStreetMap community-mapped data) are trusted far more than a bare
    regex match anywhere on the page.

    location_match: True if the phone's area code is confirmed to belong to
    the target state, False if it's confirmed to belong to a different one
    (or a non-US NANP region), None if unknown — None doesn't affect the
    score either way, only a confirmed mismatch does.

    The overall ratio drives Medium vs Low, but "High" additionally requires
    passing hard gates: an email (if present) must either match the site's
    own domain OR have been explicitly published by the site itself via a
    mailto: link, a phone (if present) must not be a suspicious/fake
    pattern or a confirmed wrong-state/foreign number, the name mustn't
    look like a listicle title, and the domain itself mustn't be a known
    directory/SaaS/offshore-BPO site. A weighted ratio alone let too many
    leads that failed one of these basic checks slip into "High" — these
    are non-negotiable.

    Email gate rationale: domain-match exists to catch an email that isn't
    actually this business's own — either a REGEX-sourced one (the "an
    article mentions ten other companies, one of their emails ends up on
    this page" pattern; stays hard-gated, since scrape.py already drops a
    non-domain-matching regex email before one is even chosen, so
    email_source=="regex" here always implies a domain match in practice),
    or a mailto: that happens to point at a DIFFERENT custom business
    domain (e.g. a leftover address from a rebrand, a shared vendor/agency
    contact, or two similarly-named businesses getting attributed to each
    other — a real case found in practice: a "Madison Dental Care" site
    whose own domain was "madisondentalcare.com" had a mailto: to
    "md@dentalcaremadison.com", a different domain entirely).

    The ONE exemption from domain-match is a mailto: pointing at a
    well-known FREE CONSUMER email provider (extract.CONSUMER_EMAIL_DOMAINS
    — gmail.com, yahoo.com, outlook.com, ...) — very common for a real
    small business (event planners, florists, single-dentist practices) to
    just use a personal inbox, and there's no "domain" for that address to
    plausibly match in the first place. A mailto: to some OTHER custom
    domain gets no such pass — it's held to the same domain-match
    requirement as everything else, since "the site owner published this
    mailto:" doesn't rule out it pointing at the wrong business.
    """
    from . import blocklist  # local import: blocklist has no reverse dependency on this module

    is_listicle_name = blocklist.is_low_quality_name(company_name or "")
    is_bad_domain = bool(domain) and (blocklist.is_blocked_domain(domain) or blocklist.is_foreign_tld(domain))

    score = 0
    max_score = 1  # name check always applies
    if name_source in ("jsonld", "og", "osm"):
        score += 1

    max_score += 1  # a listicle-looking name is a red flag regardless of where it came from
    if not is_listicle_name:
        score += 1

    if email:
        max_score += 2
        if email_source == "mailto":
            score += 1
        if email_domain_match:
            score += 1

    if phone:
        max_score += 2
        if phone_source in ("tel", "osm"):
            score += 1
        if not phone_suspicious:
            score += 1

    if location_match is not None:
        max_score += 1
        if location_match:
            score += 1

    ratio = score / max_score if max_score else 0.0

    email_consumer_domain = email.split("@")[-1].lower() if email and "@" in email else ""
    email_ok = (
        (not email)
        or email_domain_match
        or (email_source == "mailto" and email_consumer_domain in CONSUMER_EMAIL_DOMAINS)
    )
    phone_ok = (not phone) or (not phone_suspicious and location_match is not False)
    has_contact_info = bool(email) or bool(phone)
    passes_hard_gates = (not is_listicle_name) and (not is_bad_domain) and email_ok and phone_ok and has_contact_info

    if is_bad_domain or is_listicle_name or not has_contact_info:
        label = "Low"
    elif passes_hard_gates and ratio >= 0.75:
        label = "High"
    elif ratio >= 0.4:
        label = "Medium"
    else:
        label = "Low"
    return round(ratio, 2), label