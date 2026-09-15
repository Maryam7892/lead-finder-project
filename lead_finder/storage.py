"""Persist and dedupe collected leads. Backed by a single CSV file for simplicity."""

from __future__ import annotations

import os
from typing import List, Dict

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
LEADS_CSV = os.path.join(DATA_DIR, "leads.csv")

COLUMNS = [
    "company_name", "website", "domain", "phone", "email",
    "all_emails_found", "status", "service", "target_vertical", "state", "search_query",
    "name_source", "email_source", "email_domain_match",
    "phone_source", "confidence_score", "confidence_label",
    "owner_name", "linkedin_url",
]

# Empty cells round-trip through CSV as NaN, not "". Left unhandled, that
# breaks any "does this field have a value" check downstream — str(NaN) is
# the 3-character string "nan", which passes a naive `.str.len() > 0` filter.
# Every column gets normalized to a real, consistent empty value on load.
_TEXT_COLUMNS = [
    "company_name", "website", "domain", "phone", "email", "all_emails_found",
    "status", "service", "target_vertical", "state", "search_query", "name_source",
    "email_source", "phone_source", "owner_name", "linkedin_url",
]


def ensure_store():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(LEADS_CSV):
        pd.DataFrame(columns=COLUMNS).to_csv(LEADS_CSV, index=False)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ""
    for col in _TEXT_COLUMNS:
        df[col] = df[col].fillna("").astype(str)
        # A stray literal "nan"/"none" string (from an earlier buggy save) is
        # just as empty as a real NaN — normalize those too.
        df[col] = df[col].where(~df[col].str.lower().isin(["nan", "none"]), "")
    df["email_domain_match"] = df["email_domain_match"].map(
        lambda v: str(v).strip().lower() == "true" if pd.notna(v) else False
    )
    df["confidence_score"] = pd.to_numeric(df["confidence_score"], errors="coerce").fillna(0.0)
    df["confidence_label"] = df["confidence_label"].replace("", "Unrated")
    return df[COLUMNS]


def load_leads() -> pd.DataFrame:
    """Load saved leads, upgrading older CSVs and normalizing empty values."""
    ensure_store()
    df = pd.read_csv(LEADS_CSV, dtype=str)
    return _normalize(df)


