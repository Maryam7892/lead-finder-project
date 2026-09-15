"""
Web search backend for finding candidate company websites.

Only DuckDuckGo (via the free `ddgs` package, no API key needed) is
supported here — Brave/Google Programmable Search/SerpAPI were removed since
they need paid or metered API keys. Web search is also treated as a
secondary source now: OpenStreetMap (see osm_source.py) provides more
reliable, geo-accurate business listings for free and is queried first when
enabled; this module fills in gaps and handles services OSM doesn't tag well.

Returns a plain list[dict] with keys: title, url, snippet.
"""

from __future__ import annotations

import os
import time
from typing import List, Dict, Optional, Sequence, Tuple, Union

DEFAULT_TIMEOUT = 30

SearchTarget = Optional[Union[str, Sequence[str]]]


def build_queries(service: str, state: str, search_target: SearchTarget = None,
                   cities: Optional[List[str]] = None,
                   extra_terms: Optional[List[str]] = None,
                   city_term_limit: Optional[int] = None) -> List[str]:
    """
    Build a set of query variants to widen coverage for one service/state pair.

    Normally we search for other companies offering `service` itself (e.g.
    "Bookkeeping companies in Texas"). When `search_target` names something
    that differs from `service`, we're instead searching for a *different*
    kind of prospect who needs that service (e.g. service="Dental Billing",
    search_target="dental practices") — so the query phrasing drops the
    "companies/firms/providers" framing, which would read oddly applied to
    the target noun instead of the service name.

    `search_target` accepts EITHER a single string OR a list of synonym
    phrasings for the same target — e.g. Floral Businesses passes
    ["florists and floral design businesses", "flower shops",
    "flower delivery services", "wedding florists"] so the query set covers
    how people actually describe the trade, not just one noun phrase.

    `cities`, if given, adds city-level variants alongside the whole-state
    ones — whole-state search tends to surface national/remote firms since
    "in Texas" matches any company that mentions serving Texas at all;
    city-level search ("in Austin") skews toward businesses actually local
    to that metro.

    `city_term_limit` caps how many of the synonym terms get expanded across
    EVERY city — state-level queries always use every term (cheap: 6 queries
    each), but city-level queries multiply term-count × city-count, which is
    where a large city list and several synonyms combine into thousands of
    queries. Default None = every term gets city-level expansion too (the
    original, combinatorial behavior); pass e.g. 1 to only fan the *first*
    term out across cities while every term still gets its 6 state-level
    variants. This is the single biggest lever on total query count for a
    state with a big city list.
    """
    extra_terms = extra_terms or []
    cities = cities or []

    if search_target is None:
        targets = [service]
    elif isinstance(search_target, str):
        targets = [search_target]
    else:
        targets = [t for t in search_target if t and t.strip()] or [service]

    city_targets = targets if city_term_limit is None else targets[:max(0, city_term_limit)]

    seen = set()
    queries: List[str] = []

    def _add(q: str) -> None:
        key = q.lower()
        if key not in seen:
            seen.add(key)
            queries.append(q)

    def _variants(target: str) -> Tuple[List[str], List[str]]:
        is_self = target.strip().lower() == service.strip().lower()
        if not is_self:
            base = [
                f"{target} in {state}",
                f"{target} near {state}",
                f"local {target} {state}",
                f"find {target} {state}",
                f"{target} directory {state}",
                f"{target} near me {state}",
            ]
            city_queries = [f"{target} in {city}, {state}" for city in cities]
        else:
            base = [
                f"{target} companies in {state}",
                f"{target} firms {state}",
                f"small business {target} services {state}",
                f"best {target} companies {state}",
                f"local {target} providers {state}",
                f"{target} services near me {state}",
            ]
            city_queries = [f"{target} companies in {city}, {state}" for city in cities]
        base += [f"{target} {term} {state}" for term in extra_terms]
        return base, city_queries

    # State-level queries first, for every term — cheap regardless of term count.
    for target in targets:
        base, _ = _variants(target)
        for q in base:
            _add(q)
    # City-level queries only for the allowed subset of terms.
    for target in city_targets:
        _, city_queries = _variants(target)
        for q in city_queries:
            _add(q)

    return queries


# `ddgs` is a multi-engine metasearch client (Bing/Google/Brave/Yahoo/
# Yandex/Mojeek/Wikipedia + Mullvad-proxied variants), scraping each
# engine's public results page. Defaults tuned from real runs:
#   - mullvad_* : DNS-fail on many networks -> excluded
#   - mojeek / wikipedia : slow, frequent timeouts, thin results for this
#     kind of query -> excluded from the default (still selectable via env)
#   - order matters: ddgs tries engines left-to-right, so the fastest /
#     highest-yield ones go first.
DEFAULT_BACKEND = "google,bing,brave,yahoo,yandex"
DEFAULT_DDGS_TIMEOUT = 25   # per-engine HTTP timeout, seconds
DEFAULT_RETRIES = 2         # extra attempts when *every* engine fails


def _backend() -> str:
    return os.getenv("SEARCH_ENGINE_BACKEND", "").strip() or DEFAULT_BACKEND


def search_ddgs(query: str, max_results: int = 15,
                timeout: Optional[int] = None, retries: Optional[int] = None) -> List[Dict]:
    """
    Run one query across the configured engines. On a total failure (every
    engine errored/timed out) it retries up to `retries` times with a short
    backoff before giving up — most of these failures are transient network
    timeouts or a momentary rate-limit, not a hard block, so a retry
    usually clears them. Returns a single `__ERROR__` row only if every
    attempt fails.
    """
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException

    backend = _backend()
    timeout = timeout if timeout is not None else int(os.getenv("SEARCH_TIMEOUT", str(DEFAULT_DDGS_TIMEOUT)))
    retries = retries if retries is not None else int(os.getenv("SEARCH_RETRIES", str(DEFAULT_RETRIES)))

    last_err = "unknown error"
    for attempt in range(retries + 1):
        try:
            results = []
            with DDGS(timeout=timeout) as ddgs:
                for r in ddgs.text(query, max_results=max_results, region="us-en", backend=backend):
                    results.append({
                        "title": r.get("title", ""),
                        "url": r.get("href") or r.get("url", ""),
                        "snippet": r.get("body", ""),
                    })
            return results  # includes a legitimately-empty result set
        except DDGSException as e:
            last_err = str(e)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(2.0 * (attempt + 1))

    return [{"title": "", "url": "", "snippet":
             f"__ERROR__:All search engines failed after {retries + 1} tries — {last_err}"}]


def run_search(query: str, backend: str = "ddgs", max_results: int = 15, pause: float = None) -> List[Dict]:
    """
    Dispatch to the search backend, with a pause beforehand to look less
    automated to the free endpoints. Pause default 2.5s, configurable via
    SEARCH_QUERY_DELAY. Raising it helps avoid *triggering* new
    rate-limiting; it won't lift a block already placed on an IP.
    """
    if pause is None:
        pause = float(os.getenv("SEARCH_QUERY_DELAY", "2.5"))
    time.sleep(pause)
    return search_ddgs(query, max_results)