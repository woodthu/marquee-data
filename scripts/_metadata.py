"""
Shared metadata-extraction helpers for the VIFF scrapers. Pulls
og:image, og:description, og:title, director, year from a film or
series page. Used by both `backfill_from_wayback.py` (one-time) and
`scrape_viff_series.py` (daily) so the schema stays consistent.

Stdlib only.
"""
from __future__ import annotations

import html as _html
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def html_unescape(s: str | None) -> str | None:
    """Decode HTML entities in scraped text.

    VIFF (and many CMSes) double-encode meta content: '&#039;' inside
    the og:description tag is what comes through as 'Wong Kar-wai's'.
    Without decoding, the iOS app shows '&amp;' / '&#039;' literally.
    Idempotent — already-decoded strings round-trip unchanged. Two
    passes catch double-encoding ("&amp;amp;" → "&amp;" → "&").
    """
    if s is None:
        return None
    return _html.unescape(_html.unescape(s))


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) marquee-data/1.0"
)


import signal


class _FetchDeadline(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _FetchDeadline()


def _fetch_inner(url: str, timeout: float) -> str | None:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            return None
        raw = resp.read()
        if raw[:2] == b"\x1f\x8b":
            import gzip
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", errors="replace")


def fetch(url: str, attempts: int = 2, backoff: float = 2.0, timeout: float = 8.0, hard_deadline: float = 15.0) -> str | None:
    """GET with retry. Two paths depending on calling thread:

    - **Main thread**: enforces a wall-clock deadline per attempt via
      `signal.alarm`. This guards against the multi-hour hangs we hit
      earlier when Wayback trickled bytes slower than the per-read
      timeout. Critical: clear the alarm BEFORE inter-retry sleep so
      it can't fire mid-sleep and escape the catch.
    - **Worker thread**: signal.alarm is main-thread-only, so we fall
      back to socket-level timeouts (`urlopen(timeout=...)`). Less
      protective against trickle-attacks but the only safe option
      from threads. Workers should keep their work bounded so a
      stuck socket can't sink the whole pool.
    """
    import threading
    if threading.current_thread() is not threading.main_thread():
        # Worker-thread path: socket timeouts only. We rely on the
        # caller to bound concurrency so a single stuck connection
        # can't block the whole pipeline.
        for i in range(attempts):
            try:
                return _fetch_inner(url, timeout)
            except (HTTPError, URLError, TimeoutError, ConnectionResetError, OSError):
                if i == attempts - 1:
                    return None
                time.sleep(backoff * (i + 1))
        return None

    prev_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    try:
        for i in range(attempts):
            try:
                signal.alarm(int(hard_deadline))
                try:
                    return _fetch_inner(url, timeout)
                finally:
                    signal.alarm(0)
            except (HTTPError, URLError, TimeoutError, ConnectionResetError, OSError, _FetchDeadline):
                if i == attempts - 1:
                    return None
                time.sleep(backoff * (i + 1))
        return None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev_handler)


# Two-pattern lookup so the regex's terminator matches the opening
# quote — content="…" can legitimately contain ' apostrophes (and
# vice versa). The single combined pattern stopped at the first
# apostrophe inside the value, truncating descriptions like "Wong
# Kar-wai's most popular film…" to just "Wong Kar-wai".
_META_PROP_DBL = lambda prop: re.compile(
    r'<meta[^>]+property=["\']' + re.escape(prop) + r'["\'][^>]+content="([^"]+)"',
    re.IGNORECASE,
)
_META_PROP_SGL = lambda prop: re.compile(
    r"<meta[^>]+property=[\"']" + re.escape(prop) + r"[\"'][^>]+content='([^']+)'",
    re.IGNORECASE,
)
_META_NAME_DBL = lambda name: re.compile(
    r'<meta[^>]+name=["\']' + re.escape(name) + r'["\'][^>]+content="([^"]+)"',
    re.IGNORECASE,
)
_META_NAME_SGL = lambda name: re.compile(
    r"<meta[^>]+name=[\"']" + re.escape(name) + r"[\"'][^>]+content='([^']+)'",
    re.IGNORECASE,
)


