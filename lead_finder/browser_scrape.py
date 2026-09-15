"""
Browser-based re-scrape fallback, for leads where the normal HTTP scrape
(scrape.py) found a website but came up with no email at all. Some sites
render their navigation, contact-page link, or the contact details
themselves via JavaScript after the initial page load — a plain
requests.get() only sees the raw HTML before that JS runs, so it can
genuinely miss content a real browser would see.

This uses Playwright to load the page in a real (headless) browser, wait
for it to finish rendering, then runs the exact same extraction pipeline
(scrape.scrape_company, via html_override / contact_page_fetcher) against
the fully-rendered HTML — no separate/duplicated extraction logic here, so
this can't drift out of sync with the main scraper.

Requires the `playwright` package AND its browser binaries:
    pip install playwright
    playwright install chromium

This is a genuinely heavier fallback — loading a page and waiting for it to
render takes real seconds, versus a fraction of a second for a plain HTTP
request — so it's meant to run on a specific batch of leads (an automatic
capped pass after each search, or a manual batch you pick), not as the
default scraping method for every search.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from . import scrape

PAGE_TIMEOUT_MS = 70000
# How long to let a page settle after DOM-content-loaded before reading it,
# instead of waiting for full network idle (see _fetch_rendered_html).
JS_SETTLE_MS = 1500


def is_available() -> bool:
    """True if playwright is installed and its browser binaries are present."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


def _fetch_rendered_html(page, url: str) -> Optional[str]:
    """
    Loads `url` into an ALREADY-OPEN browser page/tab (see
    rescrape_many_with_browser — one browser + one page is opened once per
    batch and reused for every lead, not relaunched per site) and returns
    the resulting HTML.

    Waits for `domcontentloaded` + a short fixed settle time
    (JS_SETTLE_MS), not full `networkidle`. `networkidle` (no network
    activity for 500ms) is the more "complete" wait, but sites with
    persistent background connections (chat widgets, analytics, ad
    trackers — common on small-business sites) can hold it open for the
    entire timeout even though the actual contact info rendered almost
    immediately. `domcontentloaded` returns as soon as the DOM itself is
    parsed; the extra fixed wait covers typical JS-driven content
    (lazy-loaded footers, cookie-consent-gated sections) without paying for
    the slowest network connection on the page. Trade-off: a page whose
    contact info renders even later than JS_SETTLE_MS after DOM-load could
    still be missed — same class of risk `networkidle` already had for
    pages that never go fully idle, just shifted to a different failure
    mode, and much faster on the common case.
    """
    try:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(JS_SETTLE_MS)
        except Exception:
            # A slow/hanging load — fall back to whatever rendered so far.
            pass
        return page.content()
    except Exception:
        return None


def rescrape_with_browser(page, url: str, fallback_name: str = "", fallback_phone: str = "",
                           state_abbr: str = "") -> Optional[Dict]:
    """
    Re-scrapes one company's site with a real browser page instead of a
    plain HTTP request. `page` is a live Playwright page the caller already
    opened (see rescrape_many_with_browser) — this function only navigates
    it, it never launches its own browser. Returns an updated record dict
    in the same shape as scrape.scrape_company, or None if the browser
    fetch itself failed entirely — a None return means "couldn't even try",
    distinct from a record that tried and still found no contact info.
    """
    html = _fetch_rendered_html(page, url)
    if html is None:
        return None

    return scrape.scrape_company(
        url, fallback_name=fallback_name, fallback_phone=fallback_phone, state_abbr=state_abbr,
        html_override=html, contact_page_fetcher=lambda u: _fetch_rendered_html(page, u),
    )


def rescrape_many_with_browser(leads: List[Dict], progress_cb: Optional[Callable] = None) -> List[Dict]:
    """
    leads: list of lead dicts (the row shape used throughout the app) —
    each needs at least "website", "company_name", "phone", "state".
    Returns each lead with its record fields updated wherever the browser
    re-scrape found something new; leads where the browser fetch failed
    entirely, or found nothing better than what was already there, are
    returned unchanged (never made worse than the original).

    Launches exactly ONE browser (and reuses one page/tab, navigated to
    each lead's URL in turn) for the WHOLE batch, instead of the previous
    launch-a-browser-per-lead approach. A full Chromium launch is the
    expensive part of this pipeline (much more than loading one more page
    in an already-running browser) — for a batch of N leads this cuts N
    browser launches down to 1.
    """
    if not leads:
        return []

    results: List[Dict] = []
    total = len(leads)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_default_timeout(PAGE_TIMEOUT_MS)
                for i, lead in enumerate(leads):
                    updated = rescrape_with_browser(
                        page, lead.get("website", ""), fallback_name=lead.get("company_name", ""),
                        fallback_phone=lead.get("phone", ""), state_abbr=lead.get("state_abbr", ""),
                    )
                    if updated and (updated.get("email") or updated.get("phone")):
                        merged = dict(lead)
                        merged.update({k: v for k, v in updated.items() if v})
                        results.append(merged)
                    else:
                        results.append(lead)
                    if progress_cb:
                        progress_cb(i + 1, total, results[-1])
            finally:
                browser.close()
    except Exception:
        # Playwright itself failed (not installed, binary missing, launch
        # error) — return whatever was already processed plus the rest
        # unchanged, same "never worse than the original" guarantee as a
        # per-lead failure.
        results.extend(leads[len(results):])
    return results
