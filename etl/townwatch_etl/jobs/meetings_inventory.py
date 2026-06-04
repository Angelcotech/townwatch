"""
Meeting inventory ingest — platform-agnostic dispatcher.

Enumerates every meeting from whichever civic platform the jurisdiction
uses and writes one row per meeting. Vote/agenda extraction from the
PDFs is a separate downstream job (extract_minutes / extract_agendas).

Dispatch is driven by jurisdiction config:
  platform_hints.agenda_platform = "civicengage" → scrapers/civicengage_agendacenter
  platform_hints.agenda_platform = "civicclerk"  → scrapers/civicclerk_meetings

Adding a new platform is a new scraper module that returns the same
MeetingRecord shape; this file picks it up via the dispatch table below.

Idempotent: re-runs don't duplicate. Conflict on (body, date, agenda_url)
updates the row in place.

Run:
    python -m townwatch_etl.jobs.meetings_inventory --jurisdiction grovetown-ga
    python -m townwatch_etl.jobs.meetings_inventory --jurisdiction columbia-county-ga
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Protocol

from ..ingest_base import IngestJob
from ..jurisdiction import load_config


# Common shape across platforms. Each scraper module exposes its own
# MeetingRecord dataclass with these fields — duck-typed; no shared
# import to keep the boundary clean.
class _MeetingRecordProto(Protocol):
    agenda_id: int
    meeting_date: object  # datetime.date
    meeting_type: str
    category_id: int
    category_name: str
    description: str | None
    agenda_url: str | None
    minutes_url: str | None
    agenda_posted_at: object | None  # datetime


@dataclass
class _PlatformBinding:
    """Per-platform glue: how to build the categories dict + how to call
    the scraper's inventory iterator + what source_name to record."""
    body_config_key: str               # key in governing_bodies entry (e.g. "civicengage")
    inventory_fn: callable             # scraper's inventory()
    inventory_kwargs_builder: callable # (config, categories_dict) -> dict of kwargs
    source_name_builder: callable      # (config) -> str
    source_url_builder: callable       # (config) -> str


def _build_civicengage_binding() -> _PlatformBinding:
    from ..scrapers.civicengage_agendacenter import inventory as ce_inventory

    def kwargs(config: dict, categories: dict[int, str]) -> dict:
        base_url = config.get("platform_hints", {}).get("agenda_base_url")
        if not base_url:
            raise ValueError("civicengage requires platform_hints.agenda_base_url")
        return {"base_url": base_url, "categories": categories}

    def source_name(config: dict) -> str:
        base = config["platform_hints"]["agenda_base_url"]
        return f"{base.replace('https://', '').replace('http://', '')}/AgendaCenter"

    def source_url(config: dict) -> str:
        return f"{config['platform_hints']['agenda_base_url']}/AgendaCenter"

    return _PlatformBinding(
        body_config_key="civicengage",
        inventory_fn=ce_inventory,
        inventory_kwargs_builder=kwargs,
        source_name_builder=source_name,
        source_url_builder=source_url,
    )


def _build_civicclerk_binding() -> _PlatformBinding:
    from ..scrapers.civicclerk_meetings import inventory as cc_inventory

    def kwargs(config: dict, categories: dict[int, str]) -> dict:
        tenant = config.get("platform_hints", {}).get("civicclerk_tenant")
        if not tenant:
            raise ValueError("civicclerk requires platform_hints.civicclerk_tenant")
        return {"tenant": tenant, "categories": categories}

    def source_name(config: dict) -> str:
        tenant = config["platform_hints"]["civicclerk_tenant"]
        return f"{tenant}.portal.civicclerk.com"

    def source_url(config: dict) -> str:
        tenant = config["platform_hints"]["civicclerk_tenant"]
        return f"https://{tenant}.portal.civicclerk.com/"

    return _PlatformBinding(
        body_config_key="civicclerk",
        inventory_fn=cc_inventory,
        inventory_kwargs_builder=kwargs,
        source_name_builder=source_name,
        source_url_builder=source_url,
    )


_PLATFORM_BINDINGS: dict[str, callable] = {
    "civicengage": _build_civicengage_binding,
    "civicclerk": _build_civicclerk_binding,
    # Add new platforms here (granicus, legistar, boarddocs, …).
}


