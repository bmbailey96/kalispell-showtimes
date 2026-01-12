import re
import cloudscraper
from datetime import date, datetime, timedelta
from typing import Optional, Dict, List, Tuple

from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, render_template

# Python 3.9+ has zoneinfo built in
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

# ------------ CONFIG ------------

TRIBUTE_THEATRE_URL = (
    "https://www.tributemovies.com/cinema/Montana/Kalispell/"
    "Cinemark-Signature-Stadium-Kalispell-14/10338/"
)

DEFAULT_DAYS_AHEAD = 60

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.google.com/",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

CACHE_TTL_SECONDS = 180

DATE_LABEL_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s([A-Za-z]{3})\s(\d{1,2}):$")
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12
}

MOUNTAIN_TZ = ZoneInfo("America/Denver") if ZoneInfo else None


def now_local() -> datetime:
    if MOUNTAIN_TZ:
        return datetime.now(MOUNTAIN_TZ)
    return datetime.now()


def normalize_title(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t


# ------------ SCRAPER ------------

def _guess_year(today: date, month: int, day_num: int) -> int:
    candidates = []
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(y, month, day_num))
        except ValueError:
            pass

    if not candidates:
        return today.year

    def score(d: date) -> Tuple[int, int]:
        delta = (d - today).days
        in_window = 0 if (-60 <= delta <= 300) else 1
        return (in_window, abs(delta))

    best = min(candidates, key=score)
    return best.year


def fetch_tribute_html() -> Tuple[Optional[str], Optional[str], Optional[int], Optional[str]]:
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )

    last_err = None
    last_status = None
    last_url = None

    for _attempt in range(1, 4):
        try:
            resp = scraper.get(
                TRIBUTE_THEATRE_URL,
                headers=HEADERS,
                timeout=25,
                allow_redirects=True,
            )
            last_status = resp.status_code
            last_url = resp.url

            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
                continue

            text = resp.text or ""
            if len(text) < 1000:
                last_err = f"HTML too short (len={len(text)}) likely blocked"
                continue

            return text, None, resp.status_code, resp.url

        except Exception as e:
            last_err = f"ScraperError: {str(e)}"

    return None, (last_err or "Unknown fetch failure"), last_status, last_url


def parse_tribute_schedule(html: str) -> Dict[str, Dict]:
    soup = BeautifulSoup(html, "html.parser")
    today = date.today()
    movie_spans: Dict[str, Dict] = {}

    headers = soup.select("h2.media-heading") or soup.select(".media-body h2")
    if not headers:
        return {}

    for h2 in headers:
        title = h2.get_text(" ", strip=True)
        if not title or len(title) < 2:
            continue

        lowered = title.lower()
        if lowered in ("regular showtimes", "showtimes", "coming soon"):
            continue

        norm = normalize_title(title)

        media_div = h2.find_parent("div", class_="media")
        ticket_div = None

        if media_div:
            sib = media_div
            for _ in range(0, 8):
                sib = sib.find_next_sibling()
                if sib is None:
                    break
                if getattr(sib, "name", None) == "div" and "ticketicons" in (sib.get("class") or []):
                    ticket_div = sib
                    break

        if ticket_div:
            info = movie_spans.setdefault(norm, {"display_title": title, "dates": set()})
            for b in ticket_div.find_all("b"):
                raw = b.get_text(" ", strip=True)
                m = DATE_LABEL_RE.match(raw)
                if not m:
                    continue

                month = MONTHS.get(m.group(2))
                day_num = int(m.group(3))
                if not month:
                    continue

                year = _guess_year(today, month, day_num)
                try:
                    info["dates"].add(date(year, month, day_num))
                except ValueError:
                    continue

    return movie_spans


def build_schedule(days_ahead: int, spans: Dict[str, Dict]) -> List[dict]:
    today = date.today()
    horizon = today + timedelta(days=days_ahead)
    result = []

    for _norm, info in spans.items():
        dates_set = info.get("dates") or set()
        lower = today - timedelta(days=7)

        # Keep only dates near our window, then sort
        filtered = sorted([d for d in dates_set if lower <= d <= horizon])
        if not filtered:
            continue

        first, last = filtered[0], filtered[-1]
        result.append({
            "title": info.get("display_title") or "Untitled",
            "first_date": first.isoformat(),
            "last_date": last.isoformat(),
            "dates": [d.isoformat() for d in filtered],  # <-- THIS IS THE FIX
            "days_until_start": (first - today).days,
            "run_length_days": len(filtered),            # actual number of listed dates
        })

    result.sort(key=lambda x: (x["days_until_start"], x["run_length_days"], x["title"]))
    return result


# ------------ APP LOGIC ------------

_cache = {
    "fetched_at": None,
    "spans": None,
    "fetch_error": None,
    "fetch_status": None,
    "fetch_url": None,
    "html_len": None,
}


def get_cached_spans() -> Tuple[Optional[Dict[str, Dict]], Optional[str]]:
    now = now_local()

    if _cache["fetched_at"] and _cache["spans"]:
        age = (now - _cache["fetched_at"]).total_seconds()
        if age < CACHE_TTL_SECONDS:
            return _cache["spans"], None

    html, err, status, final_url = fetch_tribute_html()
    _cache.update({
        "fetched_at": now,
        "fetch_error": err,
        "fetch_status": status,
        "fetch_url": final_url,
        "html_len": len(html) if html else 0,
    })

    if not html:
        return None, (err or "Fetch failed")

    spans = parse_tribute_schedule(html)
    _cache["spans"] = spans
    return spans, None


app = Flask(__name__)


@app.route("/api/debug_raw")
def api_debug_raw():
    html, err, status, final_url = fetch_tribute_html()
    return jsonify({
        "ok": bool(html),
        "error": err,
        "status": status,
        "final_url": final_url,
        "len": len(html) if html else 0,
        "has_media_heading": ("media-heading" in html if html else False),
        "sample_head": html[:1000] if html else None
    })


@app.route("/api/showtimes")
def api_showtimes():
    try:
        days = int(request.args.get("days", DEFAULT_DAYS_AHEAD))
    except Exception:
        days = DEFAULT_DAYS_AHEAD

    days = max(1, min(days, 365))

    spans, err = get_cached_spans()
    if err or not spans:
        return jsonify({
            "generated_at": now_local().isoformat(),
            "movies": [],
            "error": err,
            "debug": _cache
        }), 503

    return jsonify({
        "generated_at": now_local().isoformat(),
        "days_ahead": days,
        "movies": build_schedule(days, spans),
        "source": "TributeMovies"
    })


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
