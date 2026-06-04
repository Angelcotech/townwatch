"""
Resolve a jurisdiction's IANA time zone — graceful, never-blocking, self-healing.

Forum comment windows hinge on a meeting's *local* clock (the window closes 12h
before the meeting starts). Computing that correctly means knowing the
jurisdiction's time zone. We resolve it automatically at onboarding and persist
it, but — crucially — onboarding must NEVER hard-fail because a zone is
uncertain. Someone just funded this town; the last thing they should see is a
crash. So resolution always returns a usable answer plus a confidence flag, and
the uncertain ones become a troubleshoot item to refine later, not a blocker.

Escalation ladder (each rung more general than the last); the first hit wins:

  1. explicit  — `jurisdiction.timezone` in the config. The operator override;
                 always trusted (if it's a real IANA zone). status=verified.
  2. county    — `county_fips` in the COUNTY_TZ exception table. US time zones
                 are observed at the county line, so the county nails it even in
                 a state that spans two zones. status=verified.
  3. state     — the state's predominant zone, by state_fips/abbr.
                 • single-zone state  → status=verified (the whole state is this zone)
                 • multi-zone state   → status=assumed  (best guess; may need an
                   override for a town in the minority zone)
  4. default   — last resort if even the state is unknown. status=assumed.

`status='assumed'` rows still onboard and still get a working forum (the 12h
window absorbs an hour-or-two offset); they're flagged so troubleshoot_timezones
can list them and an operator can confirm with a one-line config override, which
flips them to verified on the next sync. This is geographic reference data (like
FIPS), so it lives in code as a single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Belt-and-suspenders default. Used only as the final ladder rung and by cutoff
# queries for a row not yet resolved. US-Eastern is the safest single guess for
# an unknown US jurisdiction (most populous zone).
DEFAULT_TIMEZONE = "America/New_York"


# (state_fips, USPS abbr, predominant IANA zone, spans_multiple_zones)
_STATE_TZ: list[tuple[str, str, str, bool]] = [
    ("01", "AL", "America/Chicago", False),
    ("02", "AK", "America/Anchorage", True),
    ("04", "AZ", "America/Phoenix", True),     # Navajo Nation observes DST
    ("05", "AR", "America/Chicago", False),
    ("06", "CA", "America/Los_Angeles", False),
    ("08", "CO", "America/Denver", False),
    ("09", "CT", "America/New_York", False),
    ("10", "DE", "America/New_York", False),
    ("11", "DC", "America/New_York", False),
    ("12", "FL", "America/New_York", True),    # western panhandle is Central
    ("13", "GA", "America/New_York", False),
    ("15", "HI", "Pacific/Honolulu", False),
    ("16", "ID", "America/Boise", True),        # north Idaho is Pacific
    ("17", "IL", "America/Chicago", False),
    ("18", "IN", "America/Indiana/Indianapolis", True),  # NW + SW corners Central
    ("19", "IA", "America/Chicago", False),
    ("20", "KS", "America/Chicago", True),      # far-west counties Mountain
    ("21", "KY", "America/New_York", True),     # western KY is Central
    ("22", "LA", "America/Chicago", False),
    ("23", "ME", "America/New_York", False),
    ("24", "MD", "America/New_York", False),
    ("25", "MA", "America/New_York", False),
    ("26", "MI", "America/Detroit", True),      # 4 western UP counties Central
    ("27", "MN", "America/Chicago", False),
    ("28", "MS", "America/Chicago", False),
    ("29", "MO", "America/Chicago", False),
    ("30", "MT", "America/Denver", False),
    ("31", "NE", "America/Chicago", True),      # panhandle is Mountain
    ("32", "NV", "America/Los_Angeles", True),  # West Wendover is Mountain
    ("33", "NH", "America/New_York", False),
    ("34", "NJ", "America/New_York", False),
    ("35", "NM", "America/Denver", False),
    ("36", "NY", "America/New_York", False),
    ("37", "NC", "America/New_York", False),
    ("38", "ND", "America/Chicago", True),      # southwest ND is Mountain
    ("39", "OH", "America/New_York", False),
    ("40", "OK", "America/Chicago", False),
    ("41", "OR", "America/Los_Angeles", True),  # Malheur County is Mountain
    ("42", "PA", "America/New_York", False),
    ("44", "RI", "America/New_York", False),
    ("45", "SC", "America/New_York", False),
    ("46", "SD", "America/Chicago", True),      # western SD is Mountain
    ("47", "TN", "America/Chicago", True),      # east TN is Eastern
    ("48", "TX", "America/Chicago", True),      # El Paso area is Mountain
    ("49", "UT", "America/Denver", False),
    ("50", "VT", "America/New_York", False),
    ("51", "VA", "America/New_York", False),
    ("53", "WA", "America/Los_Angeles", False),
    ("54", "WV", "America/New_York", False),
    ("55", "WI", "America/Chicago", False),
    ("56", "WY", "America/Denver", False),
    ("60", "AS", "Pacific/Pago_Pago", False),
    ("66", "GU", "Pacific/Guam", False),
    ("69", "MP", "Pacific/Saipan", False),
    ("72", "PR", "America/Puerto_Rico", False),
    ("78", "VI", "America/St_Thomas", False),
]

_BY_FIPS: dict[str, tuple[str, bool]] = {f: (tz, multi) for f, _a, tz, multi in _STATE_TZ}
_BY_ABBR: dict[str, tuple[str, bool]] = {a.upper(): (tz, multi) for _f, a, tz, multi in _STATE_TZ}
_ABBR_FIPS: dict[str, str] = {a.upper(): f for f, a, _tz, _m in _STATE_TZ}

# Multi-zone states whose minority-zone counties are FULLY enumerated in
# _COUNTY_TZ below. For these, a county that is NOT in the exception table is
# confirmably in the state's predominant zone → resolve as 'verified' (no review
# noise for the majority of the state). Multi-zone states absent here (TN, KY,
# IN, NE, ND, SD, AZ, NV, AK) have large or sub-county splits we haven't fully
# encoded, so an unmatched county there stays 'assumed'.
_COMPLETE_EXCEPTION_STATES: set[str] = {
    "12",  # FL — panhandle Central counties fully listed
    "48",  # TX — El Paso + Hudspeth are the only Mountain counties
    "26",  # MI — 4 western-UP Central counties
    "20",  # KS — 4 far-west Mountain counties
    "41",  # OR — Malheur is the only Mountain county
    "16",  # ID — north-Idaho Pacific counties fully listed
}


# County-level exceptions: counties that observe a DIFFERENT zone than their
# state's predominant one. Keyed by 5-digit county FIPS. This is the rung that
# makes a multi-zone state resolve precisely from data we already have
# (county_fips is a core config field). High-confidence, small, stable sets are
# encoded here; states with large or sub-county splits (TN, KY, IN, NE, ND, SD,
# AZ-Navajo, NV, AK) intentionally fall through to predominant+assumed until an
# override or a town there is onboarded — better an honest "assumed" flag than a
# silently-wrong "verified". Extend as jurisdictions come online.
_COUNTY_TZ: dict[str, str] = {
    # Florida western panhandle → Central
    "12005": "America/Chicago",  # Bay
    "12013": "America/Chicago",  # Calhoun
    "12033": "America/Chicago",  # Escambia
    "12045": "America/Chicago",  # Gulf
    "12059": "America/Chicago",  # Holmes
    "12063": "America/Chicago",  # Jackson
    "12091": "America/Chicago",  # Okaloosa
    "12113": "America/Chicago",  # Santa Rosa
    "12131": "America/Chicago",  # Walton
    "12133": "America/Chicago",  # Washington
    # Texas far west → Mountain
    "48141": "America/Denver",   # El Paso
    "48229": "America/Denver",   # Hudspeth
    # Michigan western Upper Peninsula → Central
    "26043": "America/Chicago",  # Dickinson
    "26053": "America/Chicago",  # Gogebic
    "26071": "America/Chicago",  # Iron
    "26109": "America/Chicago",  # Menominee
    # Kansas far-west counties → Mountain
    "20071": "America/Denver",   # Greeley
    "20075": "America/Denver",   # Hamilton
    "20181": "America/Denver",   # Sherman
    "20199": "America/Denver",   # Wallace
    # Oregon → Mountain
    "41045": "America/Denver",   # Malheur
    # North Idaho → Pacific
    "16009": "America/Los_Angeles",  # Benewah
    "16017": "America/Los_Angeles",  # Bonner
    "16021": "America/Los_Angeles",  # Boundary
    "16035": "America/Los_Angeles",  # Clearwater
    "16055": "America/Los_Angeles",  # Kootenai
    "16057": "America/Los_Angeles",  # Latah
    "16061": "America/Los_Angeles",  # Lewis
    "16069": "America/Los_Angeles",  # Nez Perce
    "16079": "America/Los_Angeles",  # Shoshone
}


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving a jurisdiction's time zone."""
    timezone: str       # a valid IANA zone, always
    source: str         # explicit | county | state | default
    status: str         # verified | assumed
    note: str = ""      # human-readable context, esp. for assumed/overridden

    @property
    def verified(self) -> bool:
        return self.status == "verified"


