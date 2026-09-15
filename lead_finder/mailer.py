"""
Actual email sending, via plain SMTP (works with Gmail/Outlook/most
providers using an app password — no third-party paid API required).

Three responsibilities:
  - send_email / send_batch: dispatch messages with a delay between sends
  - sent-history tracking: so the same lead doesn't get emailed twice
  - a suppression list: emails/domains to never contact, checked before every send

Nothing here sends anything unless send_email/send_batch is explicitly
called — loading this module or reading config does not send mail.
"""

from __future__ import annotations

import os
import re
import smtplib
import time
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
SENT_LOG_PATH = os.path.join(DATA_DIR, "sent_log.csv")
SUPPRESSION_PATH = os.path.join(DATA_DIR, "suppression_list.csv")

SENT_LOG_COLUMNS = ["domain", "email", "company_name", "service", "subject", "status", "error", "sent_at"]


# ------------------------------------------------------------------ config --
def get_smtp_config() -> Optional[Dict]:
    """Read SMTP settings from environment. Returns None if incomplete."""
    host = os.getenv("SMTP_HOST", "")
    port = os.getenv("SMTP_PORT", "")
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    from_email = os.getenv("FROM_EMAIL", "") or username
    if not (host and port and username and password and from_email):
        return None
    return {
        "host": host,
        "port": int(port),
        "username": username,
        "password": password,
        "from_email": from_email,
        "from_name": os.getenv("FROM_NAME", ""),
        "reply_to": os.getenv("REPLY_TO_EMAIL", ""),
    }


def get_sender_info() -> Dict[str, str]:
    """Fields used to fill in the template footer (name/company/phone/address)."""
    return {
        "sender_name": os.getenv("FROM_NAME", "[Your Name]"),
        "sender_title": os.getenv("SENDER_TITLE", ""),
        "sender_company": os.getenv("SENDER_COMPANY", "[Your Company]"),
        "sender_phone": os.getenv("SENDER_PHONE", "[Your Phone Number]"),
        "sender_website": os.getenv("SENDER_WEBSITE", ""),
        "sender_address": os.getenv("SENDER_ADDRESS", "[Your Business Mailing Address]"),
    }


def _is_real_value(v: str) -> bool:
    """True for an actual configured value, as opposed to one of the unset '[...]' placeholder defaults."""
    return bool(v) and not v.strip().startswith("[")


def build_signature_text(info: Dict[str, str]) -> str:
    """Plain-text signature block, skipping any field that's still on its unset placeholder default."""
    order = ("sender_name", "sender_title", "sender_company", "sender_phone", "sender_website", "sender_address")
    lines = [info.get(k, "") for k in order]
    lines = [l for l in lines if _is_real_value(l)]
    return "\n".join(lines) if lines else "[Your Name]\n[Your Company]"


ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
SIGNATURE_LOGO_PATH = os.path.join(ASSETS_DIR, "signature_logo.png")
SIGNATURE_LOGO_CID = "sig_logo"
# Fixed display box for the logo <img>, sent as explicit HTML width/height
# attributes (not just CSS) so Outlook — which often ignores "height: auto"
# and falls back to the image's native pixel size — doesn't blow the row
# height out and push the logo below the text column. Matches
# signature_logo.png's own aspect ratio (662x486, tightly cropped to its
# visible content on 2026-09-15 — the original file had ~30% dead
# whitespace padding around the mark, which is what was making the logo
# cell taller than the text next to it). Update these two numbers if the
# asset image's aspect ratio ever changes.
SIGNATURE_LOGO_WIDTH = 240
SIGNATURE_LOGO_HEIGHT = 176

SIGNATURE_WA_ICON_PATH = os.path.join(ASSETS_DIR, "whatsapp_icon.png")
SIGNATURE_WA_ICON_CID = "wa_icon"


