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
    st.session_state.view = "roster"  # roster | profile | staff_profile | petitioner_profile
if "selected_official_id" not in st.session_state:
    st.session_state.selected_official_id = None
if "selected_staff_key" not in st.session_state:
    st.session_state.selected_staff_key = None
if "selected_petitioner" not in st.session_state:
    st.session_state.selected_petitioner = None


def go_to_profile(official_id: int) -> None:
    st.session_state.view = "profile"
    st.session_state.selected_official_id = official_id


def go_to_staff_profile(staff_key: str) -> None:
    st.session_state.view = "staff_profile"
    st.session_state.selected_staff_key = staff_key


def go_to_petitioner_profile(name: str) -> None:
    st.session_state.view = "petitioner_profile"
    st.session_state.selected_petitioner = name


def go_to_roster() -> None:
    st.session_state.view = "roster"
    st.session_state.selected_official_id = None
    st.session_state.selected_staff_key = None
    st.session_state.selected_petitioner = None


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
            LEFT JOIN motion m ON m.id = v.motion_id AND m.data_status = 'clean'
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


def _parse_staff_entry(s: str) -> dict[str, Any]:
    """Parse 'Title Name' staff string into structured parts."""
    parts = s.strip().split()
    if not parts:
        return {"title": "Staff", "first_name": None, "last_name": s, "name": s}
    if len(parts) >= 3:
        return {
            "title": " ".join(parts[:-2]),
            "first_name": parts[-2],
            "last_name": parts[-1],
            "name": f"{parts[-2]} {parts[-1]}",
        }
    if len(parts) == 2:
        return {"title": None, "first_name": parts[0], "last_name": parts[1], "name": s}
    return {"title": None, "first_name": None, "last_name": parts[0], "name": s}


@st.cache_data(ttl=300)
def load_staff_roster() -> pd.DataFrame:
    """Aggregate unique staff members from meeting.staff_present arrays."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT entry::text AS raw_entry, COUNT(DISTINCT mtg.id) AS meetings,
                   MIN(mtg.meeting_date) AS first_seen,
                   MAX(mtg.meeting_date) AS last_seen
            FROM meeting mtg, jsonb_array_elements_text(mtg.staff_present) AS entry
            WHERE mtg.staff_present IS NOT NULL
            GROUP BY raw_entry
            ORDER BY meetings DESC
        """).fetchall()
    aggregated: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        raw = r["raw_entry"].strip('"')
        parsed = _parse_staff_entry(raw)
        key = ((parsed["first_name"] or "").lower(), (parsed["last_name"] or "").lower())
        if key in aggregated:
            agg = aggregated[key]
            agg["meetings"] += int(r["meetings"])
            agg["titles"].add(parsed["title"] or "Staff")
            agg["raw_entries"].add(raw)
            if r["first_seen"] < agg["first_seen"]:
                agg["first_seen"] = r["first_seen"]
            if r["last_seen"] > agg["last_seen"]:
                agg["last_seen"] = r["last_seen"]
        else:
            aggregated[key] = {
                "key": f"{key[0]}|{key[1]}",
                "name": parsed["name"],
                "first_name": parsed["first_name"],
                "last_name": parsed["last_name"],
                "titles": {parsed["title"] or "Staff"},
                "raw_entries": {raw},
                "meetings": int(r["meetings"]),
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
            }
    out = []
    for a in aggregated.values():
        a["title_summary"] = " · ".join(sorted(t for t in a["titles"] if t))
        a["years"] = round((a["last_seen"] - a["first_seen"]).days / 365.25, 1) if a["last_seen"] != a["first_seen"] else 0
        out.append(a)
    df = pd.DataFrame(out)
    if not df.empty:
        df = df.sort_values("meetings", ascending=False)
    return df


