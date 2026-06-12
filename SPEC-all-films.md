# Spec: `films.json` — searchable all-films archive

**Status:** proposed · **Owner repos:** scraping in `woodthu/marquee-data-private`, JSON published from this repo (`marquee-data`) · **Consumer:** Marquee iOS app

## Goal

Make **every film either cinema has ever screened** searchable in the app — not
just films that happened to belong to a scraped series. Today the search corpus
is `What's On ∪ series programmes`, so a one-off past screening that wasn't part
of a series is invisible. This spec adds a standalone `films.json` archive that
accumulates all films across both cinemas, with their **complete screening
dates**, so users can look back at anything the app has seen.

## Why a new file (not derived from existing data)

- **VIFF exposes no public back-catalog.** Once a screening rolls off viff.org,
  it's gone. So for VIFF we *must* accumulate what we've seen over time — the
  same monotonic principle the existing `viff-series.json` already uses
  (`film_slugs` is "never removed once observed").
- **Cinematheque DOES publish a structured all-films index** at
  `thecinematheque.ca/films` (paginated `/films?page=N`, each film at
  `/films/<year>/<slug>`). This is an authoritative source we can scrape
  directly and reproducibly — preferred over relying on accumulation for
  Cinematheque.
- **Programme guides cannot substitute.** They're just links to PDFs (title +
  cover + date + `pdfURL`); the film lineup lives *inside* the rendered PDF as
  layout, not as structured data. Extracting it would need fragile per-PDF OCR.
  The structured film pages above are the clean source.

## GitHub Action affordability

Not a concern. The daily Action already builds `viff-series.json` at ~3.4 MB
(113 series). A flat film record is smaller than a series record; even
5,000–10,000 films is a few MB of JSON — trivial for the runner to regenerate
and for GitHub Pages to serve. Adds a few seconds per run.

## THE CRITICAL REQUIREMENT: date completeness (accumulate, never shrink)

A film's screening dates are only **complete before its run begins**. Both
cinemas' live pages list only **remaining/upcoming** dates — so once a run
starts, then finishes, the live page shows a *shrinking* then *empty* set. If
the scraper ever overwrote a film's dates with the current live set, it would
**erase already-passed screenings** the moment they happened.

This is exactly the problem the app already solved for series in
`SeriesRepository.unionedProgramme(for:)`: it **unions showtimes by start
instant** across the archive snapshot and the live feed, so neither source's
dates are lost. **`films.json` must follow the same monotonic union.**

### Required merge rule (scraper side, per film)

On each daily run, for every film observed:

1. Look up the film's existing record by **year-qualified id** (see below).
2. **Union** the freshly-scraped `screenings` into the stored `screenings`,
   deduping by `start` instant (a screening is identical iff same `start` —
   two genuinely distinct screenings always differ by start time, the same
   assumption the app's What's On dedup relies on).
3. **Never delete** a stored screening just because it's absent from today's
   live scrape. A date drops out of the live page when it passes; that's
   expected, and the stored copy is the record of it.
4. Keep the **richest** metadata seen (prefer a value over null/empty for
   runtime, director, synopsis, poster) so a later thin scrape doesn't blank a
   field an earlier rich one filled.
5. A film is **never removed** from `films.json` once observed (monotonic, same
   as `viff-series.json`).

> Net effect: a film scraped weeks before its run has its full date set
> captured up front; as the run proceeds and the live page shrinks, the stored
> set stays complete. After the run, the live page no longer lists it at all —
> and the stored record is the only place those dates survive (especially for
> VIFF, which has no back-catalog).

## ID scheme (must match the app)

Reuse the app's **year-qualified Cinematheque id** convention so records line up
with What's On / series data and don't collide across years:

- Cinematheque: `cinematheque-<year>-<slug>` derived from the film-page URL
  `…/films/<year>/<slug>` (the app's `CinemathequeSource.filmSlug` rule:
  `<year>-<slug>` when the path carries a 4-digit year, else bare slug).
- VIFF: `viff-<slug>` (matches existing VIFF film ids).

The year-qualification matters — it's what fixed the "two Love Letters collide
into one" bug. Distinct films across years (1995 Iwai vs 1953 Tanaka *Love
Letter*) must keep distinct ids.

## Output contract: `data/films.json`

Published automatically at
`https://woodthu.github.io/marquee-data/films.json` (the existing
`publish-pages` workflow copies any `data/*.json` to the Pages root — **no
workflow edit needed**).

```json
{
  "films": [
    {
      "id": "cinematheque-2026-love-letter",
      "cinemaId": "cinematheque",
      "title": "Love Letter",
      "director": "Shunji Iwai",
      "year": 1995,
      "country": "Japan",
      "runtimeMinutes": 117,
      "synopsis": "A short one-line/one-paragraph summary — NOT the full description (see below).",
      "posterURL": "https://...",
      "detailURL": "https://thecinematheque.ca/films/2026/love-letter",
      "callouts": ["New Restoration", "Vancouver Premiere"],
      "screenings": [
        { "start": "2026-07-12T18:30:00", "venue": "The Cinematheque" },
        { "start": "2026-07-14T19:00:00", "venue": "The Cinematheque" }
      ],
      "firstSeen": "2026-06-01",
      "lastSeen": "2026-06-12"
    }
  ]
}
```

Field notes:
- `screenings[].start`: **local wall-clock ISO8601, no timezone suffix** — same
  format `viff-series.json` already emits (e.g. `2025-07-19T15:20:00`). The app
  interprets these in `America/Vancouver`.
