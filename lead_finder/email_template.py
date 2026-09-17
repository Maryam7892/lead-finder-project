"""
Per-service outreach email templates, each built around a simple three-beat
pitch: what the prospect is dealing with right now, what we actually do
about it, and what they get at the end. The beats aren't explicitly labeled
in the copy itself (no "(that's the input)" asides) — they're just how each
message is paragraphed, which reads far more like something a person wrote.

Deliberately does NOT send anything by itself — render_for_lead only
produces text/HTML you can hand to mailer.send_email, or paste elsewhere.

Templates are keyed by service name (matching lead_finder.states.SERVICE_PRESETS)
so the dashboard can auto-switch the visible template when the sidebar's
service selector changes. Edits are saved per-service to a small JSON file
so they persist across app restarts without templates for other services
overwriting each other. Only the core message is stored/edited here — the
signature and legal footer are appended automatically at render time from
.env, so they can't drift out of sync across services or get accidentally
edited away.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
from typing import Dict, Tuple

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
CUSTOM_TEMPLATES_PATH = os.path.join(DATA_DIR, "custom_email_templates.json")

FIELD_HELP = ["company_name", "service", "service_lower", "state", "phone", "email", "website"]

SERVICE_TEMPLATES: Dict[str, str] = {
    # Word-for-word identical to data/custom_email_templates.json's saved
    # override on purpose — that JSON file is checked first and wins
    # whenever it has content for a service, so this built-in copy only
    # matters as the fallback if that file is ever missing/corrupted. They
    # must stay in sync, or whichever one loads determines what real
    # companies receive.
    #
    # Deliberately only one service. The other 7 (Accounting, Tax
    # Preparation, Payroll Services, CFO Advisory, Business Consulting, HR
    # Outsourcing, Dental Billing) used to be here, and it caused a real
    # incident: any lead in the database whose stored `service` field was
    # something other than "Bookkeeping" (stale rows from testing months of
    # iteration, before the service list was locked to Bookkeeping-only)
    # fell through to that service's full default template — including
    # Dental Billing's pitch going out to a company being contacted for
    # bookkeeping. Only ever add another service here at the same time it's
    # added to ACTIVE_SERVICES in states.py, not before.
    "Bookkeeping": """Subject: Still doing your own bookkeeping?

Hi,

**You didn't start your business to spend your weekends stressing over receipts and spreadsheets.** But as a business grows, bookkeeping can quickly become another task competing for your time. 

At Profit Path Solution, we take bookkeeping completely off your plate keeping your records organized, accounts reconciled, and financials up to date so you have a clear picture of your business.

Recently, we helped a business owner who was six months behind on their books. **Within 30 days, we brought their books up to date, cleaned up their records, and gave them back 15 hours a week to focus on their business.**

I'd be happy to show you how we approach bookkeeping and see whether we could do the same for you.

**Would a quick 15-minute conversation be worthwhile?** 

Choose a time that works for you:
https://calendly.com/profitpathsolution/15min""",
}

GENERIC_TEMPLATE = """Subject: A quicker way to handle {service_lower} at {company_name}

Hi {greeting_name},

What usually gets in the way is {service_lower} handled with manual, ad hoc processes, inconsistent results, and never quite enough internal bandwidth to do it properly.

We take {service_lower} on end to end, built around how {company_name} already operates, with a single clear point of contact throughout.

What changes for you is {service_lower} handled reliably, so your team spends less time managing it and more time on what it's actually for.

Worth a quick call to see if it's a fit?"""


def get_default_for_service(service: str) -> str:
    return SERVICE_TEMPLATES.get(service, GENERIC_TEMPLATE)


# Kept for any code that still references a single default (e.g. a first-run
# fallback with no service selected yet).
DEFAULT_TEMPLATE = SERVICE_TEMPLATES["Bookkeeping"]


def _merge_fields(lead: Dict) -> Dict[str, str]:
    owner_name = lead.get("owner_name", "") or ""
    # First name only for a natural "Hi Jane," greeting, falling back to the
    # generic "Hi {company} team," when no owner was found on their site.
    owner_first_name = owner_name.split()[0] if owner_name else ""
    greeting_name = owner_first_name if owner_first_name else f"{lead.get('company_name', 'there')} team"
    return {
        "company_name": lead.get("company_name", "there"),
        "service": lead.get("service", "your industry"),
        "service_lower": str(lead.get("service", "your industry")).lower(),
        "state": lead.get("state", "your area"),
        "phone": lead.get("phone", ""),
        "email": lead.get("email", ""),
        "website": lead.get("website", ""),
        "owner_name": owner_name,
        "greeting_name": greeting_name,
    }


