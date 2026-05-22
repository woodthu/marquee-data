"""
One-time backfill of historical VIFF series + their films from
archive.org's Wayback Machine.

The Wayback Machine has captured most VIFF series pages between 2014
and today — about 127 distinct canonical series. This script walks
the CDX index, fetches every snapshot per series, parses film slugs
from each snapshot's HTML, and unions everything into
data/viff-series.json.

Run once after the initial setup. The daily `scrape_viff_series.py`
maintains the snapshot forward from there.

Stdlib only. ~10-30 minutes runtime depending on Wayback's mood.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Use the shared `fetch` so we inherit the signal.alarm hard deadline
# (prevents the multi-hour hangs we hit earlier when Wayback trickled
# bytes slower than the per-read timeout).
from _metadata import fetch

SLUG_RE = re.compile(r'href="https://viff\.org/whats-on/([^"/?#]+)/?[^"]*"')

# Same filter VIFFSource.fetchArchivedProgramme applies — these are
# obvious non-film pages that show up as `/whats-on/<slug>/` links.
NON_FILM_SLUGS = {"festival", "membership", "donate", "passes"}

# Series the iOS app fetches from VIFF's live API. Excluded from the
# archive bundle so live + archive don't fight over the same id.
# Mirrors `VIFFSource.knownSeries` in iOS and the constant in
# `scrape_viff_series.py`. Keep all three lists in sync.
ACTIVE_SERIES_SLUGS = {
    "festival-encores",
    "film-studies",
    "pantheon",
    "talking-pictures",
    "kids-club",
    "live-year-round",
}

# CDX endpoint: returns every Wayback snapshot of viff.org/series/* that
# returned a 200 + text/html. Filter to the canonical series page (one
# path component after /series/) and exclude paginated subpaths.
CDX_URL = (
    "http://web.archive.org/cdx/search/cdx"
    "?url=viff.org/series/"
    "&matchType=prefix"
    "&output=json"
    "&filter=statuscode:200"
    "&filter=mimetype:text/html"
    "&fl=urlkey,timestamp"
)

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "viff-series.json"


def fetch_cdx_rows() -> list[tuple[str, str, str]]:
    """Returns (canonical_slug, urlkey, timestamp) for every Wayback
    snapshot of a viff.org/series/<slug>/ page. Strips UTM-suffixed
    duplicates so we walk each canonical slug just once per timestamp."""
    print("Fetching CDX index...")
    raw = fetch(CDX_URL)
    if raw is None:
        print("[fatal] couldn't fetch CDX index", file=sys.stderr)
        sys.exit(1)
    rows = json.loads(raw)
    out: list[tuple[str, str, str]] = []
    for row in rows[1:]:  # row 0 is the header
        urlkey, ts = row[0], row[1]
        parts = urlkey.split("/series/", 1)
        if len(parts) != 2:
            continue
        rest = parts[1].rstrip("/")
        slug = rest.split("?")[0].split("/")[0].strip()
        if not slug:
            continue
        out.append((slug, urlkey, ts))
    print(f"  {len(out)} snapshots across {len(set(s for s, _, _ in out))} canonical slugs")
    return out


def extract_film_slugs(html: str) -> list[str]:
    """Pulls /whats-on/<slug>/ link slugs out of a series page HTML.
    Mirrors the regex in VIFFSource.fetchArchivedProgramme. Strips
    book/* sub-paths (they're ticket links not film pages)."""
    seen: set[str] = set()
    out: list[str] = []
    for slug in SLUG_RE.findall(html):
        slug = slug.strip("/")
        # Drop paths like 'film-slug/book/abc' — the leading slug is
        # the film, the rest is ticketing chatter.
        if "/" in slug:
            slug = slug.split("/")[0]
        if slug.startswith("#"):
            continue
        if slug in NON_FILM_SLUGS:
            continue
        if slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
    return out


def slug_to_title(slug: str) -> str:
    """Best-effort title — title-cases the slug, keeps small words
    lowercase. The iOS enricher overwrites this with the real og:title
    once it loads the film page. Mirrors VIFFSource.titleFromSlug."""
    small = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "vs", "via"}
    parts = slug.split("-")
    out = []
    for i, word in enumerate(parts):
        if i > 0 and word in small:
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:] if word else word)
    return " ".join(out)


def slug_to_series_name(slug: str) -> str:
    """Series-level title casing. Series slugs sometimes look like
    'best-of-2023' or 'viff25-spectrum' — same heuristic as films but
    we leave numeric tokens alone."""
    parts = slug.split("-")
    out = []
    for i, word in enumerate(parts):
        if word.isdigit():
            out.append(word)
        elif i == 0:
            out.append(word[:1].upper() + word[1:])
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def load_existing() -> dict:
    """Read the current JSON snapshot if any. Returned shape matches
    the script's output. Used to merge new slug discoveries into
    existing enriched data instead of clobbering it."""
    if not OUTPUT_PATH.exists():
        return {"series": []}
    try:
        with OUTPUT_PATH.open() as fp:
            return json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[warn] couldn't read existing snapshot, starting fresh: {exc}", file=sys.stderr)
        return {"series": []}


def main() -> int:
    rows = fetch_cdx_rows()

    # Group snapshots by canonical slug. Walk a sampled subset of
    # snapshots per series — at most MAX_SNAPSHOTS_PER_SLUG, evenly
    # spaced across the series's timeline so we capture early, mid,
    # and recent programmes. Walking every snapshot (1230+ in total)
    # ran into Wayback's per-IP rate limiter and produced spotty
    # data. Sampling reduces the request budget by ~6× while still
    # catching the bulk of historical film slugs because Wayback
    # snapshots tend to cluster within a single VIFF programme run
    # (so adjacent snapshots reveal the same films).
    MAX_SNAPSHOTS_PER_SLUG = 6

    raw_by_slug: dict[str, list[tuple[str, str]]] = {}
    for slug, urlkey, ts in rows:
        # Skip series that the iOS app already gets from the live
        # API. They don't belong in the archive bundle.
        if slug in ACTIVE_SERIES_SLUGS:
            continue
        raw_by_slug.setdefault(slug, []).append((urlkey, ts))

    by_slug: dict[str, list[tuple[str, str]]] = {}
    for slug, snaps in raw_by_slug.items():
        snaps.sort(key=lambda r: r[1])  # oldest-first
        if len(snaps) <= MAX_SNAPSHOTS_PER_SLUG:
            by_slug[slug] = snaps
        else:
            # Even-spacing sample. Always include first and last so
            # the timeline endpoints are covered.
            step = (len(snaps) - 1) / (MAX_SNAPSHOTS_PER_SLUG - 1)
            picks = [snaps[round(i * step)] for i in range(MAX_SNAPSHOTS_PER_SLUG)]
            # Dedupe in case rounding produces collisions.
            seen_ts = set()
            sampled: list[tuple[str, str]] = []
            for s in picks:
                if s[1] not in seen_ts:
                    sampled.append(s)
                    seen_ts.add(s[1])
            by_slug[slug] = sampled

    # Merge mode: read the existing snapshot, then overlay newly-
    # discovered slugs onto it. We keep enriched fields (poster,
    # synopsis, director, year, etc.) on series we've previously
    # processed; only `film_slugs` gets unioned with the new scrape.
    existing = load_existing()
    by_slug_existing = {entry["slug"]: entry for entry in existing.get("series", [])}

    print(f"to process: {len(by_slug)} series ({len(by_slug_existing)} already in snapshot)", flush=True)
    total_films = 0

    for i, slug in enumerate(sorted(by_slug.keys()), start=1):
        snapshots = by_slug[slug]
        film_slugs: list[str] = []
        seen: set[str] = set()
        # Preserve any film_slugs we've previously collected for this
        # slug so the union grows monotonically.
        prior_entry = by_slug_existing.get(slug, {})
        prior_films_raw = prior_entry.get("film_slugs", [])
        for f in prior_films_raw:
            slug_str = f["slug"] if isinstance(f, dict) else f
            if slug_str and slug_str not in seen:
                seen.add(slug_str)
                film_slugs.append(slug_str)
        first_seen = snapshots[0][1]
        last_seen = snapshots[-1][1]
        ok_count = 0

        print(f"[{i}/{len(by_slug)}] {slug}: {len(snapshots)} snapshots", flush=True)
        for urlkey, ts in snapshots:
            wayback_url = f"https://web.archive.org/web/{ts}id_/https://viff.org/series/{slug}/"
            html = fetch(wayback_url)
            if html is None:
                continue
            ok_count += 1
            for fs in extract_film_slugs(html):
                if fs not in seen:
                    seen.add(fs)
                    film_slugs.append(fs)
            # Wayback throttles per-IP after sustained traffic; 1s
            # between successful fetches is what their docs recommend
            # for unauthenticated bulk reads.
            time.sleep(1.0)

        print(f"  ok={ok_count}/{len(snapshots)} films={len(film_slugs)}", flush=True)
        total_films += len(film_slugs)

        # Build the merged entry. Preserve every prior enrichment
        # field by default (carries over title, name, hero_image,
        # description, plus per-film dicts) and only update the
        # slug-discovery fields.
        merged_entry = dict(prior_entry)
        merged_entry["slug"] = slug
        merged_entry["name"] = prior_entry.get("name") or slug_to_series_name(slug)
        merged_entry["detail_url"] = prior_entry.get("detail_url") or f"https://viff.org/series/{slug}/"
        merged_entry["first_seen_wayback"] = min(
            prior_entry.get("first_seen_wayback") or first_seen,
            first_seen,
        )
        merged_entry["last_seen_wayback"] = max(
            prior_entry.get("last_seen_wayback") or last_seen,
            last_seen,
        )
        # Re-attach prior film entries by slug — preserves their
        # enrichment dicts. New slugs are added as bare strings;
        # `enrich_metadata.py` will fill them in on the next pass.
        prior_film_entries = {}
        for f in prior_films_raw:
            if isinstance(f, dict):
                prior_film_entries[f["slug"]] = f
        merged_entry["film_slugs"] = [
            prior_film_entries.get(s, s) for s in film_slugs
        ]
        by_slug_existing[slug] = merged_entry

        # Per-series savepoint so a kill mid-run doesn't lose
        # progress. Sort by slug for deterministic diffs.
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        sorted_series = sorted(by_slug_existing.values(), key=lambda s: s["slug"])
        OUTPUT_PATH.write_text(json.dumps({"series": sorted_series}, indent=2) + "\n")

    print()
    print(f"[done] wrote {len(by_slug_existing)} series, {total_films} total film references → {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
