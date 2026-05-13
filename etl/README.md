# TownWatch ETL

Ingests public records into the TownWatch Postgres database. One job per data source per jurisdiction.

## Setup

```bash
cd etl
uv venv && source .venv/bin/activate
uv pip install -e .
cp .env.example .env
# fill in DATABASE_URL + ANTHROPIC_API_KEY
```

## Architecture

Every ingest job inherits from `IngestJob` in `townwatch_etl/ingest_base.py`:

```python
from townwatch_etl.ingest_base import IngestJob

class GrovetownOfficials(IngestJob):
    source_name = "cityofgrovetown.com"
    source_type = "scrape"
    source_url = "https://cityofgrovetown.com/198/City-Council"

    def ingest(self) -> None:
        # ... scraping work ...
        self.insert("official", {"canonical_name": "Eric Blair", ...})

if __name__ == "__main__":
    result = GrovetownOfficials().run()
    print(result)
```

### Lifecycle

`run()` orchestrates:
1. Opens DB connection (auto-commits on success, rolls back on error)
2. `open_run()` creates a `data_source` row with a unique `ingest_run_id`
3. `ingest()` runs your subclass logic; every `self.insert()` auto-attaches `data_source_id`
4. `close_run()` annotates the `data_source` row with summary stats

### Identity resolution

Use `townwatch_etl.identity` for mapping name strings to canonical officials. **Never auto-create** — always inspect candidates and decide explicitly.

```python
from townwatch_etl import identity

with connect() as conn:
    # Try exact alias match first
    oid = identity.find_by_alias(conn, "Bob Smith")

    # Or get ranked fuzzy candidates
    candidates = identity.find_candidates(conn, "Bob Smith", jurisdiction_id=42)

    # Or use single-shot resolver (high confidence only)
    oid = identity.resolve(conn, "Bob Smith", source_system="county_assessor")
    if oid is None:
        # Ambiguous — log and skip, or create new official explicitly
        ...
```

## Why no tests yet

Identity resolution is fragile — tests will be written against real ingest data, not synthetic fixtures. Phase 1 builds the first real scrape (Grovetown officials), tunes the similarity threshold against that data, then locks in tests.

## DRY_RUN mode

Set `DRY_RUN=true` in `.env` to print intended writes without executing. Useful for scraper development.
