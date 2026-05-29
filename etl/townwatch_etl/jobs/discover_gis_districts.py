"""
Auto-discover a jurisdiction's elected-district polygons layer.

Onboarding a new jurisdiction normally means hand-pasting a districts
layer URL into jurisdictions/{slug}.json. This job replaces that step:
given only the slug + its existing config (state, county_fips,
official_website), find the right ArcGIS REST FeatureServer layer URL
and propose it for review.

Strategy ladder, cheapest first:

  1. ArcGIS Online directory search  (arcgis.com/sharing/rest/search)
       Most counties publish to Esri's hosted SaaS even when they
       self-host the GIS portal. Structured JSON in/out, no scraping.

  2. URL pattern probe                (gis.{domain}, maps.{domain}, ...)
       Self-hosted Esri servers follow conventional URL shapes.
       Probe a small fixed set; for each that responds, list its
       services and look for a "districts" layer.

  3. Vision crawl                     (stub for now — see TODO)
       Screenshot the county's site, ask Sonnet to find the GIS link.
       Expensive — last resort.

Each method tags its proposal with a confidence score:
  high   = exact title match (e.g. "Commission Districts")
  medium = best fuzzy match among multiple candidates
  low    = single plausible candidate with no confirming signal

Output: the jurisdiction's proposed `gis` block. By default just
printed; with --apply, merged into jurisdictions/{slug}.json.

Run:
    python -m townwatch_etl.jobs.discover_gis_districts --slug columbia-county-ga
    python -m townwatch_etl.jobs.discover_gis_districts --slug columbia-county-ga --apply
    python -m townwatch_etl.jobs.discover_gis_districts                  # all configs
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.parse import urlparse

from ..http_client import civic_client

from ..jurisdiction import JURISDICTIONS_DIR, list_slugs, load_config


Confidence = Literal["high", "medium", "low"]

USER_AGENT = "TownWatch-discover-gis/0.1 (civic transparency research)"

# Terms that look like an elected-district layer. Ranked by specificity —
# earlier terms beat later when scoring candidates.
DISTRICT_LAYER_TERMS = [
    "commission district",
    "commissioner district",
    "council district",
    "city council ward",
    "ward",
    "board of education district",
    "school board district",
    "voting district",
    "election district",
    "district",  # last-resort catch-all
]

# Body-type keywords that disambiguate which layer to pick when an
# org publishes both council + commissioner + school-board polygons.
BODY_TYPE_KEYWORDS = {
    "county": ["commission", "commissioner", "boc"],
    "city": ["council", "ward"],
    "town": ["council", "ward"],
    "village": ["council", "ward"],
    "school_district": ["school", "board of education", "boe"],
}


@dataclass
class DistrictProposal:
    url: str
    confidence: Confidence
    method: str         # which strategy produced this proposal
    layer_title: str    # human-readable title for operator review
    item_id: Optional[str] = None


# ---------------------------------------------------------------------
# Strategy 1: ArcGIS Online directory search
# ---------------------------------------------------------------------

def search_arcgis_online(
    *, jurisdiction_name: str, state_abbr: str, body_type: str
) -> list[DistrictProposal]:
    """Query Esri's global directory for Feature Services that look
    like the jurisdiction's elected-district polygons."""
    candidates: list[DistrictProposal] = []
    body_keywords = BODY_TYPE_KEYWORDS.get(body_type, ["district"])
    # Two-pass search — narrow first, broader fallback. Each pass returns
    # up to 50 items; the API is paginated but we don't need more than
    # the first page for a discovery probe.
    queries = [
        f'"{jurisdiction_name}" {body_keywords[0]} districts type:"Feature Service"',
        f'"{jurisdiction_name}" districts {state_abbr} type:"Feature Service"',
    ]
    seen_item_ids: set[str] = set()
    with civic_client(default_timeout=20.0) as client:
        for q in queries:
            try:
                r = client.get(
                    "https://www.arcgis.com/sharing/rest/search",
                    params={"q": q, "f": "json", "num": "50"},
                )
                r.raise_for_status()
                data = r.json()
            except Exception:
                continue
            for item in data.get("results", []):
                if item.get("type") != "Feature Service":
                    continue
                iid = item.get("id")
                if iid in seen_item_ids:
                    continue
                seen_item_ids.add(iid)
                title = (item.get("title") or "").strip()
                url = item.get("url") or ""
                # We want a specific layer within the service, not the
                # service root. The search returns the service URL; we
                # probe its layers to find the district one.
                layer_url, confidence = _resolve_district_layer(
                    client, url, title, body_keywords, jurisdiction_name,
                )
                if layer_url:
                    candidates.append(DistrictProposal(
                        url=layer_url,
                        confidence=confidence,
                        method="arcgis_online_search",
                        layer_title=title,
                        item_id=iid,
                    ))
    # De-dupe by URL, keep best confidence first
    by_url: dict[str, DistrictProposal] = {}
    for c in candidates:
        existing = by_url.get(c.url)
        if existing is None or _confidence_rank(c.confidence) > _confidence_rank(existing.confidence):
            by_url[c.url] = c
    return sorted(by_url.values(), key=lambda p: -_confidence_rank(p.confidence))


