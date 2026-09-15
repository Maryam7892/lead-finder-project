"""Persist and dedupe collected leads. Backed by a single CSV file for simplicity."""

from __future__ import annotations

import os
import re
from typing import List, Dict

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
LEADS_CSV = os.path.join(DATA_DIR, "leads.csv")
# Every search run also archives an Excel snapshot here (see
# save_export_snapshot below) — unlike leads.csv, which a search run always
# REPLACES, this folder accumulates one file per run, so scraping and
# sending can be decoupled: Send Emails/Send Texts can pick leads from any
# past run's snapshot instead of only whatever the most recent search left
# in leads.csv. Added 2026-09-15.
EXPORTS_DIR = os.path.join(DATA_DIR, "exports")

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


def _sanitize_filename_part(text: str) -> str:
    """Collapse a label into a filename-safe token (letters/digits only) — e.g. 'Event Management' -> 'EventManagement'."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", text or "")
    return cleaned or "Unknown"


def build_export_filename(date_str: str, business_types: List[str], states: List[str], all_states: bool = False) -> str:
    """
    date_businesstypes_states.xlsx, e.g.
    '2026-09-15_EventManagement+FloralBusinesses_Alabama+Alaska.xlsx'.
    Multiple business types / states are joined with '+'. A "Search all 50
    states + DC" run collapses to 'AllStates' rather than listing all 51 —
    the full list is still in every row's own `state` column.
    """
    types_part = "+".join(_sanitize_filename_part(t) for t in business_types) or "Unknown"
    states_part = "AllStates" if all_states else ("+".join(_sanitize_filename_part(s) for s in states) or "Unknown")
    return f"{date_str}_{types_part}_{states_part}.xlsx"


def save_export_snapshot(df: pd.DataFrame, filename: str) -> str:
    """Archive a run's results as a standalone Excel file in EXPORTS_DIR. Returns the full path written."""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, filename)
    df.to_excel(path, index=False, engine="openpyxl")
    return path


def list_export_snapshots() -> List[Dict]:
    """Every archived run snapshot, newest first — filename, full path, and last-modified time."""
    if not os.path.isdir(EXPORTS_DIR):
        return []
    items = []
    for fname in os.listdir(EXPORTS_DIR):
        if not fname.lower().endswith(".xlsx"):
            continue
        path = os.path.join(EXPORTS_DIR, fname)
        items.append({"filename": fname, "path": path, "mtime": os.path.getmtime(path)})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def load_export_snapshots(filenames: List[str]) -> pd.DataFrame:
    """
    Read and combine one or more archived snapshots by filename (as returned
    by list_export_snapshots), deduped by domain like a normal search run.
    This is the "send from a saved file instead of the current database"
    path — scraping and sending don't have to happen in the same session.
    """
    frames = []
    for fname in filenames:
        path = os.path.join(EXPORTS_DIR, fname)
        if os.path.exists(path):
            frames.append(pd.read_excel(path, dtype=str, engine="openpyxl"))
    if not frames:
        return _normalize(pd.DataFrame(columns=COLUMNS))
    combined = pd.concat(frames, ignore_index=True)
    combined = _normalize(combined)
    return combined.drop_duplicates(subset=["domain"], keep="last")


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