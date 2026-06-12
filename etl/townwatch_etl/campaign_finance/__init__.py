"""
Campaign-finance domain — the funding paper trail.

Ingests campaign-finance filings (CCDRs, exemption affidavits, financial
disclosures) from public repositories into campaign_filing +
campaign_contribution. First source: Georgia's ethics record-search system
(recordsearch.py), which hosts every GA local filing office's scanned
documents behind an unauthenticated JSON API (discovered 2026-06-12).

Compartmentalization: this package owns the domain. It uses only shared
infrastructure (http_client, db, identity, document_text, llm_client,
audit) — no imports from other domain packages, and nothing imports from
here except its job (jobs/ingest_campaign_finance.py).
"""