- `screenings` is the **monotonic union** described above — earliest first.
- Field names use the app's **camelCase** consumer convention (`cinemaId`,
  `runtimeMinutes`, `posterURL`, `detailURL`) — matches what the iOS
  `Codable` models expect, so the fetcher needs no key remapping. (Internally
  the scraper may store snake_case and translate on emit, as the VIFF pipeline
  does.)
- `callouts` (`[String]`): editorial flags scraped **verbatim** from the film
  page — e.g. `"New Restoration"`, `"Vancouver Premiere"`, `"Filmmakers in
  Attendance"`. These map to the app's `FilmDetailEnricher.formatTags`, which
  drive the laurel badges shown on search rows, listing rows, and the detail
  title block — so they're important to carry, not optional chrome. Source:
  Cinematheque `<span class="callout">…</span>` on the film page; VIFF's
  equivalent premiere/restoration tags. Scrape the text exactly as published
  (no normalization) — the app picks/styles them via `TopLaurelPicker`.
  Plain film **format** (e.g. "DCP", "35mm") is a *separate* Cinematheque
  `<span class="filmFormat">`; if captured, keep it in its own `format` field,
  NOT mixed into `callouts` (the app keeps them distinct — format → Details
  section, callouts → title badges).
- `firstSeen` / `lastSeen` (`YYYY-MM-DD`): provenance only, for debugging the
  monotonic accumulation. Not required by the app.

### Callouts also follow richest-wins, not last-write

Like the metadata fields (rule 4), a film's `callouts` should be the **best set
seen**, not blindly overwritten by the latest scrape. A premiere/restoration tag
present while the film was active shouldn't vanish if a later thin scrape (or a
post-run page) drops it. Union the callout strings across observations
(dedup case-insensitively), same monotonic spirit as the screening dates.

### Synopsis: short only — do NOT store the full description

Store at most a **short synopsis** (one line / one paragraph), used as a
search-result subtitle and a cold-launch preview. **Do not scrape the full
multi-paragraph description into `films.json`.** Reasons:

- **The app already fetches the full description live.** `FilmDetailEnricher`
  scrapes each film's page when the user *opens* it and builds a rich
  `synopsisBlocks` structure (paragraphs + reviewer blockquotes) that the detail
  page renders. A full synopsis in the archive would duplicate that for every
  opened film.
- **Synopsis is the heaviest field by far** — multi-paragraph prose across
  thousands of films is what would bloat the archive from a few MB into
  something large. Title/director/year/poster are tiny by comparison.
- **Search doesn't use it** — matching is on title (+ director/series), not
  body text, so the full description buys search nothing.
- **Graceful detail page** — an archive film opens with its short synopsis
  instantly while the enricher backfills the rich blocks within a moment, the
  same cascade every film already uses. No blank state.

So the division of labour is: `films.json` carries the **lean searchable
record** (id, title, director, year, poster, short synopsis, complete
screening dates); the **live enricher** owns the full description, cast, media
gallery, and other rich detail-page content — fetched on demand, never frozen
in the archive.

## iOS consumer (this is the app-repo follow-up, listed for completeness)

Add a `FilmsArchiveFetcher` mirroring `VIFFArchiveFetcher` exactly:
- Conditional GET (ETag) against `…/films.json`, on-disk cache under `Caches/`,
  silent best-effort failure.
- Payload sanity-check (`data.count > 256` && contains `"films"`).
- Load into a read-only archive slot; **never persisted into the live
  `screeningsByCinema`** (keep it a separate corpus the way the VIFF archive
  bundle is), so the live-fetch cycle can't act on it.

### LIVE IS AUTHORITATIVE FOR CURRENT/UPCOMING — archive is PAST only

This is the boundary to get exactly right. The archive's screening *times* must
NEVER override or supplement what the Live API / `What's On` shows for a
currently-playing film:

- The **scraper side already enforces this in practice**: it freezes (stops
  re-fetching) a film once its run is over, so the archive's live edge is only
  ever past/just-passed dates; current dates keep flowing from the cinema's
  live calendar into `screeningsByCinema`, not here.
- The **app side must enforce it structurally**: when a film id exists in BOTH
  the live schedule and the archive, the live record WINS for everything
  user-facing — showtimes, "Going", What's On rows. The archive contributes
  that film to **search only**, and even there it's deduped by id so the live
  entry is the one shown. The archive's `screenings` are used solely to render
  a *past* film's history (muted/"PAST"), exactly like the VIFF series archive
  already does — never to assert a current showtime.
- Concretely: do NOT union archive `screenings` into a live film's showtimes.
  The VIFF series union (`unionedProgramme`) merges live∪archive because there
  the archive is the ONLY source of a mid-run film's already-passed dates; for
  this all-films archive the live schedule is always present for current films,
  so archive dates for a live film are redundant at best and stale at worst —
  drop them in favour of live.

Then extend `SearchView.rebuildCorpus()` to add a third source after series:
films from the archive, deduped by id against what What's On + series already
contributed (so a currently-playing film isn't doubled). **De-rank / section**
archive-only past films below current results so search still surfaces what the
user can actually go see — a "Past films" section under the live "Films"
section.

## Risks to accept knowingly

1. **Frozen TMDB matches.** A film's poster/match is captured at scrape time; if
   a match was wrong it persists. Mitigation: the app already re-enriches films
   it renders, so a wrong archive poster self-corrects once the film is opened —
   but the *search-row* poster may be stale until then. Acceptable.
2. **VIFF becomes "archive of record."** Since VIFF has no back-catalog, our
   stored VIFF films are the only record. Accuracy obligation noted; the
   monotonic-never-delete rule is what protects it.
3. **Corpus depends on install age** for VIFF (we only have what we've seen
   since the scraper started). Cinematheque is reproducible from its index, so
   this only affects VIFF tail history. Acceptable; improves over time.