class MeetingsInventory(IngestJob):
    """Platform-agnostic inventory job."""
    source_type = "scrape"

    def __init__(self, slug: str, *, calendar_from: "date | None" = None) -> None:
        super().__init__()
        self.slug = slug
        # CivicEngage calendar pass scrapes upcoming meetings from this date
        # forward (default today). A past date backfills/enriches existing
        # meetings with time + location.
        self.calendar_from = calendar_from
        self.config = load_config(slug)
        platform = (self.config.get("platform_hints") or {}).get("agenda_platform")
        if not platform:
            raise ValueError(
                f"{slug} config missing platform_hints.agenda_platform "
                f"(one of: {sorted(_PLATFORM_BINDINGS)})"
            )
        if platform not in _PLATFORM_BINDINGS:
            raise ValueError(
                f"{slug} declared platform={platform!r}, but no scraper is registered. "
                f"Add a binding in jobs/meetings_inventory.py:_PLATFORM_BINDINGS."
            )
        self.platform = platform
        self.binding = _PLATFORM_BINDINGS[platform]()
        self.source_name = self.binding.source_name_builder(self.config)
        self.source_url = self.binding.source_url_builder(self.config)
        # FIPS code used to scope governing_body lookups to THIS jurisdiction.
        # Without this scope, "Planning Commission" in multiple cities would
        # collide and inserts would attach to the wrong body.
        self.jurisdiction_fips = self.config["jurisdiction"].get("county_fips") \
            if self.config["jurisdiction"]["type"] == "county" \
            else self.config["jurisdiction"].get("place_fips")

        # Build {category_id: body_name} from the per-platform config block
        self.category_to_body = {
            b[self.binding.body_config_key]["category_id"]: b["name"]
            for b in self.config.get("governing_bodies", [])
            if self.binding.body_config_key in b
               and "category_id" in b[self.binding.body_config_key]
        }

    def ingest(self) -> None:
        assert self.conn is not None
        for cat_id, body_name in self.category_to_body.items():
            body_id = self._find_body(body_name)
            if body_id is None:
                print(f"  ⊘ body '{body_name}' not in DB — skipping")
                continue
            print(f"  → scraping {body_name} ({self.platform} catID={cat_id})")
            kwargs = self.binding.inventory_kwargs_builder(
                self.config, {cat_id: body_name},
            )
            count = 0
            for m in self.binding.inventory_fn(**kwargs):
                # Savepoint per meeting: a single malformed record must not roll
                # back the rest of this town's inventory (run() wraps the whole
                # town in one transaction). One bad meeting → skip it, keep going.
                try:
                    with self.conn.transaction():
                        self._upsert_meeting(body_id, m)
                    count += 1
                except Exception as e:
                    self.rows_failed += 1
                    print(f"    ✗ {body_name} {getattr(m, 'meeting_date', '?')}: "
                          f"{type(e).__name__}: {e} — skipped")
            print(f"  ✓ {body_name}: processed {count} meetings")

        # CivicEngage AgendaCenter only has meetings whose agenda is already
        # posted (past / imminent). The public calendar lists meetings further
        # out, with time + location — pull those so cities have a forward view.
        if self.platform == "civicengage":
            self._ingest_calendar()

    def _ingest_calendar(self) -> None:
        """Forward-looking upcoming meetings from the CivicEngage public calendar
        (date / time / location). Inserts agenda-less rows that the AgendaCenter
        pass later upgrades when the agenda posts (see _upsert_meeting step 2)."""
        from ..scrapers.civicengage_calendar import upcoming_meetings, default_body_keywords
        base_url = (self.config.get("platform_hints") or {}).get("agenda_base_url")
        if not base_url:
            return
        body_keywords = default_body_keywords(self.config.get("governing_bodies", []))
        if not body_keywords:
            return
        from_date = self.calendar_from or datetime.now().date()
        print(f"  → scraping upcoming meetings (civicengage calendar, from {from_date})")
        count = 0
        for mt in upcoming_meetings(base_url=base_url, body_keywords=body_keywords, from_date=from_date):
            body_id = self._find_body(mt.body_name)
            if body_id is None:
                continue
            try:
                with self.conn.transaction():
                    self._upsert_calendar_meeting(body_id, mt)
                count += 1
            except Exception as e:
                self.rows_failed += 1
                print(f"    ✗ calendar {mt.body_name} {getattr(mt, 'meeting_date', '?')}: "
                      f"{type(e).__name__}: {e} — skipped")
        print(f"  ✓ calendar: {count} upcoming meeting(s)")

    def _upsert_calendar_meeting(self, body_id: int, mt) -> None:
        """A calendar meeting is identified by (body, date) — the agenda_url is an
        attribute filled later, not an identity. So if ANY row already exists for
        that (body, date) — whether AgendaCenter created it (has an agenda) or a
        prior calendar run did — ENRICH it with time/location rather than insert a
        duplicate. Otherwise insert a new agenda-less 'scheduled' row. Prefers the
        agenda-bearing row when more than one exists."""
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT id FROM meeting WHERE governing_body_id = %s AND meeting_date = %s "
            "ORDER BY (agenda_url IS NOT NULL) DESC, id ASC LIMIT 1",
            (body_id, mt.meeting_date),
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE meeting SET meeting_time = COALESCE(%s::time, meeting_time), "
                "location = COALESCE(%s, location), updated_at = now() WHERE id = %s",
                (mt.meeting_time, mt.location, row["id"]),
            )
            self.rows_skipped += 1
            return
        self.insert("meeting", {
            "governing_body_id": body_id,
            "meeting_date":      mt.meeting_date,
            "meeting_type":      "regular",
            "agenda_url":        None,
            "minutes_url":       None,
            "status":            "scheduled",
            "meeting_time":      mt.meeting_time,
            "location":          mt.location,
        })

    def _find_body(self, body_name: str) -> int | None:
        """Find body by name, scoped to THIS jurisdiction. Without scoping,
        cities and counties that share body names (Planning Commission is
        common) would collide and inserts would attach to the wrong body.
        """
        assert self.conn is not None
        if not self.jurisdiction_fips:
            raise RuntimeError(
                f"Cannot resolve body — config for {self.slug} has no fips_code-equivalent "
                f"(neither place_fips nor county_fips). Fix the jurisdiction config."
            )
        row = self.conn.execute(
            """
            SELECT gb.id FROM governing_body gb
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            WHERE gb.name = %s AND j.fips_code = %s
            LIMIT 1
            """,
            (body_name, self.jurisdiction_fips),
        ).fetchone()
        return row["id"] if row else None

    def _upsert_meeting(self, body_id: int, m: _MeetingRecordProto) -> None:
        assert self.conn is not None and self.data_source_id is not None
        status = self._derive_status(m)
        # meeting_time + location: CivicClerk events and the CivicEngage calendar
        # both carry these; the AgendaCenter listing doesn't (getattr keeps every
        # record shape working). For UPCOMING meetings — which have no agenda yet
        # — this is the only source of time/location.
        meeting_time = getattr(m, "meeting_time", None)
        location = getattr(m, "location", None)
        packet_url = getattr(m, "packet_url", None)  # supporting-docs deck (CivicClerk)

        # 1. Exact match. NULL-safe (IS NOT DISTINCT FROM): plain `agenda_url = %s`
        #    is never true when agenda_url is NULL, so agenda-less meetings would
        #    otherwise duplicate on every run.
        existing = self.conn.execute(
            "SELECT id FROM meeting WHERE governing_body_id = %s AND meeting_date = %s "
            "AND agenda_url IS NOT DISTINCT FROM %s",
            (body_id, m.meeting_date, m.agenda_url),
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE meeting SET minutes_url = %s, status = %s, "
                "agenda_posted_at = COALESCE(%s, agenda_posted_at), "
                "meeting_time = COALESCE(%s::time, meeting_time), "
                "location = COALESCE(%s, location), "
                "packet_url = COALESCE(%s, packet_url), updated_at = now() WHERE id = %s",
                (m.minutes_url, status, m.agenda_posted_at, meeting_time, location, packet_url, existing["id"]),
            )
            self.rows_skipped += 1
            return

        # 2. Reconcile a calendar pre-seed: a record that HAS an agenda_url
        #    (AgendaCenter, once the agenda posts) should UPGRADE the bare row the
        #    calendar created earlier for the same (body, date) — agenda AND
        #    minutes both still NULL — rather than insert a duplicate.
        if m.agenda_url is not None:
            preseed = self.conn.execute(
                "SELECT id FROM meeting WHERE governing_body_id = %s AND meeting_date = %s "
                "AND agenda_url IS NULL AND minutes_url IS NULL LIMIT 1",
                (body_id, m.meeting_date),
            ).fetchone()
            if preseed:
                self.conn.execute(
                    "UPDATE meeting SET agenda_url = %s, minutes_url = %s, status = %s, "
                    "agenda_posted_at = COALESCE(%s, agenda_posted_at), "
                    "meeting_time = COALESCE(%s::time, meeting_time), "
                    "location = COALESCE(%s, location), "
                    "packet_url = COALESCE(%s, packet_url), updated_at = now() WHERE id = %s",
                    (m.agenda_url, m.minutes_url, status, m.agenda_posted_at,
                     meeting_time, location, packet_url, preseed["id"]),
                )
                self.rows_skipped += 1
                return

        # 3. New meeting.
        self.insert("meeting", {
            "governing_body_id": body_id,
            "meeting_date":      m.meeting_date,
            "meeting_type":      m.meeting_type,
            "agenda_url":        m.agenda_url,
            "minutes_url":       m.minutes_url,
            "packet_url":        packet_url,
            "status":            status,
            "agenda_posted_at":  m.agenda_posted_at,
            "meeting_time":      meeting_time,
            "location":          location,
        })

    @staticmethod
    def _derive_status(m: _MeetingRecordProto) -> str:
        today = datetime.now().date()
        if m.minutes_url:
            return "minutes_published"
        if m.meeting_date > today:
            return "agenda_published"
        return "completed"


# Back-compat alias for any caller still importing the old name.
CivicEngageMeetingsInventory = MeetingsInventory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", required=True)
    parser.add_argument("--calendar-since", metavar="YYYY-MM-DD",
                        help="CivicEngage calendar pass starts here instead of today "
                             "(backfill/enrich existing meetings with time + location).")
    args = parser.parse_args()
    cal_from = (datetime.strptime(args.calendar_since, "%Y-%m-%d").date()
                if args.calendar_since else None)
    result = MeetingsInventory(args.jurisdiction, calendar_from=cal_from).run()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
