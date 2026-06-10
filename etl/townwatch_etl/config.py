import os
from pathlib import Path
from dotenv import load_dotenv

# Anchor the .env to the repo (etl/.env, one level above this package) rather than
# searching from the current working directory — so a job run from ANY cwd (the
# repo root, a sibling repo, a slash command) resolves the same DATABASE_URL
# instead of failing with "No database URL configured". Falls back to the default
# cwd-upward search when the anchored file is absent (e.g. the container, where no
# .env is shipped and the Railway-provided env vars are used as-is).
#
# override=True so the local .env is the source of truth: an EMPTY or stale
# variable already exported in the ambient shell (e.g. ANTHROPIC_API_KEY="")
# would otherwise shadow the real value in .env under the default override=False
# and cause confusing "could not resolve authentication" errors.
_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(_ENV_PATH if _ENV_PATH.exists() else None, override=True)

# Prefer DATABASE_URL; fall back to DATABASE_PUBLIC_URL (Railway exposes both —
# the internal one and the public-proxy one). This lets a service wired with
# either variable name boot, and turns the otherwise-cryptic KeyError into an
# actionable message naming exactly what's missing.
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "No database URL configured: set DATABASE_URL (preferred) or "
        "DATABASE_PUBLIC_URL in the service environment."
    )
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
# Mistral OCR — primary text extraction for scanned PDFs (validated ~50x
# cheaper, ~25x faster, and MORE complete than frontier vision). When unset,
# the pipeline falls back to vision for scanned docs.
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Vision-path rasterization DPI. 0/unset → send the raw PDF document (current
# default, no behaviour change). Set a DPI (e.g. 150) to rasterize scanned
# pages to images at that resolution before sending — smaller payload, lower
# latency. Tune to the accuracy knee found by jobs.dpi_sweep.
VISION_RENDER_DPI = int(os.environ.get("VISION_RENDER_DPI", "0")) or None