def meta(html: str, prop: str) -> str | None:
    """og:* / twitter:* meta — returns decoded content of first match
    or None. HTML entities ('&#039;', '&amp;') are decoded so the
    JSON carries plain text, not entity-encoded markup."""
    m = _META_PROP_DBL(prop).search(html) or _META_PROP_SGL(prop).search(html)
    return html_unescape(m.group(1).strip()) if m else None


def meta_named(html: str, name: str) -> str | None:
    m = _META_NAME_DBL(name).search(html) or _META_NAME_SGL(name).search(html)
    return html_unescape(m.group(1).strip()) if m else None


def cdx_latest_snapshot(url: str) -> str | None:
    """Returns the most recent Wayback snapshot timestamp for `url`,
    or None if Wayback has never captured it."""
    snapshots = cdx_all_snapshots(url, limit=1)
    return snapshots[0] if snapshots else None


def cdx_all_snapshots(url: str, limit: int = 20) -> list:
    """Returns up to `limit` Wayback snapshot timestamps for `url`,
    newest first. Used as a fallback when the latest snapshot 404s
    or returns junk — the worker can walk older snapshots until one
    yields a usable HTML body.

    `limit=-N` is CDX's "newest-first, N results" syntax. We pull
    the full row and extract the timestamp column ourselves so the
    caller can also see status codes if needed. Filters out 4xx/5xx
    rows so we don't waste fetches on known-broken snapshots.
    """
    import json
    cdx = (
        "http://web.archive.org/cdx/search/cdx"
        f"?url={url}"
        "&output=json"
        "&filter=statuscode:200"
        "&filter=mimetype:text/html"
        "&fl=timestamp"
        f"&limit=-{max(limit, 1)}"
    )
    raw = fetch(cdx)
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if len(rows) < 2:
        return []
    out: list = []
    for row in rows[1:]:
        if isinstance(row, list) and row:
            out.append(row[0])
        elif isinstance(row, dict) and row.get("timestamp"):
            out.append(row["timestamp"])
    return out




# `c-col-subtitle` sits directly under the masthead title on VIFF
# series pages and carries the actual run dates ("Dec 26 – Jan 8").
# The earlier `menu-description` span is the SITE-WIDE nav window
# (always the upcoming festival) and shouldn't be used for run dates.
_SUBTITLE_DATE_RE = re.compile(
    r'class="c-col-subtitle"[^>]*>([^<]+)</h3>',
    re.IGNORECASE,
)
# A date-shaped subtitle: month abbrev + day number, possibly a
# range. Filters out non-date subtitles like "Box Office Helpline"
# and "Visit Us" that share the c-col-subtitle class.
_LOOKS_LIKE_DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)[a-z]*\.?\s+\d",
    re.IGNORECASE,
)


def extract_date_range(html: str) -> str | None:
    """Pulls the per-series run-date range from the
    `c-col-subtitle` heading directly under the masthead title.
    Falls back to nil when no date-shaped subtitle is found.

    Earlier this used `<span class='menu-description'>`, but that
    span carries the SITE NAV's upcoming festival window — same
    value across every page on viff.org regardless of which series
    you're viewing. Useless for archive run dates. The
    `c-col-subtitle` heading is per-series."""
    for m in _SUBTITLE_DATE_RE.finditer(html):
        candidate = html_unescape(m.group(1).strip())
        if not candidate:
            continue
        if not _LOOKS_LIKE_DATE_RE.search(candidate):
            continue
        return re.sub(r"\s+", " ", candidate)
    return None


# The long editorial description on VIFF series pages lives in
# `<div class="c-col-content c-wysiwyg">` with one or more
# paragraphs. The first `<p class="lead-p">` is the canonical
# blurb VIFF features at the top of the page; it's longer and
# more specific than og:description.
_LEAD_P_RE = re.compile(
    r'<p\s+class="lead-p"[^>]*>([\s\S]*?)</p>',
    re.IGNORECASE,
)


