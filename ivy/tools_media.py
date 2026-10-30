# ivy/tools_media.py
"""YouTube and music: find the right thing and play it in one call."""

import json
import re
import time
from typing import Literal
from urllib.parse import quote

import requests

from . import config, senses
from .toolkit import tool
from .tools import _as_str, _osa, _running, default_browser, open_in_browser, resolve_app

_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
# Wikidata asks bots for a descriptive User-Agent and throttles fake browser ones.
_BOT_UA = "IVY-1/1.0 (personal desktop assistant; python-requests)"


# =====================================================================
# YOUTUBE (no API key: reads the results page's embedded JSON)
# =====================================================================

def _walk(node, key):
    if isinstance(node, dict):
        if key in node:
            yield node[key]
        for v in node.values():
            yield from _walk(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)


def _text(field) -> str:
    if not isinstance(field, dict):
        return ""
    return field.get("simpleText") or "".join(r.get("text", "") for r in field.get("runs", []))


def youtube_search(query: str, limit: int = 5) -> list[dict]:
    r = requests.get("https://www.youtube.com/results",
                     params={"search_query": query, "hl": "en", "gl": "US"},
                     headers={"User-Agent": _BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
                     cookies={"CONSENT": "YES+1"}, timeout=10)
    r.raise_for_status()
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", r.text, re.S)
    if not m:
        raise RuntimeError("YouTube changed its page layout")
    videos = []
    for v in _walk(json.loads(m.group(1)), "videoRenderer"):
        if "videoId" not in v:
            continue
        videos.append({
            "id": v["videoId"],
            "title": _text(v.get("title")),
            "channel": _text(v.get("ownerText")),
            "length": _text(v.get("lengthText")) or "live",
            "url": f"https://www.youtube.com/watch?v={v['videoId']}",
        })
        if len(videos) >= limit:
            break
    return videos


@tool(ack="Finding it on YouTube.",
      query="What to watch, e.g. 'gotham chess', 'how to tie a tie', 'lofi radio'",
      pick="Which result to play: 1 = top result")
def play_youtube(query: str, pick: int = 1) -> str:
    """Find a YouTube video and play it in Romeu's browser, in one step. Use for any
    'play/watch/show me X on YouTube' request — don't web_search first."""
    videos = youtube_search(query, limit=max(5, pick))
    if not videos:
        return f"No YouTube results for '{query}'."
    v = videos[min(pick, len(videos)) - 1]
    open_in_browser(v["url"])
    what = f"\"{v['title']}\" by {v['channel']} ({v['length']})"
    browser = default_browser()
    for _ in range(12):
        if v["id"] in senses.browser_tab(browser)[1]:
            return f"Now on YouTube in {browser}: {what}."
        time.sleep(0.25)
    return f"Sent {what} to {browser}, but I couldn't confirm the tab opened."


@tool(group="media")
def search_youtube(query: str) -> str:
    """List the top YouTube results (title, channel, length, URL) without opening any."""
    videos = youtube_search(query)
    return "\n".join(f"{i}. {v['title']} — {v['channel']} ({v['length']}) {v['url']}"
                     for i, v in enumerate(videos, 1)) or "No results."


# =====================================================================
# MUSIC
# =====================================================================

def _spotify_api_uri(query: str) -> tuple[str, str] | None:
    """Exact search via Spotify's Web API (needs free client id/secret in .env)."""
    if not (config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET):
        return None
    tok = requests.post("https://accounts.spotify.com/api/token",
                        data={"grant_type": "client_credentials"},
                        auth=(config.SPOTIFY_CLIENT_ID, config.SPOTIFY_CLIENT_SECRET), timeout=8)
    tok.raise_for_status()
    kinds = "playlist" if "playlist" in query.lower() else "album" if "album" in query.lower() else "artist,track"
    q = re.sub(r"\b(playlist|album)\b", "", query, flags=re.I).strip()
    r = requests.get("https://api.spotify.com/v1/search", params={"q": q, "type": kinds, "limit": 1},
                     headers={"Authorization": f"Bearer {tok.json()['access_token']}"}, timeout=8)
    r.raise_for_status()
    data = r.json()
    artist = (data.get("artists", {}).get("items") or [None])[0]
    if artist and artist["name"].lower() == q.lower():
        return artist["uri"], artist["name"]
    for kind in ("tracks", "playlists", "albums", "artists"):
        item = (data.get(kind, {}).get("items") or [None])[0]
        if item:
            by = ", ".join(a["name"] for a in item.get("artists", []))
            return item["uri"], f"{item['name']}" + (f" by {by}" if by else "")
    return None


def _wikidata_uri(query: str) -> tuple[str, str] | None:
    """Keyless fallback: Wikidata stores Spotify IDs for most artists and albums."""
    s = requests.get("https://www.wikidata.org/w/api.php", headers={"User-Agent": _BOT_UA}, timeout=8,
                     params={"action": "wbsearchentities", "search": query, "language": "en",
                             "format": "json", "limit": 8, "type": "item"}).json()
    ids = [x["id"] for x in s.get("search", [])]
    if not ids:
        return None
    ents = requests.get("https://www.wikidata.org/w/api.php", headers={"User-Agent": _BOT_UA}, timeout=8,
                        params={"action": "wbgetentities", "ids": "|".join(ids), "props": "claims|labels",
                                "languages": "en", "format": "json"}).json()["entities"]
    want = re.sub(r"[^a-z0-9 ]", "", query.lower())
    for qid in ids:
        label = ents[qid].get("labels", {}).get("en", {}).get("value", "")
        norm = re.sub(r"[^a-z0-9 ]", "", label.lower())
        if not norm or not (norm in want or want in norm):
            continue    # a different thing that merely matched the search
        claims = ents[qid].get("claims", {})
        for prop, kind in (("P2207", "track"), ("P2205", "album"), ("P1902", "artist")):
            for c in claims.get(prop, []):
                value = c["mainsnak"].get("datavalue", {}).get("value")
                if value:
                    return f"spotify:{kind}:{value}", label
    return None


_CACHE_PATH = config.ROOT / "music_cache.json"


def _cache() -> dict:
    try:
        return json.loads(_CACHE_PATH.read_text())
    except Exception:
        return {}


def _spotify_lookup(query: str) -> tuple[str, str] | None:
    key = query.lower().strip()
    cached = _cache().get(key)
    if cached:
        return tuple(cached)
    found = _spotify_lookup_uncached(query)
    if found:
        data = _cache()
        data[key] = list(found)
        _CACHE_PATH.write_text(json.dumps(data, indent=1))
    return found


def _mb_get(path: str, **params) -> dict:
    for attempt in range(2):     # MusicBrainz allows ~1 request/second and 503s beyond that
        r = requests.get(f"https://musicbrainz.org/ws/2/{path}", params={"fmt": "json", **params},
                         headers={"User-Agent": _BOT_UA}, timeout=8)
        if r.status_code != 503:
            r.raise_for_status()
            return r.json()
        time.sleep(1.2)
    r.raise_for_status()


def _musicbrainz_artist_uri(name: str) -> tuple[str, str] | None:
    """Second keyless source: MusicBrainz links artists to their Spotify page."""
    artists = _mb_get("artist/", query=f'artist:"{name}"', limit=3).get("artists", [])
    top = next((a for a in artists if a.get("score", 0) >= 90 and a["name"].lower() == name.lower()), None)
    if not top:
        return None
    time.sleep(1.1)
    rels = _mb_get(f"artist/{top['id']}", inc="url-rels").get("relations", [])
    for rel in rels:
        m = re.search(r"open\.spotify\.com/artist/([A-Za-z0-9]{22})", rel.get("url", {}).get("resource", ""))
        if m:
            return f"spotify:artist:{m.group(1)}", top["name"]
    return None


def _spotify_lookup_uncached(query: str) -> tuple[str, str] | None:
    try:
        found = _spotify_api_uri(query)
        if found:
            return found
    except Exception as e:
        print(f"  (Spotify API search failed: {e})")
    title, _, artist = query.partition(" by ")
    # song/album name first, then the whole phrase, then just the artist
    for q in dict.fromkeys(filter(None, [title.strip(), query, artist.strip()])):
        try:
            found = _wikidata_uri(q)
        except Exception:
            found = None
        if found:
            return found
    try:
        return _musicbrainz_artist_uri((artist or query).strip())
    except Exception as e:
        print(f"  (MusicBrainz lookup failed: {e})")
    return None


def _spotify_ready(timeout: float = 12.0):
    """Launch Spotify if needed; it ignores 'play track' until fully started."""
    if _running("Spotify"):
        return
    _osa('tell application "Spotify" to launch')
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if _osa('tell application "Spotify" to return (player state as string)'):
                time.sleep(2.5)
                return
        except Exception:
            pass
        time.sleep(0.5)


def _spotify_play(uri: str) -> str:
    _spotify_ready()
    before = _osa('tell application "Spotify" to return id of current track') if _running("Spotify") else ""
    for _attempt in range(2):
        _osa(f'tell application "Spotify" to play track {_as_str(uri)}')
        t0 = time.time()
        while time.time() - t0 < 5:
            time.sleep(0.5)
            now = _osa('tell application "Spotify" to return id of current track')
            if now != before or uri.startswith("spotify:track:") and now == uri:
                return _osa('tell application "Spotify" to return (name of current track) & " by " & (artist of current track)')
    return ""


def _apple_music_play(query: str) -> str:
    q = _as_str(query)
    script = (f'tell application "Music"\n'
              f'set hits to (every track of library playlist 1 whose name contains {q} or artist contains {q} or album contains {q})\n'
              f'if hits is {{}} then return ""\n'
              f'play item 1 of hits\n'
              f'return (name of current track) & " by " & (artist of current track)\nend tell')
    return _osa(script, timeout=30)


@tool(ack="Putting it on.",
      query="Song, artist, album or playlist, e.g. 'Queen', 'Bohemian Rhapsody by Queen'",
      app="auto = Romeu's usual music app")
def play_music(query: str, app: Literal["auto", "Spotify", "Music", "YouTube"] = "auto") -> str:
    """Find and play music in one step — Spotify, Apple Music, or YouTube."""
    if app == "auto":
        app = config.MUSIC_APP if resolve_app(config.MUSIC_APP) else "YouTube"
    if app == "Spotify":
        found = _spotify_lookup(query)
        if found:
            playing = _spotify_play(found[0])
            if playing:
                return f"Playing {playing} on Spotify."
        _osa(f'open location {_as_str("spotify:search:" + quote(query))}')
        hint = "" if config.SPOTIFY_CLIENT_ID else (
            " (Exact song search needs a free Spotify API key: SPOTIFY_CLIENT_ID and "
            "SPOTIFY_CLIENT_SECRET in .env.)")
        return f"Couldn't pin down '{query}' automatically, so I opened Spotify's search for it.{hint}"
    if app == "Music":
        playing = _apple_music_play(query)
        return f"Playing {playing} in Apple Music." if playing else f"'{query}' isn't in the Apple Music library."
    videos = youtube_search(query + " official audio")
    if not videos:
        return f"Nothing found for '{query}'."
    open_in_browser(videos[0]["url"])
    return f"Playing \"{videos[0]['title']}\" on YouTube."
