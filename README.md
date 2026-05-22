# marquee-data

Public snapshot of historical VIFF series + the films programmed inside them, so the Marquee iOS app can browse past programming after VIFF's site stops listing it.

## What's in `data/viff-series.json`

```json
{
  "series": [
    {
      "slug": "pantheon",
      "name": "Pantheon",
      "detail_url": "https://viff.org/series/pantheon/",
      "film_slugs": [
        {
          "slug": "yi-yi",
          "title": "Yi Yi",
          "director": "Edward Yang",
          "year": 2000,
          "country": "Taiwan/Japan",
          "runtime_min": 173,
          "synopsis": "...",
          "poster": "https://...",
          "screenings": [{ "start": "2024-09-30T18:30:00", "venue": "VIFF Centre" }]
        }
      ],
      "current_date_range": "Sep 28 – Oct 8 2024",
      "last_seen_live": "20240930"
    }
  ]
}
```

`film_slugs` is monotonic — entries are never removed once observed. Same for the series list itself: a series never gets deleted.

## Output URL

GitHub Pages serves the JSON at:
<https://woodthu.github.io/marquee-data/viff-series.json>

The iOS app fetches from there with ETag caching; bundled JSON is the cold-launch fallback.

## How updates happen

This repo is data-only. The scrape pipeline lives in a private companion repo
(`woodthu/marquee-data-private`) so the scraper details, Wayback fallback logic, and any
internal heuristics aren't public. The private workflow runs daily at 19:00 UTC
(11 AM Vancouver PST), updates `data/viff-series.json`, and pushes the diff back here.

This repo's only workflow re-publishes the JSON via Pages whenever `data/` changes on `main`.