def build_signature_html(info: Dict[str, str], include_images: bool = True) -> str:
    """
    HTML signature block matching the reference layout: logo on the left,
    a thin gold vertical divider, then name/title/company, then four
    contact lines in order — phone, WhatsApp (same number, distinguished by
    the real WhatsApp icon image rather than a text label), website
    (always displayed with a www. prefix regardless of how it's stored in
    .env), address. Both images (cid:sig_logo, cid:wa_icon) are embedded as
    inline attachments by send_email via get_signature_inline_images().
    include_images=False renders the same text layout without either <img>
    tag, for contexts where the image attachments aren't being sent (e.g. a
    plain preview) — no current caller uses this, but the option is kept.
    """
    name = info.get("sender_name", "")
    title = info.get("sender_title", "")
    company = info.get("sender_company", "")
    phone = info.get("sender_phone", "")
    address = info.get("sender_address", "")
    website = info.get("sender_website", "")

    def _icon_row(icon_html: str, content_html: str, bg_color: str = "#d99a1b") -> str:
        return (
            f'<div style="margin-top: 6px;">'
            f'<span style="display:inline-block; width:22px; height:22px; background:{bg_color}; '
            f'border-radius:50%; text-align:center; color:#ffffff; font-size:12px; line-height:22px; '
            f'margin-right:8px; vertical-align:middle;">{icon_html}</span>'
            f'<span style="vertical-align:middle;">{content_html}</span></div>'
        )

    right = []
    if _is_real_value(name):
        right.append(f'<div style="font-weight: bold; font-size: 20px; color: #111111;">{name}</div>')
    if _is_real_value(title):
        right.append(f'<div style="font-size: 15px; color: #222222; margin-bottom: 2px;">{title}</div>')
    if _is_real_value(company):
        right.append(f'<div style="font-weight: bold; font-size: 15px; color: #111111; margin-bottom: 8px;">{company}</div>')

    # Line 1: phone
    if _is_real_value(phone):
        right.append(_icon_row("&#128222;", phone))
    # Line 2: WhatsApp — same number, distinguished from line 1 by the real
    # WhatsApp glyph rather than a text label (the "WhatsApp:" prefix was
    # removed 2026-09-15 — the icon alone already says what it is). Uses
    # the actual embedded WhatsApp icon image when images are available
    # (real sends + the in-app preview); falls back to the old generic
    # speech-bubble badge only for a hypothetical images-off render (no
    # current caller passes include_images=False, but the option exists).
    if _is_real_value(phone):
        if include_images:
            right.append(
                '<div style="margin-top: 6px;">'
                f'<img src="cid:{SIGNATURE_WA_ICON_CID}" width="22" height="22" alt="WhatsApp" '
                'style="width:22px; height:22px; vertical-align:middle; margin-right:8px; '
                'border-radius:50%; display:inline-block;">'
                f'<span style="vertical-align:middle;">{phone}</span></div>'
            )
        else:
            right.append(_icon_row("&#128172;", phone, bg_color="#25D366"))
    # Line 3: website, always shown with a www. prefix regardless of how
    # it's stored in .env (e.g. "profitpathsolution.com" -> "www.profitpathsolution.com").
    if _is_real_value(website):
        bare = website.strip()
        for prefix in ("https://", "http://", "www."):
            if bare.lower().startswith(prefix):
                bare = bare[len(prefix):]
        display_website = f"www.{bare}"
        url = f"https://{display_website}"
        right.append(_icon_row("&#127760;", f'<a href="{url}" style="color: #1a73e8; text-decoration: none;">{display_website}</a>'))
    # Line 4: address
    if _is_real_value(address):
        right.append(_icon_row("&#128205;", address))

    right_html = "".join(right)

    logo_cell = ""
    if include_images:
        # Explicit width AND height on both the <img> and its <td> — not
        # just CSS — because Outlook's rendering engine often ignores
        # "height: auto" and falls back to the image's native pixel size,
        # which would blow out the row height and visually push the logo
        # below the text column next to it. valign is set as an HTML
        # attribute too (Outlook honors that more reliably than CSS
        # vertical-align). See SIGNATURE_LOGO_WIDTH/HEIGHT above.
        logo_cell = (
            f'<td valign="middle" width="{SIGNATURE_LOGO_WIDTH}" '
            f'style="vertical-align: middle; padding-right: 22px;">'
            f'<img src="cid:{SIGNATURE_LOGO_CID}" alt="" '
            f'width="{SIGNATURE_LOGO_WIDTH}" height="{SIGNATURE_LOGO_HEIGHT}" '
            f'style="width:{SIGNATURE_LOGO_WIDTH}px; height:{SIGNATURE_LOGO_HEIGHT}px; display:block;">'
            f'</td>'
        )

    table = (
        '<table cellpadding="0" cellspacing="0" style="font-family: Arial, Helvetica, sans-serif; '
        'font-size: 13px; color: #222222;"><tr>'
        f'{logo_cell}'
        '<td valign="middle" style="width: 1px; background-color: #d99a1b;">&nbsp;</td>'
        f'<td valign="middle" style="vertical-align: middle; padding-left: 22px;">{right_html}</td>'
        '</tr></table>'
    )
    return table


def get_signature_inline_images() -> Dict[str, str]:
    """The cid -> filepath mapping send_email needs to actually attach the signature images."""
    images = {}
    if os.path.exists(SIGNATURE_LOGO_PATH):
        images[SIGNATURE_LOGO_CID] = SIGNATURE_LOGO_PATH
    if os.path.exists(SIGNATURE_WA_ICON_PATH):
        images[SIGNATURE_WA_ICON_CID] = SIGNATURE_WA_ICON_PATH
    return images

