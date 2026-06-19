import requests
from bs4 import BeautifulSoup
from urllib.parse import quote, urlparse
import json
import logging
import re
import os
import hashlib

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dataclasses import dataclass, field
from .utils import FreetarError

LOGGER = logging.getLogger(__name__)
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

LOCAL_MODE = os.environ.get("FREETAR_LOCAL", "").lower() in ("1", "true", "yes", "on")
DISK_CACHE = os.environ.get("FREETAR_DISK_CACHE", "").lower() in ("1", "true", "yes", "on")
DISK_CACHE_DIR = Path(os.environ.get("FREETAR_DISK_CACHE_DIR", "~/.cache/freetar")).expanduser()


def configure_ug(local: bool = None, disk_cache: bool = None, disk_cache_dir: str = None):
    global LOCAL_MODE, DISK_CACHE, DISK_CACHE_DIR
    if local is not None:
        LOCAL_MODE = local
    if disk_cache is not None:
        DISK_CACHE = disk_cache
    if disk_cache_dir:
        DISK_CACHE_DIR = Path(disk_cache_dir).expanduser()


def _cache_key(mode: str, kind: str, url: str, params: dict = None) -> str:
    payload = json.dumps({
        "mode": mode,
        "kind": kind,
        "url": url,
        "params": params or {},
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _cache_paths(cache_key: str) -> tuple[Path, Path]:
    return DISK_CACHE_DIR / f"{cache_key}.txt", DISK_CACHE_DIR / f"{cache_key}.json"


def _read_disk_cache(cache_key: str) -> Optional[str]:
    raw_path, meta_path = _cache_paths(cache_key)
    if not raw_path.exists() or not meta_path.exists():
        return None
    return raw_path.read_text()


def _write_disk_cache(cache_key: str, text: str, metadata: dict):
    DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    raw_path, meta_path = _cache_paths(cache_key)
    raw_path.write_text(text)
    meta_path.write_text(json.dumps(metadata, indent=2, sort_keys=True))


def _normalise_tab_path(tab_path: str) -> str:
    path = urlparse(str(tab_path)).path
    if path.startswith("/tab/"):
        path = path[len("/tab/"):]
    return path.strip("/")


def clear_disk_cache(keep_tab_paths: list[str] = None) -> dict:
    if not DISK_CACHE_DIR.exists():
        return {"deleted": 0, "kept": 0}

    keep = {_normalise_tab_path(path) for path in (keep_tab_paths or [])}
    deleted = 0
    kept = 0
    for meta_path in DISK_CACHE_DIR.glob("*.json"):
        raw_path = meta_path.with_suffix(".txt")
        try:
            metadata = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            metadata = {}

        should_keep = (
            metadata.get("kind") == "tab"
            and _normalise_tab_path(metadata.get("tab_path", "")) in keep
        )
        if should_keep:
            kept += 1
            continue

        for path in (meta_path, raw_path):
            try:
                path.unlink()
                deleted += 1
            except FileNotFoundError:
                pass

    return {"deleted": deleted, "kept": kept}


def _log_fetch_failure(kind: str, url: str, response: requests.Response = None):
    mode = "local" if LOCAL_MODE else "proxy"
    status_code = response.status_code if response is not None else None
    final_url = response.url if response is not None else url
    redirect_target = None
    if response is not None:
        redirect_target = response.headers.get("Location")
        if not redirect_target and response.url != url:
            redirect_target = response.url
    snippet = ""
    if response is not None and response.text:
        snippet = response.text[:300].replace("\n", " ")

    LOGGER.warning(
        "Ultimate Guitar fetch failed mode=%s kind=%s url=%s status=%s redirect=%s snippet=%r",
        mode,
        kind,
        final_url,
        status_code,
        redirect_target,
        snippet,
    )


def _fetch_upstream(kind: str, url: str, params: dict = None, metadata: dict = None) -> str:
    mode = "local" if LOCAL_MODE else "proxy"
    cache_key = _cache_key(mode, kind, url, params)
    if DISK_CACHE:
        cached = _read_disk_cache(cache_key)
        if cached is not None:
            LOGGER.info("Using disk cache for %s %s", kind, url)
            return cached

    response = None
    try:
        response = SESSION.get(url, params=params, timeout=20)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        _log_fetch_failure(kind, url, response)
        raise e

    if DISK_CACHE:
        request_metadata = {
            "created": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "mode": mode,
            "original_url": response.url,
        }
        request_metadata.update(metadata or {})
        _write_disk_cache(cache_key, response.text, request_metadata)

    return response.text


def _parse_store_data(html: str) -> dict:
    bs = BeautifulSoup(html, 'html.parser')
    data = bs.find("div", {"class": "js-store"})
    return json.loads(data.attrs['data-content'])


def _search_url(value: str, page: int) -> tuple[str, Optional[dict]]:
    if LOCAL_MODE:
        return "https://www.ultimate-guitar.com/search.php", {
            "page": page,
            "search_type": "title",
            "value": value,
        }
    return f"https://proxy.freetar.de/search.php?page={page}&search_type=title&value={quote(value)}", None


def _tab_url(url_path: str) -> str:
    base_url = "https://tabs.ultimate-guitar.com/tab/" if LOCAL_MODE else "https://tabs.proxy.freetar.de/tab/"
    return base_url + str(url_path)


@dataclass
class SearchResult:
    artist_name: str
    song_name: str
    tab_url: str
    artist_url: str
    _type: str
    version: int
    votes: int
    rating: float

    def __init__(self, data: str):
        self.artist_name = data["artist_name"]
        self.song_name = data["song_name"]
        self.tab_url = urlparse(data["tab_url"]).path
        self.artist_url = data["artist_url"]
        self._type = data["type"]
        self.version = data["version"]
        self.votes = int(data["votes"])
        self.rating = round(data["rating"], 1)

    def __repr__(self):
        return f"{self.artist_name} - {self.song_name} (ver {self.version}) ({self._type} {self.rating}/5 - {self.votes} votes)"


@dataclass
class SongDetail:
    tab: str
    artist_name: str
    song_name: str
    version: int
    difficulty: str
    capo: str
    tuning: str
    tab_url: str
    alternatives: list[SearchResult] = field(default_factory=list)

    def __init__(self, data: dict):
        self.tab = data["store"]["page"]["data"]["tab_view"]["wiki_tab"]["content"]
        self.artist_name = data["store"]["page"]["data"]["tab"]['artist_name']
        self.song_name = data["store"]["page"]["data"]["tab"]["song_name"]
        self.version = int(data["store"]["page"]["data"]["tab"]["version"])
        self._type = data["store"]["page"]["data"]["tab"]["type"]
        self.rating = int(data["store"]["page"]["data"]["tab"]["rating"])
        self.difficulty = data["store"]["page"]["data"]["tab_view"]["ug_difficulty"]
        self.appliciture = data["store"]["page"]["data"]["tab_view"]["applicature"]
        self.chords = []
        self.fingers_for_strings = []
        if type(data["store"]["page"]["data"]["tab_view"]["meta"]) is dict:
            self.capo = data["store"]["page"]["data"]["tab_view"]["meta"].get("capo")
            _tuning = data["store"]["page"]["data"]["tab_view"]["meta"].get("tuning")
            self.tuning = f"{_tuning['value']} ({_tuning['name']})" if _tuning else None
        self.tab_url = data["store"]["page"]["data"]["tab"]["tab_url"]
        self.alternatives = []
        for alternative in data["store"]["page"]["data"]["tab_view"]["versions"]:
            if alternative.get("type", "") != "Official":
                self.alternatives.append(SearchResult(alternative))
        self.fix_tab()

    def __repr__(self):
        return f"{self.artist_name} - {self.song_name}"

    def fix_tab(self):
        tab = self.tab
        tab = tab.replace("\r\n", "<br/>")
        tab = tab.replace("\n", "<br/>")
        tab = tab.replace(" ", "&nbsp;")
        tab = tab.replace("[tab]", "")
        tab = tab.replace("[/tab]", "")

        # (?P<root>[A-Ha-h](#|b)?) : Chord root is any letter A - H with an optional sharp or flat at the end
        # (?P<quality>[^[/]+)?  : Chord quality is anything after the root, but before the `/` for the base note
        # (?P<bass>/[A-Ha-h](#|b)?)? :  Chord quality is anything after the root, including parens in the case of 'm(maj7)'
        # tab = re.sub(r'\[ch\](?P<root>[A-Ga-g](#|b)?)(?P<quality>[#\w()]+)?(?P<bass>/[A-Ga-g](#|b)?)?\[\/ch\]', self.parse_chord, tab)
        tab = re.sub(r'\[ch\](?P<root>[A-Ha-h](#|b)?)(?P<quality>[^[/]+)?(?P<bass>/[A-Ha-h](#|b)?)?\[\/ch\]', self.parse_chord, tab)
        self.tab = tab

    def parse_chord(self, chord):
        root = '<span class="chord-root">%s</span>' % chord.group('root')
        quality = ''
        bass = ''
        if chord.group('quality') is not None:
            quality = '<span class="chord-quality">%s</span>' % chord.group('quality')
        if chord.group('bass') is not None:
            bass = '/<span class="chord-bass">%s</span>' % chord.group('bass')[1:]
        return '<span class="chord fw-bold">%s</span>' % (root + quality + bass)


@dataclass
class Search:
    results: dict
    total_pages: int
    current_page: int

    def __init__(self, value: str, page: int):
        try:
            url, params = _search_url(value, page)
            html = _fetch_upstream("search", url, params=params, metadata={
                "search_term": value,
                "page": page,
            })
            data = _parse_store_data(html)
            self.results = self.get_results(data)
            self.total_pages = data['store']['page']['data']['pagination']['total']
            self.current_page = data['store']['page']['data']['pagination']['current']
            #print(json.dumps(data, indent=4))
        except requests.exceptions.RequestException:
            # don't print full URL here, in case of 404
            raise FreetarError(f"Could not find any chords for '{value}'.")
        except (KeyError, ValueError, AttributeError) as e:
            raise FreetarError(f"Could not search for chords: {e}") from e

    def get_results(self, data: object):
        results = data['store']['page']['data']['results']
        ug_results = []
        for result in results:
            _type = result.get("type")
            if _type and _type not in ("Pro", "Official"):
                s = SearchResult(result)
                ug_results.append(s)
        return ug_results


def get_chords(s: SongDetail) -> SongDetail:
    if s.appliciture is None:
        return dict(), dict()

    chords = {}
    fingerings = {}

    for chord in s.appliciture:
        for chord_variant in s.appliciture[chord]:
            frets = chord_variant["frets"]
            min_fret = min(frets)
            max_fret = max(frets)
            possible_frets = list(range(min_fret, max_fret+1))
            variants_temp = {
                possible_fret: [1 if b == possible_fret else 0 for b in frets][::-1]
                for possible_fret
                in possible_frets
                if possible_fret > 0
            }

            variants = dict()
            found = False
            for fret, fingers in variants_temp.items():
                try:
                    if not found and fingers.index(1) >= 0:
                        found = True
                except ValueError:
                    ...

                if found:
                    variants[fret] = fingers

            if not len(variants):
                continue
            while len(variants) < 6:
                variants[max(variants) + 1] = [0] * 6

            variant_strings_pressed = [*variants.values()]
            variant_strings_pressed = [sum(x) for x in zip(*variant_strings_pressed)]
            unstrummed_strings = [int(not bool(y)) for y in variant_strings_pressed]

            fingering_for_variant = []
            for finger, x in zip(chord_variant["fingers"][::-1], unstrummed_strings):
                fingering_for_variant.append("x" if x else finger)
            fingering_for_variant = fingering_for_variant

            if chord not in chords:
                chords[chord] = []
                fingerings[chord] = []
            chords[chord].append(variants)
            fingerings[chord].append(fingering_for_variant)

    return chords, fingerings


def ug_tab(url_path: str):
    try:
        html = _fetch_upstream("tab", _tab_url(url_path), metadata={
            "tab_path": _normalise_tab_path(url_path),
        })
        data = _parse_store_data(html)
        s = SongDetail(data)
        s.chords, s.fingers_for_strings = get_chords(s)
        return s
    except (KeyError, ValueError, AttributeError, requests.exceptions.RequestException) as e:
        raise FreetarError(f"Could not parse chord: {e}") from e
