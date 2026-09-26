import base64
import io
import os
import threading
import time
from datetime import datetime
from functools import lru_cache

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from lead_finder import search, scrape, storage, email_template, blocklist, mailer, osm_source, extract, area_codes, texter, browser_scrape, junk_filter
from lead_finder.states import (
    US_STATES, ACTIVE_SERVICES, get_search_target,
    get_major_cities, get_target_verticals, get_extra_metro_areas,
)

load_dotenv(override=True)  # .env always wins over a stray shell-exported var of the same name

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SENT_RECORDS_DIR = os.path.join(DATA_DIR, "sent_records")
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "lead_finder", "assets")


@lru_cache(maxsize=1)
def _logo_data_uri() -> str:
    """Base64 data URI for the brand logo, used in the header banner.
    Reads logo_header.png, not signature_logo.png. The email signature
    uses the flat-white original, which is fine inside an email body, but
    on this app's dark header it rendered as an ugly white box, so a
    transparent cutout was generated once (white background thresholded
    to alpha 0, soft edge to avoid a jagged fringe) and saved as a
    separate asset. The app's whole gold/black palette is pulled from
    this same logo (see the 2026-09-23 CLAUDE.md change log entry) so
    the dashboard and the outbound email signature read as one brand."""
    path = os.path.join(ASSETS_DIR, "logo_header.png")
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _to_xlsx_bytes(df: pd.DataFrame, sheet_name: str = "Leads") -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
    return buf.getvalue()


def _format_elapsed(seconds: float) -> str:
    """'2m 14s' / '48s' — used by the Find Leads progress display so a long
    run shows real elapsed time, not just a fraction, which is what
    actually tells you "still working" apart from "stuck"."""
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _write_daily_sent_records_csv(results):
    """
    Writes/appends sent-email results into data/sent_records/{business type}/{date}.csv
    — one folder per target vertical (or 'General' if a lead has none), one
    CSV per calendar day inside it. This is in addition to, not instead of,
    mailer.py's own sent_log.csv, which is still needed internally for the
    "skip leads already emailed" dedup check — this is the human-readable,
    organized-for-review export layered on top of it.
    """
    if not results:
        return
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    by_vertical = {}
    for r in results:
        vertical = (r.get("target_vertical") or "General").strip() or "General"
        by_vertical.setdefault(vertical, []).append(r)

    for vertical, rows in by_vertical.items():
        safe_vertical = "".join(c if c.isalnum() or c in " &()-" else "_" for c in vertical).strip()
        folder = os.path.join(SENT_RECORDS_DIR, safe_vertical)
        os.makedirs(folder, exist_ok=True)
        file_path = os.path.join(folder, f"{today}.csv")

        out_rows = [{
            "company_name": r["company_name"], "email": r["to"], "service": r["service"],
            "target_vertical": r.get("target_vertical", ""), "subject": r.get("subject", ""),
            "status": "sent" if r["success"] else "failed", "error": r.get("error", ""),
            "sent_at": pd.Timestamp.now().isoformat(),
        } for r in rows]
        df_out = pd.DataFrame(out_rows)
        file_exists = os.path.exists(file_path)
        df_out.to_csv(file_path, mode="a", header=not file_exists, index=False)

st.set_page_config(
    page_title="Lead Finder",
    page_icon="📈",
    layout="wide",
)


# ---------------------------------------------------- background send workers --
# Streamlit reruns its whole script on every interaction (a tab switch, any
# widget change, a reconnect) and abandons whatever the *previous* run was
# doing mid-execution. A synchronous send loop inside a button's on-click
# handler is part of that run, so any interaction during a long send — even
# just switching tabs — silently kills it partway through. A plain
# threading.Thread is a real OS thread outside Streamlit's own script-execution
# machinery, so it keeps running regardless of what the main script does
# afterward. Progress is tracked via a plain dict (safe to mutate from
# another thread — nothing here calls any st.* rendering function off the
# main thread, which is the actual restriction).
def _run_email_batch_in_background(smtp_config, jobs, delay_seconds, state):
    def progress_cb(done, total, result):
        status = "sent" if result["success"] else "failed"
        mailer.record_sent(result["domain"], result["to"], result["company_name"],
                            result["service"], result["subject"], status, result.get("error", ""))
        state["done"] = done
        state["total"] = total
    def should_stop():
        return state.get("stop_requested", False)
    results = mailer.send_batch(smtp_config, jobs, delay_seconds=delay_seconds,
                                 progress_cb=progress_cb, should_stop=should_stop)
    _write_daily_sent_records_csv(results)
    state["results"] = results
    state["stopped_early"] = state.get("stop_requested", False)
    state["complete"] = True


def _run_text_batch_in_background(texting_config, jobs, delay_seconds, state):
    def progress_cb(done, total, result):
        status = "sent" if result["success"] else "failed"
        texter.record_sent_text(result["to"], result["company_name"], result["service"],
                                 status, result.get("error", ""), variant=result.get("variant", ""))
        state["done"] = done
        state["total"] = total
    def should_stop():
        return state.get("stop_requested", False)
    results = texter.send_text_batch(texting_config, jobs, delay_seconds=delay_seconds,
                                      progress_cb=progress_cb, should_stop=should_stop)
    state["results"] = results
    state["stopped_early"] = state.get("stop_requested", False)
    state["complete"] = True


def _run_bulk_batch_in_background(smtp_config, texting_config, email_jobs, text_jobs,
                                   email_delay, text_delay, state):
    """
    Bulk Send's background worker: runs the email batch, then the text
    batch, in sequence — not interleaved row-by-row in real time, but the
    net effect is the same as "row by row, email if it has one, text if
    it doesn't, both if it has both" — every row that qualifies for a
    channel gets that channel, exactly once, using the exact same
    send_batch/send_text_batch primitives (and the exact same sent-log
    recording) as the standalone Send Emails/Send Texts tabs. Doing it as
    two sequential passes (instead of a genuinely interleaved single
    pass) means this reuses those two already-tested functions completely
    unmodified, rather than a third, riskier combined-send implementation.
    """
    def should_stop():
        return state.get("stop_requested", False)

    email_results = []
    if email_jobs:
        def email_cb(done, total, result):
            status = "sent" if result["success"] else "failed"
            mailer.record_sent(result["domain"], result["to"], result["company_name"],
                                result["service"], result["subject"], status, result.get("error", ""))
            state["email_done"] = done
            state["email_total"] = total
        email_results = mailer.send_batch(smtp_config, email_jobs, delay_seconds=email_delay,
                                           progress_cb=email_cb, should_stop=should_stop)
        _write_daily_sent_records_csv(email_results)

    text_results = []
    if text_jobs and not should_stop():
        def text_cb(done, total, result):
            status = "sent" if result["success"] else "failed"
            texter.record_sent_text(result["to"], result["company_name"], result["service"],
                                     status, result.get("error", ""), variant=result.get("variant", ""))
            state["text_done"] = done
            state["text_total"] = total
        text_results = texter.send_text_batch(texting_config, text_jobs, delay_seconds=text_delay,
                                               progress_cb=text_cb, should_stop=should_stop)

    state["email_results"] = email_results
    state["text_results"] = text_results
    state["stopped_early"] = state.get("stop_requested", False)
    state["complete"] = True


# ------------------------------------------------- live send progress + stop --
# A real incident (2026-09-26): a user started a 1,004-email batch, watched
# the progress bar read "0/1004" for over 3 minutes, and concluded sending
# was stuck — it wasn't; 700+ emails had actually gone out. The old design
# only updated the displayed count when the user manually clicked a
# "Refresh progress" button, which nothing told them to do. Separately,
# there was no way to stop a running batch short of killing the whole
# Streamlit process. `st.fragment(run_every=...)` (stable since Streamlit
# 1.37, confirmed present in the pinned 1.38.0) fixes both: it reruns just
# this block on a timer, independent of the rest of the page, so the count
# is never more than ~1s stale with no user action required, and a Stop
# button lives right next to it. Shared by Send Emails and Send Texts
# (single done/total counter); Bulk Send uses its own two-bar variant below
# since it tracks email and text progress separately.
@st.fragment(run_every="1s")
def _render_send_progress(state_key: str, item_noun: str = "emails"):
    state = st.session_state.get(state_key)
    if state is None:
        return
    done, total = state.get("done", 0), state.get("total", 0)
    is_running = not state.get("complete", False)
    if total:
        st.progress(done / total if total else 0.0,
                    text=f"{done}/{total} {item_noun} processed" + ("" if is_running else " — complete"))
    if is_running:
        if state.get("stop_requested"):
            st.info(f"Stopping — finishing the {item_noun[:-1]} already in flight, "
                    f"then no more will send ({done}/{total} sent so far).", icon="🛑")
        else:
            st.info(f"Sending in progress ({done}/{total}) — updates automatically, "
                    "no need to refresh, and this keeps running even if you switch tabs.", icon="⏳")
            if st.button("🛑 Stop sending", key=f"stop_{state_key}", type="secondary"):
                state["stop_requested"] = True
    else:
        results = state.get("results", [])
        sent_ok = sum(1 for r in results if r["success"])
        sent_fail = len(results) - sent_ok
        stopped_early = state.get("stopped_early", False)
        not_attempted = total - done
        tail = f" {not_attempted} were never attempted (stopped early)." if stopped_early and not_attempted else ""
        if sent_fail == 0:
            st.success(f"{'Stopped.' if stopped_early else 'Done.'} Sent {sent_ok} {item_noun}.{tail}")
        else:
            st.warning(f"{'Stopped.' if stopped_early else 'Done.'} Sent {sent_ok}, failed {sent_fail}.{tail} "
                       "See the log below for details.")
            failures_df = pd.DataFrame([
                {"company": r.get("company_name", ""), "to": r.get("to", ""), "error": r["error"]}
                for r in results if not r["success"]
            ])
            st.dataframe(failures_df, use_container_width=True, hide_index=True)
        if st.button("Clear this status", key=f"clear_{state_key}"):
            st.session_state[state_key] = None
            st.rerun()


@st.fragment(run_every="1s")
def _render_bulk_send_progress(state_key: str):
    state = st.session_state.get(state_key)
    if state is None:
        return
    is_running = not state.get("complete", False)
    email_done, email_total = state.get("email_done", 0), state.get("email_total", 0)
    text_done, text_total = state.get("text_done", 0), state.get("text_total", 0)
    if email_total:
        st.progress(email_done / email_total, text=f"Emails: {email_done}/{email_total}")
    if text_total:
        st.progress(text_done / text_total, text=f"Texts: {text_done}/{text_total}")
    if is_running:
        if state.get("stop_requested"):
            st.info("Stopping — finishing anything already in flight, then no more will send.", icon="🛑")
        else:
            st.info("Sending in progress — updates automatically, no need to refresh, and this "
                    "keeps running even if you switch tabs.", icon="⏳")
            if st.button("🛑 Stop sending", key=f"stop_{state_key}", type="secondary"):
                state["stop_requested"] = True
    else:
        email_results = state.get("email_results", [])
        text_results = state.get("text_results", [])
        email_ok = sum(1 for r in email_results if r["success"])
        text_ok = sum(1 for r in text_results if r["success"])
        stopped_early = state.get("stopped_early", False)
        st.success(f"{'Stopped.' if stopped_early else 'Done.'} Sent {email_ok} email(s), {text_ok} text(s).")
        all_failures = (
            [{"company": r.get("company_name", ""), "channel": "email", "to": r.get("to", ""), "error": r["error"]}
             for r in email_results if not r["success"]]
            + [{"company": r.get("company_name", ""), "channel": "text", "to": r.get("to", ""), "error": r["error"]}
               for r in text_results if not r["success"]]
        )
        if all_failures:
            st.warning(f"{len(all_failures)} failed — see below.")
            st.dataframe(pd.DataFrame(all_failures), use_container_width=True, hide_index=True)
        if st.button("Clear this status", key=f"clear_{state_key}"):
            st.session_state[state_key] = None
            st.rerun()


def _run_browser_retry_in_background(leads, state):
    def progress_cb(done, total, result):
        state["done"] = done
        state["total"] = total
    updated_rows = browser_scrape.rescrape_many_with_browser(leads, progress_cb=progress_cb)
    found_count = sum(1 for orig, upd in zip(leads, updated_rows) if upd.get("email") and not orig.get("email"))
    storage.save_leads(updated_rows)
    state["results"] = updated_rows
    state["found_count"] = found_count
    state["complete"] = True

UNCAPPED_LIMIT = 100_000

STATUS_BADGE = {
    "ok": "🟢 ok",
    "no_contact_found": "🟡 no contact found",
    "failed": "🔴 failed",
}

CONFIDENCE_BADGE = {
    "High": "🟢 High",
    "Medium": "🟡 Medium",
    "Low": "🔴 Low",
    "Unrated": "⚪ Unrated",
}