@st.cache_data(ttl=300)
def load_petitioner_roster() -> pd.DataFrame:
    with connect() as conn:
        rows = conn.execute("""
            SELECT m.petitioner_name,
                   COUNT(*) AS motions,
                   COALESCE(SUM(m.dollar_amount), 0) AS total_dollar,
                   MIN(mtg.meeting_date) AS first_seen,
                   MAX(mtg.meeting_date) AS last_seen
            FROM motion m
            JOIN meeting mtg ON mtg.id = m.meeting_id
            WHERE m.petitioner_name IS NOT NULL
              AND m.data_status = 'clean'
            GROUP BY m.petitioner_name
            ORDER BY motions DESC
        """).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["first_seen"] = pd.to_datetime(df["first_seen"])
        df["last_seen"] = pd.to_datetime(df["last_seen"])
    return df


@st.cache_data(ttl=300)
def load_staff_member_record(first_name: str, last_name: str) -> dict[str, Any]:
    """Look up everything we know about one staff member by name."""
    with connect() as conn:
        # Meetings attended (deserialize JSONB to find entries matching this person)
        rows = conn.execute("""
            SELECT mtg.id AS meeting_id, mtg.meeting_date, mtg.meeting_type,
                   entry::text AS raw_entry,
                   gb.name AS body_name
            FROM meeting mtg, jsonb_array_elements_text(mtg.staff_present) AS entry
            JOIN governing_body gb ON gb.id = mtg.governing_body_id
            WHERE mtg.staff_present IS NOT NULL
              AND LOWER(entry::text) LIKE %s
              AND LOWER(entry::text) LIKE %s
            ORDER BY mtg.meeting_date DESC
        """, (f"%{first_name.lower()}%", f"%{last_name.lower()}%")).fetchall()

        meetings = [dict(r) for r in rows]

        # Items where this person was the staff_recommender
        recs = conn.execute("""
            SELECT m.id, m.title, m.motion_type, m.outcome, mtg.meeting_date,
                   m.staff_recommender, m.dollar_amount
            FROM motion m
            JOIN meeting mtg ON mtg.id = m.meeting_id
            WHERE m.staff_recommender ILIKE %s AND m.staff_recommender ILIKE %s
              AND m.data_status = 'clean'
            ORDER BY mtg.meeting_date DESC
        """, (f"%{first_name}%", f"%{last_name}%")).fetchall()
        recommendations = [dict(r) for r in recs]

    return {"meetings": meetings, "recommendations": recommendations}


@st.cache_data(ttl=300)
def load_petitioner_record(name: str) -> dict[str, Any]:
    with connect() as conn:
        motions = conn.execute("""
            SELECT m.id, m.title, m.motion_type, m.outcome, m.dollar_amount,
                   m.locations, m.description,
                   m.vote_tally_yes, m.vote_tally_no,
                   mtg.meeting_date, gb.name AS body_name
            FROM motion m
            JOIN meeting mtg ON mtg.id = m.meeting_id
            JOIN governing_body gb ON gb.id = mtg.governing_body_id
            WHERE m.petitioner_name = %s
              AND m.data_status = 'clean'
            ORDER BY mtg.meeting_date DESC
        """, (name,)).fetchall()
    return {"motions": [dict(m) for m in motions]}


