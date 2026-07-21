"""External metadata lookup from public music APIs.

Three sources, queried by artist/title: Discogs (release styles), MusicBrainz
(recording genres/tags), and Last.fm (track top tags). Only documented JSON API
endpoints are used -- no HTML scraping of any site.

The network fetchers are thin (URL build -> GET JSON) and the parsers are pure
(canned JSON -> normalized ``[{name, count?}]``), so the parsing is unit-tested
without touching the network. routes.py owns the cache + orchestration.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import DISCOGS_TOKEN, LASTFM_KEY, log

# MusicBrainz etiquette: a descriptive User-Agent with contact info, and no more
# than one request per second. Discogs also wants an identifying UA.
USER_AGENT = "Vibedentify/1.0 (+https://github.com/Sif0-Dyas/Vibe_Identify)"
TIMEOUT = 5  # seconds, per source

_REMIX_RE = re.compile(
    r"[\(\[]([^)\]]*\b(?:remix|edit|bootleg|vip|flip|rework|re-?edit|mix|dub)\b[^)\]]*)[\)\]]",
    re.I,
)


def parse_track(payload, title, filename):
    """Best-effort (artist, title, remix) for searching. The metadata tags (often
    under payload['tags']['tag']) win for artist/title; otherwise fall back to an
    'Artist - Title' split of the title/filename. The remix/edit descriptor is
    pulled from bracketed text and stripped from the search title."""
    tag = ((payload or {}).get("tags") or {}).get("tag") or {}
    name = (title or "").strip() or (Path(filename).stem if filename else "")
    artist = (tag.get("artist") or tag.get("albumartist") or "").strip()
    track = (tag.get("title") or "").strip()
    if not artist and " - " in name:
        artist = name.split(" - ", 1)[0].strip()
    if not track:
        track = name.split(" - ", 1)[1].strip() if " - " in name else name.strip()
    remix = ""
    m = _REMIX_RE.search(track)
    if m:
        remix = m.group(1).strip()
    clean = _REMIX_RE.sub("", track).strip(" -_")
    return artist, (clean or track), remix


def configured():
    """Which sources can be queried: Discogs/Last.fm need a key; MusicBrainz doesn't."""
    return {
        "discogs": bool(DISCOGS_TOKEN),
        "musicbrainz": True,
        "lastfm": bool(LASTFM_KEY),
    }


def _errmsg(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, (urllib.error.URLError, TimeoutError, OSError)):
        return "timeout or network error"
    return "lookup failed"


def _get_json(url):
    """GET a JSON document with the app User-Agent and a hard timeout."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # nosec B310  # https, fixed API hosts only
        return json.loads(r.read().decode("utf-8", "replace"))


# --- Discogs ---------------------------------------------------------------
def fetch_discogs(artist, title):
    """Search Discogs releases; returns (raw_json | None, error | None)."""
    if not DISCOGS_TOKEN:
        return None, "no DISCOGS_TOKEN configured"
    params = {
        "type": "release",
        "artist": artist,
        "release_title": title,
        "per_page": 5,
        "token": DISCOGS_TOKEN,
    }
    url = "https://api.discogs.com/database/search?" + urllib.parse.urlencode(
        {k: v for k, v in params.items() if v}
    )
    try:
        return _get_json(url), None
    except Exception as e:  # degrade per-source, never fatal
        log.info("discogs lookup failed: %s", e)
        return None, _errmsg(e)


def parse_discogs(data):
    """Styles (then genres) of the best release match -> [{name}]."""
    results = (data or {}).get("results") or []
    if not results:
        return []
    best = results[0]
    names = list(best.get("style") or []) + list(best.get("genre") or [])
    return _dedupe([{"name": n} for n in names])


# --- MusicBrainz -----------------------------------------------------------
_mb_lock = threading.Lock()
_mb_last = [0.0]  # monotonic timestamp of the last MB request (1 req/s throttle)


def fetch_musicbrainz(artist, title):
    """Search MusicBrainz recordings; returns (raw_json | None, error | None).
    Serialized to <=1 request/second per their rate-limit etiquette."""
    q = f'recording:"{title}"'
    if artist:
        q = f'artist:"{artist}" AND ' + q
    url = "https://musicbrainz.org/ws/2/recording?" + urllib.parse.urlencode(
        {"query": q, "fmt": "json", "limit": 5}
    )
    with _mb_lock:
        wait = 1.0 - (time.monotonic() - _mb_last[0])
        if 0 < wait <= 1.0:
            time.sleep(wait)
        try:
            data = _get_json(url)
            return data, None
        except Exception as e:  # degrade per-source, never fatal
            log.info("musicbrainz lookup failed: %s", e)
            return None, _errmsg(e)
        finally:
            _mb_last[0] = time.monotonic()


def parse_musicbrainz(data):
    """Genres + folksonomy tags of the best recording -> [{name, count}]."""
    recs = (data or {}).get("recordings") or []
    if not recs:
        return []
    best = recs[0]
    rows = (best.get("genres") or []) + (best.get("tags") or [])
    agg = {}
    for x in rows:
        n = (x.get("name") or "").strip()
        if not n:
            continue
        c = x.get("count")
        key = n.lower()
        if key not in agg or (c or 0) > (agg[key].get("count") or 0):
            agg[key] = {"name": n, "count": c}
    return sorted(agg.values(), key=lambda x: -(x.get("count") or 0))


# --- Last.fm ---------------------------------------------------------------
def fetch_lastfm(artist, title):
    """track.getTopTags; returns (raw_json | None, error | None)."""
    if not LASTFM_KEY:
        return None, "no LASTFM_KEY configured"
    params = {
        "method": "track.gettoptags",
        "artist": artist,
        "track": title,
        "api_key": LASTFM_KEY,
        "autocorrect": 1,
        "format": "json",
    }
    url = "https://ws.audioscrobbler.com/2.0/?" + urllib.parse.urlencode(params)
    try:
        data = _get_json(url)
    except Exception as e:  # degrade per-source, never fatal
        log.info("last.fm lookup failed: %s", e)
        return None, _errmsg(e)
    if isinstance(data, dict) and data.get("error"):
        return None, str(data.get("message") or "last.fm error")
    return data, None


def parse_lastfm(data):
    """toptags.tag -> [{name, count}]."""
    tags = ((data or {}).get("toptags") or {}).get("tag") or []
    if isinstance(tags, dict):  # single-tag responses come back un-listed
        tags = [tags]
    out = []
    for t in tags:
        n = (t.get("name") or "").strip()
        if n:
            out.append({"name": n, "count": t.get("count")})
    return out


def _dedupe(rows):
    """Drop case-insensitive duplicate names, preserving first-seen order."""
    seen, out = set(), []
    for r in rows:
        n = (r.get("name") or "").strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append({**r, "name": n})
    return out
