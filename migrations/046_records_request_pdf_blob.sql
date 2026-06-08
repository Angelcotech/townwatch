-- Store the rendered records-request PDF in the database instead of on a
-- local filesystem. The ETL worker and the web app are SEPARATE Railway
-- services with separate disks, so a PDF written to public/records-requests/
-- by the ETL never reaches the web service. Postgres is the one resource both
-- share — so the PDF bytes live here and the web app serves them from an
-- admin-gated route (/admin/records-request/<id>/pdf).
--
-- pdf_path is kept for backward-compat/debugging (the old logical
-- /records-requests/<file> string) but is no longer the delivery path.

ALTER TABLE records_request ADD COLUMN IF NOT EXISTS pdf_bytes BYTEA;
ALTER TABLE records_request ADD COLUMN IF NOT EXISTS pdf_filename TEXT;
