"""
OpenStreetMap business listings via the Overpass API — completely free, no
API key, and geographically accurate since it's community-mapped place data
rather than a web search ranking. This is the highest-trust source available
without a paid API: a listing here means someone locally confirmed this
business exists at this address, not that it ranked well for some keywords.

Coverage varies by area (rural/small-town businesses are less consistently
mapped than urban ones) and phone/website/email tags are optional, so treat
this as one input among several, not a replacement for search entirely.

NOTE: this module could not be exercised against the live Overpass API from
the environment this was built in (network access was restricted to package
registries only) — the query/response handling follows Overpass's documented
JSON output format, but run a small single-state test first before relying
on it for a large batch.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

import requests

OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
REQUEST_TIMEOUT = int(os.getenv("OSM_TIMEOUT", "120"))
HEADERS = {"User-Agent": "LeadFinder/1.0 (local research tool)"}

# OSM tags to query per service. Each entry is (key, value). Dental Billing
# intentionally queries dentists (amenity=dentist is a well-mapped standard
# OSM tag) since that service's actual prospects are dental practices, not
# other billing companies — matching the same search-target logic used for
# the web-search backend.
SERVICE_OSM_TAGS: Dict[str, List[Tuple[str, str]]] = {
    "Bookkeeping": [("office", "accountant"), ("office", "tax_advisor")],
    "Accounting": [("office", "accountant"), ("office", "tax_advisor")],
    "Tax Preparation": [("office", "tax_advisor"), ("office", "accountant")],
    "Payroll Services": [("office", "accountant")],
    "CFO Advisory": [("office", "financial_advisor"), ("office", "consulting")],
    "Business Consulting": [("office", "consulting")],
    "HR Outsourcing": [("office", "employment_agency")],
    "Dental Billing": [("amenity", "dentist")],
}


def get_osm_tags_for_service(service: str) -> Optional[List[Tuple[str, str]]]:
    return SERVICE_OSM_TAGS.get(service)


def _build_query(state_abbr: str, tags: List[Tuple[str, str]]) -> str:
    filters = "\n".join(
        f'  node["{k}"="{v}"](area.searchArea);\n  way["{k}"="{v}"](area.searchArea);'
        for k, v in tags
    )
    return f"""
[out:json][timeout:{REQUEST_TIMEOUT - 15}];
area["ISO3166-2"="US-{state_abbr}"]["admin_level"="4"]->.searchArea;
(
{filters}
);
out center tags;
""".strip()


def _run_overpass(query: str) -> List[Dict]:
    """One Overpass HTTP call. Returns [] on any error rather than raising."""
    try:
        resp = requests.post(OVERPASS_ENDPOINT, data={"data": query},
                              headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    results = []
    for el in data.get("elements", []):
        el_tags = el.get("tags", {})
        name = el_tags.get("name", "").strip()
        if not name:
            continue
        phone = _clean_phone(el_tags.get("phone") or el_tags.get("contact:phone", ""))
        website = _clean_website(el_tags.get("website") or el_tags.get("contact:website", ""))
        city = el_tags.get("addr:city", "")
        results.append({"name": name, "phone": phone, "website": website, "city": city})
    return results


def _clean_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return ""
    return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"


def _clean_website(raw: str) -> str:
    url = (raw or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def query_osm_by_tags(tags: List[Tuple[str, str]], state_abbr: str) -> List[Dict]:
    """
    Core query function: takes OSM (key, value) tags directly rather than
    looking them up by service. Returns a list of {"name", "phone",
    "website", "city"} dicts, deduped by name+website.

    Issues ONE Overpass call PER TAG rather than combining every tag into a
    single query. A vertical with 2-3 tags used to run as one big query
    covering all of them — on a dense state (CA, TX) that query can return
    enough elements to hit Overpass's practical size/time limits and time
    out or get truncated, losing every tag's results at once. Splitting
    means one tag failing (or being genuinely huge) doesn't take the others
    down with it, the same partial-failure-tolerant pattern used for the
    web-search engines. Costs more requests, not more Overpass *load* per
    request — the total element count queried is the same either way.
    """
    if not tags:
        return []

    seen = set()
    results: List[Dict] = []
    for tag in tags:
        query = _build_query(state_abbr, [tag])
        for biz in _run_overpass(query):
            key = (biz["name"].lower(), biz["website"].lower())
            if key not in seen:
                seen.add(key)
                results.append(biz)
    return results


def query_osm_businesses(service: str, state_abbr: str) -> List[Dict]:
    """Convenience wrapper: looks up a service's own OSM tags and queries them."""
    tags = get_osm_tags_for_service(service)
    return query_osm_by_tags(tags or [], state_abbr)