def _resolve_district_layer(
    client, service_url: str, service_title: str,
    body_keywords: list[str], jurisdiction_name: str,
) -> tuple[Optional[str], Confidence]:
    """Given a FeatureServer root URL, list its layers and pick the
    one that most resembles an elected-district polygon layer.
    Returns (full_layer_url, confidence) or (None, _) if nothing fits.
    """
    if not service_url:
        return None, "low"
    # ArcGIS REST: /FeatureServer?f=json returns the list of layers
    try:
        r = client.get(service_url, params={"f": "json"}, timeout=15.0)
        r.raise_for_status()
        meta = r.json()
    except Exception:
        return None, "low"
    layers = meta.get("layers", []) or []
    if not layers:
        # Maybe service_url already IS a layer URL (ends in /N)
        m = re.match(r"^(.*?/(?:Feature|Map)Server)/(\d+)$", service_url)
        if m:
            return service_url, _score_layer_title(service_title, body_keywords, jurisdiction_name)
        return None, "low"

    best: tuple[Optional[str], Confidence, int] = (None, "low", -1)
    for layer in layers:
        if layer.get("type") not in ("Feature Layer", None):
            continue
        layer_name = (layer.get("name") or "").strip()
        layer_id = layer.get("id")
        score = _score_layer_text(layer_name, body_keywords, jurisdiction_name)
        # Service title also informative when the layer itself is generically named
        if score == 0:
            score = max(score, _score_layer_text(service_title, body_keywords, jurisdiction_name) - 1)
        if score > best[2]:
            full_url = f"{service_url.rstrip('/')}/{layer_id}"
            conf: Confidence = "high" if score >= 3 else ("medium" if score >= 2 else "low")
            best = (full_url, conf, score)
    return best[0], best[1]


def _score_layer_title(title: str, body_keywords: list[str], jurisdiction_name: str) -> Confidence:
    n = _score_layer_text(title, body_keywords, jurisdiction_name)
    return "high" if n >= 3 else ("medium" if n >= 2 else "low")