def save_leads(new_rows: List[Dict], replace: bool = False) -> pd.DataFrame:
    """
    Persist leads, deduped by domain. Returns the resulting table.

    replace=True  -> the database becomes EXACTLY these rows; anything
                     previously stored is discarded. This is what a search
                     run uses: the app only ever shows the current run's
                     results, never an accumulation across past runs.
    replace=False -> merge these rows into the existing database by domain
                     (keep last). Used only for in-place updates of rows
                     that are already there, e.g. the browser re-scrape
                     retry — it must not wipe the rest of the run.
    """
    ensure_store()
    new_df = _normalize(pd.DataFrame(new_rows))
    if replace:
        combined = new_df
    else:
        combined = pd.concat([load_leads(), new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["domain"], keep="last")
    combined.to_csv(LEADS_CSV, index=False)
    return combined


def clear_leads():
    ensure_store()
    pd.DataFrame(columns=COLUMNS).to_csv(LEADS_CSV, index=False)


def rescan_confidence(df: pd.DataFrame) -> pd.DataFrame:
    """
    Recompute confidence_score/confidence_label (and email_domain_match) for
    already-stored leads using the current scoring/blocklist rules, without
    re-scraping any sites. This is what lets an existing leads.csv benefit
    from filtering improvements made after it was collected — e.g. a new
    blocklist entry, a fixed listicle-name pattern, or stricter scoring —
    without needing to re-run the original search.

    This also re-judges the chosen email itself, not just its score:
    - a mailto: address whose domain is now a known-bad site (e.g. a
      leaked/syndicated contact pointing at an offshore BPO under an
      unrelated company's identity)
    - a regex-sourced email that never matched the site's own domain in
      the first place (proven noise — someone else's address mentioned on
      the page)
    - a **syntactically invalid** address (added 2026-09-14, after a real
      site's mailto: href turned out to be a page-builder typo —
      `mailto:emailto:@domain.com` — with no real local part, accepted
      verbatim before `extract_mailto_emails` started validating against
      `EMAIL_RE`; this catches any row collected before that fix)
    - a **generic/shared-inbox prefix** now in `blocklist.GENERIC_EMAIL_PREFIXES`
      that wasn't when the row was first collected (e.g. "hi@", "contact@",
      "wecare@" were added 2026-09-14 after showing up unfiltered — this
      lets an existing CSV benefit without re-scraping)
    - an address `extract.is_placeholder_email` now rejects (added
      2026-09-15: this check runs at scrape time but was missing here,
      so a placeholder rule added after a row was collected e.g. the
      32-hex-char/`sentry.*` auto-tracking-email pattern never
      retroactively applied until now)
    all get dropped. If `all_emails_found` has another candidate that
    passes every check, that's used instead.

    It only re-judges fields already on disk; it can't discover a phone or
    email a row never had. Re-run a search for that.
    """
    from . import extract, area_codes, blocklist
    from .states import US_STATES

    def _passes_email_checks(e: str) -> bool:
        if not e or not extract.EMAIL_RE.fullmatch(e):
            return False
        if extract.is_placeholder_email(e):
            return False
        if extract.email_domain_is_blocked(e):
            return False
        if e.split("@")[0].lower() in blocklist.GENERIC_EMAIL_PREFIXES:
            return False
        return True

    def _rescan_row(row):
        state_abbr = US_STATES.get(row.get("state", ""), "")
        domain = row.get("domain", "")
        email = row.get("email", "")
        email_source = row.get("email_source", "")
        phone = row.get("phone", "")
        all_emails = [e.strip() for e in str(row.get("all_emails_found", "")).split(";") if e.strip()]

        # Re-judge the chosen email against current rules.
        email_is_bad = bool(email) and (
            not _passes_email_checks(email)
            or (email_source == "regex" and not extract.email_domain_matches(email, domain))
        )
        if email_is_bad:
            # Try to recover a same-domain, non-blocked, non-generic
            # replacement from the full candidate list before giving up on
            # having an email at all.
            replacement_candidates = [
                e for e in all_emails
                if e != email and _passes_email_checks(e)
                and extract.email_domain_matches(e, domain)
            ]
            email = extract.best_email(replacement_candidates) if replacement_candidates else ""
            email_source = "regex" if email else ""

        email_domain_match = extract.email_domain_matches(email, domain) if email else False
        phone_suspicious = extract.is_suspicious_phone(phone) if phone else True
        location_match = area_codes.phone_matches_state(phone, state_abbr) if (phone and state_abbr) else None

        score, label = extract.score_lead(
            name_source=row.get("name_source", ""), company_name=row.get("company_name", ""),
            email=email, email_source=email_source,
            email_domain_match=email_domain_match,
            phone=phone, phone_source=row.get("phone_source", ""),
            phone_suspicious=phone_suspicious, location_match=location_match,
            domain=domain,
        )
        owner_name = extract.revalidate_owner_name(row.get("owner_name", ""))

        return pd.Series({
            "email": email, "email_source": email_source,
            "confidence_score": score, "confidence_label": label,
            "email_domain_match": email_domain_match,
            "status": "ok" if (email or phone) else "no_contact_found",
            "owner_name": owner_name,
        })

    if df.empty:
        return df

    df = df.copy()
    updated = df.apply(_rescan_row, axis=1)
    df["email"] = updated["email"]
    df["email_source"] = updated["email_source"]
    df["confidence_score"] = updated["confidence_score"]
    df["confidence_label"] = updated["confidence_label"]
    df["email_domain_match"] = updated["email_domain_match"]
    df["status"] = updated["status"]
    df["owner_name"] = updated["owner_name"]
    return df


def rescan_and_save() -> pd.DataFrame:
    df = load_leads()
    df = rescan_confidence(df)
    ensure_store()
    df.to_csv(LEADS_CSV, index=False)
    return df