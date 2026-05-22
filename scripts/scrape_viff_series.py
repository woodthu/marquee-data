"""
Daily forward-scrape of VIFF series + their films. Maintains
data/viff-series.json by:

1. Discovering currently-active series from viff.org's homepage links.
2. Re-scraping every series we already know about (from the prior
   JSON) so films currently programmed in known series get appended.
3. Unioning observed films into each series's `film_slugs` array,
   never deleting.

The historical seed comes from `backfill_from_wayback.py` (one-time).
This script is what GitHub Actions runs daily to keep things current.

Stdlib only.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) marquee-data/1.0"
)

SLUG_RE = re.compile(r'href="https://viff\.org/whats-on/([^"/?#]+)/?[^"]*"')
SERIES_LINK_RE = re.compile(r'href="https://viff\.org/series/([^"/?#]+)/?[^"]*"')
DATE_RANGE_RE = re.compile(
    r"<span\s+class=['\"]menu-description['\"]\s*>([^<]+)</span>",
    re.IGNORECASE,
)


def extract_date_range(html: str) -> str | None:
    """Pulls the play-date range from a VIFF series page.
    Format: 'Sep 28 – Oct 8 2023' or 'Oct 2 – 12 2025'.
    The daily scraper writes this to `current_date_range` while a
    series is active; once the series stops appearing live, the
    field stays in the JSON — preserving the run dates as the
    series transitions to archive.
    """
    m = DATE_RANGE_RE.search(html)
    if not m:
        return None
    raw = m.group(1).strip()
    # Decode common HTML entities.
    raw = raw.replace("&amp;", "&").replace("&#039;", "'").replace("&ndash;", "–").replace("&mdash;", "–")
    # Collapse whitespace.
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw or None

NON_FILM_SLUGS = {"festival", "membership", "donate", "passes"}

# Series the iOS app fetches from VIFF's live API — they're standing
# programs that update continuously. We deliberately keep them OUT
# of the archive snapshot so the iOS app doesn't have two competing
# entries for the same series. Mirrors `VIFFSource.knownSeries` in
# the iOS code; keep these in sync if the iOS list changes.
ACTIVE_SERIES_SLUGS = {
    "festival-encores",
    "film-studies",
    "pantheon",
    "talking-pictures",
    "kids-club",
    "live-year-round",
}

# Pages we crawl to discover currently-active series.
DISCOVERY_PAGES = [
    "https://viff.org/",
    "https://viff.org/whats-on/",
]

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "viff-series.json"


def fetch(url: str, attempts: int = 3) -> str | None:
    for attempt in range(attempts):
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=20) as response:
                if response.status != 200:
                    return None
                raw = response.read()
                if raw[:2] == b"\x1f\x8b":
                    import gzip
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            if attempt == attempts - 1:
                print(f"[warn] {url}: {exc}", file=sys.stderr)
                return None
            time.sleep(2 * (attempt + 1))
    return None


def extract_film_slugs(html: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for slug in SLUG_RE.findall(html):
        slug = slug.strip("/")
        if "/" in slug:
            slug = slug.split("/")[0]
        if not slug or slug.startswith("#"):
            continue
        if slug in NON_FILM_SLUGS:
            continue
        if slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
    return out


def discover_series_slugs() -> set[str]:
    """Pulls /series/<slug>/ link slugs off VIFF's homepage + What's On.
    These are what's "currently active" — the daily run uses this
    union'd with the prior JSON's series list so retired series stay
    in the snapshot but new ones get picked up automatically."""
    discovered: set[str] = set()
    for url in DISCOVERY_PAGES:
        html = fetch(url)
        if html is None:
            continue
        for slug in SERIES_LINK_RE.findall(html):
            slug = slug.strip("/").split("?")[0].split("/")[0]
            if slug:
                discovered.add(slug)
    return discovered


def slug_to_series_name(slug: str) -> str:
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


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_date_range_end(raw: str | None) -> tuple[int, int, int] | None:
    """Returns (year, month, day) for the end of a VIFF date range,
    or None if the range can't be parsed.

    VIFF uses several shapes:
      "Sep 28 – Oct 8 2023"      (cross-month, "Mon DD")
      "Oct 2 – 12 2025"          (same-month, "Mon DD ... DD")
      "March 14 – April 2, 2024" (full month names + comma)
      "29 Sep – 9 Oct 2022"      (day-first European)

    Strategy: pull the trailing 4-digit year, then in the head text:
      1. Try day-first pairs ("DD Mon"); if any, take the last.
      2. Else try month-first pairs ("Mon DD"); if any, take the last.
      3. Else same-month form: first month token + last trailing
         integer.
    Returning a tuple keeps this stdlib-only without `datetime`.
    """
    if not raw:
        return None
    text = raw.replace(",", " ")
    ym = re.search(r"(?P<year>(?:19|20)\d{2})\s*$", text)
    if not ym:
        return None
    year = int(ym.group("year"))
    head = text[:ym.start()].strip()

    def month_of(name: str) -> int | None:
        n = name.lower()
        return _MONTHS.get(n[:4]) or _MONTHS.get(n[:3])

    # Day-first ("DD Mon"): pick the last such pair if any.
    df_pairs = list(re.finditer(r"(\d{1,2})\s+([A-Za-z]{3,})", head))
    if df_pairs:
        last = df_pairs[-1]
        m = month_of(last.group(2))
        if m:
            return (year, m, int(last.group(1)))

    # Month-first ("Mon DD"): only valid when there are 2+ pairs
    # (cross-month range). A single pair means same-month form
    # — falls through to the trailing-day branch below.
    mf_pairs = list(re.finditer(r"([A-Za-z]{3,})\s+(\d{1,2})", head))
    if len(mf_pairs) >= 2:
        last = mf_pairs[-1]
        m = month_of(last.group(1))
        if m:
            return (year, m, int(last.group(2)))

    # Same-month form ("Oct 2 – 12"): leading month + trailing day.
    word = re.search(r"([A-Za-z]{3,})", head)
    last_day = re.search(r"(\d{1,2})\s*$", head)
    if word and last_day:
        m = month_of(word.group(1))
        if m:
            return (year, m, int(last_day.group(1)))
    return None


# Days a series can stay un-rediscovered before we treat its
# film roster as frozen. Two weeks is generous enough that a
# brief homepage rotation gap doesn't burn the entry, but tight
# enough that a series that genuinely ran for a weekend and
# disappeared isn't re-fetched daily for months.
PAST_SERIES_STALE_DAYS = 14


def is_past_series(prior: dict, today_yyyymmdd: str) -> bool:
    """Decides whether a known series's film roster is frozen
    enough that re-fetching its detail page is wasted work. We
    only skip when we have positive signal that the run is over —
    erring toward re-fetching when in doubt.

    A series is "past" (skip the network) when EITHER:
      1. Its `current_date_range` parses to an end date strictly
         before today, OR
      2. It was last observed live (`last_seen_live`) more than
         PAST_SERIES_STALE_DAYS ago — i.e. VIFF stopped promoting
         it on the homepage / what's-on weeks ago, so its film
         roster is unlikely to change.

    Anything we can't classify is treated as "still active" and
    re-fetched. Better to spend the bandwidth than miss a new film.
    """
    today_y = int(today_yyyymmdd[:4])
    today_m = int(today_yyyymmdd[4:6])
    today_d = int(today_yyyymmdd[6:8])
    today_tuple = (today_y, today_m, today_d)
    # `current_date_range` is the freshly-extracted field from this
    # script; `date_range` is the older field set by enrichment.
    # Either signal that the run ended in the past is enough.
    for key in ("current_date_range", "date_range"):
        end = parse_date_range_end(prior.get(key))
        if end is not None and end < today_tuple:
            return True
    last_seen = prior.get("last_seen_live")
    if last_seen and len(last_seen) >= 8 and last_seen[:8].isdigit():
        last_y = int(last_seen[:4])
        last_m = int(last_seen[4:6])
        last_d = int(last_seen[6:8])
        # Days-between via integer Julian-ish approximation. We
        # don't need calendrical exactness — a 2-3 day jitter at
        # the threshold is fine since the threshold itself is
        # arbitrary.
        approx_today = today_y * 365 + today_m * 30 + today_d
        approx_last = last_y * 365 + last_m * 30 + last_d
        if approx_today - approx_last > PAST_SERIES_STALE_DAYS:
            return True
    return False


def load_existing() -> dict:
    if not OUTPUT_PATH.exists():
        return {"series": []}
    try:
        with OUTPUT_PATH.open() as fp:
            return json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[warn] couldn't read existing snapshot, starting fresh: {exc}", file=sys.stderr)
        return {"series": []}


def main() -> int:
    today = time.strftime("%Y%m%d")  # matches Wayback's timestamp format
    existing = load_existing()
    existing_by_slug: dict[str, dict] = {entry["slug"]: entry for entry in existing.get("series", [])}

    # Series to re-scrape today: every series we already know about
    # (so we keep adding new films) PLUS any series VIFF currently
    # promotes (so newly-launched series enter the snapshot the day
    # they appear).
    discovered = discover_series_slugs()
    print(f"discovered {len(discovered)} series from homepage/what's-on: {sorted(discovered)}")
    # Drop active series from both prior + discovered. The iOS app
    # owns those via VIFFSource's live API call; including them here
    # would duplicate state and let stale film slugs from the
    # archive shadow live data.
    targets = (set(existing_by_slug.keys()) | discovered) - ACTIVE_SERIES_SLUGS
    if not targets:
        print("[fatal] nothing to scrape — neither existing snapshot nor discovery yielded slugs", file=sys.stderr)
        return 1

    fresh_active: set[str] = set(discovered)
    updated: list[dict] = []
    skipped_past = 0

    for slug in sorted(targets):
        prior = existing_by_slug.get(slug, {})
        # Past-series gate: a series whose run has ended and whose
        # roster is therefore frozen doesn't need a daily re-fetch.
        # `is_past_series` looks at the cached `current_date_range`
        # (preferred — definitive when present) plus the
        # `last_seen_live` heuristic for series whose date range
        # never parsed cleanly. Discovery overrides this — if VIFF
        # is currently promoting the series again (re-screening,
        # festival encore), we re-fetch even if the cached range
        # looks past.
        if slug not in discovered and is_past_series(prior, today):
            updated.append(prior)
            skipped_past += 1
            continue

        url = f"https://viff.org/series/{slug}/"
        html = fetch(url)
        # Prior films are a mix of bare-slug strings (legacy seed shape
        # from `backfill_from_wayback.py`) and dicts (current enriched
        # shape `{"slug": ..., "director": ..., ...}`). We preserve
        # whatever shape was there so we don't churn JSON unnecessarily;
        # `enrich_metadata.py` is what migrates strings to dicts.
        prior_films: list = list(prior.get("film_slugs", []))

        if html is None:
            # Series page no longer reachable. Keep prior data; mark
            # nothing as observed today. If a series stays unreachable
            # for many runs, that's a signal it's been retired —
            # downstream consumers can compare last_seen_live to today.
            print(f"  [skip] {slug}: 404/unreachable (kept {len(prior_films)} prior films)")
            updated.append({
                **prior,
                "slug": slug,
                "name": prior.get("name") or slug_to_series_name(slug),
                "detail_url": prior.get("detail_url") or url,
                "film_slugs": prior_films,
            })
            continue

        film_slugs = extract_film_slugs(html)
        # Union: prior films first (stable history), then any newly-
        # observed films appended. Prior films may be either bare
        # slug strings (legacy seed shape) or dicts with a `slug`
        # key (current enriched shape) — `enrich_metadata.py`
        # migrates strings to dicts on first walk. We normalize to
        # the dict shape here so a fresh-discovery slug doesn't
        # ever land as a string in the same array.
        def slug_of(film):
            return film.get("slug") if isinstance(film, dict) else film

        merged_films = list(prior_films)
        seen = {slug_of(f) for f in prior_films if slug_of(f)}
        for slug in film_slugs:
            if slug not in seen:
                merged_films.append({"slug": slug})
                seen.add(slug)

        added = len(merged_films) - len(prior_films)
        marker = " +%d new" % added if added else ""
        print(f"  [ok] {slug}: {len(film_slugs)} now / {len(merged_films)} total{marker}")

        entry = {
            "slug": slug,
            "name": prior.get("name") or slug_to_series_name(slug),
            "detail_url": url,
            "film_slugs": merged_films,
        }
        # Carry forward Wayback-era seen markers if present.
        if "first_seen_wayback" in prior:
            entry["first_seen_wayback"] = prior["first_seen_wayback"]
        if "last_seen_wayback" in prior:
            entry["last_seen_wayback"] = prior["last_seen_wayback"]
        # Capture the play-date range from the live series page.
        # Persisted across runs even after the series rotates out of
        # discovery — that's how archive entries inherit dates from
        # when they were active. If today's fetch fails to find a
        # range (rare), keep whatever prior value we had.
        date_range = extract_date_range(html)
        if date_range:
            entry["current_date_range"] = date_range
        elif "current_date_range" in prior:
            entry["current_date_range"] = prior["current_date_range"]
        # Track when we last observed the series live on viff.org.
        entry["last_seen_live"] = today
        updated.append(entry)

    # Make sure any prior series not in `targets` (shouldn't happen,
    # but defensively) survives the rewrite.
    for slug, prior in existing_by_slug.items():
        if slug in ACTIVE_SERIES_SLUGS:
            continue
        if slug not in (e["slug"] for e in updated):
            updated.append(prior)

    updated.sort(key=lambda s: s["slug"])
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps({"series": updated}, indent=2) + "\n")

    print()
    print(f"[done] {len(updated)} series in snapshot, {sum(len(s.get('film_slugs', [])) for s in updated)} total films")
    if skipped_past:
        print(f"[done] skipped {skipped_past} past series (frozen roster)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