def extract_long_description(html: str) -> str | None:
    """The editorial blurb VIFF features above the programme list.
    Substantially longer + richer than og:description. Falls back
    to nil when the page doesn't carry a lead paragraph."""
    m = _LEAD_P_RE.search(html)
    if not m:
        return None
    inner = m.group(1)
    # Strip nested tags (links, line breaks) and collapse whitespace.
    text = re.sub(r"<[^>]+>", " ", inner)
    text = html_unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def extract_series_meta(html: str) -> dict:
    """Series page: og:title, og:image, the long lead-paragraph
    description (preferred over the truncated og:description), the
    per-series subtitle date range, and the per-film showtimes
    parsed from each c-event-card. Showtimes are returned as a
    map keyed by film slug; the enrichment pipeline folds each
    entry into its corresponding film record so the iOS app can
    render real screening rows on archive films."""
    long_desc = extract_long_description(html)
    return {
        "title": meta(html, "og:title"),
        "hero_image": meta(html, "og:image"),
        "description": long_desc or meta(html, "og:description") or meta_named(html, "description"),
        "date_range": extract_date_range(html),
        "film_showtimes": extract_series_film_showtimes(html),
    }


# A series page lists each film as a `c-event-card` with one or more
# `c-event-instance` rows for showtimes. The earlier regex tried to
# slice the page into card-bounded chunks via lookahead, but
# `c-event-card` substrings appear in the document inconsistently
# (variant classes like `c-event-card__content` confuse the boundary
# detection). The current approach: locate every "card title" link
# (which is unique-per-card and identifies the film slug), then
# associate the next batch of `c-event-instance` blocks with that
# card. The next card's title link is the boundary.
_CARD_TITLE_LINK_RE = re.compile(
    r'<h3\s+class="c-event-card__title"[^>]*>\s*<a[^>]+href="https://viff\.org/whats-on/([^"/?#]+)',
    re.IGNORECASE,
)
# Instance attribute extractors — operate on the slice of HTML
# between two consecutive card-title links.
_INSTANCE_DELIMITER_RE = re.compile(
    r'<div\s+class="c-event-instance"',
    re.IGNORECASE,
)
_INSTANCE_TIME_RE = re.compile(
    r'class="c-event-instance__time"[^>]*>\s*([^<]+?)\s*</div>',
    re.IGNORECASE,
)
_INSTANCE_DATE_RE = re.compile(
    r'class="c-event-instance__date"[^>]*>[\s\S]*?<span[^>]*>\s*([^<]+?)\s*</span>',
    re.IGNORECASE,
)
_INSTANCE_VENUE_RE = re.compile(
    r'class="c-event-instance__venue"[^>]*>\s*([^<]+?)\s*</span>',
    re.IGNORECASE,
)


# Maps VIFF's abbreviated month names (as seen in c-event-instance__date
# spans) to month numbers. Lowercased for case-insensitive lookup.
_MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _resolve_showtime_iso(date_str: str | None, time_str: str | None, anchor_year: int) -> str | None:
    """Stitch VIFF's "Sat Jan 03" + "7:50 pm" + an anchor year into
    an ISO timestamp the iOS app can decode with one DateFormatter.

    VIFF strips the year from per-event labels (the year is implicit
    on the live page), so we infer it from the snapshot timestamp.
    `anchor_year` is the year the series page was captured in. If the
    parsed month is far behind (>6 mo) the anchor month, we assume
    the showtime rolls into the next calendar year — handles the
    Dec-snapshot-listing-Jan-screenings case. We don't have the
    anchor month here, so fall back to the simpler rule: month <
    current-anchor-month-6 → bump year. Caller can pass an anchor
    *date* via separate helper if more precision is needed.
    """
    if not date_str or not time_str:
        return None
    # Date format: "Sat Jan 03" — three tokens after split.
    parts = date_str.strip().split()
    if len(parts) < 3:
        return None
    # Series pages use 3-letter month abbrev ("Jan"); film pages use
    # full names ("January"). Try the full token first, then the
    # 3-letter prefix as a fallback.
    raw_month = parts[1].lower().rstrip(".")
    month = _MONTH_ABBR.get(raw_month) or _MONTH_ABBR.get(raw_month[:3])
    if month is None:
        return None
    try:
        day = int(parts[2])
    except ValueError:
        return None
    # Time format: "7:50 pm" or "10:30 am".
    t = time_str.strip().lower()
    m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)", t)
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3) == "pm":
        hour += 12
    minute = int(m.group(2))
    return f"{anchor_year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00"


