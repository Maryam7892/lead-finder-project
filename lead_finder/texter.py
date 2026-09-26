"""
Text message sending via 8x8's SMS API (https://developer.8x8.com/connect/reference/send-sms-single).

Uses the client's existing 8x8 account/app directly — no rented number, no
separate device required, just an API key and subaccount ID from the 8x8
account itself. Plain REST over `requests` (already a dependency here); no
official 8x8 Python SDK exists on PyPI, so this is a direct integration
against their documented API rather than a third-party wrapper.

(SMS Gateway/own-phone support was removed after switching to 8x8 — the
client already has an active 8x8 account/app, so there's no reason to also
require a spare Android phone. If a different backend is ever needed again,
mailer.py's structure is the template to follow.)

Mirrors mailer.py's structure deliberately — rate-limited batch sending, a
sent-log to avoid texting the same lead twice, and the same suppression
list emails already check.
"""

from __future__ import annotations

import os
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd
import requests

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
SENT_TEXT_LOG_PATH = os.path.join(DATA_DIR, "sent_text_log.csv")

SENT_TEXT_LOG_COLUMNS = ["phone", "company_name", "service", "variant", "status", "error", "sent_at"]

EIGHTXEIGHT_API_BASE = "https://sms.8x8.com/api/v1"
REQUEST_TIMEOUT = 40


# ------------------------------------------------------------------ config --
def get_eightxeight_config() -> Optional[Dict]:
    """
    Read 8x8 settings from environment. Both the API key and subaccount ID
    come from the client's own 8x8 Connect account (developer.8x8.com →
    API keys / subaccounts) — not something to invent, just copy what's
    shown there. Returns None if incomplete.
    """
    api_key = os.getenv("EIGHTXEIGHT_API_KEY", "")
    subaccount_id = os.getenv("EIGHTXEIGHT_SUBACCOUNT_ID", "")
    if not (api_key and subaccount_id):
        return None
    return {
        "api_key": api_key,
        "subaccount_id": subaccount_id,
        # Optional alphanumeric/numeric Sender ID shown as the "from" on the
        # recipient's phone. Some 8x8 accounts require this, others infer a
        # default — leave blank to use the account's default.
        "source": os.getenv("EIGHTXEIGHT_SOURCE", "").strip(),
    }


def get_active_texting_config() -> Optional[Dict]:
    """Kept as the entry point app.py calls, for a stable interface even though there's one backend."""
    return get_eightxeight_config()


# ------------------------------------------------------------- formatting --
def to_e164(raw_phone: str, default_country_code: str = "1") -> Optional[str]:
    """
    Best-effort conversion to E.164 format (+15124325029), which 8x8's API
    expects for the destination field. Numbers already starting with + are
    assumed correct and passed through digit-cleaned. A bare 10-digit US
    number gets the default country code (1) prepended. Returns None if it
    doesn't look like a usable number at all.
    """
    raw = (raw_phone or "").strip()
    if raw.startswith("+"):
        digits = re.sub(r"\D", "", raw)
        return f"+{digits}" if digits else None

    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+{default_country_code}{digits}"
    if len(digits) == 11 and digits.startswith(default_country_code):
        return f"+{digits}"
    if 8 <= len(digits) <= 15:
        # Not clearly US-format, but plausibly a valid international number
        # missing its leading +. Better to attempt than silently drop it.
        return f"+{digits}"
    return None


# -------------------------------------------------------------------- send --
def send_text(config: Dict, to_phone: str, body: str) -> Tuple[bool, str]:
    """Send one text message via 8x8. Returns (success, error_message)."""
    to_e164_number = to_e164(to_phone)
    if not to_e164_number:
        return False, f"Could not parse '{to_phone}' as a sendable phone number"

    url = f"{EIGHTXEIGHT_API_BASE}/subaccounts/{config['subaccount_id']}/messages"
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {"destination": to_e164_number, "text": body}
    if config.get("source"):
        payload["source"] = config["source"]

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return True, ""
        # 400/401/426/500 are the documented error codes — surface the
        # actual response body so a real cause (bad auth, bad number
        # format, etc.) is visible instead of a generic failure.
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        return False, str(e)


def send_text_batch(config: Dict, jobs: List[Dict], delay_seconds: float = 3.0,
                     progress_cb: Optional[Callable] = None,
                     should_stop: Optional[Callable[[], bool]] = None) -> List[Dict]:
    """
    jobs: list of {"to": str, "body": str, ...any extra keys you want echoed
    back in the result, e.g. "domain", "company_name"}.
    Returns each job dict with "success" and "error" added.

    should_stop: see mailer.send_batch's docstring — same mechanism,
    checked before every send and in 0.5s increments during the delay.
    """
    results = []
    total = len(jobs)
    for i, job in enumerate(jobs):
        if should_stop and should_stop():
            break
        success, error = send_text(config, job["to"], job["body"])
        result = dict(job, success=success, error=error)
        results.append(result)
        if progress_cb:
            progress_cb(i + 1, total, result)
        if i < total - 1:
            remaining = delay_seconds
            while remaining > 0:
                if should_stop and should_stop():
                    break
                chunk = min(0.5, remaining)
                time.sleep(chunk)
                remaining -= chunk
            if should_stop and should_stop():
                break
    return results


# --------------------------------------------------------------- sent log --
def _ensure_sent_text_log():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(SENT_TEXT_LOG_PATH):
        pd.DataFrame(columns=SENT_TEXT_LOG_COLUMNS).to_csv(SENT_TEXT_LOG_PATH, index=False)
        return
    # Migrate an older log missing a newer column (e.g. "variant", added
    # 2026-09-18) — rewrite its header/rows with the new column added
    # (blank for existing rows) BEFORE any append happens. Without this,
    # record_sent_text's mode="a" append would silently misalign columns
    # against the file's still-old header row.
    existing = pd.read_csv(SENT_TEXT_LOG_PATH, dtype=str)
    if list(existing.columns) != SENT_TEXT_LOG_COLUMNS:
        for col in SENT_TEXT_LOG_COLUMNS:
            if col not in existing.columns:
                existing[col] = ""
        existing[SENT_TEXT_LOG_COLUMNS].to_csv(SENT_TEXT_LOG_PATH, index=False)


def load_sent_text_log() -> pd.DataFrame:
    _ensure_sent_text_log()
    # dtype=str is essential here: pandas' automatic type inference parses a
    # "+15125550101"-style value as an integer, silently dropping the +.
    df = pd.read_csv(SENT_TEXT_LOG_PATH, dtype=str)
    for col in SENT_TEXT_LOG_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df.fillna("")


def record_sent_text(phone: str, company_name: str, service: str, status: str, error: str = "",
                      variant: str = "") -> None:
    _ensure_sent_text_log()
    row = pd.DataFrame([{
        "phone": phone, "company_name": company_name, "service": service, "variant": variant,
        "status": status, "error": error, "sent_at": pd.Timestamp.utcnow().isoformat(),
    }])
    row.to_csv(SENT_TEXT_LOG_PATH, mode="a", header=False, index=False)


def already_texted_numbers(status_filter: str = "sent") -> set:
    """Keyed by phone (not domain, unlike email) since that's what a text is sent to."""
    df = load_sent_text_log()
    if status_filter:
        df = df[df["status"] == status_filter]
    return set(df["phone"].dropna().astype(str))