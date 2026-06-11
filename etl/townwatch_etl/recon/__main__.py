"""
Recon harness CLI.

    python -m townwatch_etl.recon validate <registry.json> [--slug X] [--lenient]
    python -m townwatch_etl.recon check-urls <registry.json> --slug X

`validate` is the gate the recon skill runs BEFORE writing a registry entry:
non-zero exit means the entry (most importantly, any absence claim) lacks the
required attestations. `check-urls` runs the playbook's exact-URL liveness
rule over an entry's recorded URLs, detecting soft-404s (HTTP 200 bodies that
are really "page not found" — the cause of GA's first false finding).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .validate import absence_claims, validate_entry

_SOFT_404_RE = re.compile(
    r"page\s+not\s+found|404\s+error|no\s+longer\s+available|page\s+you\s+requested",
    re.I,
)

_URL_FIELDS = ("official_website", "agenda_source_url", "records_intake_url")


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text())


def _cmd_validate(args) -> int:
    reg = _load(args.registry)
    vocab = (reg.get("registry_meta") or {}).get("field_vocabularies") or {}
    jurisdictions = reg.get("jurisdictions") or {}
    targets = {args.slug: jurisdictions[args.slug]} if args.slug else jurisdictions
    if args.slug and args.slug not in jurisdictions:
        print(f"no entry {args.slug!r} in {args.registry}")
        return 2

    failed = 0
    for slug, entry in targets.items():
        errors = validate_entry(slug, entry, vocab, strict=not args.lenient)
        claims = absence_claims(entry)
        if errors:
            failed += 1
            print(f"✗ {slug}  ({len(claims)} absence claim(s))")
            for e in errors:
                print(f"    - {e}")
        elif args.verbose or args.slug:
            tag = f"{len(claims)} absence claim(s), attested" if claims else "no absence claims"
            print(f"✓ {slug}  ({tag})")
    total = len(targets)
    print(f"\n{total - failed}/{total} entries pass" + (" (lenient)" if args.lenient else ""))
    return 1 if failed else 0


def _cmd_check_urls(args) -> int:
    from ..http_client import civic_get

    reg = _load(args.registry)
    entry = (reg.get("jurisdictions") or {}).get(args.slug)
    if entry is None:
        print(f"no entry {args.slug!r} in {args.registry}")
        return 2

    urls: list[tuple[str, str]] = []
    for f in _URL_FIELDS:
        if entry.get(f):
            urls.append((f, entry[f]))
    pc = entry.get("public_comment") or {}
    if isinstance(pc, dict) and pc.get("submit_url"):
        urls.append(("public_comment.submit_url", pc["submit_url"]))
    sweep = ((entry.get("verification") or {}).get("structure_sweep") or {})
    for u in sweep.get("sections_enumerated") or []:
        urls.append(("structure_sweep", u))

    bad = 0
    for field, url in urls:
        try:
            r = civic_get(url, timeout=30.0)
            if r.status_code != 200:
                bad += 1
                print(f"✗ {field}: HTTP {r.status_code} — {url}")
            elif _SOFT_404_RE.search(r.text[:5000]):
                bad += 1
                print(f"✗ {field}: soft-404 (200 with not-found body) — {url}")
            else:
                print(f"✓ {field}: 200 ({len(r.content):,} bytes) — {url}")
        except Exception as e:
            bad += 1
            print(f"✗ {field}: {type(e).__name__}: {e} — {url}")
    print(f"\n{len(urls) - bad}/{len(urls)} URLs live")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m townwatch_etl.recon")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate", help="validate registry entries (attestation gate)")
    p_val.add_argument("registry", help="path to a recon registry.json")
    p_val.add_argument("--slug", help="validate only this jurisdiction")
    p_val.add_argument("--lenient", action="store_true",
                       help="vocabulary checks only — survey pre-harness entries")
    p_val.add_argument("--verbose", action="store_true")
    p_val.set_defaults(fn=_cmd_validate)

    p_url = sub.add_parser("check-urls", help="exact-URL liveness + soft-404 detection")
    p_url.add_argument("registry")
    p_url.add_argument("--slug", required=True)
    p_url.set_defaults(fn=_cmd_check_urls)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