def render_template(template: str, lead: Dict) -> str:
    """Renders just the core message (no signature/footer) with merge fields substituted."""
    fields = _merge_fields(lead)
    try:
        return template.format(**fields)
    except KeyError as e:
        return f"Template error: missing field {e}. Available fields: {', '.join(fields.keys())}"


def _split_subject(rendered: str, fallback_company: str) -> Tuple[str, str]:
    lines = rendered.split("\n")
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0][len("Subject:"):].strip()
        body = "\n".join(lines[1:]).lstrip("\n")
    else:
        subject = f"A note for {fallback_company}"
        body = rendered
    return subject, body


def _render_bold_html(text: str) -> str:
    """Converts **bold** markdown segments to <strong> tags, escaping everything else."""
    parts = re.split(r'(\*\*.+?\*\*)', text)
    out = []
    for part in parts:
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append(f"<strong>{html_lib.escape(part[2:-2])}</strong>")
        else:
            out.append(html_lib.escape(part))
    return "".join(out)

def _strip_markdown_bold(text: str) -> str:
    """Removes ** markers for the plain-text version, where bold can't render."""
    return text.replace("**", "")

def _text_to_html_paragraphs(text: str) -> str:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "".join(f"<p>{_render_bold_html(p)}</p>" for p in paragraphs)


def render_for_lead(lead: Dict) -> Tuple[str, str]:
    """
    Render using *this lead's own* service template (not whatever template
    happens to be open in the editor) — so a batch of mixed-service leads
    each gets the pitch that actually matches what they were found under.
    Returns (subject, plain_body) with the signature already appended. For
    the HTML version (with the real embedded signature images), see
    render_html_for_lead.
    """
    from . import mailer  # local import: avoids any import-order issues at module load

    tpl = load_template(lead.get("service", "") or "Bookkeeping", lead.get("target_vertical", ""))
    rendered_core = render_template(tpl, lead)
    subject, body = _split_subject(rendered_core, lead.get("company_name", "you"))
    body = _strip_markdown_bold(body)

    signature = mailer.build_signature_text(mailer.get_sender_info())
    full_body = f"{body}\n\nBest,\n{signature}"
    return subject, full_body


def render_html_for_lead(lead: Dict) -> str:
    """
    HTML counterpart to render_for_lead — same core message, wrapped in
    basic paragraph markup, with the real signature (name/title/contact
    details plus the two embedded images) appended instead of the plain
    text version. Pair with mailer.get_signature_inline_images() when
    actually sending, so the cid: image references in this HTML resolve.
    """
    from . import mailer

    tpl = load_template(lead.get("service", "") or "Bookkeeping", lead.get("target_vertical", ""))
    rendered_core = render_template(tpl, lead)
    _, body = _split_subject(rendered_core, lead.get("company_name", "you"))

    body_html = _text_to_html_paragraphs(body)
    signature_html = mailer.build_signature_html(mailer.get_sender_info())
    return (
        '<div style="font-family: Arial, Helvetica, sans-serif; font-size: 14px; '
        'line-height: 1.5; color: #222222;">'
        f"{body_html}"
        "<p>Best,</p>"
        f'<div style="margin-top: 4px;">{signature_html}</div>'
        "</div>"
    )


def render_html_preview_for_lead(lead: Dict) -> str:
    """
    Same as render_html_for_lead, but with the signature images embedded as
    base64 data URIs instead of cid: references — for showing a true
    in-app preview (Streamlit can't resolve cid: outside a real email).
    The actual sent email still uses proper cid: attachments via
    mailer.send_email, which is the correct approach for real sending
    (smaller message, more consistent client support than inline base64).
    """
    import base64

    from . import mailer

    html_body = render_html_for_lead(lead)
    for cid, path in mailer.get_signature_inline_images().items():
        if os.path.exists(path):
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            html_body = html_body.replace(f"cid:{cid}", f"data:image/png;base64,{b64}")
    return html_body


