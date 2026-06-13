# TownWatch ETL — Python + LibreOffice for legacy DOC/DOCX conversion.
#
# The agenda extractor dispatches by content type. Most agendas are PDF
# (handled by pdfplumber + Anthropic SDK), but vintage council agendas
# arrive as .doc or .docx. libreoffice is needed for the .doc binary
# format — no usable pure-Python reader exists.
#
# Build (from repo root — Railway uses the repo root as its build
# context unless rootDirectory is set, so all COPY paths in this file
# are written relative to the repo root for portability):
#   docker build -t townwatch-etl -f etl/Dockerfile .
#
# Run a job:
#   docker run --rm --env-file ./etl/.env townwatch-etl \
#     python -m townwatch_etl.jobs.extract_agendas --all
#
# On Railway: dockerfilePath=etl/Dockerfile, no rootDirectory needed.

FROM python:3.12-slim

# libreoffice-core + libreoffice-writer is the smallest install that
# handles .doc / .docx → text/pdf conversion. Skip the full GUI cask.
# fonts-liberation is needed so DOC layout renders correctly during
# headless conversion.
# poppler-utils provides pdftotext — a streaming C extractor that pulls text
# from arbitrarily large PDFs at near-constant memory. pdfplumber/pdfminer
# (used for layout-fidelity on normal docs) balloons to 10+ GB on big
# image-heavy government packets (a 196 MB CivicClerk packet peaked ~11 GB),
# so document_text routes oversized PDFs to pdftotext instead — the permanent,
# scale-proof path as packets keep growing.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-core \
        libreoffice-writer \
        fonts-liberation \
        poppler-utils \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Replicate the repo layout under /app: the package resolves its data dirs
# RELATIVE to its own source location (jurisdiction.py / migrate.py /
# audit.py use Path(__file__).parents[2] / "jurisdictions" | "migrations").
# So townwatch_etl must live two levels under the dir that also holds
# jurisdictions/ and migrations/ — i.e. /app/etl/townwatch_etl alongside
# /app/jurisdictions and /app/migrations.
#
# The package source is copied BEFORE `pip install` so the build backend
# can actually find the package to put in the wheel — the previous order
# (pyproject only, install, then source) gave hatchling nothing to ship and
# the build failed once hatchling stopped tolerating an empty selection.
COPY etl/pyproject.toml ./etl/
COPY etl/townwatch_etl ./etl/townwatch_etl
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ./etl

# Runtime data the jobs read by relative path. Copied after the install so
# editing a config doesn't bust the dependency layer cache.
COPY jurisdictions ./jurisdictions
COPY migrations ./migrations

# Run from the in-tree source (not the site-packages copy) so __file__ sits
# at /app/etl/townwatch_etl/... and parents[2] == /app, where the data dirs
# live. `python -m` honors PYTHONPATH ahead of site-packages.
ENV PYTHONPATH=/app/etl

# Default to printing the orchestrator's help; real commands (and the
# Railway cron's startCommand) are passed at runtime. NOTE: there is no
# townwatch_etl.cli module despite the [project.scripts] entry point — that
# console script is currently broken and unused; the deploy invokes jobs via
# `python -m townwatch_etl.jobs.<job>` directly.
CMD ["python", "-m", "townwatch_etl.jobs.daily_refresh", "--help"]