def resolve_showtime_iso(date_str: str | None, time_str: str | None, anchor_ts: str | None) -> str | None:
    """Public entrypoint. `anchor_ts` is the snapshot's Wayback
    timestamp (YYYYMMDDhhmmss) for the page the showtime came from,
    or None when fetched live. We pull the year+month from the
    anchor, then pick the year whose calendar date is closest to —
    and not more than ~3 months before — the anchor.

    Why: a late-Dec 2025 snapshot listing "Sat Jan 03" is showing
    Jan 2026, not Jan 2025. We choose the candidate year that puts
    the screening within [anchor - 90d, anchor + 365d]."""
    if anchor_ts and len(anchor_ts) >= 6:
        try:
            anchor_year = int(anchor_ts[:4])
            anchor_month = int(anchor_ts[4:6])
        except ValueError:
            anchor_year = time.gmtime().tm_year
            anchor_month = time.gmtime().tm_mon
    else:
        anchor_year = time.gmtime().tm_year
        anchor_month = time.gmtime().tm_mon
    iso = _resolve_showtime_iso(date_str, time_str, anchor_year)
    if iso is None:
        return None
    # Roll the year forward when the showtime's month is well behind
    # the anchor's month (e.g. anchor=Dec, screening=Jan → next year).
    showtime_month = int(iso[5:7])
    if anchor_month - showtime_month > 6:
        return _resolve_showtime_iso(date_str, time_str, anchor_year + 1)
    if showtime_month - anchor_month > 6:
        return _resolve_showtime_iso(date_str, time_str, anchor_year - 1)
    return iso


def extract_series_film_showtimes(html: str) -> dict:
    """Walks every film card on a series page and returns
    `{film_slug: [{time, date, venue}, ...]}`.

    Slicing strategy: match every card-title link and use the
    spans between consecutive matches as each card's body. Cards
    can have 0..N instance blocks (Wayback snapshots with lazy-
    loaded showtime lists capture nothing in some cases)."""
    title_matches = list(_CARD_TITLE_LINK_RE.finditer(html))
    if not title_matches:
        return {}
    out: dict = {}
    for i, m in enumerate(title_matches):
        slug = m.group(1)
        body_start = m.end()
        body_end = title_matches[i + 1].start() if i + 1 < len(title_matches) else len(html)
        body = html[body_start:body_end]
        # Each instance block ends at the next instance start, OR at
        # the end of the card body.
        inst_starts = [d.start() for d in _INSTANCE_DELIMITER_RE.finditer(body)]
        instances = []
        for j, start in enumerate(inst_starts):
            end = inst_starts[j + 1] if j + 1 < len(inst_starts) else len(body)
            block = body[start:end]
            time_m = _INSTANCE_TIME_RE.search(block)
            date_m = _INSTANCE_DATE_RE.search(block)
            venue_m = _INSTANCE_VENUE_RE.search(block)
            if not (time_m or date_m):
                continue
            instances.append({
                "time": html_unescape(time_m.group(1).strip()) if time_m else None,
                "date": html_unescape(date_m.group(1).strip()) if date_m else None,
                "venue": html_unescape(venue_m.group(1).strip()) if venue_m else None,
            })
        if slug not in out:
            out[slug] = instances
    return out


# Long film synopsis lives in `<div class="c-event__description c-col-content c-wysiwyg">`,
# made of one or more `<p>` paragraphs. The og:description meta tag is a separate
# short marketing blurb VIFF surfaces in social previews — usually 1 sentence vs
# the body's 2-3 paragraphs. The body version matches what the user reads on the
# page, so we prefer it.
_FILM_BODY_DESC_RE = re.compile(
    r'<div\s+class="c-event__description\s+c-col-content\s+c-wysiwyg"[^>]*>([\s\S]*?)</div>',
    re.IGNORECASE,
)