def _load_overrides() -> Dict[str, str]:
    if os.path.exists(CUSTOM_TEMPLATES_PATH):
        try:
            with open(CUSTOM_TEMPLATES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


# Vertical-specific pitches, keyed by target_vertical label (see
# states.py's BOOKKEEPING_TARGET_VERTICALS). When a lead's own
# target_vertical has an entry here, it takes priority over the
# service-level template — e.g. a lead found under "Event Management"
# gets this instead of the generic Bookkeeping pitch, since a pitch that
# mentions vendor payments and client deposits by name converts better
# than a generic one. Built-in only for now, not yet editable from the
# Email Template tab the way service-level templates are.
VERTICAL_TEMPLATES: Dict[str, str] = {
    "Event Management": """Subject: A clearer picture of your event management business finances

Hi,

You didn't start your event management company to spend your weekends stressing over receipts, spreadsheets, and accounting software. But as your events grow, keeping track of vendor payments, client deposits, invoices, and expenses across multiple events can quickly become another task competing for your time.

And when those numbers aren't properly organized and reconciled, it can be difficult to see what each event actually made, where your money is going, and what your cash flow looks like.

At Profit Path Solution, we take bookkeeping completely off your plate — keeping your records organized, accounts reconciled, and financials up to date so you have a clear picture of your event management company's finances and can make informed decisions with confidence.

Recently, we helped an event management business owner who was six months behind on their books. Within 30 days, we brought their books up to date, cleaned up their records, and gave them back 15 hours a week to focus on their business.

I'd be happy to show you how we approach bookkeeping and see whether we could do the same for your event management company.

Would a quick 15-minute conversation be worthwhile?

Choose a time that works for you:
https://calendly.com/profitpathsolution/15min""",

    "Floral Businesses": """Subject: A clearer picture of your floral business finances

Hi,

You didn't start your floral business to spend your weekends stressing over receipts, spreadsheets, and accounting software. But as your business grows, keeping track of flower purchases, supplier invoices, delivery costs, event expenses, and client payments can quickly become another task competing for your time.

And when those numbers aren't properly organized and reconciled, it can be difficult to see what your arrangements or events are actually making, where your money is going, and what your cash flow looks like.

At Profit Path Solution, we take bookkeeping completely off your plate — keeping your records organized, accounts reconciled, and financials up to date so you have a clear picture of your floral business's finances and can make informed decisions with confidence.

Recently, we helped a floral business owner who was six months behind on their books. Within 30 days, we brought their books up to date, cleaned up their records, and gave them back 15 hours a week to focus on their business.

I'd be happy to show you how we approach bookkeeping and see whether we could do the same for your floral business.

Would a quick 15-minute conversation be worthwhile?

Choose a time that works for you:
https://calendly.com/profitpathsolution/15min""",
}


def load_template(service: str, target_vertical: str = "") -> str:
    """
    Vertical-specific template wins when one exists for this exact
    target_vertical, falling back to the service-level template (saved
    custom override, or built-in default) otherwise.
    """
    if target_vertical and target_vertical in VERTICAL_TEMPLATES:
        return VERTICAL_TEMPLATES[target_vertical]
    overrides = _load_overrides()
    saved = overrides.get(service, "")
    if saved.strip():
        return saved
    return get_default_for_service(service)


def save_template(service: str, template: str) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    overrides = _load_overrides()
    overrides[service] = template
    with open(CUSTOM_TEMPLATES_PATH, "w", encoding="utf-8") as f:
        json.dump(overrides, f, indent=2)


def reset_template(service: str) -> None:
    overrides = _load_overrides()
    if service in overrides:
        del overrides[service]
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CUSTOM_TEMPLATES_PATH, "w", encoding="utf-8") as f:
            json.dump(overrides, f, indent=2)


# --------------------------------------------------------------- SMS --
# Deliberately much shorter than the email templates — SMS is read in a
# glance, not skimmed like an email, and every carrier charges/segments by
# length. Reuses the same merge-field logic since SMS needs the same fields
# (no Subject: line to split out, unlike email, and no HTML/signature
# images — SMS stays plain text only).
CUSTOM_SMS_TEMPLATES_PATH = os.path.join(DATA_DIR, "custom_sms_templates.json")

SMS_TEMPLATES: Dict[str, str] = {
    "Bookkeeping": (
        "Hi {greeting_name}, this is {sender_name} from {sender_company}. "
        "We help businesses like {company_name} simplify bookkeeping and accounting. "
        "Worth a quick call? Reply STOP to opt out."
    ),
}

GENERIC_SMS_TEMPLATE = (
    "Hi {greeting_name}, this is {sender_name} from {sender_company}. "
    "We help with {service_lower}. Worth a quick call? Reply STOP to opt out."
)

