"""
Extraction success-rate ledger.

Persists one row per meeting extraction into ``extraction_outcome`` so the
clean/recovered/failed breakdown the jobs print becomes a queryable artifact
— the rollout-confidence number, the maintenance early-warning, and the audit
trail, sliceable by jurisdiction / job / time / recovery outcome.

"Produced a record" = clean + recovered (the headline success rate). Only
``failed`` (extraction raised, no record) and per-window ``anomalies`` need a
human; everything else is a success the pipeline earned.
"""

from __future__ import annotations

import json
import uuid

from .db import connect


def new_run_id() -> str:
    """A run id groups one job invocation's outcomes."""
    return str(uuid.uuid4())


def record_outcome(
    *,
    run_id: str,
    job_name: str,
    meeting_id: int,
    jurisdiction_id: int | None,
    outcome: str,              # 'clean' | 'recovered' | 'failed'
    report=None,               # ExtractionReport, or None for a failure
) -> None:
    """Write one extraction outcome. Best-effort: never let a ledger write
    break an extraction run (metrics must not jeopardise the work)."""
    total = clean = recovered = anomaly = 0
    anomaly_kinds: dict[str, int] = {}
    method = None
    if report is not None:
        total = report.total_units
        clean = report.clean
        recovered = report.recovered
        anomaly = len(report.anomalies)
        method = report.method or None
        for a in report.anomalies:
            anomaly_kinds[a.kind] = anomaly_kinds.get(a.kind, 0) + 1
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO extraction_outcome
                    (run_id, job_name, meeting_id, jurisdiction_id, outcome,
                     units_total, units_clean, units_recovered, units_anomaly,
                     anomaly_kinds, method)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (run_id, job_name, meeting_id, jurisdiction_id, outcome,
                 total, clean, recovered, anomaly, json.dumps(anomaly_kinds), method),
            )
    except Exception as e:  # noqa: BLE001 — ledger is non-critical
        print(f"   ⚠ ledger write failed (non-fatal): {type(e).__name__}: {e}")


def health(days: int = 30, job_name: str | None = None) -> dict:
    """Compute the success-rate picture over the last ``days``. Success =
    'produced a record' = clean + recovered."""
    where = ["eo.created_at > now() - make_interval(days => %s)"]
    params: list = [days]
    if job_name:
        where.append("eo.job_name = %s")
        params.append(job_name)
    w = " AND ".join(where)
    with connect() as conn:
        overall = conn.execute(
            f"""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE eo.outcome IN ('clean','recovered')) AS produced,
                   count(*) FILTER (WHERE eo.outcome = 'clean')     AS clean,
                   count(*) FILTER (WHERE eo.outcome = 'recovered') AS recovered,
                   count(*) FILTER (WHERE eo.outcome = 'failed')    AS failed
            FROM extraction_outcome eo WHERE {w}
            """,
            params,
        ).fetchone()
        by_method = conn.execute(
            f"""
            SELECT eo.method,
                   count(*) AS total,
                   round(100.0 * count(*) FILTER (WHERE eo.outcome IN ('clean','recovered')) / count(*), 1) AS success_pct
            FROM extraction_outcome eo WHERE {w}
            GROUP BY eo.method ORDER BY total DESC
            """,
            params,
        ).fetchall()
        by_juris = conn.execute(
            f"""
            SELECT j.display_name AS jurisdiction,
                   count(*) AS total,
                   round(100.0 * count(*) FILTER (WHERE eo.outcome IN ('clean','recovered')) / count(*), 1) AS success_pct,
                   count(*) FILTER (WHERE eo.outcome = 'failed') AS failed
            FROM extraction_outcome eo
            LEFT JOIN jurisdiction j ON j.id = eo.jurisdiction_id
            WHERE {w}
            GROUP BY j.display_name ORDER BY total DESC
            """,
            params,
        ).fetchall()
        anomalies = conn.execute(
            f"""
            SELECT k.key AS anomaly_kind, sum((k.value)::int) AS n
            FROM extraction_outcome eo, jsonb_each(eo.anomaly_kinds) k
            WHERE {w}
            GROUP BY k.key ORDER BY n DESC
            """,
            params,
        ).fetchall()
    return {
        "overall": dict(overall),
        "by_method": [dict(r) for r in by_method],
        "by_jurisdiction": [dict(r) for r in by_juris],
        "anomaly_kinds": [dict(r) for r in anomalies],
    }
