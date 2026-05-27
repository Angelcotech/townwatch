-- Migration 021: jurisdiction outer boundary polygon.
--
-- Used purely for visual identification — a small SVG outline rendered
-- next to the jurisdiction name on its landing page. Sourced from
-- Census TIGERweb (universal across US counties + incorporated places)
-- by ingest_boundary.py.
--
-- No GIST index: we don't query this column spatially (no point-in-
-- polygon, no intersection). It's just rendered with ST_AsSVG.

ALTER TABLE jurisdiction
    ADD COLUMN boundary GEOMETRY(MultiPolygon, 4326);