def _score_layer_text(text: str, body_keywords: list[str], jurisdiction_name: str) -> int:
    """Numeric score (higher = better) for how district-y this string looks."""
    if not text:
        return 0
    t = text.lower()
    score = 0
    # +2: contains a body-type keyword (commission/council/etc.)
    if any(kw in t for kw in body_keywords):
        score += 2
    # +2: contains the word 'district' or 'ward'
    if "district" in t or "ward" in t:
        score += 2
    # +1: contains the jurisdiction name (filters out unrelated layers)
    if jurisdiction_name.lower() in t:
        score += 1
    # +1: "county districts" / "city districts" - the local-government
    # naming convention when no body keyword is present. Disambiguates
    # local commissioner districts from federal/state ones inside the
    # same multi-layer redistricting service.
    if "county district" in t or "city district" in t:
        score += 1
    # -3: clearly NOT a local elected-body district. Federal congressional
    # districts and state house/senate districts often live in the same
    # ArcGIS service as the local ones; this penalty keeps them out.
    for fed_state in ("congressional", "u.s. house", "us house",
                      "state senate", "state house", "senate district",
                      "house district"):
        if fed_state in t:
            score -= 3
    # -2: not a polygon layer at all (parcels, zoning, infrastructure)
    for neg in ("parcel", "zoning", "address point", "road", "subdivision",
                "easement", "floodplain", "utility", "precinct"):
        if neg in t:
            score -= 2
    return score


def _confidence_rank(c: Confidence) -> int:
    return {"high": 3, "medium": 2, "low": 1}[c]


# ---------------------------------------------------------------------
# Strategy 2: URL pattern probe (self-hosted Esri servers)
# ---------------------------------------------------------------------

PATTERN_HOSTS = ["gis.{host}", "maps.{host}", "geo.{host}", "arcgis.{host}"]
PATTERN_PATHS = [
    "/server/rest/services",   # ArcGIS Server default
    "/arcgis/rest/services",   # ArcGIS Server with /arcgis prefix
]


def probe_known_patterns(
    *, official_website: str, jurisdiction_name: str, body_type: str,
) -> list[DistrictProposal]:
    """Try a small set of conventional URL shapes for self-hosted
    Esri servers. For each that returns a valid services directory,
    list its services and look for district layers within them."""
    if not official_website:
        return []
    base_host = urlparse(official_website).netloc.replace("www.", "")
    if not base_host:
        return []
    body_keywords = BODY_TYPE_KEYWORDS.get(body_type, ["district"])
    candidates: list[DistrictProposal] = []
    with civic_client(default_timeout=15.0) as client:
        for host_pat in PATTERN_HOSTS:
            host = host_pat.format(host=base_host)
            for path in PATTERN_PATHS:
                root = f"https://{host}{path}"
                try:
                    r = client.get(root, params={"f": "json"}, timeout=8.0)
                    if r.status_code != 200:
                        continue
                    dir_meta = r.json()
                except Exception:
                    continue
                # Walk services + folders, score each candidate
                for svc in dir_meta.get("services", []) or []:
                    svc_name = svc.get("name") or ""
                    svc_type = svc.get("type") or ""
                    if svc_type not in ("FeatureServer", "MapServer"):
                        continue
                    svc_url = f"{root}/{svc_name}/{svc_type}"
                    layer_url, confidence = _resolve_district_layer(
                        client, svc_url, svc_name, body_keywords, jurisdiction_name,
                    )
                    if layer_url:
                        candidates.append(DistrictProposal(
                            url=layer_url,
                            confidence=confidence,
                            method="url_pattern_probe",
                            layer_title=svc_name,
                        ))
                # Folders contain nested services — explore one level only
                for folder in dir_meta.get("folders", []) or []:
                    folder_url = f"{root}/{folder}"
                    try:
                        rr = client.get(folder_url, params={"f": "json"}, timeout=8.0)
                        if rr.status_code != 200:
                            continue
                        fmeta = rr.json()
                    except Exception:
                        continue
                    for svc in fmeta.get("services", []) or []:
                        svc_name = svc.get("name") or ""
                        svc_type = svc.get("type") or ""
                        if svc_type not in ("FeatureServer", "MapServer"):
                            continue
                        # svc_name may include the folder prefix; strip if so
                        svc_path = svc_name.split("/", 1)[-1] if "/" in svc_name else svc_name
                        svc_url = f"{folder_url}/{svc_path}/{svc_type}"
                        layer_url, confidence = _resolve_district_layer(
                            client, svc_url, svc_name, body_keywords, jurisdiction_name,
                        )
                        if layer_url:
                            candidates.append(DistrictProposal(
                                url=layer_url,
                                confidence=confidence,
                                method="url_pattern_probe",
                                layer_title=svc_name,
                            ))
    return sorted(candidates, key=lambda p: -_confidence_rank(p.confidence))


