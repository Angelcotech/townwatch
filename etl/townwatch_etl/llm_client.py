"""
Spend metering chokepoint.

Every model/OCR call costs money; the fund ledger needs the REAL cost per unit
of work. There is no shared Anthropic client today (~10 call sites construct
their own), so metering is done with a thread-local accumulator rather than by
forcing every extractor through one client object:

    with meter() as usage:          # opens a fresh Usage for this unit of work
        extraction = extract_from_pdf(pdf)   # call sites inside record into it
    cost = pricing.cost_usd(usage)  # → settle the fund reservation

Call sites add ONE line next to each model/OCR call:
    record_anthropic(MODEL, response.usage)      # after a messages.* call
    record_mistral_pages(len(pages))             # after an OCR call

If no meter() is active (e.g. a one-off script), record_* are no-ops, so adding
them is always safe. meter() blocks nest: an inner block's usage rolls up into
the outer one on exit, so a per-meeting meter captures everything beneath it.

This is the spend analogue of the http_client (HTTP) and db.connect (DB)
chokepoints — one place, all domains. Coverage is per-call-site: a call without
a record_* line spends unmetered, so grep for messages.create/stream and ensure
each has a neighbouring record_anthropic.
"""

from __future__ import annotations

import contextlib
import threading

from .pricing import Usage

_local = threading.local()


def current_usage() -> Usage | None:
    return getattr(_local, "usage", None)


@contextlib.contextmanager
def meter():
    """Accumulate model/OCR usage for the duration of this block into a fresh
    Usage (yielded). Nestable: on exit, this block's usage merges into the
    enclosing meter (if any), so costs roll up."""
    prev = getattr(_local, "usage", None)
    u = Usage()
    _local.usage = u
    try:
        yield u
    finally:
        _local.usage = prev
        if prev is not None:
            prev.merge(u)


def record_anthropic(model: str, usage) -> None:
    """Record one Anthropic response's `.usage`. No-op if no meter is active."""
    u = current_usage()
    if u is not None and usage is not None:
        u.record_anthropic(model, usage)


def record_mistral_pages(pages: int) -> None:
    """Record OCR page count. No-op if no meter is active."""
    u = current_usage()
    if u is not None:
        u.record_mistral_pages(pages)
