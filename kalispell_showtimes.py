import re
import socket
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, Response

# ------------ CONFIG ------------

THEATRE_URL_TEMPLATE = (
    "https://www.cinemark.com/theatres/mt-kalispell/"
    "cinemark-signature-stadium-kalispell-14?showDate={show_date}"
)

# How many days ahead to scrape by default
DEFAULT_DAYS_AHEAD = 60

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}

# Titles that are clearly not actual movies (UI junk, etc.)
BANNED_TITLES_LOWER = {
    "add to watch list",
    "details",
    "trailer",
    "question mark icon",
    "?",
}

RATING_PATTERN = re.compile(
    r"^(G|PG|PG-13|PG 13|R|NC-17|NR|Not Rated)\b"
)
TIME_PATTERN = re.compile(r"^\d{1,2}:\d{2}(am|pm)$", re.IGNORECASE)

SHOWDATE_PATTERN = re.compile(r"showDate=(\d{4}-\d{2}-\d{2})")


def normalize_title(title: str) -> str:
    """
    Make a 'canonical' key for a movie title so that
    little variations collapse together.
    """
    t = title.lower()
    # Strip everything except letters and digits
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t


# ------------ SCRAPER ------------

def fetch_html_for_date(d: date) -> str | None:
    """Download the Cinemark showtimes page for a specific date."""
    url = THEATRE_URL_TEMPLATE.format(show_date=d.isoformat())
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"[WARN] Failed to fetch {url}: {e}")
        return None


