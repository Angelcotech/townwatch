"""
Validate every jurisdiction config against the master schema.

Loud-failure linter. Reports each config that fails validation with a
human-readable error. Used:
  - During CI to keep configs aligned as the schema evolves
  - After bulk onboarding to catch missing fields
  - Manually when debugging "why does this jurisdiction behave oddly?"

Exit code is non-zero if ANY config fails — safe to wire into a
pre-commit hook or CI gate.

Run:
    python -m townwatch_etl.jobs.validate_configs
    python -m townwatch_etl.jobs.validate_configs --slug grovetown-ga
"""

from __future__ import annotations

import argparse
import sys

from ..jurisdiction import list_slugs, load_config


def validate_one(slug: str) -> tuple[bool, str]:
    """Returns (ok, message). Message is empty on success."""
    try:
        load_config(slug, validate=True)
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", help="Validate one jurisdiction only")
    args = parser.parse_args()

    slugs = [args.slug] if args.slug else list_slugs()
    if not slugs:
        print("No jurisdiction configs found.")
        return 0

    passed = 0
    failed = 0
    print(f"Validating {len(slugs)} jurisdiction config(s)...\n")
    for slug in slugs:
        ok, msg = validate_one(slug)
        if ok:
            passed += 1
            print(f"  ✓ {slug}")
        else:
            failed += 1
            print(f"  ✗ {slug}")
            for line in msg.splitlines():
                print(f"      {line}")
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
