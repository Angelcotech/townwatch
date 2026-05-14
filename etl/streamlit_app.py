"""
TownWatch — Phase 1 prototype dashboard.

Profile-first: the Official Profile is the central experience. Everything
else (votes, motions, findings, decisions) is a SECTION inside a profile.

Landing page: roster (current + historical + candidates).
Click any official → full profile.

Run:
    cd etl
    source .venv/bin/activate
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from townwatch_etl.db import connect


# =====================================================================
# Page config + styling
# =====================================================================

st.set_page_config(
    page_title="TownWatch — Grovetown, GA",
    page_icon="🏛",
    layout="wide",
)

NAVY = "#1A2B4A"
LIGHT_BG = "#FAFAF7"
CARD_BORDER = "#E5E7EB"

st.markdown(
    f"""
    <style>
      .stApp {{ background: {LIGHT_BG}; }}
      .block-container {{ padding-top: 2rem; padding-bottom: 4rem; max-width: 1200px; }}
      h1, h2, h3, h4 {{ color: {NAVY}; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; letter-spacing: -0.01em; }}
      h1 {{ font-size: 2rem; font-weight: 700; }}
      h2 {{ font-size: 1.4rem; font-weight: 600; margin-top: 2rem; }}
      h3 {{ font-size: 1.1rem; font-weight: 600; }}
      .official-card {{
        background: white;
        border: 1px solid {CARD_BORDER};
        border-radius: 10px;
        padding: 1rem 1.25rem;
        margin-bottom: 0.5rem;
        cursor: pointer;
      }}
      .official-name {{ font-size: 1.05rem; font-weight: 600; color: {NAVY}; }}
      .official-meta {{ font-size: 0.85rem; color: #555; margin-top: 0.25rem; }}
      .stat-pill {{
        display: inline-block;
        background: #F3F4F6;
        padding: 2px 10px;
        border-radius: 99px;
        font-size: 0.85rem;
        margin-right: 0.5rem;
        color: {NAVY};
      }}
      .finding-card {{
        background: white;
        border-left: 4px solid {NAVY};
        padding: 0.75rem 1rem;
        margin-bottom: 0.5rem;
        border-radius: 4px;
      }}
      .stMetric {{ background: white; padding: 0.75rem 1rem; border-radius: 8px; border: 1px solid {CARD_BORDER}; }}
      [data-testid="stMetricLabel"] {{ font-size: 0.8rem; color: #6B7280; }}
      [data-testid="stMetricValue"] {{ color: {NAVY}; font-weight: 600; }}
    </style>
    """,
    unsafe_allow_html=True,
)


# =====================================================================
# Navigation state
# =====================================================================

if "view" not in st.session_state:
    st.session_state.view = "roster"  # "roster" | "profile"
if "selected_official_id" not in st.session_state:
    st.session_state.selected_official_id = None


def go_to_profile(official_id: int) -> None:
    st.session_state.view = "profile"
    st.session_state.selected_official_id = official_id


def go_to_roster() -> None:
    st.session_state.view = "roster"
    st.session_state.selected_official_id = None


# =====================================================================
# Data loaders
# =====================================================================

@st.cache_data(ttl=300)
def load_roster() -> pd.DataFrame:
    with connect() as conn:
        rows = conn.execute("""
            SELECT
                o.id, o.canonical_name, o.first_name, o.last_name,
                o.party_affiliation,
                COUNT(DISTINCT v.id) AS votes,
                MIN(mtg.meeting_date) AS first_vote,
                MAX(mtg.meeting_date) AS last_vote,
                BOOL_OR(t.is_current) AS is_current,
                (
                    SELECT s.name FROM term t2
                    JOIN seat s ON s.id = t2.seat_id
                    WHERE t2.official_id = o.id AND t2.is_current = true
                    LIMIT 1
                ) AS current_seat
            FROM official o
            LEFT JOIN vote v ON v.official_id = o.id
            LEFT JOIN motion m ON m.id = v.motion_id
            LEFT JOIN meeting mtg ON mtg.id = m.meeting_id
            LEFT JOIN term t ON t.official_id = o.id
            GROUP BY o.id, o.canonical_name, o.first_name, o.last_name, o.party_affiliation
            HAVING COUNT(DISTINCT v.id) > 0 OR BOOL_OR(t.is_current) IS TRUE
            ORDER BY BOOL_OR(t.is_current) DESC NULLS LAST, COUNT(DISTINCT v.id) DESC
        """).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["first_vote"] = pd.to_datetime(df["first_vote"], errors="coerce")
        df["last_vote"] = pd.to_datetime(df["last_vote"], errors="coerce")
        df["years"] = (df["last_vote"] - df["first_vote"]).dt.days / 365.25
        df["years"] = df["years"].fillna(0).round(1)
        df["is_current"] = df["is_current"].fillna(False)
    return df


@st.cache_data(ttl=300)
def load_db_stats() -> dict[str, Any]:
    with connect() as conn:
        return {
            "officials":  conn.execute("SELECT COUNT(*) AS n FROM official").fetchone()["n"],
            "votes":      conn.execute("SELECT COUNT(*) AS n FROM vote").fetchone()["n"],
            "motions":    conn.execute("SELECT COUNT(*) AS n FROM motion").fetchone()["n"],
            "meetings":   conn.execute("SELECT COUNT(*) AS n FROM meeting").fetchone()["n"],
            "findings":   conn.execute("SELECT COUNT(*) AS n FROM finding").fetchone()["n"],
            "earliest":   conn.execute("SELECT MIN(meeting_date) AS d FROM meeting").fetchone()["d"],
            "latest":     conn.execute("SELECT MAX(meeting_date) AS d FROM meeting").fetchone()["d"],
        }


@st.cache_data(ttl=300)
def load_official_record(official_id: int) -> dict[str, Any]:
    with connect() as conn:
        official = conn.execute("""
            SELECT o.id, o.canonical_name, o.first_name, o.last_name, o.party_affiliation,
                   o.email, o.phone, o.official_website, o.bio_text,
                   COUNT(DISTINCT v.id) AS votes,
                   MIN(mtg.meeting_date) AS first_vote,
                   MAX(mtg.meeting_date) AS last_vote,
                   BOOL_OR(t.is_current) AS is_current
            FROM official o
            LEFT JOIN vote v ON v.official_id = o.id
            LEFT JOIN motion m ON m.id = v.motion_id
            LEFT JOIN meeting mtg ON mtg.id = m.meeting_id
            LEFT JOIN term t ON t.official_id = o.id
            WHERE o.id = %s
            GROUP BY o.id
        """, (official_id,)).fetchone()
        if not official:
            return {}

        terms = conn.execute("""
            SELECT t.id, t.start_date, t.end_date, t.how_seated, t.is_current,
                   s.name AS seat_name, gb.name AS body_name, j.display_name AS jurisdiction
            FROM term t
            JOIN seat s ON s.id = t.seat_id
            JOIN governing_body gb ON gb.id = s.governing_body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            WHERE t.official_id = %s
            ORDER BY t.start_date DESC
        """, (official_id,)).fetchall()

        breakdown = conn.execute("""
            SELECT m.motion_type,
                   COUNT(*) AS total,
                   SUM(CASE WHEN v.vote_value = 'yes' THEN 1 ELSE 0 END) AS yes_count,
                   SUM(CASE WHEN v.vote_value = 'no' THEN 1 ELSE 0 END) AS no_count,
                   SUM(CASE WHEN v.vote_value = 'abstain' THEN 1 ELSE 0 END) AS abstain_count,
                   SUM(CASE WHEN v.vote_value = 'conflict_recusal' THEN 1 ELSE 0 END) AS recusal_count
            FROM vote v
            JOIN motion m ON m.id = v.motion_id
            WHERE v.official_id = %s
            GROUP BY m.motion_type
            ORDER BY total DESC
        """, (official_id,)).fetchall()

        findings = conn.execute("""
            SELECT pattern_id, severity, title, explanation, metrics
            FROM finding
            WHERE subject_official_id = %s
            ORDER BY severity DESC
        """, (official_id,)).fetchall()

        aliases = conn.execute("""
            SELECT alias_name, source_system FROM official_alias
            WHERE official_id = %s ORDER BY alias_name
        """, (official_id,)).fetchall()

        # Per-motion-type drill-down
        detail = conn.execute("""
            SELECT m.id AS motion_id, m.motion_type, m.title, m.description, m.outcome,
                   m.vote_tally_yes, m.vote_tally_no, m.motion_number,
                   v.vote_value, v.notes, mtg.meeting_date
            FROM vote v
            JOIN motion m ON m.id = v.motion_id
            JOIN meeting mtg ON mtg.id = m.meeting_id
            WHERE v.official_id = %s
            ORDER BY mtg.meeting_date DESC
        """, (official_id,)).fetchall()

    return {
        "official": dict(official),
        "terms": [dict(t) for t in terms],
        "breakdown": [dict(b) for b in breakdown],
        "findings": [dict(f) for f in findings],
        "aliases": [dict(a) for a in aliases],
        "detail": [dict(d) for d in detail],
    }


# =====================================================================
# UI components
# =====================================================================

def render_header() -> None:
    stats = load_db_stats()
    col_title, col_action = st.columns([3, 1])
    with col_title:
        st.markdown("# 🏛 TownWatch — Grovetown, GA")
        st.caption(
            f"Indexed: {stats['earliest']} → {stats['latest']} · "
            f"{stats['officials']} officials · {stats['motions']:,} motions · "
            f"{stats['votes']:,} votes · {stats['findings']} findings"
        )
    with col_action:
        if st.session_state.view == "profile":
            if st.button("← Back to roster", use_container_width=True):
                go_to_roster()
                st.rerun()


def render_roster() -> None:
    df = load_roster()
    if df.empty:
        st.info("No officials in the database yet.")
        return

    current = df[df["is_current"] == True]  # noqa: E712
    historical = df[df["is_current"] != True]  # noqa: E712

    # ---- Current officials ----
    st.markdown("## Current officials")
    st.caption(f"{len(current)} active member" + ("s" if len(current) != 1 else ""))

    if not current.empty:
        cols = st.columns(min(len(current), 4))
        for i, (_, row) in enumerate(current.iterrows()):
            with cols[i % len(cols)]:
                _render_official_card(row, current=True)

    # ---- Historical officials ----
    st.markdown("## Historical officials")
    st.caption(f"{len(historical)} past member" + ("s" if len(historical) != 1 else ""))

    if not historical.empty:
        cols = st.columns(4)
        for i, (_, row) in enumerate(historical.iterrows()):
            with cols[i % 4]:
                _render_official_card(row, current=False)

    # ---- Candidates section (data not yet ingested) ----
    st.markdown("## Candidates")
    st.caption("Upcoming and recently-filed candidates for Grovetown offices")

    st.info(
        "**Mayor — Special Election, November 2026**\n\n"
        "The Mayor seat is currently vacant. Council voted (Apr 13, 2026) "
        "to call a special election to fill the unexpired term. Qualifying "
        "fee set at **$612**.\n\n"
        "Candidate filings are not yet tracked in TownWatch. "
        "We'll list candidates here as they file with the City Clerk."
    )


def _render_official_card(row: pd.Series, *, current: bool) -> None:
    label = "Current" if current else "Historical"
    seat_or_status = row.get("current_seat") or label
    years_part = f" · {row['years']}yr" if row["years"] else ""
    if st.button(
        f"**{row['canonical_name']}**\n\n"
        f"{seat_or_status} · {int(row['votes']):,} votes{years_part}",
        key=f"official_{row['id']}",
        use_container_width=True,
    ):
        go_to_profile(int(row["id"]))
        st.rerun()


def render_profile(official_id: int) -> None:
    rec = load_official_record(official_id)
    if not rec:
        st.error("Official not found.")
        return

    o = rec["official"]
    terms = rec["terms"]
    breakdown = rec["breakdown"]
    findings = rec["findings"]
    aliases = rec["aliases"]
    detail = rec["detail"]

    # ---- Header ----
    st.markdown(f"# {o['canonical_name']}")
    current_term = next((t for t in terms if t["is_current"]), None)
    if current_term:
        st.caption(
            f"{current_term['seat_name']} · {current_term['body_name']} · "
            f"{current_term['jurisdiction']} · current"
        )
    elif terms:
        t = terms[0]
        st.caption(
            f"Formerly: {t['seat_name']} · {t['body_name']} · "
            f"{t['jurisdiction']}"
        )
    else:
        st.caption("No term record on file — historical vote record only.")

    # ---- Headline stats ----
    total_votes = int(o["votes"] or 0)
    if total_votes > 0 and o["first_vote"] and o["last_vote"]:
        years = round((o["last_vote"] - o["first_vote"]).days / 365.25, 1)
    else:
        years = 0
    yes_count = sum(int(b["yes_count"]) for b in breakdown)
    yes_pct = int(yes_count * 100 / max(total_votes, 1)) if total_votes else 0
    recusals = sum(int(b["recusal_count"]) for b in breakdown)
    no_count = sum(int(b["no_count"]) for b in breakdown)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Years in record", f"{years}")
    c2.metric("Total votes", f"{total_votes:,}")
    c3.metric("Yes rate", f"{yes_pct}%")
    c4.metric("Recusals", f"{recusals}")

    # ---- Flagged findings about this person ----
    if findings:
        st.markdown("## Findings flagged for this official")
        for f in findings:
            with st.container():
                st.markdown(
                    f'<div class="finding-card"><strong>{f["title"]}</strong><br>'
                    f'<span style="font-size:0.8rem;color:#6B7280;">'
                    f'pattern: {f["pattern_id"]} · severity {f["severity"]}'
                    f'</span></div>',
                    unsafe_allow_html=True,
                )
                if f.get("explanation"):
                    with st.expander("Why this is flagged"):
                        st.write(f["explanation"])

    # ---- Voting record by topic ----
    if breakdown:
        st.markdown("## Voting record by topic")
        bd_df = pd.DataFrame(breakdown)
        bd_df["yes_pct"] = (bd_df["yes_count"] * 100 / bd_df["total"]).round(0).astype(int)
        display = bd_df[[
            "motion_type", "yes_pct", "total",
            "yes_count", "no_count", "abstain_count", "recusal_count",
        ]].rename(columns={
            "motion_type": "Topic",
            "yes_pct": "% Yes",
            "total": "Total",
            "yes_count": "Yes",
            "no_count": "No",
            "abstain_count": "Abstain",
            "recusal_count": "Recusal",
        })
        st.dataframe(
            display, use_container_width=True, hide_index=True,
            column_config={
                "% Yes": st.column_config.ProgressColumn(
                    "% Yes", format="%d%%", min_value=0, max_value=100,
                ),
            },
        )

    # ---- Decisions by topic (drill-down) ----
    if detail:
        st.markdown("## Decisions by topic")
        st.caption(
            "Every vote this official cast, grouped by topic. Dissents "
            "(no / abstain / recusal — the rare events) sort to the top of each group."
        )
        detail_df = pd.DataFrame(detail)
        for _, row in pd.DataFrame(breakdown).iterrows():
            mtype = row["motion_type"]
            count = int(row["total"])
            yes_pct = int(row["yes_count"] * 100 / max(count, 1))
            dissents = int(row["no_count"]) + int(row["abstain_count"]) + int(row["recusal_count"])
            label = f"{mtype}  ·  {count} votes  ·  {yes_pct}% yes"
            if dissents > 0:
                label += f"  ·  ⓘ {dissents} dissent" + ("s" if dissents != 1 else "")

            with st.expander(label):
                cat = detail_df[detail_df["motion_type"] == mtype].copy()
                if cat.empty:
                    st.caption("No detail rows.")
                    continue
                cat["is_dissent"] = cat["vote_value"].isin(["no", "abstain", "conflict_recusal"])
                cat = cat.sort_values(["is_dissent", "meeting_date"], ascending=[False, False])

                for _, m in cat.iterrows():
                    emoji = {
                        "yes": "✓", "no": "✗", "abstain": "○",
                        "conflict_recusal": "⚠", "absent": "—",
                    }.get(m["vote_value"], "?")
                    date_str = m["meeting_date"].strftime("%Y-%m-%d") if hasattr(m["meeting_date"], "strftime") else str(m["meeting_date"])
                    outcome_tag = f" → {m['outcome']}" if m["outcome"] != "passed" else ""
                    tally = f"({m['vote_tally_yes']}-{m['vote_tally_no']})"
                    st.markdown(f"**{emoji} {date_str}** · {m['title']} {tally}{outcome_tag}")
                    if m["description"]:
                        st.caption(m["description"])
                    if m["notes"]:
                        st.info(f"📌 {m['notes']}")
                    st.markdown("---")

    # ---- Career history (terms) ----
    if terms:
        st.markdown("## Career on this body")
        for t in terms:
            tag = " (current)" if t["is_current"] else ""
            end = t["end_date"] or "present"
            st.markdown(f"- **{t['seat_name']}** · {t['start_date']} → {end}{tag}")

    # ---- Property holdings (placeholder) ----
    st.markdown("## Property & business interests")
    st.warning(
        "Property records and business affiliations are not yet captured for this official. "
        "The platform supports both data categories — they require per-official lookups in "
        "the county assessor (qPublic) and Georgia Secretary of State Corporate Registry."
    )

    # ---- Aliases (for audit) ----
    if aliases and len(aliases) > 1:
        with st.expander(f"Name variations recorded ({len(aliases)})"):
            for a in aliases:
                st.markdown(f"- `{a['alias_name']}` · via {a['source_system']}")

    # ---- What's missing ----
    st.markdown("## What's missing on this profile")
    gaps = [
        "**Personal financial disclosure** — Georgia does not require local officials to file.",
        "**Property records** — available per-parcel via Columbia County qPublic; not yet ingested for this person.",
        "**Business affiliations** — searchable in GA Corporate Registry; not yet cross-referenced.",
        "**Campaign contributions** — paper records held by Grovetown City Clerk until Jan 2027; "
        "state portal (ethics.ga.gov) will absorb local filings thereafter.",
    ]
    for g in gaps:
        st.markdown(f"- {g}")


# =====================================================================
# Main
# =====================================================================

render_header()
st.markdown("---")

if st.session_state.view == "profile" and st.session_state.selected_official_id:
    render_profile(st.session_state.selected_official_id)
else:
    render_roster()