def extract_movies_for_date(html: str) -> set[str]:
    """
    Parse the HTML for a single day and return a set of movie titles.

    Heuristic:
    - Find the "Showtimes for" header.
    - From there, walk forward through <a> tags.
    - Treat an <a> as a movie title if:
      * It isn't a banned word,
      * It isn't a time string like "7:45pm",
      * There is a rating string (G, PG-13, etc.) somewhere nearby.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Find the start of the showtimes section
    text_node = soup.find(string=re.compile(r"^Showtimes for", re.IGNORECASE))
    if not text_node:
        return set()

    start_tag = text_node.parent

    # Find an approximate "end" marker (email signup/footer)
    stop_node = soup.find(
        string=re.compile(r"Get email updates about movies", re.IGNORECASE)
    )

    titles: set[str] = set()

    for a in start_tag.find_all_next("a"):
        # Stop if we walk into the footer
        if stop_node and a is stop_node:
            break

        raw = a.get_text(strip=True)
        if not raw:
            continue

        title = raw.strip()

        # Skip UI junk by title text
        if title.lower() in BANNED_TITLES_LOWER:
            continue

        # Skip obvious showtime strings like "7:45pm"
        if TIME_PATTERN.match(title):
            continue

        # Look ahead for a rating string near this link
        rating_text = a.find_next(string=RATING_PATTERN)
        if not rating_text:
            continue

        if stop_node and rating_text is stop_node:
            continue

        titles.add(title)

    return titles


def extract_available_dates_from_slider(today_html: str, today: date, days_ahead: int) -> list[date]:
    """
    Look inside today's HTML for any ?showDate=YYYY-MM-DD occurrences
    (the date slider links), and return only the real dates within
    [today, today + days_ahead].

    This avoids hitting fake dates that just bounce back to today's showtimes.
    """
    if not today_html:
        return []

    found = set()
    for match in SHOWDATE_PATTERN.findall(today_html):
        try:
            d = datetime.strptime(match, "%Y-%m-%d").date()
        except ValueError:
            continue
        # Only future-ish dates within the window
        if today <= d <= today + timedelta(days=days_ahead):
            found.add(d)

    # Always include today explicitly
    found.add(today)

    dates_sorted = sorted(found)
    print(f"[INFO] Slider dates within window: {[d.isoformat() for d in dates_sorted]}")
    return dates_sorted


def build_schedule(days_ahead: int) -> list[dict]:
    """
    Scrape from today out 'days_ahead' days,
    and build a movie list.

    For each movie we track:
      - the *set* of dates it actually has showtimes
      - first_date  = earliest date in that set
      - last_date   = latest date in that set
      - run_length  = number of distinct dates in that set

    We ONLY scrape dates that Cinemark actually exposes in the
    date slider (showDate=YYYY-MM-DD). This avoids fake dates
    that just show today's lineup again.
    """
    today = date.today()

    # Fetch today's page once (we use it for the slider + today's shows)
    today_html = fetch_html_for_date(today)
    if not today_html:
        print("[WARN] Could not fetch today's HTML; falling back to range-based dates.")
        # Fallback: dumb range if we can't read slider at all
        date_list = [today + timedelta(days=offset) for offset in range(days_ahead + 1)]
    else:
        slider_dates = extract_available_dates_from_slider(today_html, today, days_ahead)
        if slider_dates:
            date_list = slider_dates
        else:
            # If slider parsing fails, fallback to the old behavior
            print("[WARN] No slider dates found; using simple range of days.")
            date_list = [today + timedelta(days=offset) for offset in range(days_ahead + 1)]

    movie_spans: dict[str, dict] = {}

    for d in date_list:
        # Reuse today's HTML if we already fetched it
        if d == today and today_html is not None:
            html = today_html
        else:
            html = fetch_html_for_date(d)

        if not html:
            continue

        titles = extract_movies_for_date(html)
        if not titles:
            continue

        for title in titles:
            norm = normalize_title(title)
            info = movie_spans.setdefault(
                norm,
                {
                    "display_title": title,
                    "dates": set(),  # set[date]
                },
            )

            # Prefer the more descriptive / longer title
            if len(title) > len(info["display_title"]):
                info["display_title"] = title

            info["dates"].add(d)

    result: list[dict] = []

    for norm, info in movie_spans.items():
        dates_set: set[date] = info["dates"]
        if not dates_set:
            continue

        all_dates = sorted(dates_set)
        first = all_dates[0]
        last = all_dates[-1]

        days_until = (first - date.today()).days
        run_len = len(all_dates)  # number of actual days it plays

        result.append(
            {
                "title": info["display_title"],
                "first_date": first.isoformat(),
                "last_date": last.isoformat(),
                "days_until_start": days_until,
                "run_length_days": run_len,
            }
        )

    # Sort movies by soonest start, then alphabetically
    result.sort(key=lambda x: (x["days_until_start"], x["title"]))
    return result


# ------------ FLASK APP ------------

app = Flask(__name__)


@app.route("/api/showtimes")
def api_showtimes():
    """JSON API: /api/showtimes?days=N"""
    try:
        days = int(request.args.get("days", DEFAULT_DAYS_AHEAD))
    except ValueError:
        days = DEFAULT_DAYS_AHEAD

    # Clamp between 1 and 120 for sanity
    days = max(1, min(days, 120))

    data = build_schedule(days)

    resp = jsonify(
        {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "days_ahead": days,
            "movies": data,
        }
    )
    # Tell browsers NOT to cache this, so phone/desktop both always see fresh data
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/")
def index():
    """Serve the phone-friendly HTML UI."""
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
        Cinemark Signature Stadium Kalispell 14 · auto-scraped every time you refresh
      </div>
      <div class="controls">
        <label>
          Days ahead
          <input id="daysInput" type="number" min="1" max="120" value="60">
        </label>
        <button id="reloadBtn">Refresh</button>
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
        if (!raw) {
          return { daysAhead: 60, sortMode: 'start' };
        }
        const obj = JSON.parse(raw);
        return {
          daysAhead: typeof obj.daysAhead === 'number' ? obj.daysAhead : 60,
          sortMode: obj.sortMode === 'run' ? 'run' : 'start',
        };
      } catch (e) {
        return { daysAhead: 60, sortMode: 'start' };
      }
    }

    function saveSettings() {
      try {
        localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
      } catch (e) {
        // ignore
      }
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
      try {
        localStorage.setItem(HIDDEN_KEY, JSON.stringify(Array.from(hiddenSet)));
      } catch (e) {
        // ignore
      }
    }

    let settings = loadSettings();
    let hiddenSet = loadHidden();
    let cachedMovies = [];

    function applySortModeUI() {
      sortButtons.forEach(btn => {
        const mode = btn.dataset.mode || 'start';
        if (mode === settings.sortMode) {
          btn.classList.add('active-sort');
        } else {
          btn.classList.remove('active-sort');
        }
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
      if (daysUntil === 0) return 'Now playing';
      if (daysUntil === 1) return 'Starts in 1 day';
      if (daysUntil > 1) return `Starts in ${daysUntil} days`;
      const ago = Math.abs(daysUntil);
      if (ago === 1) return 'Started 1 day ago';
      return `Started ${ago} days ago`;
    }

    function humanRunLength(run) {
      if (run === 1) return 'Plays 1 day only';
      return `Plays for ${run} days total`;
    }

    function prettyDate(iso) {
      const d = new Date(iso + 'T12:00:00'); // noon to avoid TZ weirdness
      return d.toLocaleDateString(undefined, {
        weekday: 'short',
        month: 'short',
        day: 'numeric'
      });
    }

    // --- urgency logic ---
    function daysRemaining(movie) {
      // normalize to local midnight for "today"
      const now = new Date();
      const today = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 12, 0, 0);
      const last = new Date(movie.last_date + 'T12:00:00');

      const diffMs = last - today;
      const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
      return diffDays;
    }

    function getUrgency(movie) {
      const remaining = daysRemaining(movie);
      const run = movie.run_length_days;

      // RED: final chance (today or tomorrow only)
      if (remaining <= 1) {
        return 'red';
      }
      // YELLOW: very limited total run OR leaving within ~5 days
      if (run <= 3 || remaining <= 5) {
        return 'yellow';
      }
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
          if (a.run_length_days !== b.run_length_days) {
            return a.run_length_days - b.run_length_days;
          }
          if (a.days_until_start !== b.days_until_start) {
            return a.days_until_start - b.days_until_start;
          }
          return a.title.localeCompare(b.title);
        });
      } else {
        movies.sort((a, b) => {
          if (a.days_until_start !== b.days_until_start) {
            return a.days_until_start - b.days_until_start;
          }
          if (a.run_length_days !== b.run_length_days) {
            return a.run_length_days - b.run_length_days;
          }
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

    async function loadData() {
      const raw = parseInt(daysInput.value || String(settings.daysAhead || 60), 10);
      let days = isNaN(raw) ? 60 : raw;
      days = Math.min(120, Math.max(1, days));
      daysInput.value = String(days);
      settings.daysAhead = days;
      saveSettings();

      statusEl.textContent = `Loading up to ${days} days ahead from the theatre site…`;
      nowGrid.innerHTML = '';
      soonGrid.innerHTML = '';
      laterGrid.innerHTML = '';
      nowEmpty.style.display = 'none';
      soonEmpty.style.display = 'none';
      laterEmpty.style.display = 'none';

      try {
        // cache: 'no-store' to force fresh data each time, plus timestamp busting
        const res = await fetch(`/api/showtimes?days=${days}&t=${Date.now()}`, { cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();

        cachedMovies = data.movies || [];

        statusEl.textContent = `Generated at ${new Date(data.generated_at).toLocaleString()} · window: ${data.days_ahead} days`;

        renderMovies();
      } catch (err) {
        console.error(err);
        statusEl.textContent = 'Error talking to the scraper. Is the Python app running and online?';
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
        const mode = btn.dataset.mode || 'start';
        settings.sortMode = mode;
        saveSettings();
        applySortModeUI();
        statusEl.textContent = mode === 'run'
          ? 'Sorting by shortest runs.'
          : 'Sorting by soonest start.';
        renderMovies(); // just resort cached data, no refetch
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
