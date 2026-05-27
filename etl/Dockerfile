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
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-core \
        libreoffice-writer \
        fonts-liberation \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps from pyproject.toml. Use hatchling-built wheel for speed.
COPY etl/pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

COPY etl/townwatch_etl ./townwatch_etl

# Default to printing help; real commands are passed at runtime.
CMD ["python", "-m", "townwatch_etl.cli", "--help"]