def _is_valid_iana(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def is_multi_zone(*, state_fips: str | None = None, state_abbr: str | None = None) -> bool:
    """True if the state spans more than one zone (an override may be warranted)."""
    if state_fips and state_fips in _BY_FIPS:
        return _BY_FIPS[state_fips][1]
    if state_abbr and state_abbr.upper() in _BY_ABBR:
        return _BY_ABBR[state_abbr.upper()][1]
    return False


def tz_for_state(*, state_fips: str | None = None, state_abbr: str | None = None) -> str | None:
    """Predominant IANA zone for a state, by FIPS or USPS abbr. None if unknown."""
    if state_fips and state_fips in _BY_FIPS:
        return _BY_FIPS[state_fips][0]
    if state_abbr and state_abbr.upper() in _BY_ABBR:
        return _BY_ABBR[state_abbr.upper()][0]
    return None


def resolve_timezone(config: dict) -> Resolution:
    """
    Resolve a (merged) jurisdiction config's IANA time zone. NEVER raises — the
    worst case is DEFAULT_TIMEZONE with status='assumed'. Walks the escalation
    ladder (explicit → county → state → default) and reports where the answer
    came from and how much to trust it.
    """
    j = config.get("jurisdiction", {}) if isinstance(config, dict) else {}
    if not isinstance(j, dict):
        j = {}
    name = j.get("name") or "?"

    # 1. Explicit override.
    explicit = (j.get("timezone") or "").strip()
    if explicit:
        if _is_valid_iana(explicit):
            return Resolution(explicit, "explicit", "verified", "set in config")
        # A typo'd override must not block onboarding — ignore it, keep walking,
        # and leave a breadcrumb so troubleshoot can flag it.
        bad_note = f"config timezone {explicit!r} is not a valid IANA zone; ignored"
    else:
        bad_note = ""

    state_fips = (j.get("state_fips") or "").strip()
    state_abbr = (j.get("state") or "").strip()
    county_fips = (j.get("county_fips") or "").strip()
    multi = is_multi_zone(state_fips=state_fips, state_abbr=state_abbr)

    # 2. County-level exception (precise even in a multi-zone state).
    if county_fips and county_fips in _COUNTY_TZ:
        note = "by county" + (f"; {bad_note}" if bad_note else "")
        return Resolution(_COUNTY_TZ[county_fips], "county", "verified", note)

    # 3. State predominant zone.
    state_tz = tz_for_state(state_fips=state_fips, state_abbr=state_abbr)
    if state_tz:
        if not multi:
            note = "single-zone state" + (f"; {bad_note}" if bad_note else "")
            return Resolution(state_tz, "state", "verified", note)
        # Multi-zone state. If we know the full set of minority-zone counties and
        # this county isn't one of them (checked at rung 2), it's confirmably in
        # the predominant zone — verified, not a guess.
        fips_for_state = state_fips or _ABBR_FIPS.get(state_abbr.upper(), "")
        if county_fips and fips_for_state in _COMPLETE_EXCEPTION_STATES:
            note = "predominant zone; county confirmed not an exception"
            if bad_note:
                note = f"{bad_note}; {note}"
            return Resolution(state_tz, "state", "verified", note)
        note = (f"{state_abbr or state_fips} spans multiple zones; using predominant "
                f"{state_tz}. Set jurisdiction.timezone if this town is in the minority "
                f"zone.")
        if bad_note:
            note = f"{bad_note}; {note}"
        return Resolution(state_tz, "state", "assumed", note)

    # 4. Default.
    note = (f"[{name}] no usable state for timezone (state_fips={state_fips!r}, "
            f"state={state_abbr!r}); defaulting to {DEFAULT_TIMEZONE}. Set "
            f"jurisdiction.timezone in the config.")
    if bad_note:
        note = f"{bad_note}; {note}"
    return Resolution(DEFAULT_TIMEZONE, "default", "assumed", note)
