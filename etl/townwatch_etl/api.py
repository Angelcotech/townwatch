"""
Correction-intake service — the ONE small HTTP surface in the ETL domain.

Everything else in the ETL is batch. This is the single always-on endpoint that
exists so the public can report an error and have it land in Postgres while the
WEB layer stays strictly read-only. The browser never calls this directly — the
Next.js site forwards a report server-side (keeping this URL and any token off
the client), and this service performs the write via townwatch_etl.corrections.

Endpoints:
    GET  /healthz       — liveness probe
    POST /corrections   — record one public error report

Run locally:
    cd etl && .venv/bin/python -m townwatch_etl.api          # serves :8000
    cd etl && .venv/bin/uvicorn townwatch_etl.api:app --port 8000

Deploy (Railway): a second service off this same repo with start command
    uvicorn townwatch_etl.api:app --host 0.0.0.0 --port $PORT
sharing DATABASE_URL with the batch worker. Optionally set INTAKE_TOKEN to a
shared secret; when set, requests must send it as `X-Intake-Token` (the Next.js
proxy adds it). This keeps the public write path authenticated end-to-end
without exposing anything to the browser.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from . import corrections

app = FastAPI(title="TownWatch correction intake", docs_url=None, redoc_url=None)


class CorrectionIn(BaseModel):
    entity_type: str = Field(..., description="motion|vote|finding|meeting|official|agenda_item")
    entity_id: int = Field(..., gt=0)
    reported_issue: str = Field(..., min_length=1, max_length=2000)
    field: str | None = Field(default=None, max_length=120)
    suggested_value: str | None = Field(default=None, max_length=2000)
    source_note: str | None = Field(default=None, max_length=2000)
    reporter_contact: str | None = Field(default=None, max_length=320)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _check_token(supplied: str | None) -> None:
    """When INTAKE_TOKEN is configured, require it. No token configured = open."""
    expected = os.environ.get("INTAKE_TOKEN")
    if expected and supplied != expected:
        raise HTTPException(status_code=401, detail="invalid intake token")


@app.post("/corrections")
def post_correction(
    body: CorrectionIn,
    request: Request,
    x_intake_token: str | None = Header(default=None),
) -> dict[str, object]:
    _check_token(x_intake_token)
    client_ip = request.client.host if request.client else None
    try:
        result = corrections.submit(
            entity_type=body.entity_type,
            entity_id=body.entity_id,
            reported_issue=body.reported_issue,
            field=body.field,
            suggested_value=body.suggested_value,
            source_note=body.source_note,
            reporter_contact=body.reporter_contact,
            reporter_ip=client_ip,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **result}


def main() -> int:
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
