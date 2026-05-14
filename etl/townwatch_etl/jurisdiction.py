"""
Jurisdiction config loader.

Every ETL job reads its parameters from a jurisdiction config JSON file
in /jurisdictions/<slug>.json. This removes all hardcoded URLs, bodies,
and data-source endpoints from the code — adding a new town means filing
a new config file, never editing job code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


JURISDICTIONS_DIR = Path(__file__).resolve().parents[2] / "jurisdictions"


def config_path(slug: str) -> Path:
    """Return the absolute path of a jurisdiction config file."""
    return JURISDICTIONS_DIR / f"{slug}.json"


def load_config(slug: str) -> dict[str, Any]:
    """Load and return a jurisdiction config dict. Raises if missing."""
    path = config_path(slug)
    if not path.exists():
        raise FileNotFoundError(
            f"Jurisdiction config not found: {path}\n"
            f"Available configs in {JURISDICTIONS_DIR}:\n  "
            + "\n  ".join(p.stem for p in JURISDICTIONS_DIR.glob("*.json"))
        )
    return json.loads(path.read_text())


def list_slugs() -> list[str]:
    """Return all jurisdiction config slugs currently available."""
    slugs: list[str] = []
    for p in JURISDICTIONS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        # A jurisdiction config has a top-level "jurisdiction" object with a place_fips.
        if isinstance(data, dict) and "jurisdiction" in data and "place_fips" in data["jurisdiction"]:
            slugs.append(p.stem)
    return sorted(slugs)