# -------------------------------------------------------------------- send --
def send_email(smtp_config: Dict, to_email: str, subject: str, body: str,
                html_body: Optional[str] = None,
                inline_images: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
    """
    Send one email. Returns (success, error_message).

    If html_body is given, sends a proper multipart message (plain-text
    fallback + HTML) instead of plain text alone — recipients whose email
    client can't or won't render HTML still get a readable plain version.
    inline_images maps a Content-ID (no angle brackets, e.g. "sig_logo") to
    a file path; reference it in html_body as <img src="cid:sig_logo">.
    """
    try:
        from_display = smtp_config.get("from_name") or smtp_config["from_email"]

        if html_body:
            msg = MIMEMultipart("related")
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(body, "plain", "utf-8"))
            alt.attach(MIMEText(html_body, "html", "utf-8"))
            msg.attach(alt)
            for cid, path in (inline_images or {}).items():
                if not os.path.exists(path):
                    continue
                with open(path, "rb") as f:
                    img = MIMEImage(f.read())
                img.add_header("Content-ID", f"<{cid}>")
                img.add_header("Content-Disposition", "inline", filename=os.path.basename(path))
                msg.attach(img)
        else:
            msg = MIMEText(body, "plain", "utf-8")

        msg["Subject"] = subject
        msg["To"] = to_email
        msg["From"] = formataddr((from_display, smtp_config["from_email"]))
        if smtp_config.get("reply_to"):
            msg["Reply-To"] = smtp_config["reply_to"]

        with smtplib.SMTP(smtp_config["host"], smtp_config["port"], timeout=20) as server:
            server.starttls()
            server.login(smtp_config["username"], smtp_config["password"])
            server.sendmail(smtp_config["from_email"], [to_email], msg.as_string())
        return True, ""
    except Exception as e:
        return False, str(e)


def send_batch(smtp_config: Dict, jobs: List[Dict], delay_seconds: float = 4.0,
                progress_cb: Optional[Callable] = None) -> List[Dict]:
    """
    jobs: list of {"to": str, "subject": str, "body": str, "html_body":
    (optional) str, "inline_images": (optional) dict, ...any extra keys you
    want echoed back in the result, e.g. "domain", "company_name"}.
    Returns each job dict with "success" and "error" added.
    """
    results = []
    total = len(jobs)
    for i, job in enumerate(jobs):
        success, error = send_email(
            smtp_config, job["to"], job["subject"], job["body"],
            html_body=job.get("html_body"), inline_images=job.get("inline_images"),
        )
        result = dict(job, success=success, error=error)
        results.append(result)
        if progress_cb:
            progress_cb(i + 1, total, result)
        if i < total - 1:
            time.sleep(delay_seconds)
    return results


# --------------------------------------------------------------- sent log --
def _ensure_sent_log():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(SENT_LOG_PATH):
        pd.DataFrame(columns=SENT_LOG_COLUMNS).to_csv(SENT_LOG_PATH, index=False)


def load_sent_log() -> pd.DataFrame:
    _ensure_sent_log()
    df = pd.read_csv(SENT_LOG_PATH, dtype=str)
    for col in SENT_LOG_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df.fillna("")


def record_sent(domain: str, email: str, company_name: str, service: str,
                 subject: str, status: str, error: str = "") -> None:
    _ensure_sent_log()
    row = pd.DataFrame([{
        "domain": domain, "email": email, "company_name": company_name,
        "service": service, "subject": subject, "status": status, "error": error,
        "sent_at": pd.Timestamp.utcnow().isoformat(),
    }])
    row.to_csv(SENT_LOG_PATH, mode="a", header=False, index=False)


def already_sent_domains(status_filter: str = "sent") -> set:
    df = load_sent_log()
    if status_filter:
        df = df[df["status"] == status_filter]
    return set(df["domain"].dropna().astype(str))


# ---------------------------------------------------------- suppression --
def _ensure_suppression():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(SUPPRESSION_PATH):
        pd.DataFrame(columns=["value", "added_at"]).to_csv(SUPPRESSION_PATH, index=False)


def load_suppression() -> List[str]:
    _ensure_suppression()
    df = pd.read_csv(SUPPRESSION_PATH, dtype=str)
    if "value" not in df.columns:
        return []
    return sorted(set(df["value"].dropna().astype(str).str.lower()))


def add_to_suppression(values: List[str]) -> None:
    _ensure_suppression()
    existing = load_suppression()
    new_values = {v.strip().lower() for v in values if v.strip()}
    new_values -= set(existing)
    if not new_values:
        return
    rows = pd.DataFrame([{"value": v, "added_at": pd.Timestamp.utcnow().isoformat()} for v in new_values])
    rows.to_csv(SUPPRESSION_PATH, mode="a", header=False, index=False)


def _normalize_phone_digits(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def is_suppressed(email: str = "", domain: str = "", phone: str = "") -> bool:
    """
    One shared list across both channels — if someone opts out, that should
    mean opt out entirely, not just from whichever channel they replied on.
    """
    suppressed = load_suppression()
    email_l = (email or "").lower()
    domain_l = (domain or "").lower()
    phone_digits = _normalize_phone_digits(phone)
    if email_l and email_l in suppressed:
        return True
    if domain_l and domain_l in suppressed:
        return True
    if phone_digits:
        for entry in suppressed:
            if _normalize_phone_digits(entry) == phone_digits:
                return True
    return False