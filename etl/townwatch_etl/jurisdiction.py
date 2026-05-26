"""
Jurisdiction config loader — master template + state-defaults cascade.

The shape of a jurisdiction config is defined by
jurisdictions/_jurisdiction.schema.json. Per-state defaults live in
jurisdictions/_state_defaults/{state}.json. Per-jurisdiction overrides
live in jurisdictions/{slug}.json.

load_config(slug) returns the fully-merged config:

    _state_defaults/{state}.json   (cascading defaults: body aliases,
                                    election cycles, form-of-government,
                                    platform conventions)
            ↓ deep-merged under
    jurisdictions/{slug}.json      (per-jurisdiction overrides)
            ↓ validated against
    _jurisdiction.schema.json      (structural validation; loud on miss)

This lets us change defaults for all GA jurisdictions by editing one
file, and lets us add required fields to the schema by editing one
file — both surface across every jurisdiction without per-file edits.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


JURISDICTIONS_DIR = Path(__file__).resolve().parents[2] / "jurisdictions"
SCHEMA_PATH = JURISDICTIONS_DIR / "_jurisdiction.schema.json"
STATE_DEFAULTS_DIR = JURISDICTIONS_DIR / "_state_defaults"

# Keys whose presence in state defaults is structural metadata, not data
# to merge into every jurisdiction. Stripped before merging.
_STATE_DEFAULTS_META_KEYS = {"_schema_notes", "state_meta"}


def config_path(slug: str) -> Path:
    """Return the absolute path of a jurisdiction config file."""
    return JURISDICTIONS_DIR / f"{slug}.json"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge `override` onto `base`. Override wins on scalars
    and lists; dicts merge recursively. Returns a new dict (does not
    mutate inputs).
    """
    out = deepcopy(base)
    for key, override_val in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(override_val, dict):
            out[key] = _deep_merge(out[key], override_val)
        else:
            out[key] = deepcopy(override_val)
    return out


def _strip_state_defaults_meta(state_defaults: dict) -> dict:
    """Remove structural-metadata keys so they don't merge into
    per-jurisdiction configs."""
    return {k: v for k, v in state_defaults.items() if k not in _STATE_DEFAULTS_META_KEYS}


def _load_state_defaults(state_abbr: str) -> dict[str, Any]:
    """
    Load and return state-level defaults for `state_abbr`. Loud failure
    when a config declares a state but no defaults file exists — silent
    fallback would let a misconfigured state slip through to production.
    """
    if not state_abbr:
        return {}
    path = STATE_DEFAULTS_DIR / f"{state_abbr.lower()}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No state defaults found at {path}\n"
            f"A jurisdiction declared state={state_abbr!r} but no _state_defaults file exists.\n"
            f"Create {path.name} (mirroring the shape of ga.json) before loading this jurisdiction."
        )
    return _strip_state_defaults_meta(_read_json(path))


def _load_schema() -> dict[str, Any]:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Master schema missing: {SCHEMA_PATH}")
    return _read_json(SCHEMA_PATH)


def load_config(slug: str, *, validate: bool = True) -> dict[str, Any]:
    """
    Return the fully-merged jurisdiction config for `slug`.

    Reads per-jurisdiction overrides, merges them over state defaults,
    and (by default) validates the result against the master schema.
    Validation errors raise — silent acceptance would let a config drift
    out of compliance with the template.

    Pass validate=False only in tooling that EXPECTS to see schema
    failures (e.g. validate_configs.py reporting on what's broken).
    """
    path = config_path(slug)
    if not path.exists():
        raise FileNotFoundError(
            f"Jurisdiction config not found: {path}\n"
            f"Available configs in {JURISDICTIONS_DIR}:\n  "
            + "\n  ".join(
                p.stem for p in JURISDICTIONS_DIR.glob("*.json")
                if not p.name.startswith("_")
            )
        )
    per_jurisdiction = _read_json(path)
    state_abbr = (per_jurisdiction.get("jurisdiction") or {}).get("state")
    state_defaults = _load_state_defaults(state_abbr) if state_abbr else {}
    merged = _deep_merge(state_defaults, per_jurisdiction)

    if validate:
        validate_config(merged, slug=slug)
    return merged


def validate_config(config: dict[str, Any], *, slug: str | None = None) -> None:
    """
    Validate a (merged) jurisdiction config against the master schema.

    Raises jsonschema.ValidationError on schema mismatch. Imported
    lazily so the jsonschema dep isn't required for code paths that
    don't validate.
    """
    import jsonschema
    schema = _load_schema()
    try:
        jsonschema.validate(instance=config, schema=schema)
    except jsonschema.ValidationError as e:
        e.message = f"[{slug or '<unknown>'}] {e.message}"
        raise


def jurisdiction_fips(config: dict[str, Any]) -> str:
    """Canonical FIPS code for the jurisdiction — matches `jurisdiction.fips_code`
    in the DB. Cities use place_fips (Census 7-digit place ID); counties use
    county_fips (5-digit). Loud failure when neither is present so a misconfigured
    jurisdiction can't silently match the wrong DB row.

    Use this in every ETL job that filters by `j.fips_code = ?` rather than
    reaching into config["jurisdiction"]["place_fips"] directly.
    """
    j = config["jurisdiction"]
    fips = j.get("place_fips") or j.get("county_fips")
    if not fips:
        raise RuntimeError(
            f"Jurisdiction config has neither place_fips nor county_fips: "
            f"name={j.get('name')!r}, type={j.get('type')!r}"
        )
    return fips


def list_slugs() -> list[str]:
    """Return all jurisdiction config slugs currently available."""
    slugs: list[str] = []
    for p in JURISDICTIONS_DIR.glob("*.json"):
        # Skip template + state-default + other infra files (anything starting with '_')
        if p.name.startswith("_"):
            continue
        try:
            data = _read_json(p)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "jurisdiction" in data:
            slugs.append(p.stem)
    return sorted(slugs)