def extract_film_body_description(html: str) -> str | None:
    """Extracts the long body synopsis from a VIFF film page.
    Concatenates leading `<p>` paragraphs (skipping `<blockquote>`
    pull-quotes) and returns plain text. Falls back to None when
    the wysiwyg block isn't present (older snapshots, or pages
    without editorial copy)."""
    m = _FILM_BODY_DESC_RE.search(html)
    if not m:
        return None
    inner = m.group(1)
    # Collect <p>...</p> blocks. Stop at the first <blockquote> —
    # the long-form synopsis comes before press quotes / awards.
    blockquote_pos = inner.find("<blockquote")
    if blockquote_pos >= 0:
        inner = inner[:blockquote_pos]
    # Pull each paragraph's inner text.
    paras = re.findall(r"<p[^>]*>([\s\S]*?)</p>", inner, re.IGNORECASE)
    if not paras:
        return None
    cleaned: list[str] = []
    for p in paras:
        text = re.sub(r"<[^>]+>", "", p)
        text = html_unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            cleaned.append(text)
    if not cleaned:
        return None
    return " ".join(cleaned)


# VIFF film pages embed director / year in a JSON-LD <script type="application/ld+json">
# block, plus a sidebar listing fields like "Director" + "Year". Try the JSON-LD first
# (more structured), fall back to the sidebar regex.
_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
    re.IGNORECASE,
)
# VIFF film pages render the details panel as alternating
# `c-event__details-title` (label) + `c-event__details-details`
# (value) divs. The values are wrapped in <p> inside the value
# div. This pattern matches a label, then captures the entire
# <p> body — we then HTML-strip the captured group to drop any
# inner anchors / spans.
#
# Label variants: VIFF inconsistently pluralizes labels for films
# with multiple credits ("Director" vs "Directors", "Country" vs
# "Country of Origin"). The regex ALWAYS matches a fixed label as a
# token followed by an optional trailing suffix (e.g. "of Origin",
# "s") so a single regex covers both forms.
# `<p[^>]*>` (was `<p>`) matches both bare paragraphs and ones
# carrying a class attribute. VIFF wraps detail values inconsistently
# — some pages use `<p>Roman Polanski</p>` (Wayback 2024+ snapshots),
# others use `<p class="p1">Roman Polanski</p>` (older snapshots and
# the live site as of 2022). The earlier `<p>`-only regex silently
# missed the latter, leaving 350+ films without director / country
# / runtime even though the data was right there in the HTML.
# Inner-anchor markup: 2022 festival pages wrap the director name
# in an anchor that links to the bio block on the same page —
# `<p><a href="#director-bio">Marie Kreutzer</a></p>`. The earlier
# `[^<]+?` body capture aborted at the `<a>` and yielded nothing.
# We now capture the full `<p>` inner span and html-strip post-hoc
# (see `extract_film_meta`), which handles bare text, anchored
# names, and `<span>` chips uniformly.
_DETAIL_RE = lambda label: re.compile(
    r'class="c-event__details-title"[^>]*>\s*' + re.escape(label) + r'(?:s|\s+of\s+Origin)?\s*</div>'
    r'\s*<div class="c-event__details-details"[^>]*>\s*<p[^>]*>(.+?)</p>',
    re.IGNORECASE | re.DOTALL,
)


def _clean_detail_value(raw: str) -> str:
    """Strips inline tags from a detail-panel `<p>` body and
    collapses whitespace. The raw capture from `_DETAIL_RE` may
    contain `<a>` / `<span>` markup (festival pages link the
    director to a bio anchor); the user-facing value is just
    the text content."""
    if not raw:
        return ""
    # Drop any tag (anchors, spans, breaks). Leaving entities for
    # html_unescape to resolve at the call site.
    text = re.sub(r"<[^>]+>", "", raw)
    return re.sub(r"\s+", " ", text).strip()
