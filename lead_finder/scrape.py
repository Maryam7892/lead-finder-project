"""
Fetches a company's website (and, if needed, its contact/about page) and
extracts name, phone, and email — preferring mailto:/tel: links and
structured business data over raw regex matches, and scoring the result so
low-trust extractions can be filtered out downstream.

Runs with a small thread pool since this step is dominated by network wait
time, not CPU.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import tldextract

from . import extract
from . import area_codes
from . import blocklist

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
REQUEST_TIMEOUT = 20
CONTACT_LINK_KEYWORDS = ("contact", "about", "get-in-touch", "reach-us")

# Use the bundled public-suffix snapshot instead of fetching it live on every
# run — avoids network calls/warnings and works fine offline.
_TLD_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())


def _domain(url: str) -> str:
    ext = _TLD_EXTRACTOR(url)
    return ".".join(p for p in [ext.domain, ext.suffix] if p)


def _fetch(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200 and "text/html" in resp.headers.get("Content-Type", ""):
            return resp.text
    except requests.RequestException:
        return None
    return None


def _find_contact_link(html: str, base_url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = (a.get_text() or "").lower()
        if any(k in href.lower() or k in label for k in CONTACT_LINK_KEYWORDS):
            return urljoin(base_url, href)
    return None


def _title_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


# Separators a site puts between its name and a tagline/location in <title>
# / og:site_name. Colon/dash/slash only count as separators when whitespace
# surrounds them, so "24:01 Floral Design" and hyphenated names survive.
_TITLE_SEPARATORS = re.compile(r"\s*\|\s*|\s*::\s*|\s+[:–—•·»]\s+|\s+-\s+|\s+/\s+")


def _despace_initials(name: str) -> str:
    """
    "J E N N Y & J U N E" -> "JENNY & JUNE". Only fires when most tokens are
    single letters (a real name never looks like that); otherwise untouched.
    """
    tokens = name.split()
    if len(tokens) >= 4 and sum(1 for t in tokens if len(t) == 1) >= len(tokens) * 0.6:
        return re.sub(r"(?<=\b\w) (?=\w\b)", "", name).strip()
    return name


def _clean_title_name(title: str) -> str:
    """First segment of a <title>, before any tagline/location separator."""
    first = _TITLE_SEPARATORS.split(title.strip())[0].strip()
    return first or title.strip()


def _domain_based_name(domain: str) -> str:
    base = domain.split(".")[0]
    return base.replace("-", " ").title()


# Nav labels / boilerplate headings that get mistaken for a company name when
# a page has a thin or missing <title> and no structured data.
_BAD_NAME_TOKENS = {
    "", "home", "home page", "homepage", "about", "about us", "contact",
    "contact us", "menu", "welcome", "untitled", "untitled document",
    "index", "default", "page not found", "404", "same", "full", "head",
    "null", "none", "n/a", "loading", "site", "website", "new page",
}


def _normalize_company_name(name: str, domain: str) -> tuple[str, bool]:
    """
    Tidy an extracted name. Returns (name, looked_bad) — looked_bad=True means
    the extracted value was junk (a nav label, the bare domain, empty) and the
    caller should treat the name_source as untrusted / fall back to the domain.
    """
    name = _despace_initials(re.sub(r"\s+", " ", (name or "").strip()))
    # Trim a trailing tagline/location the same way a <title> is trimmed —
    # "Boulevard Florist :: Wholesale Flowers in LA" -> "Boulevard Florist".
    # Only when a real separator is present and the first segment survives.
    first_segment = _TITLE_SEPARATORS.split(name)[0].strip()
    if first_segment and len(first_segment) >= 3 and re.search(r"[A-Za-z]", first_segment):
        name = first_segment
    slug = domain.split(".")[0].replace("-", "").lower() if domain else ""
    key = name.lower().strip(" .|-–—:")
    single_token = " " not in name.strip()

    # "zola.com" / "myeventpod" as a name = the site put its bare domain
    # where its name should be. Only treat a *spaceless* value this way — a
    # real multi-word name ("Everbloom Floral Design") often concatenates to
    # its own domain and must NOT be discarded for that.
    is_slug_artifact = single_token and (
        name.lower() == domain.lower()
        or "." in name
        or name.replace(" ", "").lower() == slug
    )

    if (not name or key in _BAD_NAME_TOKENS or is_slug_artifact
            or not re.search(r"[A-Za-z]", name)):
        return (_domain_based_name(domain) if domain else name), True

    # ALL-CAPS -> Title Case ("LE FLEUR IV" -> "Le Fleur Iv"). Leave mixed
    # case alone so real casing like "CJ Matsumoto" survives.
    letters = [c for c in name if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(name) > 3:
        name = name.title()

    # Keyword-stuffed meta "name" (some sites cram SEO phrases into
    # og:site_name) — unusable as a greeting. Signal a fallback.
    if len(name) > 120:
        return (_domain_based_name(domain) if domain else name[:120]), True

    return name, False


def scrape_company(url: str, fallback_name: str = "", fallback_phone: str = "",
                    state_abbr: str = "", html_override: Optional[str] = None,
                    contact_page_fetcher: Optional[Callable[[str], Optional[str]]] = None) -> Dict:
    """
    Scrape one company's site. Returns a dict even on failure (with empty fields).

    fallback_phone: used when the site itself has no findable phone number
    but a trusted external source (e.g. an OpenStreetMap listing) already
    supplied one for this business.
    state_abbr: the target state's 2-letter code, used only to check whether
    the phone's area code plausibly belongs there (a soft confidence signal,
    not a hard filter — see area_codes.phone_matches_state).
    html_override: if given, skips the normal HTTP fetch and runs the exact
    same extraction logic against this HTML instead — used by
    browser_scrape.py's real-browser fallback for sites whose contact info
    only appears after JavaScript renders, without duplicating the
    extraction pipeline in a second place where it could drift out of sync.
    contact_page_fetcher: if given, used instead of the default plain-HTTP
    _fetch to retrieve the linked contact/about page — lets the browser-
    based path use a real browser for *that* page too, not just the
    homepage, since a contact page can just as easily need JS rendering.
    """
    domain = _domain(url)
    record = {
        "company_name": fallback_name or _domain_based_name(domain),
        "name_source": "fallback",
        "website": url,
        "domain": domain,
        "phone": "",
        "phone_source": "",
        "email": "",
        "email_source": "",
        "email_domain_match": False,
        "all_emails_found": "",
        "status": "failed",
        "confidence_score": 0.0,
        "confidence_label": "Low",
        "owner_name": "",
        "linkedin_url": "",
    }

    html = html_override if html_override is not None else _fetch(url)
    if not html:
        if fallback_phone:
            record["phone"] = fallback_phone
            record["phone_source"] = "osm"
            record["status"] = "ok"
        if fallback_name:
            record["name_source"] = "osm"
        location_match = area_codes.phone_matches_state(record["phone"], state_abbr) if state_abbr else None
        record["confidence_score"], record["confidence_label"] = extract.score_lead(
            name_source=record["name_source"], company_name=record["company_name"],
            email="", email_source="", email_domain_match=False,
            phone=record["phone"], phone_source=record["phone_source"],
            phone_suspicious=extract.is_suspicious_phone(record["phone"]) if record["phone"] else True,
            location_match=location_match, domain=domain,
        )
        return record

    # ---- name: structured data > og:site_name > cleaned <title> > domain ----
    jsonld_name = extract.extract_jsonld_org_name(html)
    og_name = extract.extract_og_site_name(html)
    title = _title_text(html)

    if jsonld_name:
        record["company_name"], record["name_source"] = jsonld_name, "jsonld"
    elif og_name:
        record["company_name"], record["name_source"] = og_name, "og"
    elif title:
        record["company_name"], record["name_source"] = _clean_title_name(title), "title"
    # else keep the domain-based fallback already set above

    # Tidy whatever we picked (strip taglines/locations, fix ALL-CAPS,
    # collapse spaced initials). If it turns out to be a nav label or the
    # bare domain, drop back to the domain-based name and mark the source
    # untrusted so scoring doesn't reward it.
    normalized_name, name_looked_bad = _normalize_company_name(record["company_name"], domain)
    record["company_name"] = normalized_name
    if name_looked_bad:
        record["name_source"] = "fallback"

    # ---- owner/decision-maker (best-effort, from the site's own text only) ----
    record["owner_name"] = extract.extract_owner_name(html) or ""
    record["linkedin_url"] = extract.extract_self_published_linkedin_url(html) or ""

    # ---- contact info: mailto:/tel: links first, regex as fallback only ----
    mailto_emails = extract.extract_mailto_emails(html)
    tel_phones = extract.extract_tel_phones(html)
    regex_emails = extract.extract_emails(html)
    regex_phones = extract.extract_phones(html)

    # If the homepage came up empty on the trusted signals, try a linked
    # contact/about page before falling back to the noisier regex pass.
    if not mailto_emails and not tel_phones:
        contact_url = _find_contact_link(html, url)
        if contact_url:
            fetcher = contact_page_fetcher or _fetch
            contact_html = fetcher(contact_url)
            if contact_html:
                mailto_emails = list(set(mailto_emails) | set(extract.extract_mailto_emails(contact_html)))
                tel_phones = list(set(tel_phones) | set(extract.extract_tel_phones(contact_html)))
                regex_emails = list(set(regex_emails) | set(extract.extract_emails(contact_html)))
                regex_phones = list(set(regex_phones) | set(extract.extract_phones(contact_html)))

    # Emails: filter placeholders and known-bad domains (a mailto: pointing
    # at an already-blocklisted offshore/directory domain is a leaked or
    # syndicated contact, not this site's own — e.g. an embedded widget).
    mailto_emails = [
        e for e in mailto_emails
        if not extract.is_placeholder_email(e) and not extract.email_domain_is_blocked(e)
    ]
    regex_emails = [
        e for e in regex_emails
        if not extract.is_placeholder_email(e) and not extract.email_domain_is_blocked(e)
    ]
    # Generic role-based prefixes (hello@, info@, support@, etc.) get
    # dropped here too, not just at send time — otherwise they still show
    # up throughout Lead Database/exports even though Send Emails was
    # already going to refuse to send to them, which just looks like the
    # filter isn't working. A personal-looking prefix (e.g. "erica@") isn't
    # touched here, only the confirmed-generic list.
    mailto_emails = [e for e in mailto_emails if e.split("@")[0].lower() not in blocklist.GENERIC_EMAIL_PREFIXES]
    regex_emails = [e for e in regex_emails if e.split("@")[0].lower() not in blocklist.GENERIC_EMAIL_PREFIXES]
    # A regex-sourced email that doesn't even match the site's own domain has
    # proven itself to be someone else's address mentioned on the page (the
    # classic "an article lists ten other companies" pattern) — not usable,
    # unlike mailto:, a bare regex match carries no signal it belongs here.
    regex_emails = [e for e in regex_emails if extract.email_domain_matches(e, domain)]

    if mailto_emails:
        # Prefer a domain-matching mailto address if one exists, but still
        # use a non-matching one (e.g. a small business intentionally using
        # a personal Gmail) rather than discard it — mailto: is still an
        # explicit signal the site owner put it there themselves.
        matching_mailto = [e for e in mailto_emails if extract.email_domain_matches(e, domain)]
        chosen_email = extract.best_email(matching_mailto or mailto_emails)
        record["email_source"] = "mailto"
        all_emails = mailto_emails
    elif regex_emails:
        chosen_email = extract.best_email(regex_emails)
        record["email_source"] = "regex"
        all_emails = regex_emails
    else:
        chosen_email = ""
        all_emails = []

    record["email"] = chosen_email
    record["all_emails_found"] = "; ".join(all_emails)
    record["email_domain_match"] = extract.email_domain_matches(chosen_email, domain)

    # Phones: prefer tel:, filter obviously-fake patterns either way, fall
    # back to an OSM-supplied phone if the site itself doesn't have one.
    tel_phones = [p for p in tel_phones if not extract.is_suspicious_phone(p)]
    regex_phones_clean = [p for p in regex_phones if not extract.is_suspicious_phone(p)]

    if tel_phones:
        chosen_phone = tel_phones[0]
        record["phone_source"] = "tel"
    elif regex_phones_clean:
        chosen_phone = regex_phones_clean[0]
        record["phone_source"] = "regex"
    elif fallback_phone and not extract.is_suspicious_phone(fallback_phone):
        chosen_phone = fallback_phone
        record["phone_source"] = "osm"
    else:
        chosen_phone = ""

    record["phone"] = chosen_phone
    phone_suspicious = extract.is_suspicious_phone(chosen_phone) if chosen_phone else True

    location_match = area_codes.phone_matches_state(chosen_phone, state_abbr) if state_abbr else None

    record["confidence_score"], record["confidence_label"] = extract.score_lead(
        name_source=record["name_source"], company_name=record["company_name"],
        email=record["email"], email_source=record["email_source"],
        email_domain_match=record["email_domain_match"],
        phone=record["phone"], phone_source=record["phone_source"],
        phone_suspicious=phone_suspicious,
        location_match=location_match, domain=domain,
    )

    record["status"] = "ok" if (record["email"] or record["phone"]) else "no_contact_found"
    return record


def scrape_many(urls_and_names: List[Dict], max_workers: int = 8, progress_cb=None) -> List[Dict]:
    """
    urls_and_names: list of {"url": ..., "name": ..., "phone": ..., "state_abbr": ...}
    ("phone" and "state_abbr" are optional — see scrape_company).
    progress_cb: optional callable(done_count, total_count) for a UI progress bar.
    """
    total = len(urls_and_names)
    # Results are filled back into their ORIGINAL position, not appended in
    # completion order — callers zip this list against their own input list
    # to attach the search query etc., so order has to be preserved.
    results: List[Optional[Dict]] = [None] * total
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                scrape_company, item["url"], item.get("name", ""),
                item.get("phone", ""), item.get("state_abbr", ""),
            ): idx
            for idx, item in enumerate(urls_and_names)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
            done += 1
            if progress_cb:
                progress_cb(done, total)
    return results