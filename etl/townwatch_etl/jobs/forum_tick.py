"""
Forum heartbeat — the hourly tick that runs the live-forum lifecycle.

The forum is time-sensitive on BOTH ends, and the daily extraction worker is too
coarse for either:

  1. OPEN — a forum goes live the moment a meeting's agenda is published AND
     extracted. Agendas post on the government's schedule, often days before a
     meeting; waiting for the 06:00 daily run can blow most of the comment
     window. So extract upcoming agendas hourly: as soon as one appears, its
     forum opens. (`extract_agendas --all --upcoming` only touches future
     meetings that have an agenda URL and no items yet, so it spends only when a
     NEW agenda shows up, and it's fund-gated per jurisdiction.)

  2. CLOSE — the window closes 12h before the meeting, when comments are
     compiled, agent-reviewed, and emailed to the clerk. An evening meeting's
     cutoff lands mid-day; a once-daily run would miss it. So submit due digests
     hourly. (`submit_comments` is idempotent — comments_submitted_at guards a
     double-send.)

Run hourly via railway.comments.toml. Subprocess isolation (like daily_refresh)
so a failure in one half never blocks the other.
"""

from __future__ import annotations

import subprocess
import sys
import time


def _run(label: str, module: str, args: list[str]) -> bool:
    print(f"\n=== forum_tick: {label} ===")
    started = time.time()
    rc = subprocess.call([sys.executable, "-m", f"townwatch_etl.jobs.{module}", *args])
    print(f"=== {label}: exit {rc} ({time.time() - started:.0f}s) ===")
    return rc == 0


def main() -> int:
    # 1. Open forums: extract any newly-published upcoming agendas.
    ok_open = _run("open (extract upcoming agendas)", "extract_agendas", ["--all", "--upcoming"])
    # 2. Close + deliver: submit digests for meetings past their −12h cutoff.
    ok_close = _run("close (submit comment digests)", "submit_comments", [])
    return 0 if (ok_open and ok_close) else 1


if __name__ == "__main__":
    sys.exit(main())