_DIRECTOR_LABEL_RE = _DETAIL_RE("Director")
_YEAR_LABEL_RE = _DETAIL_RE("Year")
_COUNTRY_LABEL_RE = _DETAIL_RE("Country")
_LANGUAGE_LABEL_RE = _DETAIL_RE("Language")
_RUNTIME_LABEL_RE = _DETAIL_RE("Runtime")
_RUNTIME_NUM_RE = re.compile(r'(\d{1,3})\s*min', re.IGNORECASE)
# VIFF dropped the `c-event__details-title>Runtime` panel format
# circa 2024 in favour of two `<span>` chips that render the
# clock-icon + duration directly. The new markup is the only one
# present on most film pages we scrape now (live AND most Wayback
# snapshots ≥2024). Falling through to the old regex too keeps
# pre-2024 snapshots covered.
_DURATION_SPAN_RE = re.compile(
    r'class=["\']event__duration["\']>\s*(\d{1,3})\s*min',
    re.IGNORECASE,
)


# A VIFF film page renders showtimes inside `c-event-instance__date-
# group` blocks: each group has a `<h4 class="c-col-subtitle">` with
# the date (e.g. "Sunday May 17") and one or more `c-event-instance`
# divs containing the time + venue. This is structurally different
# from the series page (which packs date directly into the instance
# block), so we need a film-page-specific parser.
_DATE_GROUP_RE = re.compile(
    r'class="c-event-instance__date-group"[^>]*>([\s\S]*?)(?=<div\s+class="c-event-instance__date-group"|<footer|</main)',
    re.IGNORECASE,
)
_DATE_HEADING_RE = re.compile(
    r'<h4\s+class="c-col-subtitle"[^>]*>\s*([^<]+?)\s*</h4>',
    re.IGNORECASE,
)


def extract_film_page_showtimes(html: str) -> list:
    """Walks date-group blocks on a VIFF film page and returns a flat
    list of `{time, date, venue}` instances. Same shape as the series
    page extractor, so the enrichment pipeline can fold these into
    the same `screenings` field via the existing resolver.

    Date format is bare "Sunday May 17" (no year). The caller passes
    the snapshot's Wayback timestamp as the year anchor."""
    out = []
    for group_match in _DATE_GROUP_RE.finditer(html):
        body = group_match.group(1)
        date_match = _DATE_HEADING_RE.search(body)
        if not date_match:
            continue
        # Normalize date format to match the series page extractor:
        # "Sunday May 17" → "Sun May 17" (3-letter weekday). The
        # resolver only cares about the month + day tokens, but
        # keeping the format consistent simplifies debugging.
        raw_date = re.sub(r"\s+", " ", html_unescape(date_match.group(1)) or "").strip()
        for inst_start in _INSTANCE_DELIMITER_RE.finditer(body):
            block_start = inst_start.start()
            # End at the next instance start, or end of group body.
            next_match = _INSTANCE_DELIMITER_RE.search(body, block_start + 1)
            block_end = next_match.start() if next_match else len(body)
            block = body[block_start:block_end]
            time_m = _INSTANCE_TIME_RE.search(block)
            venue_m = _INSTANCE_VENUE_RE.search(block)
            if not time_m:
                continue
            out.append({
                "time": html_unescape(time_m.group(1).strip()),
                "date": raw_date,
                "venue": html_unescape(venue_m.group(1).strip()) if venue_m else None,
            })
    return out