# ---------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------

def discover_one(slug: str) -> tuple[Optional[DistrictProposal], list[DistrictProposal]]:
    """Run the strategy ladder for one jurisdiction. Returns
    (best_proposal, all_candidates). best is None when nothing fits."""
    config = load_config(slug)
    j = config["jurisdiction"]
    name = j["display_name"]
    state = j["state"]
    body_type = j["type"]
    official_website = j.get("official_website", "")

    all_candidates: list[DistrictProposal] = []

    # Strategy 1
    print(f"  → searching ArcGIS Online for {name!r}...", file=sys.stderr)
    online = search_arcgis_online(jurisdiction_name=name, state_abbr=state, body_type=body_type)
    print(f"     {len(online)} candidate(s)", file=sys.stderr)
    all_candidates.extend(online)

    # Short-circuit when we already have a high-confidence hit
    if online and online[0].confidence == "high":
        return online[0], all_candidates

    # Strategy 2
    print(f"  → probing self-hosted patterns under {official_website!r}...", file=sys.stderr)
    probed = probe_known_patterns(
        official_website=official_website,
        jurisdiction_name=name,
        body_type=body_type,
    )
    print(f"     {len(probed)} candidate(s)", file=sys.stderr)
    all_candidates.extend(probed)

    # Pick the best across both strategies
    if not all_candidates:
        return None, []
    best = max(all_candidates, key=lambda p: _confidence_rank(p.confidence))
    return best, all_candidates


def apply_to_config(slug: str, proposal: DistrictProposal) -> None:
    """Merge the proposal into jurisdictions/{slug}.json under the
    `gis` key. Never overwrites a high-confidence existing entry."""
    path = JURISDICTIONS_DIR / f"{slug}.json"
    cfg = json.loads(path.read_text())
    existing = (cfg.get("gis") or {}).get("districts_endpoint")
    existing_conf = (cfg.get("gis") or {}).get("districts_endpoint_confidence")
    if existing and existing_conf == "high" and proposal.confidence != "high":
        print(f"  ⊘ {slug}: existing high-confidence URL — refusing to overwrite with {proposal.confidence}", file=sys.stderr)
        return
    cfg.setdefault("gis", {})
    cfg["gis"]["platform"] = "arcgis_online" if "arcgis.com" in proposal.url else "arcgis_rest"
    cfg["gis"]["districts_endpoint"] = proposal.url
    cfg["gis"]["districts_endpoint_confidence"] = proposal.confidence
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"  ✓ {slug}: applied (confidence={proposal.confidence}, method={proposal.method})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", help="Only this jurisdiction. Default: all configs.")
    parser.add_argument("--apply", action="store_true",
                        help="Write proposed URL back into the config file.")
    args = parser.parse_args()

    slugs = [args.slug] if args.slug else list_slugs()
    for slug in slugs:
        print(f"\n=== {slug} ===", file=sys.stderr)
        best, all_candidates = discover_one(slug)
        if not best:
            print(f"  ✗ {slug}: no candidates found across any strategy", file=sys.stderr)
            continue
        print(f"\n  best: {best.confidence}  via {best.method}")
        print(f"    title: {best.layer_title}")
        print(f"    url:   {best.url}")
        if len(all_candidates) > 1:
            print(f"\n  other candidates ({len(all_candidates) - 1}):")
            for c in all_candidates[1:6]:
                print(f"    [{c.confidence}] {c.method}: {c.layer_title} — {c.url}")
        if args.apply:
            apply_to_config(slug, best)
    return 0


if __name__ == "__main__":
    sys.exit(main())