# A/B/C test variants (added 2026-09-18): the client supplied 3 alternate
# SMS bodies to run in parallel across different states/business types and
# see which gets more replies. Wording is verbatim from the client, only
# "[Name]" swapped for this app's own {greeting_name} merge field (same
# placeholder role as everywhere else) -- do not otherwise edit this copy.
# "Original" is the pre-existing default above, folded in here so every
# option lives in one picker (see app.py's Send Texts tab).
SMS_TEMPLATE_VARIANTS: Dict[str, Dict[str, str]] = {
    "Bookkeeping": {
        "Original": SMS_TEMPLATES["Bookkeeping"],
        "Variant 1 (profit vs. bank balance)": (
            "Hey {greeting_name}, checking your bank app isn't the same as knowing your actual profit. "
            "I help business owners rescue months of backlogged books so they can stop guessing. "
            "How far behind is your bookkeeping right now?"
        ),
        "Variant 2 (backlog anxiety)": (
            "Hi {greeting_name}, there is a specific kind of background anxiety that comes from being "
            "months behind on your financials. We specialize in rapid cleanup and backlog recovery so "
            "you can breathe again. Are you currently doing the books yourself?"
        ),
        "Variant 3 (receipts mess)": (
            "Hey {greeting_name}, whether your receipts are stuffed in a glovebox, a desk drawer, or "
            "drowning in your email, lagging books are suffocating your cash flow. We take over the "
            "backlog and get you 100% caught up. Ready to hand over the financials mess?"
        ),
    },
}

DEFAULT_SMS_VARIANT = "Original"


def get_sms_variants(service: str) -> Dict[str, str]:
    """Every variant name -> its built-in default text for this service, for populating a picker."""
    return SMS_TEMPLATE_VARIANTS.get(service, {DEFAULT_SMS_VARIANT: SMS_TEMPLATES.get(service, GENERIC_SMS_TEMPLATE)})


def get_default_sms_for_service(service: str, variant: str = DEFAULT_SMS_VARIANT) -> str:
    return get_sms_variants(service).get(variant, GENERIC_SMS_TEMPLATE)


def _load_sms_overrides() -> Dict[str, Dict[str, str]]:
    """
    {service: {variant: template_text}}. Transparently migrates the old
    pre-2026-09-18 flat format ({service: template_text}, back when there
    was only one template per service) by treating that saved string as
    the "Original" variant's override -- so an already-customized
    template isn't silently lost/reverted by the upgrade to multi-variant
    support.
    """
    if not os.path.exists(CUSTOM_SMS_TEMPLATES_PATH):
        return {}
    try:
        with open(CUSTOM_SMS_TEMPLATES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    migrated = {}
    for service, value in raw.items():
        if isinstance(value, str):
            migrated[service] = {DEFAULT_SMS_VARIANT: value}
        elif isinstance(value, dict):
            migrated[service] = value
    return migrated


def load_sms_template(service: str, variant: str = DEFAULT_SMS_VARIANT) -> str:
    overrides = _load_sms_overrides()
    saved = overrides.get(service, {}).get(variant, "")
    if saved.strip():
        return saved
    return get_default_sms_for_service(service, variant)


def save_sms_template(service: str, template: str, variant: str = DEFAULT_SMS_VARIANT) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    overrides = _load_sms_overrides()
    overrides.setdefault(service, {})[variant] = template
    with open(CUSTOM_SMS_TEMPLATES_PATH, "w", encoding="utf-8") as f:
        json.dump(overrides, f, indent=2)


def reset_sms_template(service: str, variant: str = DEFAULT_SMS_VARIANT) -> None:
    overrides = _load_sms_overrides()
    if service in overrides and variant in overrides[service]:
        del overrides[service][variant]
        if not overrides[service]:
            del overrides[service]
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CUSTOM_SMS_TEMPLATES_PATH, "w", encoding="utf-8") as f:
            json.dump(overrides, f, indent=2)


def render_sms_for_lead(lead: Dict, variant: str = DEFAULT_SMS_VARIANT) -> str:
    """Same 'use this lead's own service' logic as render_for_lead, for SMS."""
    from . import mailer

    tpl = load_sms_template(lead.get("service", "") or "Bookkeeping", variant)
    fields = _merge_fields(lead)
    fields.update(mailer.get_sender_info())
    try:
        return tpl.format(**fields)
    except KeyError as e:
        return f"Template error: missing field {e}. Available fields: {', '.join(fields.keys())}"