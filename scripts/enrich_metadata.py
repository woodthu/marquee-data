"""
One-shot enrichment pass over data/viff-series.json. Walks every
series + every film slug, fetches the live VIFF page (with Wayback
fallback), and bakes og:image + description + film year/director/
runtime into the JSON.

Idempotent: existing fields are preserved; only missing fields get
filled in. Re-runnable safely after backfill grows the slug list.

Stdlib only. ~30 min for the current 21-series / 150-film set.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _metadata import (
    fetch,
    extract_series_meta,
    extract_film_meta,
    extract_film_page_showtimes,
    cdx_latest_snapshot,
    cdx_all_snapshots,
    resolve_showtime_iso,
)
from concurrent.futures import ThreadPoolExecutor

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "viff-series.json"
DELAY = 1.5  # seconds between fetches — gentle enough not to trip Wayback rate limit
# Hard cap so a stuck Wayback connection can't sink the whole run.
# Re-running the script picks up where it left off (savepoints per
# series), so capping just means more partial passes — never a
# silent multi-hour hang.
MAX_RUNTIME_SECONDS = 8 * 60 * 60


def wayback_url(target: str, ts: str) -> str:
    """Wraps a viff.org URL in a Wayback id_ replay so retired pages
    that 404 on the live site still resolve. `ts` is the snapshot
    timestamp recorded during the backfill."""
    return f"https://web.archive.org/web/{ts}id_/{target}"


def fetch_with_fallback(url: str, wayback_ts: str | None, prefer_wayback: bool = False) -> str | None:
    """Fetch a VIFF page with a Wayback fallback.

    `prefer_wayback=True` skips the live attempt entirely — sensible
    for archive films, which are by definition retired and almost
    always return VIFF's "Page Not Found" stub on the live URL. The
    earlier always-try-live path wasted half the fetch budget on
    pages that we knew couldn't carry useful data.

    `prefer_wayback=False` (default) tries live first — used for
    series pages, some of which are still live-served.

    VIFF returns HTTP 200 + "Page Not Found" body for retired pages
    so we string-match that, not the status code.
    """
    if not prefer_wayback:
        html = fetch(url)
        if html is not None and "Page Not Found" not in html[:4000]:
            return html
    if wayback_ts:
        return fetch(wayback_url(url, wayback_ts))
    return None


import os
# Workers per series. Default 3 — empirically near the sweet spot
# before Wayback rate-limits; can be overridden for retry passes
# (lower count = more reliable, fewer transient failures).
FILM_WORKER_COUNT = int(os.environ.get("MARQUEE_WORKERS", "3"))


def _enrich_one_film(entry: dict, series_ts: str | None) -> dict:
    """Per-film enrichment. Pure function over the entry dict —
    returns the (possibly updated) entry. Safe to call from worker
    threads: uses thread-safe `_metadata.fetch` (socket-level
    timeouts, no signal.alarm) and never mutates shared state.
    """
    fslug = entry["slug"]

    # Skip when fully enriched. We re-fetch when:
    # - any required field is missing, OR
    # - the stored title is the dirty "| Vancouver…" suffix form, OR
    # - director / year are stored as falsy placeholders, OR
    # - `date_range` key has never been stored.
    # Refresh triggers:
    # 1. Missing core fields (title, poster, synopsis, director, year,
    #    date_range) — always need a fresh fetch.
    # 2. Missing screenings AND no `screenings_resolved` flag — try
    #    the film page even if all core fields are filled, since
    #    series-page showtimes may have been absent and film-page
    #    showtimes might be available.
    # 3. Missing runtime AND no `runtime_resolved` flag — runtime
    #    extraction was added late (event__duration regex), and
    #    films enriched in early passes never had it run. Once a
    #    film has been re-walked we mark `runtime_resolved=true`
    #    so this branch doesn't keep re-fetching the same pages.
    missing_core = (
        not entry.get("title")
        or " | " in (entry.get("title") or "")
        or not entry.get("poster")
        or not entry.get("synopsis")
        or not entry.get("director")
        or not entry.get("year")
        or "date_range" not in entry
    )
    needs_screenings_pass = (
        not entry.get("screenings")
        and not entry.get("screenings_resolved")
    )
    needs_runtime_pass = (
        not entry.get("runtime_min")
        and not entry.get("runtime_resolved")
    )
    # 2025-05 director regex broadened to handle anchor-wrapped
    # directors (`<p><a href="#director-bio">Name</a></p>`). Previous
    # passes had marked these films `screenings_resolved=True` /
    # `runtime_resolved=True` even though the director was lost, so
    # neither gate above re-fetches them. This third gate forces a
    # re-walk for any film still missing director despite having
    # synopsis + runtime (i.e. the page WAS fetched successfully —
    # we just need to re-extract). Once the film is walked, the
    # `director_resolved` flag below pins it so subsequent runs
    # skip immediately even if VIFF later removes the director.
    needs_director_pass = (
        not entry.get("director")
        and not entry.get("director_resolved")
        and entry.get("synopsis")
        and not entry.get("fetch_failed")
    )
    if (
        not missing_core
        and not needs_screenings_pass
        and not needs_runtime_pass
        and not needs_director_pass
    ):
        return entry
    # Clear the polluted fields so the new pass can re-fill.
    for stale_key in ("director", "year"):
        if not entry.get(stale_key):
            entry.pop(stale_key, None)
    if " | " in (entry.get("title") or ""):
        entry.pop("title", None)

    # Retry films previously flagged as fetch_failed. Wayback
    # snapshots come and go (rate-limit landing pages get cached
    # briefly, then the real snapshot returns), and the
    # multi-snapshot walk-back below tries up to 15 alternates.
    # Clearing the flag here means each enrichment run gives a
    # fresh chance; once a successful pass lands, the flag is
    # never re-set.
    if entry.get("fetch_failed"):
        entry.pop("fetch_failed", None)
    if "date_range" not in entry:
        entry["date_range"] = None
    url = f"https://viff.org/whats-on/{fslug}/"
    film_ts = entry.get("wayback_ts")
    if film_ts is None:
        film_ts = cdx_latest_snapshot(url) or series_ts
        if film_ts:
            entry["wayback_ts"] = film_ts
        time.sleep(0.5)
    html = fetch_with_fallback(url, film_ts, prefer_wayback=True)
    # Multi-snapshot fallback: when the latest CDX hit returns nothing
    # usable, walk older snapshots until one yields a real page. Many
    # archive films have ~10+ snapshots over their lifetime; older
    # ones (during the actual run) carry the showtime widget that
    # newer ones (post-run) drop entirely. Capped at 15 attempts to
    # cover the long-tail cases where a film has a shallow Wayback
    # history and the few existing snapshots are mostly broken.
    if html is None:
        all_ts = cdx_all_snapshots(url, limit=20)
        # Skip the one we already tried.
        candidates = [t for t in all_ts if t != film_ts]
        for alt_ts in candidates[:15]:
            time.sleep(0.5)
            attempt = fetch_with_fallback(url, alt_ts, prefer_wayback=True)
            if attempt is not None:
                html = attempt
                film_ts = alt_ts
                entry["wayback_ts"] = alt_ts
                break
    if html is None:
        entry["fetch_failed"] = True
        time.sleep(DELAY)
        return entry
    m = extract_film_meta(html)
    for key, val in m.items():
        if val is not None and entry.get(key) is None:
            entry[key] = val
    # Extract showtimes from the film page itself when the series
    # page didn't supply any. The film page's `c-event-instance__
    # date-group` blocks group instances by date, with a separate
    # `<h4 class="c-col-subtitle">` heading per day — see
    # `extract_film_page_showtimes`. Only overwrite if the entry
    # has no existing screenings (those came from the series page,
    # which is the more authoritative source when both are present).
    if not entry.get("screenings"):
        page_showtimes = extract_film_page_showtimes(html)
        if page_showtimes:
            resolved = []
            for inst in page_showtimes:
                iso = resolve_showtime_iso(inst.get("date"), inst.get("time"), film_ts)
                if iso is None:
                    continue
                resolved.append({
                    "start": iso,
                    "venue": inst.get("venue"),
                })
            if resolved:
                entry["screenings"] = resolved
    # Mark the film-page screenings + runtime passes as done so
    # future runs don't re-fetch the page just to re-extract them
    # (films without runtime/showtimes on the page truly have no
    # data — the absence is durable, not a transient miss).
    entry["screenings_resolved"] = True
    entry["runtime_resolved"] = True
    # Pin director-resolution so the third gate above doesn't keep
    # re-fetching films whose pages legitimately don't carry a
    # director label (Q&A panels, talks, "various" shorts).
    entry["director_resolved"] = True
    time.sleep(DELAY)
    return entry


def _enrich_films(films: list, series_ts: str | None) -> list[dict]:
    """Parallel enrichment of every film in a series.

    Order preservation: ThreadPoolExecutor.map returns results in
    the same order as inputs, so we can rebuild new_films in the
    original sequence (the iOS bundle relies on this for stable
    diffs).
    """
    # Migrate bare-slug strings to dicts before dispatching.
    entries = [
        f if isinstance(f, dict) else {"slug": f}
        for f in films
    ]
    with ThreadPoolExecutor(max_workers=FILM_WORKER_COUNT) as pool:
        return list(pool.map(lambda e: _enrich_one_film(e, series_ts), entries))


def main() -> int:
    with OUTPUT_PATH.open() as fp:
        snapshot = json.load(fp)

    series_list = snapshot.get("series", [])
    total_films = sum(len(s.get("film_slugs", [])) for s in series_list)
    print(f"Enriching {len(series_list)} series, {total_films} films")
    print(f"Estimated runtime: {((len(series_list) + total_films) * DELAY) / 60:.1f} min")
    print(f"Runtime cap: {MAX_RUNTIME_SECONDS // 60} min (re-run to continue)")
    print(flush=True)

    deadline = time.monotonic() + MAX_RUNTIME_SECONDS
    for i, series in enumerate(series_list, start=1):
        if time.monotonic() > deadline:
            print(f"[abort] runtime cap hit at series {i}/{len(series_list)} — re-run to continue", flush=True)
            break
        slug = series["slug"]
        ts = series.get("last_seen_wayback")
        # Per-film showtimes from the series page. Populated only
        # when we re-fetch the series page below; otherwise stays
        # empty and the film loop leaves existing `screenings`
        # untouched.
        pending_showtimes: dict = {}
        # Series page meta. We re-fetch when meta_resolved isn't set
        # OR when a known-extractable field is still missing —
        # `date_range` was added after the first pass, so resolved
        # series may need a second visit just to pick up the range.
        # `showtimes_resolved` is the third-pass flag — series page
        # showtime parsing was added after most series were already
        # meta_resolved, so we need one more fetch to extract them.
        needs_series_fetch = (
            not series.get("meta_resolved")
            or "date_range" not in series
            or not series.get("showtimes_resolved")
        )
        if needs_series_fetch:
            html = fetch_with_fallback(series["detail_url"], ts)
            if html is not None:
                m = extract_series_meta(html)
                if m["title"]:
                    # Strip the " | VIFF Centre" suffix that VIFF
                    # appends to og:title — we want the bare series
                    # name.
                    title = m["title"].split(" | ")[0].strip()
                    if title:
                        series["name"] = title
                series["hero_image"] = m["hero_image"]  # may be None
                series["description"] = m["description"]
                series["date_range"] = m["date_range"]  # may be None
                series["meta_resolved"] = True
                # Per-film showtimes parsed from the series page's
                # event cards. Stashed in-memory only; the film loop
                # below reads from `pending_showtimes` and writes
                # the matching slug's `screenings` field.
                pending_showtimes = m.get("film_showtimes") or {}
                hero_marker = "+hero" if m["hero_image"] else "no-hero"
                date_marker = f" {m['date_range']}" if m["date_range"] else ""
                showtime_count = sum(len(v) for v in pending_showtimes.values())
                showtime_marker = f" + {showtime_count} showtimes" if showtime_count else ""
                print(f"[{i}/{len(series_list)}] {slug}: series ok ({hero_marker}){date_marker}{showtime_marker}", flush=True)
            else:
                print(f"[{i}/{len(series_list)}] {slug}: series fetch failed", flush=True)
            time.sleep(DELAY)

        # Per-film meta. Films inside a single series are fetched in
        # parallel via a 3-worker ThreadPoolExecutor — Wayback
        # tolerates ~3 concurrent requests per IP without throttling
        # at our 1.5s sleep. Single-threaded loop got ~4 films/min;
        # this runs ~10 films/min.
        films = series.get("film_slugs", [])
        new_films = _enrich_films(films, series_ts=ts)
        # Apply showtimes parsed from this run's series page. Done
        # outside the worker pool because pending_showtimes is series-
        # scoped — workers shouldn't see it. We overwrite rather than
        # union so a re-fetched series page reflects current truth;
        # if the series page wasn't re-fetched this run (no
        # needs_series_fetch), pending_showtimes is empty and we
        # leave existing `screenings` alone.
        #
        # Year resolution: VIFF strips years from per-event labels.
        # We anchor each showtime against the series page's Wayback
        # timestamp so a Dec-2025 snapshot listing "Jan 03" yields
        # 2026-01-03, not 2025-01-03. See `resolve_showtime_iso`.
        if pending_showtimes:
            for entry in new_films:
                slug = entry.get("slug")
                if not (slug and slug in pending_showtimes):
                    continue
                resolved = []
                for inst in pending_showtimes[slug]:
                    iso = resolve_showtime_iso(inst.get("date"), inst.get("time"), ts)
                    if iso is None:
                        continue
                    resolved.append({
                        "start": iso,
                        "venue": inst.get("venue"),
                    })
                if resolved:
                    entry["screenings"] = resolved
        # Mark this series as having had its showtime extraction pass
        # done, so the next enrichment run won't re-fetch the page
        # purely to re-derive the same instances.
        if needs_series_fetch:
            series["showtimes_resolved"] = True
        series["film_slugs"] = new_films
        # Quick savepoint after each series so a mid-run kill doesn't
        # lose progress.
        OUTPUT_PATH.write_text(json.dumps(snapshot, indent=2) + "\n")

    print()
    print(f"[done] enriched {len(series_list)} series + {total_films} films")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
