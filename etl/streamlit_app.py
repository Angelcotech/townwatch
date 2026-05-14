"""
TownWatch — Phase 1 prototype dashboard.

Quick Streamlit interface over the live Postgres data. Goal: see what
view shapes actually answer real questions before building the Next.js
production UI.

Run:
    cd etl
    source .venv/bin/activate
    streamlit run streamlit_app.py
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from townwatch_etl.db import connect


# ---------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------

st.set_page_config(
    page_title="TownWatch — Grovetown, GA",
    page_icon="🔍",
    layout="wide",
)

NAVY = "#1A2B4A"
ACCENT = "#7AAEDB"

st.markdown(
    f"""
    <style>
      .block-container {{ padding-top: 1.5rem; }}
      h1, h2, h3 {{ color: {NAVY}; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }}
      .stMetric {{ background: #F8F9FB; padding: 1rem; border-radius: 8px; }}
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------

@st.cache_data(ttl=300)
def load_motions() -> pd.DataFrame:
    with connect() as conn:
        rows = conn.execute("""
            SELECT
                m.id AS motion_id,
                m.title,
                m.motion_number,
                m.motion_type,
                m.outcome,
                m.description,
                m.vote_tally_yes,
                m.vote_tally_no,
                m.vote_tally_abstain,
                m.vote_tally_absent,
                mtg.meeting_date,
                mtg.meeting_type,
                gb.name AS body_name,
                j.display_name AS jurisdiction
            FROM motion m
            JOIN meeting mtg ON mtg.id = m.meeting_id
            JOIN governing_body gb ON gb.id = mtg.governing_body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            ORDER BY mtg.meeting_date DESC, m.id DESC
        """).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["meeting_date"] = pd.to_datetime(df["meeting_date"])
        df["year"] = df["meeting_date"].dt.year
    return df


@st.cache_data(ttl=300)
def load_votes_for_official(official_id: int) -> pd.DataFrame:
    with connect() as conn:
        rows = conn.execute("""
            SELECT
                m.id AS motion_id,
                m.title,
                m.motion_type,
                m.outcome,
                v.vote_value,
                v.notes,
                mtg.meeting_date
            FROM vote v
            JOIN motion m ON m.id = v.motion_id
            JOIN meeting mtg ON mtg.id = m.meeting_id
            WHERE v.official_id = %s
            ORDER BY mtg.meeting_date DESC
        """, (official_id,)).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["meeting_date"] = pd.to_datetime(df["meeting_date"])
    return df


@st.cache_data(ttl=300)
def load_official_breakdown(official_id: int) -> pd.DataFrame:
    """Per-motion-type yes-rate breakdown for one official."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT
                m.motion_type,
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
    return pd.DataFrame([dict(r) for r in rows])


@st.cache_data(ttl=300)
def load_officials() -> pd.DataFrame:
    with connect() as conn:
        rows = conn.execute("""
            SELECT
                o.id, o.canonical_name, o.first_name, o.last_name,
                COUNT(v.id) AS vote_count
            FROM official o
            LEFT JOIN vote v ON v.official_id = o.id
            GROUP BY o.id, o.canonical_name, o.first_name, o.last_name
            ORDER BY vote_count DESC
        """).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


@st.cache_data(ttl=300)
def load_jurisdiction_summary() -> dict[str, Any]:
    with connect() as conn:
        meetings = conn.execute("SELECT COUNT(*) AS n FROM meeting").fetchone()["n"]
        motions = conn.execute("SELECT COUNT(*) AS n FROM motion").fetchone()["n"]
        votes = conn.execute("SELECT COUNT(*) AS n FROM vote").fetchone()["n"]
        recusals = conn.execute(
            "SELECT COUNT(*) AS n FROM vote WHERE vote_value = 'conflict_recusal'"
        ).fetchone()["n"]
        date_range = conn.execute("""
            SELECT MIN(meeting_date) AS earliest, MAX(meeting_date) AS latest
            FROM meeting
        """).fetchone()
    return {
        "meetings": meetings,
        "motions": motions,
        "votes": votes,
        "recusals": recusals,
        "earliest": date_range["earliest"],
        "latest": date_range["latest"],
    }


# ---------------------------------------------------------------------
# Sidebar — filters
# ---------------------------------------------------------------------

motions = load_motions()
summary = load_jurisdiction_summary()
officials = load_officials()

st.sidebar.title("🔍 TownWatch")
st.sidebar.markdown(f"**Grovetown, GA** · {summary['earliest']} → {summary['latest']}")
st.sidebar.markdown("---")

# Motion type filter
st.sidebar.subheader("Motion types")
all_types = sorted(motions["motion_type"].dropna().unique()) if not motions.empty else []
selected_types = st.sidebar.multiselect("Filter", all_types, default=all_types)

# Year range filter
years = motions["year"].dropna().astype(int).unique() if not motions.empty else []
if len(years) > 0:
    year_min, year_max = int(min(years)), int(max(years))
    year_range = st.sidebar.slider("Year range", year_min, year_max, (year_min, year_max))
else:
    year_range = (2012, 2026)

# Outcome filter
st.sidebar.subheader("Outcome")
all_outcomes = sorted(motions["outcome"].dropna().unique()) if not motions.empty else []
selected_outcomes = st.sidebar.multiselect("Filter", all_outcomes, default=all_outcomes)

# Apply filters
filtered = motions[
    motions["motion_type"].isin(selected_types)
    & motions["outcome"].isin(selected_outcomes)
    & (motions["year"] >= year_range[0])
    & (motions["year"] <= year_range[1])
]


# ---------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------

st.title("TownWatch — Grovetown, GA")
st.caption("Phase 1 prototype · live Postgres data · for design exploration only")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Meetings", f"{summary['meetings']:,}")
c2.metric("Motions extracted", f"{summary['motions']:,}")
c3.metric("Individual votes", f"{summary['votes']:,}")
c4.metric("Recusals", f"{summary['recusals']:,}")


# ---------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------

tab0, tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "🔎 Findings",
    "📅 Decision Timeline",
    "🏗 Money & Development",
    "👤 Per-Official",
    "⚠️ Recusals & Conflicts",
    "🔮 Post-Office Patterns",
])

# ---------- Tab 0: Auto-Detected Findings ----------

with tab0:
    st.subheader("Patterns automatically detected from the data")
    st.caption(
        "Each finding is a sentence-level pattern surfaced by a deterministic detector "
        "running against the indexed public record. Severity is a structural signal, "
        "not a verdict. The data is the source of truth — citizens verify and conclude."
    )

    with connect() as conn:
        findings_rows = conn.execute("""
            SELECT f.id, f.pattern_id, f.severity, f.title, f.explanation,
                   f.metrics, f.evidence, f.detected_at,
                   o.canonical_name AS official_name,
                   gb.name AS body_name,
                   j.display_name AS jurisdiction
            FROM finding f
            LEFT JOIN official o      ON o.id = f.subject_official_id
            LEFT JOIN governing_body gb ON gb.id = f.governing_body_id
            LEFT JOIN jurisdiction j  ON j.id = f.jurisdiction_id
            ORDER BY f.severity DESC, f.pattern_id, f.id
        """).fetchall()

    if not findings_rows:
        st.info("No findings yet. Run `python -m townwatch_etl.jobs.run_patterns`.")
    else:
        # Filter chips
        all_patterns = sorted(set(r["pattern_id"] for r in findings_rows))
        st.markdown("**Filter by pattern:**")
        cols = st.columns(len(all_patterns) + 1)
        if "patterns_selected" not in st.session_state:
            st.session_state.patterns_selected = set(all_patterns)
        for i, p in enumerate(all_patterns):
            count = sum(1 for r in findings_rows if r["pattern_id"] == p)
            with cols[i]:
                if st.checkbox(f"{p} ({count})", value=p in st.session_state.patterns_selected, key=f"pat_{p}"):
                    st.session_state.patterns_selected.add(p)
                else:
                    st.session_state.patterns_selected.discard(p)

        min_sev = st.slider("Minimum severity", 1, 5, 2)

        filtered_findings = [
            r for r in findings_rows
            if r["pattern_id"] in st.session_state.patterns_selected
            and r["severity"] >= min_sev
        ]

        st.markdown(f"**Showing {len(filtered_findings)} of {len(findings_rows)} findings**")
        st.markdown("---")

        severity_colors = {5: "🔴", 4: "🟠", 3: "🟡", 2: "🔵", 1: "⚪️"}

        for f in filtered_findings:
            severity_icon = severity_colors.get(f["severity"], "⚪️")
            subject_chip = ""
            if f["official_name"]:
                subject_chip = f"`{f['official_name']}` · "
            elif f["body_name"]:
                subject_chip = f"`{f['body_name']}` · "

            st.markdown(
                f"### {severity_icon} {f['title']}"
            )
            st.caption(f"{subject_chip}pattern: `{f['pattern_id']}` · severity {f['severity']}")
            if f["explanation"]:
                with st.expander("Why this is flagged"):
                    st.write(f["explanation"])

            if f["evidence"]:
                with st.expander(f"Evidence ({len(f['evidence'])} records)"):
                    for e in f["evidence"][:20]:
                        st.markdown(f"- **{e.get('date', '')}** · {e.get('title', '')}")
            if f["metrics"]:
                with st.expander("Metrics"):
                    st.json(f["metrics"])
            st.markdown("---")

# ---------- Tab 1: Decision Timeline ----------

with tab1:
    st.subheader("Every substantive vote, plotted in time")
    st.caption("Each dot is one motion. Color = type. Hover for details.")

    if not filtered.empty:
        fig = px.scatter(
            filtered,
            x="meeting_date",
            y="motion_type",
            color="motion_type",
            symbol="outcome",
            hover_data={
                "title": True,
                "motion_number": True,
                "vote_tally_yes": True,
                "vote_tally_no": True,
                "outcome": True,
                "meeting_date": "|%Y-%m-%d",
                "motion_type": False,
            },
            height=520,
        )
        fig.update_layout(
            xaxis_title=None,
            yaxis_title=None,
            showlegend=True,
            legend_title=None,
            plot_bgcolor="white",
            margin=dict(l=10, r=10, t=10, b=10),
        )
        fig.update_xaxes(gridcolor="#EEE", showline=True, linecolor="#DDD")
        fig.update_yaxes(gridcolor="#F5F5F5", showline=True, linecolor="#DDD")
        st.plotly_chart(fig, use_container_width=True)

        # Density chart underneath
        st.subheader("Motion density by year & type")
        density = (
            filtered.groupby(["year", "motion_type"])
            .size()
            .reset_index(name="count")
        )
        fig2 = px.bar(
            density, x="year", y="count", color="motion_type",
            height=280,
        )
        fig2.update_layout(
            plot_bgcolor="white",
            margin=dict(l=10, r=10, t=10, b=10),
            xaxis_title=None, yaxis_title="Motions",
        )
        st.plotly_chart(fig2, use_container_width=True)

        st.subheader(f"Records ({len(filtered):,})")
        display = filtered[[
            "meeting_date", "motion_number", "motion_type", "outcome",
            "vote_tally_yes", "vote_tally_no", "title",
        ]].copy()
        display["meeting_date"] = display["meeting_date"].dt.strftime("%Y-%m-%d")
        st.dataframe(display, use_container_width=True, hide_index=True)
    else:
        st.info("No motions match the current filters.")


# ---------- Tab 2: Money & Development ----------

with tab2:
    st.subheader("Decisions that shape tax burden and development")
    st.caption(
        "Zoning changes, contracts, budget amendments, and ordinances — the votes "
        "most likely to affect what your neighborhood looks like and what your tax bill says."
    )

    money_types = {"zoning_change", "contract_approval", "budget_amendment", "ordinance"}
    money = motions[motions["motion_type"].isin(money_types)]

    if not money.empty:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Zoning changes (14 yrs)", int((money["motion_type"] == "zoning_change").sum()))
        c2.metric("Contracts approved", int((money["motion_type"] == "contract_approval").sum()))
        c3.metric("Budget amendments", int((money["motion_type"] == "budget_amendment").sum()))
        c4.metric("Ordinances", int((money["motion_type"] == "ordinance").sum()))

        # Timeline
        fig = px.scatter(
            money,
            x="meeting_date",
            y="motion_type",
            color="motion_type",
            symbol="outcome",
            hover_data={
                "title": True, "motion_number": True,
                "vote_tally_yes": True, "vote_tally_no": True,
                "outcome": True,
                "meeting_date": "|%Y-%m-%d",
                "motion_type": False,
            },
            height=460,
        )
        fig.update_layout(
            plot_bgcolor="white",
            margin=dict(l=10, r=10, t=10, b=10),
            xaxis_title=None, yaxis_title=None,
            legend_title=None,
        )
        st.plotly_chart(fig, use_container_width=True)

        # Year-by-year intensity
        density = money.groupby(["year", "motion_type"]).size().reset_index(name="count")
        fig2 = px.bar(density, x="year", y="count", color="motion_type", height=260)
        fig2.update_layout(plot_bgcolor="white", xaxis_title=None, yaxis_title="Per year")
        st.plotly_chart(fig2, use_container_width=True)

        st.subheader("Recent zoning + annexation activity")
        zoning = money[money["motion_type"].isin({"zoning_change", "ordinance"})].head(20)
        for _, row in zoning.iterrows():
            with st.expander(f"{row['meeting_date'].strftime('%Y-%m-%d')} · {row['title'][:90]}"):
                st.write(row["description"] or "")
                st.caption(
                    f"{row['motion_type']} · {row['outcome']} · "
                    f"vote {row['vote_tally_yes']}-{row['vote_tally_no']}"
                )
    else:
        st.info("No money/development motions in current filter range.")


# ---------- Tab 3: Per-Official ----------

with tab3:
    if not officials.empty:
        opt = officials[officials["vote_count"] > 0].copy()
        opt["label"] = opt.apply(
            lambda r: f"{r['canonical_name']} ({r['vote_count']:,} votes)",
            axis=1,
        )
        choice = st.selectbox("Choose an official", opt["label"].tolist())
        chosen_id = int(opt[opt["label"] == choice]["id"].iloc[0])
        chosen_name = opt[opt["label"] == choice]["canonical_name"].iloc[0]

        votes_df = load_votes_for_official(chosen_id)
        breakdown = load_official_breakdown(chosen_id)

        # ===== Headline statistics =====
        st.title(chosen_name)

        total_votes = len(votes_df)
        first_vote = votes_df["meeting_date"].min() if not votes_df.empty else None
        latest_vote = votes_df["meeting_date"].max() if not votes_df.empty else None
        years_served = (
            round((latest_vote - first_vote).days / 365.25, 1)
            if first_vote is not None else 0
        )
        overall_yes_pct = (
            int((votes_df["vote_value"] == "yes").sum() * 100 / max(total_votes, 1))
            if total_votes else 0
        )
        recusal_count = int((votes_df["vote_value"] == "conflict_recusal").sum())
        dissent_count = int((votes_df["vote_value"] == "no").sum())

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Years in record", f"{years_served}")
        c2.metric("Total votes", f"{total_votes:,}")
        c3.metric("Yes rate overall", f"{overall_yes_pct}%")
        c4.metric("Recusals", f"{recusal_count}")

        # ===== Topical breakdown — the centerpiece =====
        st.subheader("Voting record by topic")
        if not breakdown.empty:
            bd = breakdown.copy()
            bd["yes_pct"] = (bd["yes_count"] * 100 / bd["total"]).round(0).astype(int)

            display_bd = bd[["motion_type", "yes_pct", "total", "yes_count", "no_count", "abstain_count", "recusal_count"]].rename(
                columns={
                    "motion_type": "Topic",
                    "yes_pct": "% Yes",
                    "total": "Total Votes",
                    "yes_count": "Yes",
                    "no_count": "No",
                    "abstain_count": "Abstain",
                    "recusal_count": "Recusal",
                }
            )
            st.dataframe(
                display_bd,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "% Yes": st.column_config.ProgressColumn(
                        "% Yes",
                        format="%d%%",
                        min_value=0,
                        max_value=100,
                    ),
                },
            )

            # ===== Drill-down: every vote in every category, with full detail =====
            st.subheader("Decisions by topic")
            st.caption(
                "Each row is one motion this official voted on. Look for dissents "
                "(no/abstain/recusal — rare events) and read the descriptions for the actual decisions."
            )

            # Need full vote+motion rows joined for the drill-down
            with connect() as conn:
                detail_rows = conn.execute("""
                    SELECT m.motion_type, m.title, m.description, m.outcome,
                           m.vote_tally_yes, m.vote_tally_no, m.motion_number,
                           v.vote_value, v.notes, mtg.meeting_date
                    FROM vote v
                    JOIN motion m ON m.id = v.motion_id
                    JOIN meeting mtg ON mtg.id = m.meeting_id
                    WHERE v.official_id = %s
                    ORDER BY mtg.meeting_date DESC
                """, (chosen_id,)).fetchall()
            detail_df = pd.DataFrame([dict(r) for r in detail_rows])

            # Sort categories by total votes desc
            for _, row in bd.iterrows():
                mtype = row["motion_type"]
                count = int(row["total"])
                yes_pct = int(row["yes_pct"])
                dissents = int(row["no_count"]) + int(row["abstain_count"]) + int(row["recusal_count"])

                label = f"{mtype}  —  {count} votes  ·  {yes_pct}% yes"
                if dissents > 0:
                    label += f"  ·  ⓘ {dissents} dissent" + ("s" if dissents != 1 else "")

                with st.expander(label):
                    cat = detail_df[detail_df["motion_type"] == mtype].copy()
                    if cat.empty:
                        st.caption("No detail rows.")
                        continue

                    # Show dissents first (they're the interesting events)
                    cat["is_dissent"] = cat["vote_value"].isin(["no", "abstain", "conflict_recusal"])
                    cat = cat.sort_values(["is_dissent", "meeting_date"], ascending=[False, False])

                    for _, m in cat.iterrows():
                        vote_emoji = {
                            "yes": "✓",
                            "no": "✗",
                            "abstain": "○",
                            "conflict_recusal": "⚠",
                            "absent": "—",
                        }.get(m["vote_value"], "?")
                        date_str = m["meeting_date"].strftime("%Y-%m-%d")
                        outcome_tag = f" → {m['outcome']}" if m["outcome"] != "passed" else ""
                        tally = f"({m['vote_tally_yes']}-{m['vote_tally_no']})"

                        header = f"**{vote_emoji} {date_str}** · {m['title']} {tally}{outcome_tag}"
                        st.markdown(header)
                        if m["description"]:
                            st.caption(m["description"])
                        if m["notes"]:
                            st.info(f"📌 {m['notes']}")
                        st.markdown("---")

        # ===== Vote timeline =====
        st.subheader("Voting record over time")
        if not votes_df.empty:
            fig = px.scatter(
                votes_df, x="meeting_date", y="motion_type",
                color="vote_value", symbol="vote_value",
                hover_data={"title": True, "outcome": True, "meeting_date": "|%Y-%m-%d", "motion_type": False},
                height=380,
            )
            fig.update_layout(
                plot_bgcolor="white",
                margin=dict(l=10, r=10, t=10, b=10),
                xaxis_title=None, yaxis_title=None,
            )
            st.plotly_chart(fig, use_container_width=True)

        # ===== Raw data (the receipts) =====
        with st.expander(f"📋 Show all {total_votes:,} individual votes (raw data)"):
            display = votes_df[["meeting_date", "motion_type", "vote_value", "outcome", "title"]].copy()
            display["meeting_date"] = display["meeting_date"].dt.strftime("%Y-%m-%d")
            st.dataframe(display, use_container_width=True, hide_index=True)
    else:
        st.info("No officials with votes yet.")


# ---------- Tab 4: Recusals & Conflicts ----------

with tab4:
    st.subheader("Every declared conflict, every recusal")
    st.caption(
        "When an official declines to vote due to a conflict of interest, it's "
        "recorded in the minutes. These are the moments officials themselves "
        "tell you a conflict exists."
    )

    with connect() as conn:
        recusal_rows = conn.execute("""
            SELECT
                o.canonical_name,
                m.title,
                m.motion_type,
                m.outcome,
                v.notes,
                mtg.meeting_date
            FROM vote v
            JOIN official o ON o.id = v.official_id
            JOIN motion m ON m.id = v.motion_id
            JOIN meeting mtg ON mtg.id = m.meeting_id
            WHERE v.vote_value = 'conflict_recusal'
            ORDER BY mtg.meeting_date DESC
        """).fetchall()

    if recusal_rows:
        for r in recusal_rows:
            with st.expander(f"{r['meeting_date']} · {r['canonical_name']} recused on '{r['title'][:80]}'"):
                st.write(f"**Motion type:** {r['motion_type']}")
                st.write(f"**Outcome:** {r['outcome']}")
                if r["notes"]:
                    st.write(f"**Reason:** {r['notes']}")
    else:
        st.info(
            "**No recusals recorded yet in the extracted data.**\n\n"
            "This either means no Grovetown official has declared a financial conflict "
            "on any motion in the indexed period, or those declarations weren't captured "
            "in the minutes format. As the data grows, this view becomes a key indicator."
        )


# ---------- Tab 5: Post-Office Patterns ----------

with tab5:
    st.subheader("Post-Office Benefit Watch")
    st.caption("Detect cases where former officials benefited from votes they helped pass.")

    st.warning(
        "🔮 **Coming in Phase 1.5** — requires historical official records and "
        "GA Secretary of State Corporate Registry cross-referencing. The schema "
        "supports this query today; we need to backfill former councilmembers "
        "(Trudeau, Martin, Fisher, Jones, Smith, and others observed in extraction) "
        "and ingest LLC-to-individual mappings before the detection runs."
    )

    st.markdown("---")
    st.subheader("Unresolved names from extracted minutes")
    st.caption(
        "Officials referenced in voting records who don't yet have canonical records. "
        "These are likely former councilmembers. Backfilling them unlocks 8+ years "
        "of voting history retroactively."
    )

    # Pull unresolved names from extraction notes / raw payloads
    with connect() as conn:
        # Best signal: aliases referenced in raw_payload but not yet in official table
        unresolved = conn.execute("""
            SELECT raw_payload->'agenda_items' AS items, m.meeting_date
            FROM data_source ds
            JOIN meeting m ON m.data_source_id IS NOT NULL  -- ignore
            WHERE ds.source_name LIKE '%AgendaCenter%claude_extract'
              AND ds.raw_payload IS NOT NULL
            LIMIT 5
        """).fetchall()

    st.info(
        "Names like 'Dennis Trudeau', 'Sylvia Martin', 'Deborah Fisher', "
        "'Gary E. Jones', 'Ceretta Smith' appeared in extracted minutes "
        "without matching official records. Backfill these to unlock their "
        "complete voting histories."
    )

# Footer
st.markdown("---")
st.caption(
    "TownWatch is a prototype civic accountability platform. Data shown is sourced "
    "from public records (city website agendas/minutes via CivicEngage AgendaCenter, "
    "Columbia County assessor via qPublic, and Georgia DOAA). "
    "Documented gaps are surfaced honestly — see the jurisdiction config for what's missing and why."
)