def extract_film_meta(html: str) -> dict:
    """Film page: og:title, og:image, full body synopsis (preferred
    over the truncated og:description), plus director / year /
    runtime where available. JSON-LD takes precedence over the
    sidebar."""
    body_desc = extract_film_body_description(html)
    out = {
        "title": meta(html, "og:title"),
        "poster": meta(html, "og:image"),
        # Prefer the body-paragraph synopsis (matches the page user
        # sees) over the short og:description marketing blurb.
        "synopsis": body_desc or meta(html, "og:description") or meta_named(html, "description"),
        "director": None,
        "year": None,
        "runtime_min": None,
        "country": None,
        "language": None,
        # Per-series subtitle date range (carried for forward compat;
        # may be unreliable on Wayback snapshots).
        "date_range": extract_date_range(html),
    }

    # JSON-LD pass — VIFF embeds Schema.org Movie objects with
    # director.name + datePublished.
    import json
    for blob_match in _JSONLD_RE.finditer(html):
        body = blob_match.group(1).strip()
        # Strip CDATA wrappers (Letterboxd-style).
        body = (
            body.replace("/* <![CDATA[ */", "")
                .replace("/* ]]> */", "")
                .replace("<![CDATA[", "")
                .replace("]]>", "")
                .strip()
        )
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("@graph"), list):
                candidates.extend(entry["@graph"])
            t = entry.get("@type")
            if t in ("Movie", "Film", "TVSeries"):
                d = entry.get("director")
                if isinstance(d, dict):
                    out["director"] = out["director"] or html_unescape(d.get("name"))
                elif isinstance(d, list) and d and isinstance(d[0], dict):
                    out["director"] = out["director"] or html_unescape(d[0].get("name"))
                elif isinstance(d, str):
                    out["director"] = out["director"] or html_unescape(d)
                pub = entry.get("datePublished")
                if isinstance(pub, str) and len(pub) >= 4 and pub[:4].isdigit():
                    out["year"] = out["year"] or int(pub[:4])
                duration = entry.get("duration")
                if isinstance(duration, str):
                    # ISO-8601 duration like "PT1H30M". Pull H + M and
                    # collapse to minutes.
                    h = re.search(r"(\d+)H", duration)
                    m = re.search(r"(\d+)M", duration)
                    minutes = (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)
                    if minutes:
                        out["runtime_min"] = out["runtime_min"] or minutes

    # Sidebar fallback. VIFF's film pages use a `c-event__details`
    # panel with paired `…__details-title` / `…__details-details`
    # divs — that's where Director / Year / Country / Runtime live.
    # The capture may contain inline tags (festival pages anchor the
    # director to a bio link); `_clean_detail_value` strips them.
    if not out["director"]:
        m = _DIRECTOR_LABEL_RE.search(html)
        if m:
            value = html_unescape(_clean_detail_value(m.group(1)))
            if value:
                out["director"] = value
    if not out["year"]:
        m = _YEAR_LABEL_RE.search(html)
        if m:
            cleaned = _clean_detail_value(m.group(1))
            try:
                out["year"] = int(cleaned)
            except ValueError:
                # Pull a 4-digit year out of multi-token values
                # like "France, 2022" or "Year: 2022".
                ym = re.search(r"(19|20)\d{2}", cleaned)
                if ym:
                    out["year"] = int(ym.group(0))
    if not out["country"]:
        m = _COUNTRY_LABEL_RE.search(html)
        if m:
            value = html_unescape(_clean_detail_value(m.group(1)))
            if value:
                out["country"] = value
    if not out["language"]:
        m = _LANGUAGE_LABEL_RE.search(html)
        if m:
            value = html_unescape(_clean_detail_value(m.group(1)))
            if value:
                out["language"] = value
    if not out["runtime_min"]:
        # 1) Modern markup (~2024+): `<span class="event__duration">N min</span>`.
        d = _DURATION_SPAN_RE.search(html)
        if d:
            out["runtime_min"] = int(d.group(1))
        else:
            # 2) Legacy detail panel (pre-2024 snapshots).
            m = _RUNTIME_LABEL_RE.search(html)
            if m:
                n = _RUNTIME_NUM_RE.search(m.group(1))
                if n:
                    out["runtime_min"] = int(n.group(1))
            else:
                # 3) Last resort — synopsis often mentions "(99 min)".
                n = _RUNTIME_NUM_RE.search(out.get("synopsis") or "")
                if n:
                    out["runtime_min"] = int(n.group(1))

    # Title cleanup. VIFF's og:title decorates film titles in two
    # ways we don't want:
    #   "Film Title | Vancouver International Film Festival"
    #     → strip the trailing " | …" site-suffix.
    #   "Series Name: Film Title" / "Series Name, …: Film Title"
    #     → these are programme-line prefixes VIFF adds for retros
    #       (Pantheon, Film Studies, etc). Drop the prefix by taking
    #       everything after the last ": ".
    # Films with legitimate ":" in their name (e.g. "Punch-Drunk Love:
    # An Extra-Special Edition") will lose the colon part — it's
    # better to be slightly truncated than to ship the series name
    # as the film title.
    if out["title"]:
        title = out["title"].split(" | ")[0].strip()
        if ": " in title:
            title = title.rsplit(": ", 1)[-1].strip()
        out["title"] = title
    return out
