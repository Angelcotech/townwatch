"""
Registry-entry validation rules. Pure functions — the CLI (__main__) does I/O.

An entry is a dict in registry.json's jurisdictions map. Vocabularies come
from registry_meta.field_vocabularies so states can extend them as data.
"""

from __future__ import annotations

import re

# Fields whose values are constrained by registry_meta.field_vocabularies.
_VOCAB_FIELDS = {
    "agenda_platform": "agenda_platform",
    "clerk_email_access": "clerk_email_access",
    "records_intake": "records_intake",
    "verified": "verified",
    "confidence": "confidence",
}

# notable_gaps phrasing that constitutes an absence claim. Deliberately broad:
# a false negative here lets an unverified absence into the registry, which is
# the exact failure mode this module exists to block.
_ABSENCE_GAP_RE = re.compile(
    r"not\s+published|not\s+posted|none\s+published|no\s+(online\s+)?(agendas?|minutes|email|custodian|records|comment)"
    r"|does\s+not\s+(publish|post)|empty\s+(agendas?|minutes)?\s*page|nothing\s+(posted|published)",
    re.I,
)

# Search-pass query coverage: at least one query about documents and one about
# contact/comment channels must have been run (and recorded) before an absence
# claim in either area is accepted.
_DOC_QUERY_RE = re.compile(r"agenda|minutes", re.I)
_CHANNEL_QUERY_RE = re.compile(r"comment|records|custodian|email|clerk", re.I)


def absence_claims(entry: dict) -> list[str]:
    """Human-readable list of the absence claims this entry makes."""
    claims: list[str] = []
    if entry.get("agenda_platform") == "none_found":
        claims.append("agenda_platform=none_found")
    if entry.get("clerk_email_access") == "none_found":
        claims.append("clerk_email_access=none_found")
    if not entry.get("records_custodian_email"):
        claims.append("records_custodian_email is null")
    pc = entry.get("public_comment") or {}
    if isinstance(pc, dict):
        if pc.get("channel") in ("none_found", "in_person_only"):
            claims.append(f"public_comment.channel={pc.get('channel')}")
        if not pc.get("recipient_email"):
            claims.append("public_comment.recipient_email is null")
    for gap in entry.get("notable_gaps") or []:
        if isinstance(gap, str) and _ABSENCE_GAP_RE.search(gap):
            claims.append(f"notable_gaps: {gap[:80]}")
    return claims


def validate_entry(slug: str, entry: dict, vocabularies: dict,
                   *, strict: bool = True) -> list[str]:
    """Errors for one registry entry; empty list = valid.

    strict=True (default) enforces the absence-attestation rules. Lenient mode
    still checks vocabularies — use it only to survey pre-harness entries."""
    errors: list[str] = []

    for field, vocab_key in _VOCAB_FIELDS.items():
        val = entry.get(field)
        vocab = vocabularies.get(vocab_key) or []
        if val is not None and vocab and val not in vocab:
            errors.append(f"{field}={val!r} not in vocabulary {vocab}")

    pc = entry.get("public_comment") or {}
    if isinstance(pc, dict) and pc.get("channel") is not None:
        vocab = vocabularies.get("public_comment_channel") or []
        if vocab and pc["channel"] not in vocab:
            errors.append(f"public_comment.channel={pc['channel']!r} not in vocabulary")

    claims = absence_claims(entry)
    if not claims or not strict:
        return errors

    v = entry.get("verification") or {}

    sweep = v.get("structure_sweep") or {}
    sections = sweep.get("sections_enumerated") or []
    if not (isinstance(sections, list) and len(sections) >= 1
            and all(isinstance(s, str) and s.startswith("http") for s in sections)):
        errors.append(
            f"absence claim(s) {claims} without a structure sweep — "
            f"verification.structure_sweep.sections_enumerated must list the "
            f"section/subpage URLs actually opened (one empty subpage is never "
            f"the section; see METHODOLOGY.md §3.3)"
        )

    sp = v.get("search_pass") or {}
    queries = sp.get("queries") or []
    if not (isinstance(queries, list) and len(queries) >= 3):
        errors.append(
            f"absence claim(s) {claims} without a search-engine pass — "
            f"verification.search_pass.queries must record ≥3 queries run "
            f"(METHODOLOGY.md §3.4; the CCSD comment email was a first-page result)"
        )
    else:
        if not any(_DOC_QUERY_RE.search(q) for q in queries):
            errors.append("search_pass.queries has no agenda/minutes query")
        if not any(_CHANNEL_QUERY_RE.search(q) for q in queries):
            errors.append("search_pass.queries has no contact/comment-channel query")
        if not sp.get("engine"):
            errors.append("search_pass.engine missing (which engine was used)")

    if not (v.get("sibling_control") or "").strip():
        errors.append(
            "verification.sibling_control missing — note which sibling record "
            "type parses from the same source (if minutes parse but agendas "
            "look empty on the same CMS, suspect the recon, not the district)"
        )

    if entry.get("confidence") == "high" and not v.get("adversarial"):
        errors.append(
            "absence claims may not carry confidence=high without "
            "verification.adversarial (refute votes, e.g. '2-of-3 survived') — "
            "cap at medium otherwise"
        )

    if entry.get("verified") == "verified" and not v.get("session_date"):
        errors.append("verified='verified' requires verification.session_date")

    return errors
