"""
Repair handler base class + shared result types.

Each subclass owns ONE diagnosis_kind and tells the engine whether it
can_handle() a given disputed motion + finding. The engine asks handlers
in order until one claims; if none claim, the motion stays disputed and
is logged for manual review.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any

import psycopg


class RepairOutcome(str, Enum):
    REPAIRED = "repaired"           # Data mutated; re-run QA to confirm clean
    UNREPAIRABLE = "unrepairable"   # Confirmed cannot be fixed automatically
    SKIPPED = "skipped"             # Handler chose not to act this run
    ERROR = "error"                 # Handler crashed mid-repair


@dataclass
class RepairResult:
    outcome: RepairOutcome
    handler: str
    notes: str = ""
    mutations: dict[str, Any] | None = None   # what changed (for audit logging)


class RepairHandler(ABC):
    """Subclass per repair strategy. Each handler is independent + idempotent."""

    handler_id: str = ""

    @abstractmethod
    def can_handle(self, finding: dict, motion: dict) -> bool:
        """Return True iff this handler claims this finding+motion pair."""

    @abstractmethod
    def repair(self, conn: psycopg.Connection, finding: dict, motion: dict) -> RepairResult:
        """Mutate the database to fix the underlying issue. Return RepairResult."""
