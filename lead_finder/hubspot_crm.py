"""
HubSpot CRM integration — pushes leads into HubSpot as Contacts, so they
can be tracked through a real pipeline (New Lead -> Contacted -> Replied ->
Meeting Booked -> Proposal -> Won/Lost) using HubSpot's own UI, rather than
just sitting in our CSV.

Auth is a Private App access token (HubSpot deprecated API keys in 2022) —
create one under the HubSpot account's Settings -> Integrations -> Private
Apps, with crm.objects.contacts.read/write and crm.schemas.contacts.write
scopes, then put the token in .env as HUBSPOT_ACCESS_TOKEN.

IMPORTANT — HubSpot's free tier caps at 1,000 contacts and 2,000 marketing
emails per month. If a single search run finds more leads than that, pushing
everything to HubSpot will hit that ceiling fast; this module doesn't try to
work around that, since going over means upgrading the HubSpot plan, not a
code problem to solve here.

Nothing here pushes anything unless push_lead/push_leads_batch is
explicitly called — loading this module does not touch your HubSpot account.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Dict, List, Optional, Tuple

import requests

HUBSPOT_API_BASE = "https://api.hubapi.com"
REQUEST_TIMEOUT = 40

# Custom properties this integration needs on the Contact object, auto-
# created on first use if they don't already exist. Kept to a small, high-
# value subset of the fuller property list (Industry, Size, Source, Status,
# Last Contacted, Sequence Step, Reply Status) — enough to filter/segment
# on on HubSpot's side without a large setup step.
CUSTOM_PROPERTIES = [
    {"name": "lead_finder_target_vertical", "label": "Lead Finder: Target Vertical",
     "type": "string", "fieldType": "text", "groupName": "contactinformation"},
    {"name": "lead_finder_service", "label": "Lead Finder: Service",
     "type": "string", "fieldType": "text", "groupName": "contactinformation"},
    {"name": "lead_finder_confidence", "label": "Lead Finder: Confidence",
     "type": "string", "fieldType": "text", "groupName": "contactinformation"},
    {"name": "lead_finder_source", "label": "Lead Finder: Source",
     "type": "string", "fieldType": "text", "groupName": "contactinformation"},
]


# ------------------------------------------------------------------ config --
def get_hubspot_config() -> Optional[Dict]:
    """Read HubSpot settings from environment. Returns None if incomplete."""
    token = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        return None
    return {"token": token}


def _headers(config: Dict) -> Dict:
    return {"Authorization": f"Bearer {config['token']}", "Content-Type": "application/json"}


# --------------------------------------------------------- custom properties --
def ensure_custom_properties(config: Dict) -> Tuple[bool, str]:
    """
    Creates the custom contact properties this integration uses, if they
    don't already exist. Safe to call repeatedly — an already-existing
    property just returns a 409, which is treated as success here, not an
    error. Call this once (e.g. before the first push in a session) rather
    than before every single contact.
    """
    errors = []
    for prop in CUSTOM_PROPERTIES:
        try:
            resp = requests.post(
                f"{HUBSPOT_API_BASE}/crm/v3/properties/contacts",
                headers=_headers(config), json=prop, timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code not in (200, 201, 409):
                errors.append(f"{prop['name']}: HTTP {resp.status_code} {resp.text[:150]}")
        except Exception as e:
            errors.append(f"{prop['name']}: {e}")
    if errors:
        return False, "; ".join(errors)
    return True, ""


# -------------------------------------------------------------------- push --
def _lead_to_properties(lead: Dict) -> Dict:
    props = {
        "email": lead.get("email", ""),
        "company": lead.get("company_name", ""),
        "lifecyclestage": "lead",
        "lead_finder_target_vertical": lead.get("target_vertical", ""),
        "lead_finder_service": lead.get("service", ""),
        "lead_finder_confidence": lead.get("confidence_label", ""),
        "lead_finder_source": "Lead Finder App",
    }
    if lead.get("phone"):
        props["phone"] = lead["phone"]
    if lead.get("owner_name"):
        # HubSpot has no single "full name" property — split best-effort
        # into first/last so it shows up properly in the contact record.
        parts = lead["owner_name"].split(maxsplit=1)
        props["firstname"] = parts[0]
        if len(parts) > 1:
            props["lastname"] = parts[1]
    return {k: v for k, v in props.items() if v}


def push_lead(config: Dict, lead: Dict) -> Tuple[bool, str]:
    """
    Creates a Contact in HubSpot for this lead, or updates the existing one
    if a contact with this email already exists (HubSpot contacts are keyed
    by email; a lead with no email can't be pushed this way). Returns
    (success, error_message).
    """
    email = (lead.get("email") or "").strip()
    if not email:
        return False, "No email on this lead — HubSpot contacts require one."

    properties = _lead_to_properties(lead)
    try:
        resp = requests.post(
            f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts",
            headers=_headers(config), json={"properties": properties}, timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code in (200, 201):
            return True, ""
        if resp.status_code == 409:
            # Already exists — update it instead of erroring out.
            update_resp = requests.patch(
                f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/{email}?idProperty=email",
                headers=_headers(config), json={"properties": properties}, timeout=REQUEST_TIMEOUT,
            )
            if update_resp.status_code == 200:
                return True, ""
            return False, f"HTTP {update_resp.status_code}: {update_resp.text[:300]}"
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        return False, str(e)


def push_leads_batch(config: Dict, leads: List[Dict], delay_seconds: float = 0.5,
                      progress_cb: Optional[Callable] = None) -> List[Dict]:
    """
    leads: list of lead dicts (the same row shape used throughout the app).
    Returns each lead's dict with "success" and "error" added. A short
    delay between calls is polite to HubSpot's rate limits, not strictly
    required at small volume.
    """
    results = []
    total = len(leads)
    for i, lead in enumerate(leads):
        success, error = push_lead(config, lead)
        result = dict(lead, success=success, error=error)
        results.append(result)
        if progress_cb:
            progress_cb(i + 1, total, result)
        if i < total - 1:
            time.sleep(delay_seconds)
    return results