import re
import socket
from datetime import date, datetime, timedelta
from typing import Optional, Dict, List, Set, Tuple

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, Response

# ------------ CONFIG ------------

TRIBUTE_THEATRE_URL = (
    "https://www.tributemovies.com/cinema/Montana/Kalispell/"
    "Cinemark-Signature-Stadium-Kalispell-14/10338/"
)

DEFAULT_DAYS_AHEAD = 60  # UI can request up to 365 if you want

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Cache: prevents repeated heavy scrapes when phone refreshes or multiple clients hit at once
CACHE_TTL_SECONDS = 120

# Date label pattern seen on TributeMovies showtime lines (no year in the label)
# Example: "Thu, Jan 15:"  (sometimes appears as <b>Thu, Jan 15:</b>)
DATE_LABEL_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s([A-Za-z]{3})\s(\d{1,2}):$")

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12
}

def normalize_title(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t


# ------------ SCRAPER (TRIBUTE MOVIES) ------------

def fetch_tribute_html() -> Optional[str]:
    try:
        resp = requests.get(TRIBUTE_THEATRE_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"[WARN] Failed to fetch TributeMovies page: {e}")
        return None


def _guess_year_for_month_day(today: date, month: int, day_num: int) -> int:
    """
    TributeMovies labels don’t include year. We infer:
    - default: current year
    - if it looks like we crossed into next year (e.g., today is Dec and we see Jan/Feb), bump year
    """
    y = today.year
    # If we're in late year and we see a small month, it's probably next year
    if today.month >= 11 and month <= 2:
        y += 1
    return y


def parse_tribute_schedule(html: str) -> Dict[str, Dict]:
    """
    Returns:
      norm_title -> {
        display_title: str,
        dates: set[date]
      }

    TributeMovies structure (as shown in their page text):
      - Movie title in <h2 class="media-heading">...</h2> :contentReference[oaicite:2]{index=2}
      - Then showtime sections with lines like "Thu, Jan 8: ..." :contentReference[oaicite:3]{index=3}
    """
    soup = BeautifulSoup(html, "html.parser")
    today = date.today()

    movie_spans: Dict[str, Dict] = {}
    movie_headers = soup.select("h2.media-heading")

    if not movie_headers:
        # Fallback: sometimes the title is still visible via heading structure
        movie_headers = soup.find_all("h2")

    print(f"[INFO] Found {len(movie_headers)} movie headers on Tribute page")

    for h2 in movie_headers:
        title = h2.get_text(" ", strip=True)
        if not title:
            continue

        # The page has a couple weird blank headers in the nav sometimes; filter aggressively
        if len(title) < 2:
            continue

        norm = normalize_title(title)
        if not norm:
            continue

        info = movie_spans.setdefault(norm, {"display_title": title, "dates": set()})

        # Prefer the most "complete" title if it varies
        if len(title) > len(info["display_title"]):
            info["display_title"] = title

        # Walk forward until we hit the next movie header
        for tag in h2.find_all_next():
            if tag is h2:
                continue
            if getattr(tag, "name", None) == "h2" and "media-heading" in (tag.get("class") or []):
                break

            # Date labels are commonly in <b> tags like: <b>Thu, Jan 15:</b>
            if getattr(tag, "name", None) == "b":
                raw = tag.get_text(" ", strip=True)
                m = DATE_LABEL_RE.match(raw)
                if not m:
                    continue

                mon_abbr = m.group(2)
                day_num = int(m.group(3))
                month = MONTHS.get(mon_abbr)
                if not month:
                    continue

                year = _guess_year_for_month_day(today, month, day_num)
                try:
                    d = date(year, month, day_num)
                except ValueError:
                    continue

                info["dates"].add(d)

            # Some showtime blocks might render date labels as plain text nodes, not <b>
            # We'll catch those too.
            if isinstance(tag, str):
                txt = tag.strip()
                if not txt:
                    continue
                # normalize “Thu, Jan 15:” if it appears as text
                m = DATE_LABEL_RE.match(txt)
                if not m:
                    continue
                mon_abbr = m.group(2)
                day_num = int(m.group(3))
                month = MONTHS.get(mon_abbr)
                if not month:
                    continue
                year = _guess_year_for_month_day(today, month, day_num)
                try:
                    d = date(year, month, day_num)
                except ValueError:
                    continue
                info["dates"].add(d)

    return movie_spans


def build_schedule(days_ahead: int) -> List[dict]:
    """
    Convert TributeMovies parsed data into your UI format.
    Important: run_length_days is the count of distinct show dates within the window,
    and first/last are min/max of those dates (not "continuous run").
    """
    today = date.today()
    html = fetch_tribute_html()
    if not html:
        return []

    spans = parse_tribute_schedule(html)

    horizon = today + timedelta(days=days_ahead)
    result: List[dict] = []

    for norm, info in spans.items():
        dates_set: Set[date] = set(info.get("dates") or set())
        if not dates_set:
            continue

        # Filter dates within [today - 365, horizon] so "already playing" still shows
        filtered = sorted([d for d in dates_set if d <= horizon and d >= today - timedelta(days=365)])
        if not filtered:
            continue

        first = min(filtered)
        last = max(filtered)

        days_until = (first - today).days
        run_len = len(filtered)

        result.append(
            {
                "title": info["display_title"],
                "first_date": first.isoformat(),
                "last_date": last.isoformat(),
                "days_until_start": days_until,
                "run_length_days": run_len,
            }
        )

    result.sort(key=lambda x: (x["days_until_start"], x["run_length_days"], x["title"]))
    return result


# ------------ SIMPLE IN-MEMORY CACHE ------------

_cache = {
    "fetched_at": None,   # datetime
    "movies_full": None,  # List[dict] for a large horizon
    "horizon_days": 0
}

def get_cached_schedule(days_ahead: int) -> List[dict]:
    """
    Cache strategy:
    - scrape once for a big horizon (max requested today)
    - reuse for subsequent requests within TTL
    """
    now = datetime.now()
    fetched_at: Optional[datetime] = _cache["fetched_at"]
    movies_full: Optional[List[dict]] = _cache["movies_full"]
    horizon_days: int = _cache["horizon_days"]

    if fetched_at and movies_full and (now - fetched_at).total_seconds() < CACHE_TTL_SECONDS and horizon_days >= days_ahead:
        return _filter_to_days_ahead(movies_full, days_ahead)

    # Fetch fresh at requested horizon
    full = build_schedule(days_ahead)
    _cache["fetched_at"] = now
    _cache["movies_full"] = full
    _cache["horizon_days"] = days_ahead
    return full


def _filter_to_days_ahead(movies: List[dict], days_ahead: int) -> List[dict]:
    """
    Our movie objects already have first/last based on the filtered set when scraped.
    If cache horizon is larger than requested, we need to re-filter by days_until_start and last_date.
    This is conservative: it may keep a movie if its first_date is within horizon and last_date within horizon.
    """
    today = date.today()
    horizon = today + timedelta(days=days_ahead)
    out = []
    for m in movies:
        try:
            first = date.fromisoformat(m["first_date"])
            last = date.fromisoformat(m["last_date"])
        except Exception:
            continue
        # If its earliest showing is beyond horizon, skip
        if first > horizon:
            continue
        # Keep it (it’s in range or already playing)
        out.append(m)
    out.sort(key=lambda x: (x["days_until_start"], x["run_length_days"], x["title"]))
    return out


# ------------ FLASK APP ------------

app = Flask(__name__)
@app.route("/api/debug_raw")
def api_debug_raw():
    html = fetch_tribute_html()
    if not html:
        return jsonify({"ok": False, "error": "fetch_tribute_html returned None"}), 500

    # show a tiny slice so we can see if it's a bot wall or real page
    return jsonify({
        "ok": True,
        "url": TRIBUTE_THEATRE_URL,
        "len": len(html),
        "head": html[:1200],
        "has_media_heading": "media-heading" in html,
        "has_ticketicons": "ticketicons" in html,
        "has_Thu_Jan": "Thu, Jan" in html,
    })


@app.route("/api/showtimes")
def api_showtimes():
    """JSON API: /api/showtimes?days=N"""
    try:
        days = int(request.args.get("days", DEFAULT_DAYS_AHEAD))
    except ValueError:
        days = DEFAULT_DAYS_AHEAD

    # allow bigger now because Tribute already has sparse far-out dates
    days = max(1, min(days, 365))

    data = get_cached_schedule(days)

    resp = jsonify(
        {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "days_ahead": days,
            "movies": data,
            "source": "TributeMovies",
        }
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/")
def index():
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
    header { padding: 0.75rem 0 0.5rem; }
    h1 { font-size: 1.4rem; margin: 0; letter-spacing: 0.04em; }
    .subtitle { font-size: 0.8rem; color: var(--muted); margin-top: 0.25rem; }

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
    #reloadBtn { background: var(--accent-soft); color: var(--accent); }
    #reloadBtn:hover { background: rgba(249, 115, 22, 0.3); }
    #resetHiddenBtn {
      background: transparent;
      border: 1px solid rgba(148, 163, 184, 0.4);
      color: var(--muted);
    }
    #resetHiddenBtn:hover { border-color: rgba(248, 250, 252, 0.7); color: #e5e7eb; }

    .sort-row {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      margin-top: 0.6rem;
      flex-wrap: wrap;
      font-size: 0.75rem;
      color: var(--muted);
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
    .sort-btn + .sort-btn { border-left: 1px solid rgba(148, 163, 184, 0.4); }
    .sort-btn.active-sort { background: var(--accent); color: #020617; }

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
    .pill strong { color: var(--accent); }

    .status-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.5rem;
      margin-top: 0.55rem;
    }
    .status { font-size: 0.75rem; color: var(--muted); }
    .hidden-status { font-size: 0.7rem; color: #a5b4fc; }

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
    .title { font-size: 1.05rem; font-weight: 600; letter-spacing: 0.01em; }
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
    .card-meta { margin-top: 0.25rem; font-size: 0.8rem; }
    .meta-highlight { color: var(--accent); font-weight: 500; }
    .meta-secondary { color: var(--muted); margin-top: 0.05rem; }

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

    .hide-row { margin-top: 0.25rem; display: flex; justify-content: flex-end; }
    .hide-btn {
      background: transparent;
      border: none;
      color: var(--muted);
      font-size: 0.7rem;
      text-decoration: underline;
      padding: 0;
      cursor: pointer;
    }
    .hide-btn:hover { color: #fca5a5; }

    .section-title {
      margin-top: 1.4rem;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      color: var(--muted);
      border-top: 1px solid rgba(31, 41, 55, 0.9);
      padding-top: 0.55rem;
    }
    .empty { margin-top: 0.75rem; font-size: 0.8rem; color: var(--muted); }

    @media (min-width: 700px) {
      .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>Kalispell Showtimes Radar</h1>
      <div class="subtitle">TributeMovies (Fandango-powered) · updates whenever you refresh</div>

      <div class="controls">
        <label>Days ahead <input id="daysInput" type="number" min="1" max="365" value="60"></label>
        <button id="reloadBtn">Refresh</button>
        <button id="resetHiddenBtn">Reset hidden</button>
      </div>

      <div class="sort-row">
        <span>Sort by</span>
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
      <div class="empty" id="nowEmpty" style="display:none;">Nothing currently running. Either the world ended or the data is down.</div>
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
    const resetHiddenBtn = document.getElementById('resetHiddenBtn');
    const sortButtons = document.querySelectorAll('.sort-btn');

    const SETTINGS_KEY = 'ksrSettingsV2';
    const HIDDEN_KEY = 'ksrHiddenMoviesV2';

    function normalizeTitleJS(title) {
      return (title || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
    }

    function loadSettings() {
      try {
        const raw = localStorage.getItem(SETTINGS_KEY);
        if (!raw) return { daysAhead: 60, sortMode: 'start' };
        const obj = JSON.parse(raw);
        return {
          daysAhead: typeof obj.daysAhead === 'number' ? obj.daysAhead : 60,
          sortMode: obj.sortMode === 'run' ? 'run' : 'start',
        };
      } catch { return { daysAhead: 60, sortMode: 'start' }; }
    }

    function saveSettings() {
      try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings)); } catch {}
    }

    function loadHidden() {
      try {
        const raw = localStorage.getItem(HIDDEN_KEY);
        if (!raw) return new Set();
        const arr = JSON.parse(raw);
        if (!Array.isArray(arr)) return new Set();
        return new Set(arr);
      } catch { return new Set(); }
    }

    function saveHidden() {
      try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(Array.from(hiddenSet))); } catch {}
    }

    let settings = loadSettings();
    let hiddenSet = loadHidden();
    let cachedMovies = [];

    function applySortModeUI() {
      sortButtons.forEach(btn => {
        const mode = btn.dataset.mode || 'start';
        btn.classList.toggle('active-sort', mode === settings.sortMode);
      });
    }

    function updateHiddenStatus() {
      const c = hiddenSet.size;
      if (!c) { hiddenStatusEl.style.display = 'none'; hiddenStatusEl.textContent = ''; return; }
      hiddenStatusEl.style.display = 'block';
      hiddenStatusEl.textContent = c === 1 ? '1 movie hidden' : `${c} movies hidden`;
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
      return `Plays on ${run} day(s) total`;
    }

    function prettyDate(iso) {
      const d = new Date(iso + 'T12:00:00');
      return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
    }

    // urgency based on last_date proximity and total run length
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

      if (remaining <= 1) return 'red';
      if (run <= 3 || remaining <= 5) return 'yellow';
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
        statusEl.textContent = `Hidden "${movie.title}".`;
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

      const sorted = sortMovies(cachedMovies || [], settings.sortMode);
      const rendered = new Set();

      const now = [];
      const soon = [];
      const later = [];

      for (const m of sorted) {
        const key = normalizeTitleJS(m.title);
        if (hiddenSet.has(key)) continue;
        if (rendered.has(key)) continue;
        rendered.add(key);

        if (m.days_until_start <= 0) now.push(m);
        else if (m.days_until_start <= 14) soon.push(m);
        else later.push(m);
      }

      now.forEach(m => nowGrid.appendChild(createCard(m)));
      soon.forEach(m => soonGrid.appendChild(createCard(m)));
      later.forEach(m => laterGrid.appendChild(createCard(m)));

      updateEmptyMessages();
    }

    async function loadData() {
      const raw = parseInt(daysInput.value || String(settings.daysAhead || 60), 10);
      let days = isNaN(raw) ? 60 : raw;
      days = Math.min(365, Math.max(1, days));

      daysInput.value = String(days);
      settings.daysAhead = days;
      saveSettings();

      statusEl.textContent = `Loading up to ${days} days ahead…`;
      nowGrid.innerHTML = '';
      soonGrid.innerHTML = '';
      laterGrid.innerHTML = '';

      try {
        const res = await fetch(`/api/showtimes?days=${days}&t=${Date.now()}`, { cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        cachedMovies = data.movies || [];
        statusEl.textContent = `Generated at ${new Date(data.generated_at).toLocaleString()} · source: ${data.source}`;
        renderMovies();
      } catch (err) {
        console.error(err);
        statusEl.textContent = 'Error talking to the scraper. (Network? Render sleeping? or Tribute changed layout.)';
        nowEmpty.style.display = 'block';
        soonEmpty.style.display = 'block';
        laterEmpty.style.display = 'block';
      }
    }

    reloadBtn.addEventListener('click', loadData);

    resetHiddenBtn.addEventListener('click', () => {
      hiddenSet.clear();
      saveHidden();
      updateHiddenStatus();
      statusEl.textContent = 'All hidden movies reset.';
      renderMovies();
    });

    sortButtons.forEach(btn => {
      btn.addEventListener('click', () => {
        settings.sortMode = btn.dataset.mode || 'start';
        saveSettings();
        applySortModeUI();
        renderMovies(); // resort cached, no refetch
      });
    });

    window.addEventListener('load', () => {
      daysInput.value = String(settings.daysAhead || 60);
      applySortModeUI();
      updateHiddenStatus();
      loadData();
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

