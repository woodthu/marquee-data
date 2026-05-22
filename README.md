# marquee-data

Persists historical VIFF series + the films programmed inside them, so the Marquee iOS app can browse past programming after VIFF's site stops listing it.

## What's in `data/viff-series.json`

```json
{
  "series": [
    {
      "slug": "pantheon",
      "name": "Pantheon",
      "detail_url": "https://viff.org/series/pantheon/",
      "film_slugs": ["yi-yi", "marie-antoinette", "..."],
      "first_seen_wayback": "20221220022406",
      "last_seen_wayback": "20251012021100",
      "last_seen_live": "20260520"
    }
  ]
}
```

`film_slugs` is monotonic — slugs are never removed once observed. Same for the series list itself: a series never gets deleted, only stops getting `last_seen_live` bumped if VIFF retires it.

## Output URL

After GitHub Pages is enabled:
`https://<username>.github.io/marquee-data/viff-series.json`

## Setup (one-time)

1. Create a public repo on GitHub named `marquee-data`.
2. From this folder: `git init && git add . && git commit -m "init" && git branch -M main && git remote add origin <url> && git push -u origin main`.
3. Repo Settings → Pages → Source: **GitHub Actions**.
4. Run the backfill once locally: `python3 scripts/backfill_from_wayback.py` (~30 min, hits archive.org).
5. Commit & push the populated JSON.
6. Trigger the workflow once: Actions tab → "scrape-viff" → "Run workflow".

## How it works

- **`scripts/backfill_from_wayback.py`** — one-time. Walks every Wayback Machine snapshot of `viff.org/series/*` (we found 127 distinct series spanning 2014–2026), fetches each, and unions all observed film slugs.
- **`scripts/scrape_viff_series.py`** — daily. Discovers currently-active series from VIFF's homepage + What's On, re-scrapes every series we already know about, and unions newly-observed films into the snapshot. Marks each scraped series with `last_seen_live`.
- **`.github/workflows/scrape.yml`** — runs the daily scraper at 12:00 UTC, commits the JSON if changed, deploys to GitHub Pages.

Stdlib-only Python — no pip dependencies.