@st.cache_data(ttl=300)
def load_db_stats() -> dict[str, Any]:
    with connect() as conn:
        return {
            "officials":  conn.execute("SELECT COUNT(*) AS n FROM official").fetchone()["n"],
            "votes":      conn.execute("SELECT COUNT(*) AS n FROM vote").fetchone()["n"],
            "motions":    conn.execute("SELECT COUNT(*) AS n FROM motion WHERE data_status = 'clean'").fetchone()["n"],
            "motions_quarantined": conn.execute("SELECT COUNT(*) AS n FROM motion WHERE data_status = 'disputed'").fetchone()["n"],
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
            LEFT JOIN motion m ON m.id = v.motion_id AND m.data_status = 'clean'
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
              AND m.data_status = 'clean'
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
              AND m.data_status = 'clean'
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
        quarantine_note = (
            f" · {stats['motions_quarantined']} under review"
            if stats.get("motions_quarantined")
            else ""
        )
        st.caption(
            f"Indexed: {stats['earliest']} → {stats['latest']} · "
            f"{stats['officials']} officials · {stats['motions']:,} motions{quarantine_note} · "
            f"{stats['votes']:,} votes · {stats['findings']} findings"
        )
    with col_action:
        if st.session_state.view != "roster":
            if st.button("← Back to roster", use_container_width=True):
                go_to_roster()
                st.rerun()


def render_roster() -> None:
    df = load_roster()
    staff_df = load_staff_roster()
    pet_df = load_petitioner_roster()

    quarantined_count = load_db_stats().get("motions_quarantined", 0)
    quarantine_label = (
        f"⚠ Data Quality ({quarantined_count})" if quarantined_count else "⚠ Data Quality"
    )

    tab_elected, tab_staff, tab_petitioners, tab_candidates, tab_quarantine = st.tabs([
        f"🏛 Elected ({len(df)})",
        f"🛠 Staff ({len(staff_df)})",
        f"📋 Petitioners ({len(pet_df)})",
        "🗳 Candidates",
        quarantine_label,
    ])

    with tab_elected:
        _render_elected_roster(df)

    with tab_staff:
        _render_staff_roster(staff_df)

    with tab_petitioners:
        _render_petitioner_roster(pet_df)

    with tab_quarantine:
        _render_quarantine_panel()

    with tab_candidates:
        st.markdown("### Upcoming and recently-filed candidates")
        st.info(
            "**Mayor — Special Election, November 2026**\n\n"
            "The Mayor seat is currently vacant. Council voted (Apr 13, 2026) "
            "to call a special election to fill the unexpired term. Qualifying "
            "fee set at **$612**.\n\n"
            "Candidate filings are not yet tracked in TownWatch. "
            "We'll list candidates here as they file with the City Clerk."
        )


def _render_elected_roster(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No officials in the database yet.")
        return
    current = df[df["is_current"] == True]  # noqa: E712
    historical = df[df["is_current"] != True]  # noqa: E712

    st.markdown("#### Current")
    st.caption(f"{len(current)} active member" + ("s" if len(current) != 1 else ""))
    if not current.empty:
        cols = st.columns(min(len(current), 4))
        for i, (_, row) in enumerate(current.iterrows()):
            with cols[i % len(cols)]:
                _render_official_card(row, current=True)

    st.markdown("#### Historical")
    st.caption(f"{len(historical)} past member" + ("s" if len(historical) != 1 else ""))
    if not historical.empty:
        cols = st.columns(4)
        for i, (_, row) in enumerate(historical.iterrows()):
            with cols[i % 4]:
                _render_official_card(row, current=False)


def _render_staff_roster(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No staff members captured yet.")
        return
    st.caption(
        f"{len(df)} non-elected staff/appointed officials recorded as present in "
        "council meetings. These are the people who actually run city operations."
    )
    cols = st.columns(3)
    for i, (_, row) in enumerate(df.iterrows()):
        with cols[i % 3]:
            _render_staff_card(row)


def _render_staff_card(row: pd.Series) -> None:
    title = row["title_summary"] or "Staff"
    name = row["name"]
    meetings = int(row["meetings"])
    years_part = f" · {row['years']}yr" if row["years"] else ""
    if st.button(
        f"**{name}**\n\n"
        f"{title} · {meetings} meeting{'s' if meetings != 1 else ''}{years_part}",
        key=f"staff_{row['key']}",
        use_container_width=True,
    ):
        go_to_staff_profile(row["key"])
        st.rerun()


def _render_petitioner_roster(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No petitioners captured yet. The comprehensive extraction was just completed; this list grows as motions are tagged.")
        return
    st.caption(
        f"{len(df)} distinct petitioners (individuals, businesses, LLCs) recorded "
        "as having filed or requested at least one motion."
    )
    cols = st.columns(2)
    for i, (_, row) in enumerate(df.iterrows()):
        with cols[i % 2]:
            _render_petitioner_card(row)


@st.cache_data(ttl=60)
def _load_quarantined_motions() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("""
            SELECT m.id, m.title, m.motion_type, m.outcome,
                   m.vote_tally_yes, m.vote_tally_no, m.vote_tally_abstain, m.vote_tally_absent,
                   m.petitioner_name, m.data_status_reason, m.data_status_at,
                   mtg.meeting_date, gb.name AS body_name,
                   (SELECT COUNT(*) FROM vote WHERE motion_id = m.id) AS actual_votes,
                   (SELECT title FROM finding
                    WHERE subject_motion_id = m.id AND pattern_id LIKE 'qa_%'
                    ORDER BY severity DESC LIMIT 1) AS finding_title,
                   (SELECT explanation FROM finding
                    WHERE subject_motion_id = m.id AND pattern_id LIKE 'qa_%'
                    ORDER BY severity DESC LIMIT 1) AS finding_explanation
            FROM motion m
            JOIN meeting mtg ON mtg.id = m.meeting_id
            JOIN governing_body gb ON gb.id = mtg.governing_body_id
            WHERE m.data_status = 'disputed'
            ORDER BY m.data_status_reason, mtg.meeting_date DESC
        """).fetchall()
    return [dict(r) for r in rows]


def _render_quarantine_panel() -> None:
    motions = _load_quarantined_motions()
    if not motions:
        st.success("✓ No motions currently under data-quality review. The corpus is clean.")
        return

    st.markdown("### Motions held back from public surfaces")
    st.caption(
        "These motions failed automatic quality checks and are hidden from "
        "officials' profiles, vote counts, and the petitioner index until the "
        "underlying data is verified. This panel exists so operators can see "
        "what the platform is holding back — citizens never see quarantined data."
    )

    by_reason: dict[str, list[dict]] = {}
    for m in motions:
        by_reason.setdefault(m["data_status_reason"] or "unknown", []).append(m)

    for reason, group in by_reason.items():
        with st.expander(f"**{reason}** — {len(group)} motion(s)", expanded=False):
            st.caption(group[0].get("finding_explanation") or "")
            for m in group:
                tally = (
                    f"declared {m['vote_tally_yes']}–{m['vote_tally_no']}"
                    f"–{m['vote_tally_abstain']}–{m['vote_tally_absent']}"
                    f" · actual votes: {m['actual_votes']}"
                )
                st.markdown(
                    f"- **{m['meeting_date']} · {m['body_name']}** — {m['title']}  \n"
                    f"  _{tally}_"
                )


def _render_petitioner_card(row: pd.Series) -> None:
    name = row["petitioner_name"]
    motions = int(row["motions"])
    dollar = float(row["total_dollar"] or 0)
    dollar_str = f" · ${dollar:,.0f}" if dollar else ""
    if st.button(
        f"**{name}**\n\n"
        f"{motions} motion{'s' if motions != 1 else ''}{dollar_str}",
        key=f"pet_{name}",
        use_container_width=True,
    ):
        go_to_petitioner_profile(name)
        st.rerun()


@st.cache_data(ttl=600)
def _load_official_photo(official_id: int) -> tuple[bytes, str] | None:
    """Return (bytes, mime) for the highest-scored verified photo, or None."""
    with connect() as conn:
        row = conn.execute("""
            SELECT photo_bytes, photo_mime
            FROM official_photo
            WHERE official_id = %s AND data_status = 'verified'
              AND photo_bytes IS NOT NULL
            ORDER BY verification_score DESC, created_at DESC
            LIMIT 1
        """, (official_id,)).fetchone()
    if not row or not row["photo_bytes"]:
        return None
    return bytes(row["photo_bytes"]), row["photo_mime"] or "image/jpeg"


def _initials(name: str) -> str:
    """Return up to 2 uppercase initials for a name."""
    parts = [p for p in (name or "").split() if p and p[0].isalpha()]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _initials_svg(name: str, size: int = 120) -> bytes:
    """Generate an SVG data placeholder for officials without a verified photo."""
    initials = _initials(name)
    # Hash-driven background color for variety, kept in the navy/slate palette
    palette = ["#1E3A8A", "#1E40AF", "#1F2937", "#334155", "#475569", "#0F172A"]
    color = palette[hash(name or "") % len(palette)]
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}">'
        f'<rect width="{size}" height="{size}" rx="{size//2}" fill="{color}"/>'
        f'<text x="50%" y="55%" text-anchor="middle" dominant-baseline="middle" '
        f'font-family="-apple-system,Helvetica,Arial,sans-serif" '
        f'font-size="{int(size*0.42)}" font-weight="600" fill="#ffffff">{initials}</text>'
        f'</svg>'
    )
    return svg.encode("utf-8")


def _render_official_avatar(official_id: int, name: str, size: int = 64) -> None:
    photo = _load_official_photo(official_id)
    if photo:
        # Real photo — let st.image handle resizing
        import base64
        b64 = base64.b64encode(photo[0]).decode("ascii")
        st.markdown(
            f'<img src="data:{photo[1]};base64,{b64}" '
            f'style="width:{size}px;height:{size}px;border-radius:50%;object-fit:cover;'
            f'display:block;" />',
            unsafe_allow_html=True,
        )
    else:
        # SVG initials fallback — inline raw SVG (st.image doesn't grok SVG bytes)
        st.markdown(_initials_svg(name, size=size).decode("utf-8"), unsafe_allow_html=True)


def _render_official_card(row: pd.Series, *, current: bool) -> None:
    label = "Current" if current else "Historical"
    seat_or_status = row.get("current_seat") or label
    years_part = f" · {row['years']}yr" if row["years"] else ""
    col_photo, col_text = st.columns([1, 3])
    with col_photo:
        _render_official_avatar(int(row["id"]), row["canonical_name"], size=72)
    with col_text:
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
    header_photo, header_text = st.columns([1, 4])
    with header_photo:
        _render_official_avatar(int(o["id"]), o["canonical_name"], size=160)
    with header_text:
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

                    # Comprehensive new fields (only show when populated)
                    extras = _fetch_motion_extras(int(m["motion_id"]))
                    if extras:
                        if extras.get("petitioner_name"):
                            st.caption(f"📝 **Petitioner:** {extras['petitioner_name']}")
                        if extras.get("staff_recommender"):
                            st.caption(f"🛠 **Staff recommender:** {extras['staff_recommender']}")
                        if extras.get("dollar_amount"):
                            st.caption(f"💰 **${float(extras['dollar_amount']):,.2f}**")
                        if extras.get("locations"):
                            locs = extras["locations"] if isinstance(extras["locations"], list) else []
                            if locs:
                                st.caption(f"📍 {' · '.join(locs[:3])}")
                        if extras.get("discussion_summary"):
                            st.caption(f"💬 {extras['discussion_summary']}")

                    if m["description"]:
                        st.caption(m["description"])
                    if m["notes"]:
                        st.info(f"📌 {m['notes']}")
                    st.markdown("---")


@st.cache_data(ttl=300)
def _fetch_motion_extras(motion_id: int) -> dict[str, Any]:
    with connect() as conn:
        r = conn.execute("""
            SELECT petitioner_name, staff_recommender, presenter, movant, seconder,
                   dollar_amount, locations, documents_referenced, discussion_summary
            FROM motion WHERE id = %s
        """, (motion_id,)).fetchone()
    return dict(r) if r else {}


def render_staff_profile(staff_key: str) -> None:
    """Render a profile page for a non-elected staff member."""
    first_lower, _, last_lower = staff_key.partition("|")
    staff_df = load_staff_roster()
    match = staff_df[
        (staff_df["first_name"].str.lower() == first_lower)
        & (staff_df["last_name"].str.lower() == last_lower)
    ]
    if match.empty:
        st.error("Staff member not found.")
        return
    info = match.iloc[0]

    first_name = info["first_name"] or first_lower.title()
    last_name = info["last_name"] or last_lower.title()
    name = info["name"]

    record = load_staff_member_record(first_name, last_name)
    meetings = record["meetings"]
    recommendations = record["recommendations"]

    st.markdown(f"# {name}")
    st.caption(info["title_summary"] or "Staff")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Meetings attended", f"{int(info['meetings']):,}")
    c2.metric("Years observed", f"{info['years']}")
    c3.metric("Items recommended", f"{len(recommendations)}")
    rec_dollar = sum(float(r["dollar_amount"] or 0) for r in recommendations)
    c4.metric("$ of recommendations", f"${rec_dollar:,.0f}" if rec_dollar else "—")

    if len(info["titles"]) > 1:
        st.markdown("**Titles observed:**")
        for t in sorted(info["titles"]):
            st.markdown(f"- {t}")

    if recommendations:
        st.markdown("## Items recommended to council")
        for r in recommendations[:30]:
            dollar = f" · ${float(r['dollar_amount']):,.0f}" if r["dollar_amount"] else ""
            st.markdown(f"- **{r['meeting_date']}** · {r['title']} ({r['outcome']}){dollar}")

    st.markdown("## Meetings attended")
    with st.expander(f"Show all {len(meetings)} meetings"):
        for m in meetings[:100]:
            st.markdown(f"- **{m['meeting_date']}** · {m['body_name']} ({m['meeting_type']})")


def render_petitioner_profile(name: str) -> None:
    """Render a profile page for a petitioner / applicant entity."""
    record = load_petitioner_record(name)
    motions = record["motions"]

    st.markdown(f"# {name}")
    st.caption("Petitioner / Applicant")

    if not motions:
        st.info("No motions on file for this petitioner.")
        return

    total_dollar = sum(float(m["dollar_amount"] or 0) for m in motions)
    passed = sum(1 for m in motions if m["outcome"] == "passed")
    failed = sum(1 for m in motions if m["outcome"] == "failed")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Motions filed", f"{len(motions)}")
    c2.metric("Passed", f"{passed}")
    c3.metric("Failed", f"{failed}")
    c4.metric("$ moved", f"${total_dollar:,.0f}" if total_dollar else "—")

    # All locations associated with this petitioner
    all_locations: list[str] = []
    for m in motions:
        locs = m["locations"] if isinstance(m["locations"], list) else []
        all_locations.extend(locs)
    if all_locations:
        st.markdown("## Properties involved")
        unique_locs = sorted(set(all_locations))
        for loc in unique_locs:
            st.markdown(f"- {loc}")

    st.markdown("## Motions filed")
    for m in motions:
        emoji = {"passed": "✓", "failed": "✗", "tabled": "⊘"}.get(m["outcome"], "?")
        date_str = m["meeting_date"].strftime("%Y-%m-%d") if hasattr(m["meeting_date"], "strftime") else str(m["meeting_date"])
        tally = f"({m['vote_tally_yes']}-{m['vote_tally_no']})"
        dollar = f" · ${float(m['dollar_amount']):,.0f}" if m["dollar_amount"] else ""
        st.markdown(f"- **{emoji} {date_str}** · {m['title']} {tally}{dollar}")
        if m["description"]:
            st.caption(m["description"])

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

view = st.session_state.view
if view == "profile" and st.session_state.selected_official_id:
    render_profile(st.session_state.selected_official_id)
elif view == "staff_profile" and st.session_state.selected_staff_key:
    render_staff_profile(st.session_state.selected_staff_key)
elif view == "petitioner_profile" and st.session_state.selected_petitioner:
    render_petitioner_profile(st.session_state.selected_petitioner)
else:
    render_roster()
