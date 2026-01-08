import re
import socket
import time
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, Response

# ---------------- CONFIG ----------------

# TributeMovies page that already lists each movie + every date it plays
TRIBUTE_URL = (
    "https://www.tributemovies.com/cinema/Montana/Kalispell/"
    "Cinemark-Signature-Stadium-Kalispell-14/10338/"
)

DEFAULT_DAYS_AHEAD = 45  # UI can still request up to 120

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}

# Lines like: "Sat, Jan 17: 1:00pm"
DATE_LINE_PATTERN = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+([A-Za-z]{3})\s+(\d{1,2})(?:,\s*(\d{4}))?\s*:",
    re.IGNORECASE,
)

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Titles we never want to treat as movies (very defensive)
BANNED_TITLES_LOWER = {
    "",
    "read reviews",
    "rate movie",
    "watch trailer",
    "regular showtimes",
    "3d showtimes",
    "filters",
    "all showtimes",
    "theaters nearby",
    "about us",
}

# ---------------- HELPERS ----------------

def normalize_title(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t


def parse_showdate_line_to_date(line: str, today: date) -> date | None:
    """
    Convert "Sat, Jan 17:" to a real date.
    Tribute showtime lines typically do NOT include the year.
    We infer year with a simple rollover rule:
      - Try current year.
      - If that date is > ~300 days in the past relative to today, bump year + 1.
      - If that date is > ~300 days in the future, bump year - 1 (rare, but safe).
    """
    m = DATE_LINE_PATTERN.match(line.strip())
    if not m:
        return None

    mon_abbr = (m.group(2) or "").strip().lower()
    day_num = int(m.group(3))
    explicit_year = m.group(4)

    month = MONTHS.get(mon_abbr)
    if not month:
        return None

    if explicit_year:
        year = int(explicit_year)
        try:
            return date(year, month, day_num)
        except ValueError:
            return None

    # Infer year
    year = today.year
    try:
        d = date(year, month, day_num)
    except ValueError:
        return None

    delta = (d - today).days
    if delta < -300:
        # e.g. today is Jan 8, and we see "Dec 28" -> likely Dec 28 THIS year? Actually that's in the future.
        # This branch triggers when it looks far in the past; push forward a year.
        try:
            return date(year + 1, month, day_num)
        except ValueError:
            return None
    if delta > 300:
        # extremely unlikely, but keep it sane
        try:
            return date(year - 1, month, day_num)
        except ValueError:
            return None

    return d


# ---------------- SCRAPER (TributeMovies) ----------------

def fetch_tribute_html() -> str | None:
    try:
        resp = requests.get(TRIBUTE_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"[WARN] Failed to fetch TributeMovies page: {e}")
        return None


def extract_movie_date_sets_from_tribute(html: str) -> dict[str, dict]:
    """
    Parse the TributeMovies HTML and return:
      { normalized_title: { "display_title": "...", "dates": set[date] } }

    Structure on page is generally:
      ## <a> Movie Title </a>
      ...
      ### Regular Showtimes
      Thu, Jan 8: ...
      Fri, Jan 9: ...
      ...
      ## <a> Next Movie </a>
    """
    soup = BeautifulSoup(html, "html.parser")
    today = date.today()

    movie_spans: dict[str, dict] = {}

    # Tribute uses <h2> for movie titles (as seen in the page text)
    for h2 in soup.find_all("h2"):
        title = h2.get_text(" ", strip=True)
        if not title:
            continue

        # Clean up weird whitespace
        title = re.sub(r"\s+", " ", title).strip()

        if title.lower() in BANNED_TITLES_LOWER:
            continue

        # Some pages include non-movie headers; require at least a couple letters
        if len(re.sub(r"[^A-Za-z]+", "", title)) < 2:
            continue

        # Walk forward until the next <h2>, harvesting date lines
        dates_for_movie: set[date] = set()

        for sib in h2.next_siblings:
            if getattr(sib, "name", None) == "h2":
                break

            # Collect text from this sibling chunk
            chunk_text = ""
            if isinstance(sib, str):
                chunk_text = sib
            else:
                try:
                    chunk_text = sib.get_text("\n", strip=True)
                except Exception:
                    chunk_text = ""

            if not chunk_text:
                continue

            for raw_line in chunk_text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                d = parse_showdate_line_to_date(line, today=today)
                if d:
                    dates_for_movie.add(d)

        if not dates_for_movie:
            # If it truly has no dated showtimes, it’s not useful for your math view.
            # (Tribute usually has dates though.)
            continue

        norm = normalize_title(title)
        info = movie_spans.setdefault(
            norm,
            {"display_title": title, "dates": set()},
        )

        # Prefer longer title text if we see variations
        if len(title) > len(info["display_title"]):
            info["display_title"] = title

        info["dates"].update(dates_for_movie)

    return movie_spans


# ---------------- SCHEDULE BUILD + CACHE ----------------

# Simple in-memory cache so multiple refreshes don’t slam TributeMovies
_CACHE = {
    "ts": 0.0,
    "movie_spans": None,  # dict or None
}
CACHE_TTL_SECONDS = 10 * 60  # 10 minutes


def get_cached_movie_spans(force: bool = False) -> dict[str, dict] | None:
    now = time.time()
    if not force and _CACHE["movie_spans"] is not None and (now - _CACHE["ts"]) < CACHE_TTL_SECONDS:
        return _CACHE["movie_spans"]

    html = fetch_tribute_html()
    if not html:
        return None

    spans = extract_movie_date_sets_from_tribute(html)
    _CACHE["ts"] = now
    _CACHE["movie_spans"] = spans
    print(f"[INFO] TributeMovies parsed: {len(spans)} unique titles cached.")
    return spans


def build_schedule(days_ahead: int, force_refresh: bool = False) -> list[dict]:
    """
    Build a list of movies with:
      - the set of dates it actually plays (within the window)
      - first_date, last_date
      - run_length_days = number of distinct dates in that set
    """
    today = date.today()
    cutoff = today + timedelta(days=days_ahead)

    movie_spans = get_cached_movie_spans(force=force_refresh)
    if not movie_spans:
        return []

    result: list[dict] = []

    for norm, info in movie_spans.items():
        dates_set: set[date] = info.get("dates", set())
        if not dates_set:
            continue

        # Keep only dates inside the requested window (today .. today+days_ahead)
        window_dates = sorted(d for d in dates_set if today <= d <= cutoff)
        if not window_dates:
            continue

        first = window_dates[0]
        last = window_dates[-1]
        run_len = len(window_dates)
        days_until = (first - today).days

        result.append(
            {
                "title": info.get("display_title", ""),
                "first_date": first.isoformat(),
                "last_date": last.isoformat(),
                "days_until_start": days_until,
                "run_length_days": run_len,
            }
        )

    # Default sort: soonest start, then shorter run, then title
    result.sort(key=lambda x: (x["days_until_start"], x["run_length_days"], x["title"]))
    return result


# ---------------- FLASK APP ----------------

app = Flask(__name__)


@app.route("/api/showtimes")
def api_showtimes():
    """JSON API: /api/showtimes?days=N&force=1"""
    try:
        days = int(request.args.get("days", DEFAULT_DAYS_AHEAD))
    except ValueError:
        days = DEFAULT_DAYS_AHEAD

    days = max(1, min(days, 120))

    force = request.args.get("force", "0") == "1"

    data = build_schedule(days, force_refresh=force)

    resp = jsonify(
        {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "days_ahead": days,
            "source": "TributeMovies",
            "movies": data,
        }
    )

    # Prevent caching differences between phone/desktop
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/")
def index():
    """Serve the phone-friendly HTML UI with urgency glow."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Kalispell Showtimes Radar</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {
      color-scheme: dark;
      --bg: #05060a;
      --card: #141621;
      --accent: #f97316;
      --accent-soft: rgba(249, 115, 22, 0.2);
      --text: #f9fafb;
      --muted: #9ca3af;
      --border: #27272f;

      --yellow-glow: rgba(250, 204, 21, 0.8);
      --red-glow: rgba(248, 113, 113, 0.9);
    }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: radial-gradient(circle at top, #111827 0, #020617 45%);
      color: var(--text);
    }
    .wrap {
      max-width: 1000px;
      margin: 0 auto;
      padding: 0.75rem 1rem 2rem;
    }
    header {
      padding: 0.75rem 0 0.5rem;
    }
    h1 {
      font-size: 1.4rem;
      margin: 0;
      letter-spacing: 0.04em;
    }
    .subtitle {
      font-size: 0.8rem;
      color: var(--muted);
      margin-top: 0.25rem;
      line-height: 1.35;
    }
    .controls {
      display: flex;
      align-items: center;
      gap: 0.6rem;
      margin-top: 0.85rem;
      flex-wrap: wrap;
    }
    .controls label {
      font-size: 0.75rem;
      color: var(--muted);
      display: flex;
      align-items: center;
      gap: 0.25rem;
    }
    .controls input {
      background: #020617;
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 999px;
      padding: 0.25rem 0.7rem;
      font-size: 0.8rem;
      width: 4rem;
      text-align: center;
    }
    button {
      border-radius: 999px;
      border: none;
      padding: 0.35rem 0.9rem;
      font-size: 0.8rem;
      cursor: pointer;
    }
    #reloadBtn {
      background: var(--accent-soft);
      color: var(--accent);
    }
    #reloadBtn:hover {
      background: rgba(249, 115, 22, 0.3);
    }
    #forceBtn {
      background: rgba(56, 189, 248, 0.14);
      color: #7dd3fc;
      border: 1px solid rgba(56, 189, 248, 0.35);
    }
    #forceBtn:hover {
      background: rgba(56, 189, 248, 0.22);
    }
    #resetHiddenBtn {
      background: transparent;
      border: 1px solid rgba(148, 163, 184, 0.4);
      color: var(--muted);
    }
    #resetHiddenBtn:hover {
      border-color: rgba(248, 250, 252, 0.7);
      color: #e5e7eb;
    }
    .sort-row {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      margin-top: 0.6rem;
      flex-wrap: wrap;
      font-size: 0.75rem;
      color: var(--muted);
    }
    .sort-label {
      opacity: 0.85;
    }
    .sort-buttons {
      display: inline-flex;
      border-radius: 999px;
      border: 1px solid rgba(148, 163, 184, 0.5);
      overflow: hidden;
    }
    .sort-btn {
      font-size: 0.75rem;
      padding: 0.25rem 0.8rem;
      border-radius: 0;
      border: none;
      background: transparent;
      color: var(--muted);
    }
    .sort-btn + .sort-btn {
      border-left: 1px solid rgba(148, 163, 184, 0.4);
    }
    .sort-btn.active-sort {
      background: var(--accent);
      color: #020617;
    }
    .pill-row {
      display: flex;
      gap: 0.5rem;
      margin-top: 0.6rem;
      flex-wrap: wrap;
      font-size: 0.7rem;
    }
    .pill {
      padding: 0.15rem 0.55rem;
      border-radius: 999px;
      border: 1px solid rgba(148, 163, 184, 0.3);
      color: var(--muted);
    }
    .pill strong {
      color: var(--accent);
    }
    .status-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.5rem;
      margin-top: 0.55rem;
    }
    .status {
      font-size: 0.75rem;
      color: var(--muted);
    }
    .hidden-status {
      font-size: 0.7rem;
      color: #a5b4fc;
    }
    .grid {
      margin-top: 1.1rem;
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }
    .card {
      border-radius: 0.75rem;
      background: radial-gradient(circle at top left, rgba(15, 23, 42, 0.98), rgba(15, 23, 42, 0.92));
      border: 1px solid var(--border);
      padding: 0.6rem 0.75rem 0.55rem;
      box-shadow: 0 12px 25px rgba(0, 0, 0, 0.45);
      transition: box-shadow 0.18s ease, border-color 0.18s ease, transform 0.12s ease;
    }
    .card-urgent-yellow {
      border-color: rgba(250, 204, 21, 0.9);
      box-shadow:
        0 0 0 1px rgba(250, 204, 21, 0.6),
        0 12px 26px rgba(0, 0, 0, 0.6),
        0 0 30px rgba(250, 204, 21, 0.25);
    }
    .card-urgent-red {
      border-color: rgba(248, 113, 113, 0.95);
      box-shadow:
        0 0 0 1px rgba(248, 113, 113, 0.7),
        0 12px 26px rgba(0, 0, 0, 0.65),
        0 0 35px rgba(248, 113, 113, 0.3);
      transform: translateY(-1px);
    }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 0.5rem;
    }
    .title {
      font-size: 1.05rem;
      font-weight: 600;
      letter-spacing: 0.01em;
    }
    .bucket-tag {
      font-size: 0.65rem;
      padding: 0.2rem 0.55rem;
      border-radius: 999px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      background: rgba(15, 118, 110, 0.2);
      color: #5eead4;
      border: 1px solid rgba(45, 212, 191, 0.4);
      white-space: nowrap;
    }
    .card-dates {
      margin-top: 0.3rem;
      font-size: 0.72rem;
      color: var(--muted);
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }
    .card-meta {
      margin-top: 0.25rem;
      font-size: 0.8rem;
    }
    .meta-highlight {
      color: var(--accent);
      font-weight: 500;
    }
    .meta-secondary {
      color: var(--muted);
      margin-top: 0.05rem;
    }
    .run-row {
      margin-top: 0.3rem;
      display: inline-flex;
      gap: 0.35rem;
      align-items: center;
      flex-wrap: wrap;
    }
    .run-tag {
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      padding: 0.16rem 0.55rem;
      border-radius: 999px;
      background: rgba(248, 250, 252, 0.06);
      border: 1px solid rgba(252, 211, 77, 0.6);
      color: #facc15;
    }
    .urgency-label {
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      padding: 0.14rem 0.55rem;
      border-radius: 999px;
    }
    .urgency-yellow {
      background: rgba(250, 204, 21, 0.08);
      border: 1px solid rgba(250, 204, 21, 0.7);
      color: #facc15;
    }
    .urgency-red {
      background: rgba(248, 113, 113, 0.09);
      border: 1px solid rgba(248, 113, 113, 0.85);
      color: #fecaca;
    }
    .hide-row {
      margin-top: 0.25rem;
      display: flex;
      justify-content: flex-end;
    }
    .hide-btn {
      background: transparent;
      border: none;
      color: var(--muted);
      font-size: 0.7rem;
      text-decoration: underline;
      padding: 0;
      cursor: pointer;
    }
    .hide-btn:hover {
      color: #fca5a5;
    }
    .section-title {
      margin-top: 1.4rem;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      color: var(--muted);
      border-top: 1px solid rgba(31, 41, 55, 0.9);
      padding-top: 0.55rem;
    }
    .empty {
      margin-top: 0.75rem;
      font-size: 0.8rem;
      color: var(--muted);
    }
    @media (min-width: 700px) {
      .grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>Kalispell Showtimes Radar</h1>
      <div class="subtitle">
        Source: TributeMovies (single-page scrape) · refresh to re-pull the schedule
      </div>
      <div class="controls">
        <label>
          Days ahead
          <input id="daysInput" type="number" min="1" max="120" value="45">
        </label>
        <button id="reloadBtn">Refresh</button>
        <button id="forceBtn" title="Ignore the 10-minute server cache and re-fetch right now">Force re-scrape</button>
        <button id="resetHiddenBtn">Reset hidden</button>
      </div>
      <div class="sort-row">
        <span class="sort-label">Sort by</span>
        <div class="sort-buttons">
          <button class="sort-btn" data-mode="start">Soonest</button>
          <button class="sort-btn" data-mode="run">Shortest run</button>
        </div>
      </div>
      <div class="pill-row">
        <div class="pill"><strong>Now</strong> = already playing</div>
        <div class="pill"><strong>Soon</strong> = starts ≤ 14 days</div>
        <div class="pill"><strong>Later</strong> = starts > 14 days</div>
      </div>
      <div class="status-row">
        <div class="status" id="status">Loading…</div>
        <div class="status hidden-status" id="hiddenStatus" style="display:none;"></div>
      </div>
    </header>

    <div id="section-now">
      <div class="section-title">Now Playing</div>
      <div class="grid" id="nowGrid"></div>
      <div class="empty" id="nowEmpty" style="display:none;">Nothing currently running? That feels fake, but okay.</div>
    </div>

    <div id="section-soon">
      <div class="section-title">Coming Soon (within 2 weeks)</div>
      <div class="grid" id="soonGrid"></div>
      <div class="empty" id="soonEmpty" style="display:none;">No imminent arrivals.</div>
    </div>

    <div id="section-later">
      <div class="section-title">Later (beyond 2 weeks)</div>
      <div class="grid" id="laterGrid"></div>
      <div class="empty" id="laterEmpty" style="display:none;">The future is empty. That tracks.</div>
    </div>
  </div>

  <script>
    const statusEl = document.getElementById('status');
    const hiddenStatusEl = document.getElementById('hiddenStatus');
    const nowGrid = document.getElementById('nowGrid');
    const soonGrid = document.getElementById('soonGrid');
    const laterGrid = document.getElementById('laterGrid');
    const nowEmpty = document.getElementById('nowEmpty');
    const soonEmpty = document.getElementById('soonEmpty');
    const laterEmpty = document.getElementById('laterEmpty');
    const daysInput = document.getElementById('daysInput');
    const reloadBtn = document.getElementById('reloadBtn');
    const forceBtn = document.getElementById('forceBtn');
    const resetHiddenBtn = document.getElementById('resetHiddenBtn');
    const sortButtons = document.querySelectorAll('.sort-btn');

    const SETTINGS_KEY = 'ksrSettingsV1';
    const HIDDEN_KEY = 'ksrHiddenMoviesV1';

    function normalizeTitleJS(title) {
      return (title || '')
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '');
    }

    function loadSettings() {
      try {
        const raw = localStorage.getItem(SETTINGS_KEY);
        if (!raw) return { daysAhead: 45, sortMode: 'start' };
        const obj = JSON.parse(raw);
        return {
          daysAhead: typeof obj.daysAhead === 'number' ? obj.daysAhead : 45,
          sortMode: obj.sortMode === 'run' ? 'run' : 'start',
        };
      } catch (e) {
        return { daysAhead: 45, sortMode: 'start' };
      }
    }

    function saveSettings() {
      try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings)); } catch (e) {}
    }

    function loadHidden() {
      try {
        const raw = localStorage.getItem(HIDDEN_KEY);
        if (!raw) return new Set();
        const arr = JSON.parse(raw);
        if (!Array.isArray(arr)) return new Set();
        return new Set(arr);
      } catch (e) {
        return new Set();
      }
    }

    function saveHidden() {
      try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(Array.from(hiddenSet))); } catch (e) {}
    }

    let settings = loadSettings();
    let hiddenSet = loadHidden();
    let cachedMovies = [];

    function applySortModeUI() {
      sortButtons.forEach(btn => {
        const mode = btn.dataset.mode || 'start';
        if (mode === settings.sortMode) btn.classList.add('active-sort');
        else btn.classList.remove('active-sort');
      });
    }

    function updateHiddenStatus() {
      const count = hiddenSet.size;
      if (!count) {
        hiddenStatusEl.style.display = 'none';
        hiddenStatusEl.textContent = '';
        return;
      }
      hiddenStatusEl.style.display = 'block';
      hiddenStatusEl.textContent = count === 1 ? '1 movie hidden' : count + ' movies hidden';
    }

    function bucketTag(daysUntil) {
      if (daysUntil <= 0) return 'NOW';
      if (daysUntil <= 14) return 'SOON';
      return 'LATER';
    }

    function humanDaysUntil(daysUntil) {
      if (daysUntil <= 0) return 'Now playing';
      if (daysUntil === 1) return 'Starts in 1 day';
      return `Starts in ${daysUntil} days`;
    }

    function humanRunLength(run) {
      if (run === 1) return 'Plays 1 day only';
      return `Plays for ${run} days total`;
    }

    function prettyDate(iso) {
      const d = new Date(iso + 'T12:00:00');
      return d.toLocaleDateString(undefined, {
        weekday: 'short',
        month: 'short',
        day: 'numeric'
      });
    }

    function daysRemaining(movie) {
      const now = new Date();
      const today = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 12, 0, 0);
      const last = new Date(movie.last_date + 'T12:00:00');
      const diffMs = last - today;
      return Math.floor(diffMs / (1000 * 60 * 60 * 24));
    }

    function getUrgency(movie) {
      const remaining = daysRemaining(movie);
      const run = movie.run_length_days;

      if (remaining <= 1) return 'red';          // final chance (today or tomorrow)
      if (run <= 3 || remaining <= 5) return 'yellow'; // limited or leaving soon
      return 'none';
    }

    function specialRunLabel(movie) {
      const run = movie.run_length_days;
      if (run === 1) return 'ONE NIGHT ONLY';
      if (run === 2) return '2-DAY RUN';
      if (run === 3) return '3-DAY RUN';
      if (run <= 7) return 'LIMITED RUN';
      return '';
    }

    function createCard(movie) {
      const card = document.createElement('div');
      card.className = 'card';

      const urgency = getUrgency(movie);
      if (urgency === 'yellow') card.classList.add('card-urgent-yellow');
      if (urgency === 'red') card.classList.add('card-urgent-red');

      const header = document.createElement('div');
      header.className = 'card-header';

      const titleEl = document.createElement('div');
      titleEl.className = 'title';
      titleEl.textContent = movie.title;

      const tag = document.createElement('div');
      tag.className = 'bucket-tag';
      tag.textContent = bucketTag(movie.days_until_start);

      header.appendChild(titleEl);
      header.appendChild(tag);

      const dates = document.createElement('div');
      dates.className = 'card-dates';
      dates.textContent = `${prettyDate(movie.first_date)} → ${prettyDate(movie.last_date)}`;

      const meta = document.createElement('div');
      meta.className = 'card-meta';

      const line1 = document.createElement('div');
      line1.innerHTML = `<span class="meta-highlight">${humanDaysUntil(movie.days_until_start)}</span>`;

      const line2 = document.createElement('div');
      line2.className = 'meta-secondary';
      line2.textContent = humanRunLength(movie.run_length_days);

      meta.appendChild(line1);
      meta.appendChild(line2);

      const runLabel = specialRunLabel(movie);
      if (runLabel || urgency !== 'none') {
        const runRow = document.createElement('div');
        runRow.className = 'run-row';

        if (runLabel) {
          const runTag = document.createElement('div');
          runTag.className = 'run-tag';
          runTag.textContent = runLabel;
          runRow.appendChild(runTag);
        }

        if (urgency === 'yellow') {
          const u = document.createElement('div');
          u.className = 'urgency-label urgency-yellow';
          u.textContent = 'Leaves soon';
          runRow.appendChild(u);
        } else if (urgency === 'red') {
          const u = document.createElement('div');
          u.className = 'urgency-label urgency-red';
          u.textContent = 'Final chance';
          runRow.appendChild(u);
        }

        meta.appendChild(runRow);
      }

      const normKey = normalizeTitleJS(movie.title);
      const hideRow = document.createElement('div');
      hideRow.className = 'hide-row';

      const hideBtn = document.createElement('button');
      hideBtn.className = 'hide-btn';
      hideBtn.textContent = 'Hide';
      hideBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        hiddenSet.add(normKey);
        saveHidden();
        card.remove();
        updateEmptyMessages();
        updateHiddenStatus();
        statusEl.textContent = 'Hidden "' + movie.title + '".';
      });
      hideRow.appendChild(hideBtn);

      card.appendChild(header);
      card.appendChild(dates);
      card.appendChild(meta);
      card.appendChild(hideRow);

      return card;
    }

    function sortMovies(list, mode) {
      const movies = [...list];
      if (mode === 'run') {
        movies.sort((a, b) => {
          if (a.run_length_days !== b.run_length_days) return a.run_length_days - b.run_length_days;
          if (a.days_until_start !== b.days_until_start) return a.days_until_start - b.days_until_start;
          return a.title.localeCompare(b.title);
        });
      } else {
        movies.sort((a, b) => {
          if (a.days_until_start !== b.days_until_start) return a.days_until_start - b.days_until_start;
          if (a.run_length_days !== b.run_length_days) return a.run_length_days - b.run_length_days;
          return a.title.localeCompare(b.title);
        });
      }
      return movies;
    }

    function updateEmptyMessages() {
      nowEmpty.style.display = nowGrid.children.length ? 'none' : 'block';
      soonEmpty.style.display = soonGrid.children.length ? 'none' : 'block';
      laterEmpty.style.display = laterGrid.children.length ? 'none' : 'block';
    }

    function renderMovies() {
      nowGrid.innerHTML = '';
      soonGrid.innerHTML = '';
      laterGrid.innerHTML = '';
      nowEmpty.style.display = 'none';
      soonEmpty.style.display = 'none';
      laterEmpty.style.display = 'none';

      if (!cachedMovies || !cachedMovies.length) {
        updateEmptyMessages();
        return;
      }

      const sorted = sortMovies(cachedMovies, settings.sortMode);
      const renderedSet = new Set();

      const now = [];
      const soon = [];
      const later = [];

      for (const m of sorted) {
        const key = normalizeTitleJS(m.title);
        if (hiddenSet.has(key)) continue;
        if (renderedSet.has(key)) continue;
        renderedSet.add(key);

        if (m.days_until_start <= 0) now.push(m);
        else if (m.days_until_start <= 14) soon.push(m);
        else later.push(m);
      }

      now.forEach(m => nowGrid.appendChild(createCard(m)));
      soon.forEach(m => soonGrid.appendChild(createCard(m)));
      later.forEach(m => laterGrid.appendChild(createCard(m)));

      updateEmptyMessages();
    }

    async function loadData(force) {
      const raw = parseInt(daysInput.value || String(settings.daysAhead || 45), 10);
      let days = isNaN(raw) ? 45 : raw;
      days = Math.min(120, Math.max(1, days));
      daysInput.value = String(days);
      settings.daysAhead = days;
      saveSettings();

      statusEl.textContent = `Loading up to ${days} days ahead…`;
      nowGrid.innerHTML = '';
      soonGrid.innerHTML = '';
      laterGrid.innerHTML = '';
      nowEmpty.style.display = 'none';
      soonEmpty.style.display = 'none';
      laterEmpty.style.display = 'none';

      try {
        const forceFlag = force ? '&force=1' : '';
        const res = await fetch(`/api/showtimes?days=${days}${forceFlag}&t=${Date.now()}`, { cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();

        cachedMovies = data.movies || [];
        statusEl.textContent = `Generated at ${new Date(data.generated_at).toLocaleString()} · window: ${data.days_ahead} days · source: ${data.source || 'unknown'}`;

        renderMovies();
      } catch (err) {
        console.error(err);
        statusEl.textContent = 'Error talking to the scraper. Server asleep? Or the source site changed?';
        nowEmpty.style.display = 'block';
        soonEmpty.style.display = 'block';
        laterEmpty.style.display = 'block';
      }
    }

    reloadBtn.addEventListener('click', () => loadData(false));
    forceBtn.addEventListener('click', () => loadData(true));

    resetHiddenBtn.addEventListener('click', () => {
      hiddenSet.clear();
      saveHidden();
      updateHiddenStatus();
      statusEl.textContent = 'All hidden movies reset.';
      renderMovies();
    });

    sortButtons.forEach(btn => {
      btn.addEventListener('click', () => {
        const mode = btn.dataset.mode || 'start';
        settings.sortMode = mode;
        saveSettings();
        applySortModeUI();
        statusEl.textContent = mode === 'run'
          ? 'Sorting by shortest runs.'
          : 'Sorting by soonest start.';
        renderMovies();
      });
    });

    window.addEventListener('load', () => {
      daysInput.value = String(settings.daysAhead || 45);
      applySortModeUI();
      updateHiddenStatus();
      loadData(false);
    });
  </script>
</body>
</html>
"""
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except OSError:
        local_ip = "127.0.0.1"
    print(f"Serving on http://{local_ip}:5000  (or http://localhost:5000)")
    app.run(host="0.0.0.0", port=5000)
