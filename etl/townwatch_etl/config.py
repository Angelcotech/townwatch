import os
from dotenv import load_dotenv

# override=True so the local .env is the source of truth: an EMPTY or stale
# variable already exported in the ambient shell (e.g. ANTHROPIC_API_KEY="")
# would otherwise shadow the real value in .env under the default
# override=False and cause confusing "could not resolve authentication"
# errors. Safe in the container: no .env is shipped there, so load_dotenv is
# a no-op and the Railway-provided env vars are used as-is.
load_dotenv(override=True)

DATABASE_URL = os.environ["DATABASE_URL"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