# ---------------------------------------------------------------- styling --
# Palette pulled directly from lead_finder/assets/signature_logo.png (the
# same file the email signature embeds) so the dashboard and the outbound
# emails read as one brand instead of two unrelated looks. Gold #E3BC30 /
# #F2D571, ink #0A0A0A, warm off-white text. See CLAUDE.md change log,
# 2026-09-23 entry, for the exact extracted values.
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800&display=swap');

    :root {
        --gold: #E3BC30;
        --gold-light: #F2D571;
        --gold-dim: #8a6f1e;
        --ink: #0A0A0A;
        --card: #17171b;
        --card-hover: #1c1c21;
        --border: #2c2a22;
        --text-muted: #a8a396;
    }

    .block-container { padding-top: 1.5rem; padding-bottom: 3rem; max-width: 1400px; }

    h1, h2, h3, .lf-hero-title { font-family: 'Sora', sans-serif; }

    /* ---------------------------------------------------------- hero header */
    .lf-hero {
        display: flex; align-items: center; gap: 22px;
        padding: 22px 28px;
        margin-bottom: 1.6rem;
        border-radius: 16px;
        background: linear-gradient(135deg, #141412 0%, #0c0c0b 60%, #141210 100%);
        border: 1px solid var(--border);
        position: relative;
        overflow: hidden;
        box-shadow: 0 8px 30px rgba(0,0,0,0.35);
    }
    .lf-hero::after {
        content: "";
        position: absolute; left: 0; right: 0; bottom: 0; height: 3px;
        background: linear-gradient(90deg, transparent, var(--gold), var(--gold-light), var(--gold), transparent);
        background-size: 200% 100%;
        animation: lf-shimmer 5s linear infinite;
    }
    @keyframes lf-shimmer {
        0% { background-position: 0% 0; }
        100% { background-position: 200% 0; }
    }
    .lf-hero-logo {
        height: 56px; width: auto; flex-shrink: 0;
        filter: drop-shadow(0 2px 10px rgba(227,188,48,0.25));
    }
    .lf-hero-title {
        font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em; line-height: 1.1;
        background: linear-gradient(90deg, var(--gold-light), var(--gold) 55%, #b3892a);
        -webkit-background-clip: text; background-clip: text; color: transparent;
        margin: 0;
    }
    .lf-hero-tagline { color: var(--text-muted); margin-top: 0.35rem; font-size: 1.0rem; }
    .lf-hero-chip {
        display: inline-block; margin-top: 0.6rem;
        padding: 3px 12px; border-radius: 999px;
        border: 1px solid var(--gold-dim);
        color: var(--gold-light); font-size: 0.72rem; font-weight: 700;
        letter-spacing: 0.06em; text-transform: uppercase;
    }

    /* ------------------------------------------------------------- metrics */
    div[data-testid="stMetric"] {
        background: var(--card);
        border: 1px solid var(--border);
        border-left: 3px solid var(--gold);
        border-radius: 10px;
        padding: 16px 20px;
        transition: transform 0.15s ease, box-shadow 0.15s ease;
    }
    div[data-testid="stMetric"]:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 18px rgba(227,188,48,0.12);
        background: var(--card-hover);
    }
    div[data-testid="stMetric"] label,
    div[data-testid="stMetricLabel"] { color: var(--text-muted) !important; }
    div[data-testid="stMetricValue"] { color: var(--gold-light) !important; font-weight: 700; }

    /* ------------------------------------------------------------ headings */
    h1 { font-weight: 800; letter-spacing: -0.02em; color: #f2efe6; }
    h2, h3 { letter-spacing: -0.01em; color: #e9e5d8; }
    .lf-subtitle { color: var(--text-muted); margin-top: -0.6rem; margin-bottom: 1.2rem; font-size: 1.02rem; }
    .lf-section-label {
        color: var(--gold); font-size: 0.78rem; font-weight: 700; letter-spacing: 0.08em;
        text-transform: uppercase; margin-bottom: 0.3rem; margin-top: 0.4rem;
    }
    .lf-section-label::before { content: "◆ "; font-size: 0.65rem; }

    /* -------------------------------------------------------------- buttons */
    div[data-testid="stButton"] button[kind="primary"] {
        background: linear-gradient(135deg, var(--gold-light), var(--gold) 70%);
        border: none; font-weight: 700; color: var(--ink);
        transition: transform 0.12s ease, box-shadow 0.12s ease;
    }
    div[data-testid="stButton"] button[kind="primary"]:hover {
        background: linear-gradient(135deg, var(--gold-light), var(--gold-light) 70%);
        box-shadow: 0 4px 16px rgba(227,188,48,0.35);
        transform: translateY(-1px);
    }
    div[data-testid="stButton"] button[kind="secondary"] {
        border: 1px solid var(--border); background: transparent; color: #d8d3c4;
        transition: border-color 0.12s ease, color 0.12s ease;
    }
    div[data-testid="stButton"] button[kind="secondary"]:hover {
        border-color: var(--gold-dim); color: var(--gold-light);
    }

    /* ----------------------------------------------------------- dataframe */
    .stDataFrame { border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }

    /* --------------------------------------------------------------- tabs */
    button[data-baseweb="tab"] { font-weight: 600; color: var(--text-muted); }
    button[data-baseweb="tab"][aria-selected="true"] { color: var(--gold-light); }
    div[data-baseweb="tab-highlight"] { background-color: var(--gold) !important; height: 3px !important; }
    div[data-baseweb="tab-border"] { background-color: var(--border) !important; }

    /* ---------------------------------------------------- alerts / dividers */
    div[data-testid="stAlert"] { border-radius: 10px; }
    hr { border-color: var(--border); }

    /* ------------------------------------------------------------ scrollbar */
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-track { background: #101012; }
    ::-webkit-scrollbar-thumb {
        background: linear-gradient(180deg, var(--gold), var(--gold-dim));
        border-radius: 8px;
    }
</style>
""", unsafe_allow_html=True)

if "results_df" not in st.session_state:
    st.session_state.results_df = storage.load_leads()
if "last_run_stats" not in st.session_state:
    st.session_state.last_run_stats = None
if "last_searched_states" not in st.session_state:
    st.session_state.last_searched_states = []
if "last_run_domains" not in st.session_state:
    st.session_state.last_run_domains = None
if "lead_db_view" not in st.session_state:
    st.session_state.lead_db_view = None
if "lead_db_view_desc" not in st.session_state:
    st.session_state.lead_db_view_desc = "all leads (visit Lead Database to set a filter)"
if "email_send_state" not in st.session_state:
    st.session_state.email_send_state = None
if "text_send_state" not in st.session_state:
    st.session_state.text_send_state = None
if "bulk_send_state" not in st.session_state:
    st.session_state.bulk_send_state = None
if "browser_retry_state" not in st.session_state:
    st.session_state.browser_retry_state = None

# -------------------------------------------------------------------- header --
st.markdown(
    f'''
    <div class="lf-hero">
        <img class="lf-hero-logo" src="{_logo_data_uri()}" alt="Profit Path Solution logo" />
        <div>
            <p class="lf-hero-title">Lead Finder</p>
            <p class="lf-hero-tagline">Find prospective clients by service and state, pull their
            public contact details, and draft outreach in one place.</p>
            <span class="lf-hero-chip">Email + SMS outreach, built in</span>
        </div>
    </div>
    ''',
    unsafe_allow_html=True,
)

# --------------------------------------------------------- top control panel --
control_panel = st.container(border=True)
with control_panel:
    st.markdown('<div class="lf-section-label">Search</div>', unsafe_allow_html=True)

    c1, c2, c3 = st.columns([1.3, 2.4, 1.1])
    with c1:
        service_choice = st.selectbox("Service / industry", ACTIVE_SERVICES + ["Custom..."])
        if service_choice == "Custom...":
            service = st.text_input("Enter a custom service", placeholder="e.g. Payroll Processing")
        else:
            service = service_choice
    with c2:
        all_states = st.checkbox("Search all 50 states + DC",
                                  help="Overrides the picker below and runs the search across every state.")
        if all_states:
            state_names = list(US_STATES.keys())
            st.caption(f"Searching all {len(state_names)} states — this will take a while.")
        else:
            state_names = st.multiselect(
                "State(s)", options=list(US_STATES.keys()), default=[],
                help="Pick one or more states to search in.",
            )
    with c3:
        st.markdown("<div style='height: 1.85rem'></div>", unsafe_allow_html=True)
        run_clicked = st.button("🔍  Find companies", type="primary", use_container_width=True)
        clear_clicked = st.button("Clear saved database", use_container_width=True)

    all_verticals_for_service = get_target_verticals(service)
    selected_vertical_labels = None  # None means "no vertical concept for this service" — search as before
    if all_verticals_for_service:
        vertical_labels = [v["label"] for v in all_verticals_for_service]
        selected_vertical_labels = st.multiselect(
            "Business types to target this run", vertical_labels, default=[],
            help="Bookkeeping targets several distinct business types, not just one. Pick which "
                 "ones to search this run — e.g. just Event Management and Floral Businesses.",
        )

    if "browser_available" not in st.session_state:
        # Playwright's own availability check launches a real (throwaway)
        # browser to confirm the binary is installed — genuinely slow, so
        # it's done once per session and cached, not on every rerun.
        st.session_state.browser_available = browser_scrape.is_available()
    browser_available = st.session_state.browser_available

    with st.expander("⚙️ Search depth, result quality & advanced settings"):
        dcol1, dcol2, dcol3 = st.columns(3)
        with dcol1:
            st.markdown("**Volume**")
            uncapped = st.checkbox(
                "Fetch everything the search finds (uncapped)",
                value=True,
                help="Skips the per-state cap below and pulls every unique company "
                     "the search backend returns across all query variants. Takes longer. "
                     "On by default — uncheck to cap a run with the sliders below.",
            )
            results_per_query = st.slider(
                "Search results per query", 10, 200, 40,
                help="Applies even when uncapped — this used to be forced to 200 whenever "
                     "'uncapped' was on, which likely made the search backend paginate the "
                     "engine's results page several times per query just to gather that many. "
                     "Results ranked past the first ~40 for a hyper-local query are "
                     "overwhelmingly directories/blogspam anyway, so this cuts a lot of wasted "
                     "requests for little real yield loss. Raise it if you suspect you're "
                     "missing genuine results.",
            )
            max_to_scrape = st.slider("Max companies to scrape per state / vertical", 10, 2000, 500, disabled=uncapped)
            concurrency = st.slider("Concurrent site scrapes", 4, 50, 25)
            no_new_streak_limit = st.slider(
                "Stop early after N queries with no new companies", 5, 50, 15,
                help="Per state+business type: once this many search queries IN A ROW add zero "
                     "new unique companies to the pool, stop issuing further query variants for "
                     "it (city-level queries especially tend to re-find the same businesses once "
                     "a metro's coverage is exhausted). Adaptive, not a fixed cutoff — a "
                     "state/vertical that keeps finding new companies keeps getting searched for "
                     "as long as it keeps paying off.",
            )
            use_city_queries = st.checkbox(
                "Add city-level search queries", value=True,
                help="In addition to whole-state queries, searches every city the app "
                     "knows for the state individually. Big yield increase, but a much "
                     "longer run (one extra query per city per vertical). Turn off for "
                     "a fast whole-state-only pass.",
            )
            use_extra_query_terms = st.checkbox(
                "Use extra search phrasings per vertical", value=True,
                help="Each vertical has several synonym phrasings, not just one — e.g. "
                     "Floral Businesses also searches \"flower shops,\" \"flower delivery "
                     "services,\" and \"wedding florists,\" not just \"florists.\" These "
                     "extra phrasings are only run as whole-state queries (cheap, 6 each) "
                     "unless you raise the phrasings-per-city slider below — they do NOT "
                     "multiply the city-level query count by default. Off = just the "
                     "primary phrasing everywhere.",
            )
            city_phrasing_limit = st.slider(
                "Phrasings expanded per city", 1, 5, 1, disabled=not use_extra_query_terms,
                help="How many of a vertical's phrasings get their own query for EVERY "
                     "city — this is the number that actually controls total query count "
                     "on a big city list. Default 1 (just the primary phrasing per city; "
                     "the others still run once per state). Raising this to match the "
                     "vertical's full phrasing count reproduces the old behavior — on "
                     "California-sized states that's thousands of extra queries and "
                     "multi-hour runs for a small extra yield, so it isn't the default.",
            )
            include_metro_neighborhoods = st.checkbox(
                "Also search well-known metro neighborhoods", value=False,
                help="Adds queries for named neighborhoods inside a state's biggest "
                     "metro(s) (e.g. Wynwood, Williamsburg, Koreatown) on top of the "
                     "city-level ones — currently curated for CA, TX, FL, and NY only. "
                     "Stacks on an already-large city query set, so it's off by default.",
            )
        with dcol2:
            st.markdown("**Sources & filters**")
            use_osm = st.checkbox(
                "Include OpenStreetMap business listings", value=True,
                help="Free, no-key, community-mapped business data — often more "
                     "geographically accurate than search results since it's real "
                     "place data, not search-ranked pages. Queried first; web search "
                     "fills in the rest.",
            )
            filter_directories = st.checkbox(
                "Pre-scrape filter: directories, listicles & platforms",
                value=True,
                help="Before scraping, skip known directories (Bark, Clutch, DesignRush...), "
                     "\"Top 10\" listicle articles, generic platforms (Wikipedia, YouTube, "
                     "LinkedIn...), foreign-TLD domains, and results that never mention the "
                     "service. Turn off for the raw, unfiltered search output.",
            )
            run_junk_filter = st.checkbox(
                "Final junk filter (after scrape & scoring)",
                value=True,
                help="A second pass over the finished leads: drops directory/marketplace "
                     "domains that slipped through, national order-gateway sites, businesses "
                     "of a clearly different trade that matched a keyword (photographers, "
                     "porta-potty rental, SEO agencies), and rows whose name came out as a "
                     "nav label. Turn off to keep every scraped company and review manually.",
            )
            auto_purge_low_confidence = st.checkbox(
                "Keep only High-confidence leads", value=True,
                help="Drops Medium/Low confidence leads right after each search — only "
                     "High-confidence companies get saved. Turn off to keep the "
                     "lower-confidence ones for manual review.",
            )
        with dcol3:
            st.markdown("**Contact-info recovery**")
            auto_browser_retry = st.checkbox(
                "Auto-retry no-contact leads with a real browser", value=browser_available,
                disabled=not browser_available,
                help="Some sites only reveal contact info after JavaScript runs. Right "
                     "after scraping, this re-visits every lead with a real website but "
                     "no email/phone found using an actual headless browser (several "
                     "seconds each) and rescoring it — before High-only filtering, so a "
                     "recovered email/phone can turn a lead High instead of it being "
                     "dropped outright. Off automatically if Playwright/its browser "
                     "binary isn't installed."
                     + ("" if browser_available else " (not installed — see the Lead Database tab for setup)."),
            )
            browser_retry_cap = st.slider(
                "Max browser retries per run", 0, 1000, 150, step=10,
                disabled=not (browser_available and auto_browser_retry),
                help="A safety cap — each retry launches a real browser (several seconds), "
                     "so an uncapped run with thousands of no-contact leads could turn into "
                     "hours of retries. Only the first N (by scrape order) are retried; the "
                     "manual button in Lead Database still covers the rest afterward.",
            )
            st.caption("Search backend: DuckDuckGo/`ddgs` (free, no key) + OpenStreetMap (free, no key).")
            backend = "ddgs"

if clear_clicked:
    storage.clear_leads()
    st.session_state.results_df = storage.load_leads()
    st.session_state.last_run_stats = None
    st.rerun()

tab_find, tab_db, tab_template, tab_send, tab_text, tab_bulk = st.tabs(
    ["Find Leads", "Lead Database", "Email Template", "Send Emails", "Send Texts", "Bulk Send"]
)

# ============================================================== FIND LEADS ==
with tab_find:
    if run_clicked:
        if not service:
            st.error("Enter a service/industry before searching.")
        elif not state_names:
            st.error("Select at least one state.")
        else:
            effective_max = UNCAPPED_LIMIT if uncapped else max_to_scrape
            # "uncapped" only removes the candidate-pool ceiling below — it no
            # longer forces 200 results per individual query. Asking the search
            # backend for 200 per query likely meant paginating its results
            # page several times per query for very little real yield past the
            # first ~40 (see the slider's own help text).
            effective_results_per_query = results_per_query

            all_new_rows = []
            junk_dropped_total = 0
            junk_examples = []
            search_error_count = 0
            search_error_samples = []
            # Circuit breaker for a genuinely dead search backend (DNS down,
            # network blocked, etc.) — distinct from consecutive_no_new
            # below, which is about queries that SUCCEED but don't turn up
            # anything new. This tracks queries where EVERY engine failed
            # outright. A handful of those is normal transient flakiness;
            # a long unbroken run of them means the backend isn't reachable
            # at all, and grinding through the rest of a multi-hundred-query
            # run at ~SEARCH_QUERY_DELAY + retry-backoff seconds PER guaranteed
            # failure would waste a lot of real time for zero benefit — OSM
            # keeps working fine either way, so once this trips, web search
            # is skipped for the rest of THIS run (not just the current
            # state/target) rather than repeating the same failure hundreds
            # of times over.
            SEARCH_FAILURE_CIRCUIT_BREAKER = 8
            consecutive_search_failures = 0
            web_search_disabled_due_to_failures = False
            total_states = len(state_names)

            verticals = get_target_verticals(service)
            has_vertical_concept = verticals is not None
            if verticals and selected_vertical_labels is not None:
                verticals = [v for v in verticals if v["label"] in selected_vertical_labels]

            if has_vertical_concept and not verticals:
                st.warning("No business types selected above — nothing to search. Pick at least one.")
                targets = []
            elif verticals:
                targets = [
                    {
                        "label": v["label"],
                        "search_target": v["query_terms"] if use_extra_query_terms else v["query_terms"][:1],
                        "osm_tags": v["osm_tags"], "keywords": v["keywords"],
                        "city_term_limit": city_phrasing_limit if use_extra_query_terms else 1,
                    }
                    for v in verticals
                ]
                term_note = " (extra phrasings on)" if use_extra_query_terms else " (primary phrasing only)"
                st.info(f"{service} targets {len(targets)} business type(s) this run{term_note}: "
                        f"{', '.join(t['label'] for t in targets)}.", icon="🎯")
            else:
                targets = [{
                    "label": service, "search_target": get_search_target(service),
                    "osm_tags": osm_source.get_osm_tags_for_service(service) or [],
                    "keywords": None,  # falls back to the service-name-based relevance check
                    "city_term_limit": None,
                }]

            # Rough volume/time estimate BEFORE issuing a single query — pure
            # string building, no network calls, so this is essentially free.
            # A large city list × several verticals/phrasings can add up to
            # thousands of queries without anything here warning you first;
            # this is that warning.
            if targets and state_names:
                est_queries = 0
                for est_state in state_names:
                    est_cities = list(get_major_cities(est_state)) if use_city_queries else []
                    if use_city_queries and include_metro_neighborhoods:
                        est_cities += [a for a in get_extra_metro_areas(est_state) if a not in est_cities]
                    for t in targets:
                        est_queries += len(search.build_queries(
                            service, est_state, search_target=t["search_target"],
                            cities=est_cities, city_term_limit=t.get("city_term_limit"),
                        ))
                if est_queries > 300:
                    # A realistic estimate, not just a floor. The old
                    # version only counted SEARCH_QUERY_DELAY between
                    # queries and said so explicitly ("before counting
                    # request time, scraping...") — technically honest,
                    # but it badly undersold real runtime, since those
                    # excluded costs are usually BIGGER than the delay
                    # itself: (1) each query is a real network round-trip
                    # on top of the artificial delay, not just the delay,
                    # and (2) scraping every candidate site found is a
                    # second, separate phase (per state x business type,
                    # after all of its queries) that this never accounted
                    # for at all. Both are folded in now. Still an
                    # estimate — engines timing out/rate-limiting, or a
                    # state/vertical finding far more than
                    # TYPICAL_CANDIDATES_PER_UNIT, can push real time well
                    # past this — but it's meant to track actual runtime,
                    # not just a lower bound you already know you'll blow past.
                    AVG_QUERY_REQUEST_SECONDS = 3.0    # observed avg round-trip beyond the delay, successful case
                    AVG_SCRAPE_SECONDS_PER_SITE = 3.0  # observed avg site fetch + parse time
                    TYPICAL_CANDIDATES_PER_UNIT = 150  # observed real-run yield, not the theoretical cap
                    search_query_delay = float(os.getenv("SEARCH_QUERY_DELAY", "2.5"))
                    search_minutes = est_queries * (search_query_delay + AVG_QUERY_REQUEST_SECONDS) / 60
                    total_units_est = len(state_names) * max(len(targets), 1)
                    candidates_per_unit = min(effective_max, TYPICAL_CANDIDATES_PER_UNIT)
                    scrape_minutes = total_units_est * (candidates_per_unit / concurrency) * AVG_SCRAPE_SECONDS_PER_SITE / 60
                    est_total_min = search_minutes + scrape_minutes
                    st.warning(
                        f"This run will issue an estimated **{est_queries:,} search queries** "
                        f"across {len(state_names)} state(s) — roughly **~{est_total_min:.0f} min** "
                        f"total (~{search_minutes:.0f} min searching + ~{scrape_minutes:.0f} min "
                        f"scraping whatever's found). Still a middle estimate, not a worst case — "
                        f"search engines timing out/rate-limiting, or an unusually high yield, can "
                        f"push it well past this. To cut this down: fewer states/business types, "
                        f"lower **\"Phrasings expanded per city\"**, or turn off city-level queries "
                        f"or metro neighborhoods.",
                        icon="⏱️",
                    )

            # --- Detailed progress: everything below runs inside one
            # st.status container so there's always a single, obvious
            # "is this still working" signal — a spinner while running, a
            # checkmark when done — plus a persistent scrollback log (every
            # st.write call below stays visible in the expanded body) and a
            # running elapsed-time readout. Before this, a long multi-
            # vertical run only moved its one progress bar once per STATE,
            # so it could sit visually frozen for a very long time while
            # working through many business types within that state —
            # indistinguishable from actually being stuck. Progress now
            # advances once per (state, business type) unit instead. ---
            total_units = max(total_states * max(len(targets), 1), 1)
            completed_units = 0
            run_start_time = time.time()

            # --- Stop button: clicking ANY widget makes Streamlit abort
            # the currently-running script and start a fresh one — that's
            # how this actually stops the search, there's nothing special
            # to wire up for the click itself. What makes it a good stop
            # rather than a data-losing one is the incremental save below:
            # after every completed (state, business type) unit, whatever
            # has been kept so far is written to leads.csv, not just once
            # at the very end. So stopping mid-run leaves the database and
            # Send tabs holding every FULLY finished unit's leads, already
            # High-confidence-filtered if that's on — just without the
            # browser-retry pass and the auto-archived Excel snapshot,
            # both of which normally only happen once, after the whole
            # run completes. Do either manually afterward: "Retry with a
            # real browser" in Lead Database, or its Download-as-Excel
            # button. ---
            st.button("🛑 Stop search (keeps every business type fully "
                      "finished so far — skips browser-retry & archiving)")
            st.caption("Stopping does NOT undo anything already found — it just ends the run early. "
                       "Whatever's fully done gets saved right away, unit by unit, as the run goes.")

            with st.status("🔍 Starting search...", expanded=True) as search_status:
                unit_progress = st.progress(0.0)
                elapsed_box = st.empty()

                def _touch_elapsed():
                    elapsed_box.caption(f"⏱️ {_format_elapsed(time.time() - run_start_time)} elapsed")

                _touch_elapsed()

                for state_idx, state_name in enumerate(state_names):
                    state_abbr = US_STATES.get(state_name, "")
                    cities = list(get_major_cities(state_name)) if use_city_queries else []
                    if use_city_queries and include_metro_neighborhoods:
                        cities += [a for a in get_extra_metro_areas(state_name) if a not in cities]

                    for target in targets:
                        candidate_urls = {}  # domain -> {"url":, "name":, "query":, "phone":}
                        web_candidate_urls = {}  # web-search's own findings, merged in after OSM (see below)
                        osm_only_rows = []  # OSM businesses with no website — added directly, no scrape needed
                        skipped_count = 0
                        target_note = f" — {target['label']}" if verticals else ""
                        unit_label = f"{state_name}{target_note}"

                        # expanded=True must be re-passed on every .update() call —
                        # leaving it out doesn't "leave it unchanged" the way the
                        # label/state args do, it resets the container to collapsed
                        # on the frontend (confirmed against the pinned Streamlit
                        # 1.38.0's mutable_status_container.py: omitting `expanded`
                        # clears that field on the delta proto rather than skipping
                        # it, and the frontend treats a cleared bool field as False).
                        search_status.update(
                            label=f"🔍 Searching {unit_label} ({completed_units + 1}/{total_units})",
                            expanded=True,
                        )
                        status_box = st.empty()

                        # --- OpenStreetMap: fetched in the BACKGROUND while web search
                        # runs below, since it's an independent network call with no
                        # dependency on the search results. Only the raw fetch happens
                        # in the thread — everything is single-threaded again by the
                        # time it's turned into candidate rows, after `.join()`, so
                        # there's no concurrent-write risk on the dicts above. ---
                        osm_holder = {"results": []}

                        def _fetch_osm_bg(_tags=target["osm_tags"], _abbr=state_abbr):
                            if use_osm and _tags:
                                osm_holder["results"] = osm_source.query_osm_by_tags(_tags, _abbr)

                        osm_thread = threading.Thread(target=_fetch_osm_bg, daemon=True)
                        osm_thread.start()

                        # --- Web search: fills gaps OSM doesn't cover. Runs while the
                        # OSM thread above fetches in parallel. Skipped entirely once
                        # the circuit breaker below has tripped for this run — OSM
                        # (already fetched above) still covers this unit either way. ---
                        queries = [] if web_search_disabled_due_to_failures else search.build_queries(
                            service, state_name, search_target=target["search_target"],
                            cities=cities, city_term_limit=target.get("city_term_limit"),
                        )
                        total_queries_this_target = len(queries)
                        consecutive_no_new = 0
                        for query_idx, q in enumerate(queries):
                            status_box.info(f"{unit_label}: query {query_idx + 1}/{total_queries_this_target} "
                                             f"— \"{q}\"")
                            _touch_elapsed()  # every query — this IS the "still moving" signal
                            hits = search.run_search(q, backend=backend, max_results=effective_results_per_query)
                            added_this_query = 0
                            query_totally_failed = len(hits) == 1 and "__ERROR__" in hits[0].get("snippet", "")
                            consecutive_search_failures = consecutive_search_failures + 1 if query_totally_failed else 0
                            for h in hits:
                                url = h.get("url", "")
                                title = h.get("title", "")
                                snippet = h.get("snippet", "")
                                if not url or "__ERROR__" in snippet:
                                    if "__ERROR__" in snippet:
                                        # Don't spam one red box per failed query — count
                                        # them and show a single summary after the run.
                                        search_error_count += 1
                                        if len(search_error_samples) < 3:
                                            search_error_samples.append(snippet.replace("__ERROR__:", "").strip())
                                    continue
                                dom = scrape._domain(url)
                                if filter_directories and blocklist.should_skip_result(
                                    url, dom, title, snippet, service, keywords=target["keywords"]
                                ):
                                    skipped_count += 1
                                    continue
                                if dom and dom not in web_candidate_urls:
                                    web_candidate_urls[dom] = {"url": url, "name": title, "query": q, "phone": ""}
                                    added_this_query += 1
                                if len(web_candidate_urls) >= effective_max:
                                    break
                            consecutive_no_new = 0 if added_this_query else consecutive_no_new + 1
                            if len(web_candidate_urls) >= effective_max:
                                break
                            if consecutive_no_new >= no_new_streak_limit:
                                status_box.info(
                                    f"{unit_label}: stopping early — the last "
                                    f"{no_new_streak_limit} queries found no new companies."
                                )
                                break
                            if consecutive_search_failures >= SEARCH_FAILURE_CIRCUIT_BREAKER:
                                web_search_disabled_due_to_failures = True
                                looks_like_dns = any(
                                    "dns error" in s.lower() for s in search_error_samples
                                )
                                st.warning(
                                    f"🔌 The last {SEARCH_FAILURE_CIRCUIT_BREAKER} search queries in a row "
                                    f"failed completely (every engine, every retry) — "
                                    + ("this looks like a DNS/network problem on this machine or network "
                                       "(every engine failing with the same DNS error at once means it's "
                                       "not one engine rate-limiting you), not something re-running fixes. "
                                       "Worth checking your network connection or trying a different DNS "
                                       "server (e.g. 1.1.1.1 or 8.8.8.8) before running again. "
                                       if looks_like_dns else
                                       "this looks like the search backend is unreachable right now, not "
                                       "just one engine rate-limiting. ")
                                    + "**Skipping web search for the rest of this run** — OpenStreetMap "
                                    "results (already unaffected) keep coming in as normal for every "
                                    "remaining state/business type.",
                                    icon="🔌",
                                )
                                break

                        # --- Fold in OSM results now that the background fetch is done
                        # (usually already finished well before the web-search loop
                        # above completes). Processed here, in the main thread, so
                        # this precedence — OSM wins a domain tie over web search,
                        # matching the original OSM-first behavior — is applied
                        # without any concurrent-write risk. ---
                        osm_thread.join()
                        for biz in osm_holder["results"]:
                            if biz["website"]:
                                dom = scrape._domain(biz["website"])
                                if dom and dom not in candidate_urls:
                                    candidate_urls[dom] = {
                                        "url": biz["website"], "name": biz["name"],
                                        "query": "OpenStreetMap", "phone": biz["phone"],
                                    }
                            elif biz["phone"] or biz["name"]:
                                phone = biz["phone"]
                                location_match = area_codes.phone_matches_state(phone, state_abbr) if phone else None
                                score, label = extract.score_lead(
                                    name_source="osm", company_name=biz["name"],
                                    email="", email_source="", email_domain_match=False,
                                    phone=phone, phone_source="osm" if phone else "",
                                    phone_suspicious=extract.is_suspicious_phone(phone) if phone else True,
                                    location_match=location_match,
                                )
                                osm_only_rows.append({
                                    "company_name": biz["name"], "name_source": "osm",
                                    "website": "", "domain": f"osm:{biz['name'].lower().strip()}",
                                    "phone": phone, "phone_source": "osm" if phone else "",
                                    "email": "", "email_source": "", "email_domain_match": False,
                                    "all_emails_found": "", "status": "ok" if phone else "no_contact_found",
                                    "confidence_score": score, "confidence_label": label,
                                    "service": service, "target_vertical": target["label"],
                                    "state": state_name, "search_query": "OpenStreetMap",
                                })
                        for dom, meta in web_candidate_urls.items():
                            if dom not in candidate_urls:
                                candidate_urls[dom] = meta

                        skip_note = f" (filtered out {skipped_count} directory/listicle results)" if skipped_count else ""
                        osm_note = f", {len(osm_only_rows)} from OpenStreetMap with no website" if osm_only_rows else ""
                        status_box.info(f"Found {len(candidate_urls)} unique company sites for {unit_label}{skip_note}{osm_note}. Extracting contact info...")

                        scrape_inputs = [
                            {"url": v["url"], "name": v["name"], "phone": v.get("phone", ""),
                             "state_abbr": state_abbr, "query": v["query"]}
                            for v in candidate_urls.values()
                        ]
                        scrape_progress = st.progress(0.0)

                        def _cb(done, total, _unit_label=unit_label):
                            scrape_progress.progress(done / total if total else 1.0, text=f"{_unit_label}: {done}/{total} sites scraped")
                            _touch_elapsed()

                        scraped = scrape.scrape_many(scrape_inputs, max_workers=concurrency, progress_cb=_cb)

                        # scrape_many preserves input order, so zip against
                        # scrape_inputs (which carries the query) — not against
                        # candidate_urls.items(), which drifted out of alignment
                        # with completion-ordered results.
                        target_rows = []
                        for row, inp in zip(scraped, scrape_inputs):
                            row["service"] = service
                            row["target_vertical"] = target["label"]
                            row["state"] = state_name
                            row["search_query"] = inp["query"]
                            target_rows.append(row)
                        target_rows.extend(osm_only_rows)

                        # --- Final junk pass: runs after every other filter, on
                        # the finished rows, using this target's own keywords.
                        # Skipped entirely when the user turns the toggle off. ---
                        if run_junk_filter:
                            kept, dropped = junk_filter.partition(target_rows, target["keywords"])
                            if dropped:
                                junk_dropped_total += len(dropped)
                                junk_examples.extend(
                                    f"{r.get('company_name', '?')} ({r.get('domain', '?')}) — {reason}"
                                    for r, reason in dropped[:5]
                                )
                            all_new_rows.extend(kept)
                        else:
                            kept = target_rows
                            all_new_rows.extend(target_rows)

                        status_box.empty()
                        scrape_progress.empty()

                        completed_units += 1
                        unit_progress.progress(completed_units / total_units)
                        _touch_elapsed()
                        st.write(f"✅ **{unit_label}**: kept {len(kept)} lead(s) "
                                 f"({completed_units}/{total_units} done)")

                        # Incremental save — see the Stop button's comment
                        # above for why: this is what makes stopping early
                        # (or any other interruption) lose at most the ONE
                        # unit still in flight, never the whole run. Mirrors
                        # the final save's own High-only purge so a stopped
                        # run's leads.csv looks like a smaller, correct
                        # version of the full run's output, not raw/
                        # unfiltered. Superseded by the real final save
                        # (with browser-retry applied) if the run finishes
                        # normally — this is purely a safety net.
                        rows_so_far = (
                            [r for r in all_new_rows if r.get("confidence_label") == "High"]
                            if auto_purge_low_confidence else list(all_new_rows)
                        )
                        if rows_so_far:
                            st.session_state.results_df = storage.save_leads(rows_so_far, replace=True)

                search_status.update(
                    label=f"✅ Search complete — {_format_elapsed(time.time() - run_start_time)} total",
                    state="complete", expanded=False,
                )

            if search_error_count:
                sample = " · ".join(search_error_samples)
                looks_like_dns_summary = any("dns error" in s.lower() for s in search_error_samples)
                if web_search_disabled_due_to_failures:
                    # The circuit breaker already explained itself in detail,
                    # right when it tripped mid-run (§ above) — this is just
                    # the end-of-run recap, not a repeat of that advice.
                    st.warning(
                        f"⚠️ Web search was disabled partway through this run after "
                        f"{SEARCH_FAILURE_CIRCUIT_BREAKER} queries in a row failed completely "
                        f"({search_error_count} failed total). Results lean heavily on OpenStreetMap "
                        f"for whatever ran after that point.\n\nFirst errors: {sample}"
                    )
                elif looks_like_dns_summary:
                    st.warning(
                        f"⚠️ {search_error_count} search quer{'y' if search_error_count == 1 else 'ies'} "
                        f"returned nothing — every engine failed with a **DNS error**, which points to "
                        f"a network/DNS problem on this machine or network, not per-engine "
                        f"rate-limiting. Re-running or switching `SEARCH_ENGINE_BACKEND` likely won't "
                        f"help on its own here — worth checking your network connection or trying a "
                        f"different DNS server (e.g. 1.1.1.1 or 8.8.8.8) first. Results this run lean "
                        f"on OpenStreetMap and whatever queries did succeed.\n\nFirst errors: {sample}"
                    )
                else:
                    st.warning(
                        f"⚠️ {search_error_count} search quer{'y' if search_error_count == 1 else 'ies'} "
                        f"returned nothing — the free search engines were timing out or rate-limiting. "
                        f"Results this run lean on OpenStreetMap and whatever queries did succeed. "
                        f"Re-running later, raising `SEARCH_QUERY_DELAY`/`SEARCH_TIMEOUT` in `.env`, or "
                        f"setting `SEARCH_ENGINE_BACKEND` to a single reliable engine usually helps.\n\n"
                        f"First errors: {sample}"
                    )

            scraped_count = len(all_new_rows) + junk_dropped_total

            # --- Auto browser-retry: run BEFORE the High-only purge below, on
            # purpose — a lead that only clears the High bar once the browser
            # recovers an email/phone would otherwise get discarded first and
            # never get the chance. Only real (non-OSM) websites with no
            # contact info are candidates; capped so an uncapped search with
            # thousands of no-contact leads can't turn into an hours-long tail. ---
            browser_recovered = 0
            if auto_browser_retry and browser_available:
                retry_idx = [
                    i for i, r in enumerate(all_new_rows)
                    if r.get("status") == "no_contact_found"
                    and str(r.get("website", "")).strip()
                    and not str(r.get("website", "")).startswith("osm:")
                ][:browser_retry_cap]
                if retry_idx:
                    st.info(f"Retrying {len(retry_idx)} no-contact lead(s) with a real browser "
                             f"(several seconds each)...", icon="🌐")
                    retry_progress = st.progress(0.0)
                    retry_leads = []
                    for i in retry_idx:
                        lead = dict(all_new_rows[i])
                        lead["state_abbr"] = US_STATES.get(lead.get("state", ""), "")
                        retry_leads.append(lead)

                    def _retry_cb(done, total, _result=None):
                        retry_progress.progress(done / total if total else 1.0,
                                                 text=f"Browser retry: {done}/{total}")

                    updated_leads = browser_scrape.rescrape_many_with_browser(retry_leads, progress_cb=_retry_cb)
                    for i, updated in zip(retry_idx, updated_leads):
                        if updated.get("email") and not all_new_rows[i].get("email"):
                            browser_recovered += 1
                        all_new_rows[i] = updated
                    retry_progress.empty()
                    st.caption(f"Browser retry recovered new contact info for {browser_recovered} "
                               f"of {len(retry_idx)} attempted.")

            low_conf_discarded = 0
            if auto_purge_low_confidence:
                low_conf_discarded = sum(1 for r in all_new_rows if r.get("confidence_label") != "High")
                all_new_rows = [r for r in all_new_rows if r.get("confidence_label") == "High"]

            if all_new_rows:
                # Each run REPLACES the database — the app only ever shows the
                # current search's results, never an accumulation across runs.
                updated_df = storage.save_leads(all_new_rows, replace=True)
                st.session_state.results_df = updated_df

                # Also archive this run as its own dated Excel snapshot,
                # separate from leads.csv (which the NEXT run will replace).
                # This is what lets Send Emails/Send Texts send from an
                # older run later without re-scraping — scraping and
                # sending don't have to happen in the same session. Non-
                # critical: a failure here (e.g. disk full) shouldn't lose
                # the run itself, which is already safely saved above.
                business_types_for_name = [t["label"] for t in targets] if targets else [service]
                export_filename = storage.build_export_filename(
                    datetime.now().strftime("%Y-%m-%d"), business_types_for_name,
                    state_names, all_states=all_states,
                )
                try:
                    storage.save_export_snapshot(updated_df, export_filename)
                    st.caption(f"📁 Archived as `{export_filename}` — pick it later from Send Emails/Send Texts "
                               f"without re-running this search.")
                except Exception as e:
                    st.caption(f"⚠️ Could not archive this run's snapshot ({e}) — leads.csv itself is fine.")

                with_email = sum(1 for r in all_new_rows if r.get("email"))
                with_phone = sum(1 for r in all_new_rows if r.get("phone"))
                st.session_state.last_run_stats = {
                    "found": len(all_new_rows),
                    "with_email": with_email,
                    "with_phone": with_phone,
                    "high_confidence": len(all_new_rows),  # only High is kept
                }
                st.session_state.last_searched_states = list(state_names)
                st.session_state.last_run_domains = set(r.get("domain", "") for r in all_new_rows)
                msg = (f"Done. Scraped {scraped_count} companies this run — kept **{len(all_new_rows)}** "
                       f"(dropped {junk_dropped_total} as junk")
                if auto_purge_low_confidence:
                    msg += f", {low_conf_discarded} as non-High confidence"
                msg += "). The database now holds only this run's results."
                st.success(msg)
                if junk_examples:
                    with st.expander(f"Junk filter removed {junk_dropped_total} — see a sample"):
                        st.write("\n".join(f"- {e}" for e in junk_examples[:40]))
            else:
                st.session_state.results_df = storage.load_leads()
                st.session_state.last_run_stats = None
                st.session_state.last_run_domains = set()
                if scraped_count:
                    st.warning(f"Scraped {scraped_count} companies but kept none "
                               f"({junk_dropped_total} junk, {low_conf_discarded} non-High). "
                               "Previous database left unchanged. Try a broader term or another state.")
                else:
                    st.warning("No companies found for that search. Try a broader service term, "
                               "a different state, or another search backend.")

    if st.session_state.last_run_stats:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Companies found (this run)", st.session_state.last_run_stats["found"])
        c2.metric("With email", st.session_state.last_run_stats["with_email"])
        c3.metric("With phone", st.session_state.last_run_stats["with_phone"])
        c4.metric("High confidence", st.session_state.last_run_stats["high_confidence"])
    else:
        st.caption("Set your filters above and click **Find companies** to get started.")

# ============================================================ LEAD DATABASE ==
with tab_db:
    df = st.session_state.results_df

    if df.empty:
        st.info("No leads saved yet. Run a search from the **Find Leads** tab.")
    else:
        st.caption("This is the result of your most recent search only. Each search replaces "
                   "the database — past runs are not accumulated here.")
        base_for_filters = df.copy()

        f1, f2, f3, f4 = st.columns(4)
        with f1:
            _available_states = sorted(base_for_filters["state"].dropna().unique().tolist())
            state_filter = st.multiselect("Filter by state", _available_states)
        with f2:
            vertical_options = sorted(v for v in base_for_filters["target_vertical"].dropna().unique().tolist() if v)
            vertical_filter = st.multiselect("Filter by target type", vertical_options) if vertical_options else []
        with f3:
            confidence_filter = st.multiselect(
                "Filter by confidence", ["High", "Medium", "Low", "Unrated"],
                help="How trustworthy the extracted name/email/phone are — see the Email Template tab tooltip for scoring details.",
            )
        with f4:
            only_with_email = st.checkbox("Only show leads with an email")

        filtered = base_for_filters
        if state_filter:
            filtered = filtered[filtered["state"].isin(state_filter)]
        if vertical_filter:
            filtered = filtered[filtered["target_vertical"].isin(vertical_filter)]
        if confidence_filter:
            filtered = filtered[filtered["confidence_label"].isin(confidence_filter)]
        if only_with_email:
            filtered = filtered[filtered["email"].astype(str).str.strip().str.len() > 0]

        confidence_order = {"High": 0, "Medium": 1, "Low": 2, "Unrated": 3}
        filtered = filtered.sort_values(
            by="confidence_label", key=lambda s: s.map(confidence_order).fillna(3)
        )
        st.session_state.lead_db_view = filtered
        st.session_state.lead_db_view_desc = (
            f"state={state_filter or 'all'}, target type={vertical_filter or 'all'}, "
            f"confidence={confidence_filter or 'all'}"
            + (", email required" if only_with_email else "")
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Companies found (this run)", len(filtered))
        m2.metric("High confidence", int((filtered["confidence_label"] == "High").sum()))
        m3.metric("With email", int((filtered["email"].astype(str).str.strip().str.len() > 0).sum()))
        m4.metric("With phone", int((filtered["phone"].astype(str).str.strip().str.len() > 0).sum()))

        display_df = filtered.copy()
        display_df["status"] = display_df["status"].map(STATUS_BADGE).fillna(display_df["status"])
        display_df["confidence_label"] = display_df["confidence_label"].map(CONFIDENCE_BADGE).fillna(display_df["confidence_label"])

        st.dataframe(
            display_df[["company_name", "confidence_label", "target_vertical", "state", "phone", "email",
                        "owner_name", "linkedin_url", "website", "status"]],
            use_container_width=True,
            height=460,
            column_config={
                "website": st.column_config.LinkColumn("Website"),
                "linkedin_url": st.column_config.LinkColumn("LinkedIn"),
                "company_name": "Company",
                "confidence_label": "Confidence",
                "target_vertical": "Target Type",
                "state": "State",
                "phone": "Phone",
                "email": "Email",
                "owner_name": "Owner/Founder",
                "status": "Status",
            },
        )

        dl_col1, dl_col2 = st.columns(2)
        with dl_col1:
            st.download_button(
                "⬇ Download as CSV",
                data=filtered.to_csv(index=False).encode("utf-8"),
                file_name="leads.csv",
                mime="text/csv",
            )
        with dl_col2:
            st.download_button(
                "⬇ Download as Excel",
                data=_to_xlsx_bytes(filtered, "Leads"),
                file_name="leads.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        st.markdown("---")
        st.markdown("**Purpose-built exports**")

        def _is_clean_local_phone(row) -> bool:
            phone = row.get("phone", "")
            if not phone or extract.is_suspicious_phone(phone):
                return False
            state_abbr = US_STATES.get(row.get("state", ""), "")
            if not state_abbr:
                return True  # can't judge without a state on record — don't penalize
            return area_codes.phone_matches_state(phone, state_abbr) is True

        email_ready = filtered[
            (filtered["email"].astype(str).str.strip().str.len() > 0)
            & (filtered["email_domain_match"].astype(str).str.lower() == "true")
            & (filtered["phone"].astype(str).str.strip().str.len() > 0)
        ]
        sms_ready = filtered[filtered.apply(_is_clean_local_phone, axis=1)]

        ecol, scol = st.columns(2)
        with ecol:
            st.caption(f"**Email-ready**: matching-domain email + phone present — {len(email_ready)} rows")
            st.download_button(
                "📧 Export email-ready CSV",
                data=email_ready.to_csv(index=False).encode("utf-8"),
                file_name="email_ready_leads.csv", mime="text/csv",
                disabled=email_ready.empty,
            )
            st.download_button(
                "📧 Export email-ready Excel",
                data=_to_xlsx_bytes(email_ready, "Email-ready"),
                file_name="email_ready_leads.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                disabled=email_ready.empty,
            )
        with scol:
            st.caption(f"**SMS-ready**: phone present, structurally valid, area code matches the "
                       f"lead's own state — {len(sms_ready)} rows")
            st.download_button(
                "💬 Export SMS-ready CSV",
                data=sms_ready.to_csv(index=False).encode("utf-8"),
                file_name="sms_ready_leads.csv", mime="text/csv",
                disabled=sms_ready.empty,
            )
            st.download_button(
                "💬 Export SMS-ready Excel",
                data=_to_xlsx_bytes(sms_ready, "SMS-ready"),
                file_name="sms_ready_leads.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                disabled=sms_ready.empty,
            )
        st.caption("SMS-ready excludes toll-free (800/888/etc.) and unrecognized area codes to stay strict — "
                   "loosen this yourself in the CSV if you want to include those too.")

        st.markdown("---")
        st.markdown("**Retry with a real browser**")
        st.caption("Some sites only show contact info after JavaScript runs (React/Squarespace/Wix-style "
                   "sites) — a plain page fetch never sees it. This re-visits leads that have a real "
                   "website but no email, using an actual headless browser instead, and updates the "
                   "database if it finds something. Slower than normal scraping (several seconds per "
                   "site). A search run already does this automatically for you, up to the cap set in "
                   "Advanced settings — use this button to cover whatever that cap left out.")

        if not st.session_state.get("browser_available", False):
            st.info(
                "Not available yet. Add to your environment:\n\n"
                "```\npip install playwright --break-system-packages\nplaywright install chromium\n```\n\n"
                "The second command downloads the actual browser binary (not just the Python package) — "
                "it's a real, one-time ~150-300MB download, and is required, not optional.",
                icon="🌐",
            )
        else:
            retry_candidates = filtered[
                (filtered["website"].astype(str).str.strip().str.len() > 0)
                & (~filtered["website"].astype(str).str.startswith("osm:"))
                & (filtered["email"].astype(str).str.strip().str.len() == 0)
            ]
            st.caption(f"{len(retry_candidates)} of the {len(filtered)} leads shown above have a real "
                       "website but no email — these are the retry candidates.")

            br_state = st.session_state.get("browser_retry_state")
            br_running = br_state is not None and not br_state.get("complete", False)
            if st.button("🌐 Retry these with a browser", disabled=retry_candidates.empty or br_running):
                retry_leads = []
                for _, row in retry_candidates.iterrows():
                    lead = row.to_dict()
                    lead["state_abbr"] = US_STATES.get(lead.get("state", ""), "")
                    retry_leads.append(lead)

                new_state = {"done": 0, "total": len(retry_leads), "results": [], "found_count": 0, "complete": False}
                st.session_state.browser_retry_state = new_state
                threading.Thread(
                    target=_run_browser_retry_in_background,
                    args=(retry_leads, new_state),
                    daemon=True,
                ).start()
                st.rerun()

            br_state = st.session_state.get("browser_retry_state")
            if br_state is not None:
                done, total = br_state["done"], br_state["total"]
                if total:
                    st.progress(done / total, text=f"{done}/{total} re-checked" + (" — complete" if br_state["complete"] else ""))
                if br_state["complete"]:
                    st.success(f"Done. Found new contact info for {br_state['found_count']} of {total} leads — "
                               "updated in your database.")
                    if st.button("Clear this status", key="clear_browser_retry_state"):
                        st.session_state.browser_retry_state = None
                        st.rerun()
                else:
                    st.caption("Running in the background — safe to switch tabs, come back and refresh to check. "
                               "This is meaningfully slower than a normal search, expect several seconds per site.")
                    if st.button("🔄 Refresh progress", key="refresh_browser_retry_progress"):
                        st.rerun()

# ============================================================ EMAIL TEMPLATE ==
with tab_template:
    st.markdown(f"Editing the template for **{service or 'this service'}**. Each service gets its own "
                "template — switch the *Service / industry* dropdown above to jump to a different one.")
    st.caption("Structured around Input → Process → Output: what the prospect is dealing with, "
               "what we do about it, and what they get. Merge fields: {company_name}, {service}, "
               "{service_lower}, {state}, {phone}, {email}, {website}")

    active_service = service or "Bookkeeping"
    widget_key = f"template_editor__{active_service}"
    if widget_key not in st.session_state:
        st.session_state[widget_key] = email_template.load_template(active_service)

    template_text = st.text_area("Template", height=380, key=widget_key)

    tcol1, tcol2, tcol3 = st.columns([1, 1, 4])
    with tcol1:
        if st.button("💾 Save template", type="primary"):
            email_template.save_template(active_service, template_text)
            st.success(f"Saved for {active_service}. Loads automatically next time you pick this service.")
    with tcol2:
        if st.button("↺ Reset to default"):
            email_template.reset_template(active_service)
            st.session_state[widget_key] = email_template.get_default_for_service(active_service)
            st.rerun()

    df = st.session_state.results_df
    # The database only ever holds the current run's results, so "this run"
    # is simply the whole table.
    current_run_df = df

    if not df.empty:
        st.markdown("---")
        st.markdown("#### Preview")

        def _preview_lead_for(vertical_label: str) -> dict:
            """Prefers a real lead from the current run in this vertical; falls back to a representative example."""
            pool = current_run_df
            if vertical_label:
                pool = pool[pool["target_vertical"] == vertical_label]
            pool = pool[pool["confidence_label"].isin(["High", "Medium"])] if not pool.empty else pool
            if not pool.empty:
                return pool.iloc[0].to_dict()
            return {
                "company_name": "Example Company", "service": active_service,
                "target_vertical": vertical_label, "state": "your state", "owner_name": "",
            }

        def _render_one_preview(lead: dict, heading: str):
            st.markdown(f"**{heading}**")
            subj, body = email_template.render_for_lead(lead)
            preview_mode = st.radio("Preview as", ["Plain text", "HTML (with signature images)"],
                                     horizontal=True, label_visibility="collapsed", key=f"preview_mode_{heading}")
            if preview_mode == "Plain text":
                st.code(f"Subject: {subj}\n\n{body}", language="text")
            else:
                st.caption(f"Subject: {subj}")
                preview_html = email_template.render_html_preview_for_lead(lead)
                st.components.v1.html(
                    f'<div style="background:#ffffff; padding:20px; border-radius:6px;">{preview_html}</div>',
                    height=520, scrolling=True,
                )

        if selected_vertical_labels is None:
            # This service has no vertical concept — one straightforward preview.
            _render_one_preview(_preview_lead_for(""), active_service)
        elif not selected_vertical_labels:
            st.info("Select at least one business type above (in the search panel) to preview its template.")
        else:
            # One rendered preview per currently-selected vertical — if 2
            # business types are selected, this shows the 2 actual emails
            # that would go out, not a single generic one.
            st.caption("Showing the actual template for each business type currently selected above — "
                       "using a real lead from your last search where one exists, or a representative "
                       "example otherwise.")
            for vertical_label in selected_vertical_labels:
                _render_one_preview(_preview_lead_for(vertical_label), vertical_label)
                st.markdown("---")

        verified_df = df[df["confidence_label"].isin(["High", "Medium"])]
        if verified_df.empty:
            verified_df = df
        export_verified_only = st.checkbox("Only export High/Medium confidence leads", value=True)
        export_df = verified_df if export_verified_only else df

        def _rendered_row(row):
            subj, body = email_template.render_for_lead(row.to_dict())
            return {
                "company_name": row["company_name"], "email": row["email"],
                "service": row["service"], "confidence": row["confidence_label"],
                "subject": subj, "body": body,
            }

        st.download_button(
            "⬇ Export rendered emails (CSV)",
            data=pd.DataFrame([_rendered_row(row) for _, row in export_df.iterrows()])
                .to_csv(index=False).encode("utf-8"),
            file_name="rendered_emails.csv",
            mime="text/csv",
        )
    else:
        st.info("Once you've collected some leads, you can preview the template against them here.")

# ============================================================== SEND EMAILS ==
with tab_send:
    smtp_config = mailer.get_smtp_config()

    if not smtp_config:
        st.info(
            "Email sending isn't configured yet. Add these to your `.env` file "
            "(see `.env.example`), then restart the app:\n\n"
            "```\nSMTP_HOST=smtp.gmail.com\nSMTP_PORT=587\n"
            "SMTP_USERNAME=you@yourbusiness.com\nSMTP_PASSWORD=your-app-password\n"
            "FROM_EMAIL=you@yourbusiness.com\nFROM_NAME=Your Name\n"
            "SENDER_COMPANY=Your Company\nSENDER_PHONE=555-123-4567\n"
            "SENDER_ADDRESS=123 Main St, City, ST 00000\n```\n\n"
            "For Gmail, use an [App Password](https://myaccount.google.com/apppasswords) "
            "rather than your regular password — Google blocks plain-password SMTP logins. "
            "`SENDER_ADDRESS` matters: CAN-SPAM legally requires a physical mailing address "
            "in every commercial email.",
            icon="✉️",
        )
    else:
        # Scraping and sending don't have to happen in the same session —
        # every search run also archives a dated Excel snapshot (see
        # storage.save_export_snapshot); pick one or more here, or upload
        # file(s) from anywhere on your own machine (e.g. a downloaded
        # export you've moved or hand-edited), to send from instead of
        # whatever's currently in the live database. Leave both empty to
        # fall back to the normal live-database behavior.
        history_snapshots = storage.list_export_snapshots()
        with st.expander("📂 Send from a saved run or an uploaded file instead of the current database", expanded=False):
            if not history_snapshots:
                st.caption("No archived runs yet — every completed search auto-saves one here, "
                           "named `date_businesstypes_states.xlsx`.")
                selected_snapshots = []
            else:
                selected_snapshots = st.multiselect(
                    "Saved run(s) to send from",
                    [s["filename"] for s in history_snapshots],
                    help="Pick one or more. Their leads are combined (deduped by domain) and used "
                         "below instead of the current Lead Database tab view.",
                )
            uploaded_files = st.file_uploader(
                "Or upload Excel/CSV file(s) from your computer",
                type=["xlsx", "csv"], accept_multiple_files=True,
                help="Any of this app's own exports — a data/exports/ download, or the Lead Database "
                     "tab's CSV/Excel export — edited or not, from anywhere on your machine. Combined "
                     "with any saved run(s) picked above, deduped by domain. A file with no "
                     "service/confidence_label columns (not one of this app's own exports) will pass "
                     "through with 0 eligible leads below — the safety filters still apply.",
            )

        source_frames = []
        source_labels = []
        upload_errors = []
        if selected_snapshots:
            source_frames.append(storage.load_export_snapshots(selected_snapshots))
            source_labels.append(f"{len(selected_snapshots)} saved run(s): {', '.join(selected_snapshots)}")
        if uploaded_files:
            uploaded_df, upload_errors = storage.load_uploaded_snapshots(uploaded_files)
            source_frames.append(uploaded_df)
            source_labels.append(f"{len(uploaded_files)} uploaded file(s): "
                                  f"{', '.join(f.name for f in uploaded_files)}")
        if upload_errors:
            st.warning("Couldn't read: " + "; ".join(upload_errors))

        using_custom_source = bool(source_frames)
        if using_custom_source:
            df = storage.dedupe_keep_last(pd.concat(source_frames, ignore_index=True)) if len(source_frames) > 1 else source_frames[0]
        else:
            df = st.session_state.results_df

        if df.empty:
            st.info("No leads to send from. Run a search from **Find Leads**, or pick/upload a saved run above.")
        else:
            st.caption(f"Sending as **{smtp_config.get('from_name') or smtp_config['from_email']}** "
                       f"via {smtp_config['host']}. Each lead is rendered with *its own* service's "
                       "template automatically — a mixed batch sends the right pitch to each company.")

            if using_custom_source:
                base_df = df
            else:
                base_df = st.session_state.lead_db_view if st.session_state.lead_db_view is not None else df

            # Two hard, non-adjustable safety filters — not inherited from
            # Lead Database's filter, which stays user-adjustable there.
            # Added after real incidents: a stray lead tagged with a
            # different service (from old testing, before the service list
            # was locked down) got sent that service's pitch instead of the
            # Bookkeeping one, and Medium/Low confidence leads generated
            # auto-reply support tickets. Both are now always enforced here
            # regardless of what Lead Database's own filter is set to, AND
            # regardless of whether the leads came from the live database,
            # a saved snapshot, or an uploaded file above.
            active_service = service or "Bookkeeping"
            pre_filter_count = len(base_df)
            base_df = base_df[base_df["service"] == active_service]
            base_df = base_df[base_df["confidence_label"] == "High"]
            excluded_by_safety = pre_filter_count - len(base_df)

            source_desc = (" + ".join(source_labels) if using_custom_source else
                            f"the **Lead Database** tab's current filter ({st.session_state.lead_db_view_desc})")
            st.info(f"Working from {source_desc} "
                    f"— then restricted to **{active_service}** service and **High confidence only** "
                    f"(fixed safety rules, not adjustable here) — {len(base_df)} leads remain"
                    + (f", {excluded_by_safety} excluded by those two rules" if excluded_by_safety else "") + ".",
                    icon="🔗")

            only_unsent = st.checkbox("Skip leads already emailed", value=True)

            eligible = base_df.copy()
            no_email_mask = eligible["email"].astype(str).str.strip().str.len() == 0
            no_email_count = int(no_email_mask.sum())
            eligible = eligible[~no_email_mask]

            not_targetable_mask = ~eligible["email"].apply(blocklist.is_targetable_email)
            not_targetable_count = int(not_targetable_mask.sum())
            eligible = eligible[~not_targetable_mask]

            # Built explicitly (not via DataFrame.apply(axis=1)) — that form
            # has a real pandas quirk where certain inputs make it return a
            # DataFrame instead of a Series, which then breaks .sum()/int().
            # This construction always produces a proper boolean Series,
            # including the empty-eligible case.
            suppressed_mask = pd.Series(
                [mailer.is_suppressed(row["email"], row["domain"]) for _, row in eligible.iterrows()],
                index=eligible.index, dtype=bool,
            )
            suppressed_count = int(suppressed_mask.sum())
            eligible = eligible[~suppressed_mask]

            already_sent_count = 0
            if only_unsent:
                sent_domains = mailer.already_sent_domains()
                already_sent_mask = eligible["domain"].isin(sent_domains)
                already_sent_count = int(already_sent_mask.sum())
                eligible = eligible[~already_sent_mask]

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Ready to send", len(eligible))
            m2.metric("No email found", no_email_count,
                      help="Phone-only leads (common from OpenStreetMap) — these are candidates for Send Texts instead.")
            m3.metric("Generic/shared inbox address", not_targetable_count,
                      help="Excluded because the email prefix (support@, info@, admin@, noreply@, etc.) routes "
                           "to a shared inbox or ticketing system rather than a person — sending to these is "
                           "what generated auto-reply support tickets previously. Personal-looking addresses "
                           "(erica@, jsmith@, ...) are NOT excluded here.")
            m4.metric("Already emailed (skipped)", already_sent_count)
            m5.metric("Suppressed (skipped)", suppressed_count)
            st.caption(f"{len(eligible)} + {no_email_count} + {not_targetable_count} + {already_sent_count} + "
                       f"{suppressed_count} = {len(eligible) + no_email_count + not_targetable_count + already_sent_count + suppressed_count} "
                       f"— should match the {len(base_df)} leads reported above.")

            if not eligible.empty:
                preview_rows = []
                for _, row in eligible.head(10).iterrows():
                    subj, _ = email_template.render_for_lead(row.to_dict())
                    preview_rows.append({
                        "Company": row["company_name"], "Confidence": row["confidence_label"],
                        "State": row["state"], "Target Type": row.get("target_vertical", ""),
                        "Email": row["email"], "Subject": subj,
                    })
                st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, hide_index=True)
                if len(eligible) > 10:
                    st.caption(f"...and {len(eligible) - 10} more.")

            st.markdown("---")

            TEST_EMAIL_OPTIONS = [
                e.strip() for e in os.getenv("TEST_EMAIL_ADDRESSES", "").split(",") if e.strip()
            ] + ["Custom..."]

            # Test separately per target vertical actually present in the
            # current working set, in the service's own canonical order —
            # a mixed run (e.g. Event Management + Floral Businesses both
            # searched) has a DIFFERENT template per vertical
            # (email_template.VERTICAL_TEMPLATES), so one generic test send
            # only ever exercised whichever template load_template() falls
            # back to and never told you what a Floral lead would actually
            # receive. Falls back to a single row (no vertical) for a
            # service with no sub-verticals at all.
            verticals_for_service = get_target_verticals(active_service) or []
            ordered_labels = [v["label"] for v in verticals_for_service]
            present_verticals = set(base_df["target_vertical"].dropna().astype(str).str.strip()) - {""}
            test_verticals = [v for v in ordered_labels if v in present_verticals] or [""]

            st.markdown("**Send a test email**")
            if len(test_verticals) > 1:
                st.caption(f"{len(test_verticals)} target types are in your current leads for "
                           f"**{active_service}** — each has its own template, so send a separate "
                           f"test for each one below.")

            for v_label in test_verticals:
                row_key = v_label or "_none"
                row_title = v_label or active_service
                vcol1, vcol2 = st.columns([3, 1])
                with vcol1:
                    choice = st.selectbox(f"Send test email to — {row_title}", TEST_EMAIL_OPTIONS,
                                           key=f"test_email_choice_{row_key}")
                    if choice == "Custom...":
                        dest = st.text_input("Enter a custom test email address", key=f"test_email_custom_{row_key}")
                    else:
                        dest = choice
                with vcol2:
                    st.write("")  # vertical spacer so the button lines up with the selectbox, not its label
                    st.write("")
                    send_clicked = st.button(f"📧 Send — {row_title}", key=f"test_email_send_{row_key}")
                if send_clicked:
                    if not dest.strip():
                        st.error("Enter an email address first.")
                    else:
                        test_lead = {
                            "company_name": "Test Company", "service": active_service,
                            "target_vertical": v_label,
                            "state": state_names[0] if state_names else "Texas",
                            "phone": "(555) 123-4567", "email": "", "website": "https://example.com",
                        }
                        subj, body = email_template.render_for_lead(test_lead)
                        html_body = email_template.render_html_for_lead(test_lead)
                        inline_images = mailer.get_signature_inline_images()
                        success, error = mailer.send_email(smtp_config, dest.strip(), f"[TEST] {subj}",
                                                             body, html_body=html_body, inline_images=inline_images)
                        if success:
                            st.success(f"Test email ({row_title}) sent to {dest.strip()}. Check that inbox (and spam folder).")
                        else:
                            st.error(f"Send failed: {error}")

            delay_seconds = st.number_input("Delay between sends (seconds)", min_value=1.0, max_value=30.0,
                                              value=4.0, step=1.0,
                                              help="A gap between sends is normal outbound-email practice and "
                                                   "reduces the chance your account gets rate-limited or flagged.")

            if not eligible.empty:
                confirm = st.checkbox(f"I understand this will send {len(eligible)} real emails to real companies.")

                send_state = st.session_state.get("email_send_state")
                is_running = send_state is not None and not send_state.get("complete", False)

                if st.button("🚀 Send to all ready leads", type="primary", disabled=not confirm or is_running):
                    inline_images = mailer.get_signature_inline_images()
                    jobs = []
                    for _, row in eligible.iterrows():
                        lead_dict = row.to_dict()
                        subj, body = email_template.render_for_lead(lead_dict)
                        html_body = email_template.render_html_for_lead(lead_dict)
                        jobs.append({
                            "to": row["email"], "subject": subj, "body": body,
                            "html_body": html_body, "inline_images": inline_images,
                            "domain": row["domain"], "company_name": row["company_name"], "service": row["service"],
                            "target_vertical": row.get("target_vertical", ""),
                        })

                    new_state = {"done": 0, "total": len(jobs), "results": [], "complete": False,
                                 "stop_requested": False}
                    st.session_state.email_send_state = new_state
                    threading.Thread(
                        target=_run_email_batch_in_background,
                        args=(smtp_config, jobs, delay_seconds, new_state),
                        daemon=True,
                    ).start()
                    st.rerun()
            else:
                st.caption("Nothing eligible to send right now — adjust the filters above or run more searches.")

            # Separate from the block above so results/progress still show even
            # after a send completes and "eligible" drops to empty (everyone
            # just got marked as already-sent). Live-updating + has a Stop
            # button — see _render_send_progress's docstring for why.
            _render_send_progress("email_send_state", item_noun="emails")

            with st.expander("Suppression list (emails/domains to never contact)"):
                current = mailer.load_suppression()
                st.caption(f"{len(current)} entries currently suppressed.")
                new_entries = st.text_area("Add emails or domains (one per line)", height=80,
                                            key="suppression_input")
                if st.button("Add to suppression list"):
                    values = [v.strip() for v in new_entries.split("\n") if v.strip()]
                    if values:
                        mailer.add_to_suppression(values)
                        st.success(f"Added {len(values)} entr{'y' if len(values)==1 else 'ies'}.")
                        st.rerun()

            with st.expander("Sent history"):
                log = mailer.load_sent_log()
                if log.empty:
                    st.caption("No emails sent yet.")
                else:
                    st.dataframe(
                        log.sort_values("sent_at", ascending=False).head(200),
                        use_container_width=True, hide_index=True,
                    )
                    st.download_button(
                        "⬇ Download full send log (CSV)",
                        data=log.to_csv(index=False).encode("utf-8"),
                        file_name="sent_log.csv", mime="text/csv",
                    )

# ============================================================== SEND TEXTS ==
with tab_text:
    texting_config = texter.get_active_texting_config()

    if not texting_config:
        st.info(
            "Texting isn't configured yet. Add these to your `.env` file "
            "(see `.env.example`), then restart the app:\n\n"
            "```\nEIGHTXEIGHT_API_KEY=xxxxx\nEIGHTXEIGHT_SUBACCOUNT_ID=xxxxx\n```\n\n"
            "Get these from the [8x8 Developer Portal](https://developer.8x8.com/connect) "
            "under your existing 8x8 account — API key and subaccount ID, not something "
            "to invent. Texts send through that 8x8 account directly, no separate device "
            "or rented number needed.",
            icon="💬",
        )
    else:
        history_snapshots_t = storage.list_export_snapshots()
        with st.expander("📂 Send from a saved run or an uploaded file instead of the current database", expanded=False):
            if not history_snapshots_t:
                st.caption("No archived runs yet — every completed search auto-saves one here, "
                           "named `date_businesstypes_states.xlsx`.")
                selected_snapshots_t = []
            else:
                selected_snapshots_t = st.multiselect(
                    "Saved run(s) to send from",
                    [s["filename"] for s in history_snapshots_t],
                    help="Pick one or more. Their leads are combined (deduped by domain) and used "
                         "below instead of the current Lead Database tab view.",
                    key="sms_snapshot_picker",
                )
            uploaded_files_t = st.file_uploader(
                "Or upload Excel/CSV file(s) from your computer",
                type=["xlsx", "csv"], accept_multiple_files=True,
                help="Any of this app's own exports — a data/exports/ download, or the Lead Database "
                     "tab's CSV/Excel export — edited or not, from anywhere on your machine. Combined "
                     "with any saved run(s) picked above, deduped by domain. A file with no "
                     "service/confidence_label columns (not one of this app's own exports) will pass "
                     "through with 0 eligible leads below — the safety filters still apply.",
                key="sms_file_uploader",
            )

        source_frames_t = []
        source_labels_t = []
        upload_errors_t = []
        if selected_snapshots_t:
            source_frames_t.append(storage.load_export_snapshots(selected_snapshots_t))
            source_labels_t.append(f"{len(selected_snapshots_t)} saved run(s): {', '.join(selected_snapshots_t)}")
        if uploaded_files_t:
            uploaded_df_t, upload_errors_t = storage.load_uploaded_snapshots(uploaded_files_t)
            source_frames_t.append(uploaded_df_t)
            source_labels_t.append(f"{len(uploaded_files_t)} uploaded file(s): "
                                    f"{', '.join(f.name for f in uploaded_files_t)}")
        if upload_errors_t:
            st.warning("Couldn't read: " + "; ".join(upload_errors_t))

        using_custom_source_t = bool(source_frames_t)
        if using_custom_source_t:
            df = (storage.dedupe_keep_last(pd.concat(source_frames_t, ignore_index=True))
                  if len(source_frames_t) > 1 else source_frames_t[0])
        else:
            df = st.session_state.results_df
        if df.empty:
            st.info("No leads to send from. Run a search from **Find Leads**, or pick/upload a saved run above.")
        else:
            st.caption("Texting via 8x8. Each lead is rendered "
                       "with its own service's SMS template automatically.")

            active_sms_service = service or "Bookkeeping"

            # A/B/C variant picker (added 2026-09-18) — the client supplied
            # 3 alternate message bodies to run across different states/
            # business types and compare reply rates ("Original" is the
            # pre-existing default). Whichever variant is selected here
            # applies to the preview, test send, AND the real batch send
            # below — so testing "variant X on state Y" is just: filter
            # Lead Database to state Y, pick variant X here, send. Wording
            # is the client's own verbatim; edit at your own risk here —
            # the editor below will let you, but the point of an A/B/C
            # test is comparing the ORIGINAL wording as given.
            sms_variants = email_template.get_sms_variants(active_sms_service)
            variant_names = list(sms_variants.keys())
            variant_picker_key = f"sms_variant_choice__{active_sms_service}"
            if variant_picker_key not in st.session_state or st.session_state[variant_picker_key] not in variant_names:
                st.session_state[variant_picker_key] = variant_names[0]
            active_sms_variant = st.selectbox(
                "Message variant to use for this batch", variant_names, key=variant_picker_key,
                help="Applies to the preview below, the test text, and the real send — all from the "
                     "same variant. Switch variants and send separately per state/business type to "
                     "run the A/B/C test across them.",
            )

            sms_widget_key = f"sms_editor__{active_sms_service}__{active_sms_variant}"
            if sms_widget_key not in st.session_state:
                st.session_state[sms_widget_key] = email_template.load_sms_template(active_sms_service, active_sms_variant)

            st.markdown(f"**SMS template — {active_sms_service} / {active_sms_variant}**")
            sms_template_text = st.text_area("SMS Template", height=100, key=sms_widget_key,
                                              help="Keep it short — carriers charge/segment by length, "
                                                   "and it's read at a glance, not skimmed like an email.")
            tcol1, tcol2 = st.columns(2)
            with tcol1:
                if st.button("💾 Save SMS template", type="primary"):
                    email_template.save_sms_template(active_sms_service, sms_template_text, active_sms_variant)
                    st.success(f"Saved {active_sms_variant} for {active_sms_service}.")
            with tcol2:
                if st.button("↺ Reset SMS to default"):
                    email_template.reset_sms_template(active_sms_service, active_sms_variant)
                    st.session_state[sms_widget_key] = email_template.get_default_sms_for_service(active_sms_service, active_sms_variant)
                    st.rerun()

            st.markdown("---")

            if using_custom_source_t:
                base_df_t = df
            else:
                base_df_t = st.session_state.lead_db_view if st.session_state.lead_db_view is not None else df

            active_service_t = service or "Bookkeeping"
            pre_filter_count_t = len(base_df_t)
            base_df_t = base_df_t[base_df_t["service"] == active_service_t]
            base_df_t = base_df_t[base_df_t["confidence_label"] == "High"]
            excluded_by_safety_t = pre_filter_count_t - len(base_df_t)

            source_desc_t = (" + ".join(source_labels_t) if using_custom_source_t else
                              f"the **Lead Database** tab's current filter ({st.session_state.lead_db_view_desc})")
            st.info(f"Working from {source_desc_t} "
                    f"— then restricted to **{active_service_t}** service and **High confidence only** "
                    f"(fixed safety rules, not adjustable here) — {len(base_df_t)} leads remain"
                    + (f", {excluded_by_safety_t} excluded by those two rules" if excluded_by_safety_t else "") + ".",
                    icon="🔗")

            no_email_only = st.checkbox(
                "Only leads with no email", value=True,
                help="Texting is the fallback channel for leads you can't reach by email. "
                     "Uncheck to text everyone with a phone number, including leads that also have an email.",
            )

            eligible = base_df_t.copy()
            eligible = eligible[eligible["phone"].astype(str).str.strip().str.len() > 0]
            if no_email_only:
                eligible = eligible[eligible["email"].astype(str).str.strip().str.len() == 0]

            suppressed_mask = pd.Series(
                [mailer.is_suppressed(phone=row["phone"]) for _, row in eligible.iterrows()],
                index=eligible.index, dtype=bool,
            )
            suppressed_count = int(suppressed_mask.sum())
            eligible = eligible[~suppressed_mask]

            already_texted_mask = eligible["phone"].apply(
                lambda p: texter.to_e164(p) in texter.already_texted_numbers() if p else False
            )
            already_texted_count = int(already_texted_mask.sum())
            eligible = eligible[~already_texted_mask]

            m1, m2, m3 = st.columns(3)
            m1.metric("Ready to text", len(eligible))
            m2.metric("Already texted (skipped)", already_texted_count)
            m3.metric("Suppressed (skipped)", suppressed_count)

            if not eligible.empty:
                preview_rows = []
                for _, row in eligible.head(10).iterrows():
                    body = email_template.render_sms_for_lead(row.to_dict(), active_sms_variant)
                    preview_rows.append({
                        "Company": row["company_name"], "Confidence": row["confidence_label"],
                        "State": row["state"], "Target Type": row.get("target_vertical", ""),
                        "Phone": row["phone"], "Message": body,
                    })
                st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, hide_index=True)
                if len(eligible) > 10:
                    st.caption(f"...and {len(eligible) - 10} more.")

            st.markdown("---")
            tcol1, tcol2 = st.columns(2)
            with tcol1:
                # Mirrors the email tab's TEST_EMAIL_ADDRESSES pattern — a
                # known-numbers dropdown instead of retyping a number every
                # time, with "Custom..." always available. SMS templates
                # are service-level only (no per-vertical split like email's
                # VERTICAL_TEMPLATES — see email_template.load_sms_template),
                # so unlike Send Emails this only ever needs ONE test row —
                # the variant picker above covers what per-vertical testing
                # covers for email.
                TEST_TEXT_OPTIONS = [
                    n.strip() for n in os.getenv("TEST_TEXT_NUMBERS", "").split(",") if n.strip()
                ] + ["Custom..."]
                test_number_choice = st.selectbox("Send test text to", TEST_TEXT_OPTIONS)
                if test_number_choice == "Custom...":
                    test_number = st.text_input("Enter a custom test phone number", placeholder="+1 512 555 1234")
                else:
                    test_number = test_number_choice
                if st.button("💬 Send test text"):
                    if not test_number.strip():
                        st.error("Enter a phone number first.")
                    else:
                        test_lead = {
                            "company_name": "Test Company", "service": active_sms_service,
                            "state": state_names[0] if state_names else "Texas", "owner_name": "",
                        }
                        body = email_template.render_sms_for_lead(test_lead, active_sms_variant)
                        success, error = texter.send_text(texting_config, test_number.strip(), f"TEST SMS: {body}")
                        if success:
                            st.success(f"Test text ({active_sms_variant}) sent to {test_number.strip()}.")
                        else:
                            st.error(f"Send failed: {error}")
            with tcol2:
                text_delay_seconds = st.number_input("Delay between texts (seconds)", min_value=1.0, max_value=30.0,
                                                       value=3.0, step=1.0, key="text_delay")

            if not eligible.empty:
                confirm_text = st.checkbox(f"I understand this will text {len(eligible)} real phone numbers.")

                text_send_state = st.session_state.get("text_send_state")
                is_running_t = text_send_state is not None and not text_send_state.get("complete", False)

                if st.button("🚀 Send to all ready leads", type="primary",
                             disabled=not confirm_text or is_running_t, key="send_texts_btn"):
                    jobs = []
                    for _, row in eligible.iterrows():
                        body = email_template.render_sms_for_lead(row.to_dict(), active_sms_variant)
                        e164 = texter.to_e164(row["phone"])
                        if not e164:
                            continue
                        jobs.append({
                            "to": e164, "body": body,
                            "company_name": row["company_name"], "service": row["service"],
                            "variant": active_sms_variant,
                        })

                    new_state = {"done": 0, "total": len(jobs), "results": [], "complete": False,
                                 "stop_requested": False}
                    st.session_state.text_send_state = new_state
                    threading.Thread(
                        target=_run_text_batch_in_background,
                        args=(texting_config, jobs, text_delay_seconds, new_state),
                        daemon=True,
                    ).start()
                    st.rerun()
            else:
                st.caption("Nothing eligible to text right now — adjust the filters above or run more searches.")

            # Separate from the block above so results/progress still show even
            # after a send completes and "eligible" drops to empty. Live-
            # updating + has a Stop button — see _render_send_progress.
            _render_send_progress("text_send_state", item_noun="texts")

            with st.expander("Sent text history"):
                text_log = texter.load_sent_text_log()
                if text_log.empty:
                    st.caption("No texts sent yet.")
                else:
                    st.dataframe(
                        text_log.sort_values("sent_at", ascending=False).head(200),
                        use_container_width=True, hide_index=True,
                    )
                    st.download_button(
                        "⬇ Download full text log (CSV)",
                        data=text_log.to_csv(index=False).encode("utf-8"),
                        file_name="sent_text_log.csv", mime="text/csv",
                    )

# ============================================================== BULK SEND ==
with tab_bulk:
    smtp_config = mailer.get_smtp_config()
    texting_config = texter.get_active_texting_config()

    if not smtp_config and not texting_config:
        st.info(
            "Neither email nor texting is configured yet — set up SMTP in the Send Emails tab "
            "and/or 8x8 in the Send Texts tab first.",
            icon="📨",
        )
    else:
        st.caption(
            "Row by row, for whichever leads you point this at: **email if the company has a "
            "sendable email, text if it doesn't, both if it has both.** Draws from the same "
            "saved-run/uploaded-file picker as Send Emails and Send Texts."
            + ("" if smtp_config else " Email is off (SMTP not configured) — this will only text.")
            + ("" if texting_config else " Texting is off (8x8 not configured) — this will only email.")
        )

        history_snapshots_b = storage.list_export_snapshots()
        with st.expander("📂 Send from a saved run or an uploaded file instead of the current database", expanded=False):
            if not history_snapshots_b:
                st.caption("No archived runs yet — every completed search auto-saves one here, "
                           "named `date_businesstypes_states.xlsx`.")
                selected_snapshots_b = []
            else:
                selected_snapshots_b = st.multiselect(
                    "Saved run(s) to send from",
                    [s["filename"] for s in history_snapshots_b],
                    help="Pick one or more. Their leads are combined (deduped by domain) and used "
                         "below instead of the current Lead Database tab view.",
                    key="bulk_snapshot_picker",
                )
            uploaded_files_b = st.file_uploader(
                "Or upload Excel/CSV file(s) from your computer",
                type=["xlsx", "csv"], accept_multiple_files=True,
                help="Any of this app's own exports — a data/exports/ download, or the Lead Database "
                     "tab's CSV/Excel export — edited or not, from anywhere on your machine. Combined "
                     "with any saved run(s) picked above, deduped by domain. A file with no "
                     "service/confidence_label columns (not one of this app's own exports) will pass "
                     "through with 0 eligible leads below — the safety filters still apply.",
                key="bulk_file_uploader",
            )

        source_frames_b = []
        source_labels_b = []
        upload_errors_b = []
        if selected_snapshots_b:
            source_frames_b.append(storage.load_export_snapshots(selected_snapshots_b))
            source_labels_b.append(f"{len(selected_snapshots_b)} saved run(s): {', '.join(selected_snapshots_b)}")
        if uploaded_files_b:
            uploaded_df_b, upload_errors_b = storage.load_uploaded_snapshots(uploaded_files_b)
            source_frames_b.append(uploaded_df_b)
            source_labels_b.append(f"{len(uploaded_files_b)} uploaded file(s): "
                                    f"{', '.join(f.name for f in uploaded_files_b)}")
        if upload_errors_b:
            st.warning("Couldn't read: " + "; ".join(upload_errors_b))

        using_custom_source_b = bool(source_frames_b)
        if using_custom_source_b:
            df_b = (storage.dedupe_keep_last(pd.concat(source_frames_b, ignore_index=True))
                    if len(source_frames_b) > 1 else source_frames_b[0])
        else:
            df_b = st.session_state.results_df

        if df_b.empty:
            st.info("No leads to send from. Run a search from **Find Leads**, or pick/upload a saved run above.")
        else:
            if using_custom_source_b:
                base_df_b = df_b
            else:
                base_df_b = st.session_state.lead_db_view if st.session_state.lead_db_view is not None else df_b

            # Same two hard, non-adjustable safety filters as Send Emails/
            # Send Texts — see those tabs' own comments for why.
            active_service_b = service or "Bookkeeping"
            pre_filter_count_b = len(base_df_b)
            base_df_b = base_df_b[base_df_b["service"] == active_service_b]
            base_df_b = base_df_b[base_df_b["confidence_label"] == "High"]
            excluded_by_safety_b = pre_filter_count_b - len(base_df_b)

            source_desc_b = (" + ".join(source_labels_b) if using_custom_source_b else
                              f"the **Lead Database** tab's current filter ({st.session_state.lead_db_view_desc})")
            st.info(f"Working from {source_desc_b} "
                    f"— then restricted to **{active_service_b}** service and **High confidence only** "
                    f"(fixed safety rules, not adjustable here) — {len(base_df_b)} leads remain"
                    + (f", {excluded_by_safety_b} excluded by those two rules" if excluded_by_safety_b else "") + ".",
                    icon="🔗")

            bcol1, bcol2 = st.columns(2)
            with bcol1:
                skip_emailed_b = st.checkbox("Skip leads already emailed", value=True,
                                              disabled=not smtp_config, key="bulk_skip_emailed")
            with bcol2:
                skip_texted_b = st.checkbox("Skip leads already texted", value=True,
                                             disabled=not texting_config, key="bulk_skip_texted")

            # --- Classify each row independently per channel — this is
            # the whole feature: NOT "email, falling back to text only if
            # no email", but "send every channel this row actually
            # qualifies for". A row with both ends up in both pools. Each
            # pool's rules are copied from that channel's own standalone
            # tab exactly (targetable-email screening, suppression,
            # already-sent dedup for email; non-empty phone, suppression,
            # already-texted dedup for text — texting here deliberately
            # does NOT restrict to "no email only" the way the standalone
            # Send Texts tab defaults to, since having an email is no
            # longer a reason to skip texting in this combined flow). ---
            work = base_df_b.copy()

            if smtp_config:
                has_email = work["email"].astype(str).str.strip().str.len() > 0
                targetable = work["email"].apply(lambda e: blocklist.is_targetable_email(e) if e else False)
                email_suppressed = pd.Series(
                    [mailer.is_suppressed(row["email"], row["domain"]) if row["email"] else False
                     for _, row in work.iterrows()],
                    index=work.index, dtype=bool,
                )
                email_ok = has_email & targetable & ~email_suppressed
                if skip_emailed_b:
                    sent_domains_b = mailer.already_sent_domains()
                    email_ok = email_ok & ~work["domain"].isin(sent_domains_b)
            else:
                email_ok = pd.Series(False, index=work.index)

            if texting_config:
                has_phone = work["phone"].astype(str).str.strip().str.len() > 0
                phone_suppressed = pd.Series(
                    [mailer.is_suppressed(phone=row["phone"]) if row["phone"] else False
                     for _, row in work.iterrows()],
                    index=work.index, dtype=bool,
                )
                text_ok = has_phone & ~phone_suppressed
                if skip_texted_b:
                    texted_numbers_b = texter.already_texted_numbers()
                    already_texted = work["phone"].apply(
                        lambda p: texter.to_e164(p) in texted_numbers_b if p else False
                    )
                    text_ok = text_ok & ~already_texted
            else:
                text_ok = pd.Series(False, index=work.index)

            email_only_count = int((email_ok & ~text_ok).sum())
            text_only_count = int((text_ok & ~email_ok).sum())
            both_count = int((email_ok & text_ok).sum())
            neither_count = int((~email_ok & ~text_ok).sum())

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Email only", email_only_count)
            m2.metric("Text only", text_only_count)
            m3.metric("Both", both_count)
            m4.metric("Excluded (neither)", neither_count,
                      help="No sendable email AND no usable phone, already contacted on every "
                           "available channel, or suppressed.")

            # SMS variant — same picker as Send Texts, read-only preview
            # here (edit it from the Send Texts tab; this just selects
            # which saved variant this batch's texts use).
            active_bulk_sms_variant = email_template.DEFAULT_SMS_VARIANT
            if texting_config and text_only_count + both_count > 0:
                bulk_sms_variants = email_template.get_sms_variants(active_service_b)
                bulk_variant_names = list(bulk_sms_variants.keys())
                bulk_variant_key = f"bulk_sms_variant_choice__{active_service_b}"
                if bulk_variant_key not in st.session_state or st.session_state[bulk_variant_key] not in bulk_variant_names:
                    st.session_state[bulk_variant_key] = bulk_variant_names[0]
                active_bulk_sms_variant = st.selectbox(
                    "Message variant to use for the text side of this batch", bulk_variant_names,
                    key=bulk_variant_key,
                    help="Same variants as the Send Texts tab (edit them there). Applies to every "
                         "text this batch sends.",
                )

            combined = (email_ok | text_ok)
            preview_pool = work[combined]
            if not preview_pool.empty:
                preview_rows_b = []
                for _, row in preview_pool.head(10).iterrows():
                    gets_email = bool(email_ok.get(row.name, False))
                    gets_text = bool(text_ok.get(row.name, False))
                    channels = " + ".join(filter(None, ["Email" if gets_email else "", "Text" if gets_text else ""]))
                    subj = ""
                    if gets_email:
                        subj, _ = email_template.render_for_lead(row.to_dict())
                    preview_rows_b.append({
                        "Company": row["company_name"], "State": row["state"],
                        "Target Type": row.get("target_vertical", ""), "Channels": channels,
                        "Email": row["email"] if gets_email else "", "Phone": row["phone"] if gets_text else "",
                        "Email subject": subj,
                    })
                st.dataframe(pd.DataFrame(preview_rows_b), use_container_width=True, hide_index=True)
                if len(preview_pool) > 10:
                    st.caption(f"...and {len(preview_pool) - 10} more.")

            dcol1, dcol2 = st.columns(2)
            with dcol1:
                bulk_email_delay = st.number_input("Delay between emails (seconds)", min_value=1.0, max_value=30.0,
                                                     value=4.0, step=1.0, key="bulk_email_delay",
                                                     disabled=email_only_count + both_count == 0)
            with dcol2:
                bulk_text_delay = st.number_input("Delay between texts (seconds)", min_value=1.0, max_value=30.0,
                                                    value=3.0, step=1.0, key="bulk_text_delay",
                                                    disabled=text_only_count + both_count == 0)

            total_to_send = email_only_count + text_only_count + both_count
            if total_to_send:
                confirm_bulk = st.checkbox(
                    f"I understand this will send {email_only_count + both_count} email(s) and "
                    f"{text_only_count + both_count} text(s), to {total_to_send} real compan"
                    f"{'y' if total_to_send == 1 else 'ies'}."
                )

                bulk_send_state = st.session_state.get("bulk_send_state")
                bulk_is_running = bulk_send_state is not None and not bulk_send_state.get("complete", False)

                if st.button("🚀 Send bulk batch", type="primary",
                             disabled=not confirm_bulk or bulk_is_running, key="send_bulk_btn"):
                    inline_images_b = mailer.get_signature_inline_images()
                    email_jobs_b = []
                    text_jobs_b = []
                    for _, row in work[combined].iterrows():
                        lead_dict = row.to_dict()
                        if bool(email_ok.get(row.name, False)):
                            subj, body = email_template.render_for_lead(lead_dict)
                            html_body = email_template.render_html_for_lead(lead_dict)
                            email_jobs_b.append({
                                "to": row["email"], "subject": subj, "body": body,
                                "html_body": html_body, "inline_images": inline_images_b,
                                "domain": row["domain"], "company_name": row["company_name"],
                                "service": row["service"],
                            })
                        if bool(text_ok.get(row.name, False)):
                            sms_body = email_template.render_sms_for_lead(lead_dict, active_bulk_sms_variant)
                            e164_b = texter.to_e164(row["phone"])
                            if e164_b:
                                text_jobs_b.append({
                                    "to": e164_b, "body": sms_body,
                                    "company_name": row["company_name"], "service": row["service"],
                                    "variant": active_bulk_sms_variant,
                                })

                    new_bulk_state = {
                        "email_done": 0, "email_total": len(email_jobs_b),
                        "text_done": 0, "text_total": len(text_jobs_b),
                        "email_results": [], "text_results": [], "complete": False,
                        "stop_requested": False,
                    }
                    st.session_state.bulk_send_state = new_bulk_state
                    threading.Thread(
                        target=_run_bulk_batch_in_background,
                        args=(smtp_config, texting_config, email_jobs_b, text_jobs_b,
                              bulk_email_delay, bulk_text_delay, new_bulk_state),
                        daemon=True,
                    ).start()
                    st.rerun()

            # Live-updating + has a Stop button — see _render_bulk_send_progress.
            _render_bulk_send_progress("bulk_send_state")