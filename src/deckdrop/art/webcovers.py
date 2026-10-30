"""Cover art from the Steam store and SteamGridDB.

Steam needs no key: the store search finds the app id, and the CDN serves the same capsule,
hero and logo the client shows. SteamGridDB is optional and needs a free API key; it is the
only source that has a 920x430 wide image and a transparent logo for non-Steam games.
"""

import json
import re
import time
import urllib.error
import urllib.parse

from ..config import CACHE_DIR, FFMPEG, PNG_SIG, log
from ..detect import clean_title, norm_title
from ..i18n import tr
from ..net import net_open, proxy_url, with_retries
from ..state import STATE
from .ffmpeg import ff_cover

STEAM_SEARCH = "https://store.steampowered.com/api/storesearch"
STEAM_CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps"
GRIDDB = "https://www.steamgriddb.com/api/v2"
CACHE_TTL = 30 * 86400
STEAM_FILES = {"portrait": "library_600x900.jpg", "hero": "library_hero.jpg",
               "logo": "logo.png", "landscape": "header.jpg"}


def _key():
    return (STATE.get("griddb_key") or "").strip()


def _query(name):
    q = clean_title(name or "") or (name or "")
    q = re.sub(r"\b(rus|eng|jpn?|ru|en|jp|russian|english|uncensored|patched|repack|final|full|hd)\b", " ", q, flags=re.I)
    return re.sub(r"\s+", " ", q).strip()


def _json(url, headers=None, timeout=25):
    def go():
        with net_open(url, headers=headers or {}, timeout=timeout, proxy=proxy_url()) as r:
            return json.loads(r.read() or b"{}")
    return with_retries(urllib.parse.urlparse(url).hostname or "web", go)


def _cache_file(source, ident, slot):
    return CACHE_DIR / "art" / source / str(ident) / slot


def _download(url, source, ident, slot, headers=None):
    """Bytes of one asset, kept in ~/.cache for a month; None when the CDN has no such asset."""
    cached = _cache_file(source, ident, slot)
    try:
        if cached.is_file() and time.time() - cached.stat().st_mtime < CACHE_TTL:
            return cached.read_bytes()
    except OSError:
        pass
    try:
        def go():
            with net_open(url, headers=headers or {}, timeout=40, proxy=proxy_url()) as r:
                return r.read()
        data = with_retries(urllib.parse.urlparse(url).hostname or "web", go)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    except OSError as e:
        log(f"art cache: {e}")
    return data


def _ext(data):
    return "png" if data[:8] == PNG_SIG else "jpg"


def steam_search(q):
    """[{"id", "title", "image"}] from the Steam store search."""
    q = (q or "").strip()
    if not q:
        return []
    url = f"{STEAM_SEARCH}?term={urllib.parse.quote(q)}&l=english&cc=us"
    data = _json(url)
    out = []
    for item in data.get("items") or []:
        if not item.get("id") or not item.get("name"):
            continue
        out.append({"id": str(item["id"]), "title": item["name"], "image": item.get("tiny_image")})
    return out


def steam_pick(name):
    """(appid, title) of the exact Steam store match, else (None, None)."""
    q = _query(name)
    if len(norm_title(q)) < 3:
        return None, None
    wanted = norm_title(q)
    for item in steam_search(q):
        if norm_title(item["title"]) == wanted:
            return int(item["id"]), item["title"]
    return None, None


def steam_set(appid):
    """({slot: (bytes, ext)}, info, notes) from the Steam CDN."""
    images, notes = {}, []
    for slot, filename in STEAM_FILES.items():
        data = _download(f"{STEAM_CDN}/{appid}/{filename}", "steam", appid, slot)
        if not data:
            continue
        ext = _ext(data)
        if slot in ("landscape", "hero") and FFMPEG:
            try:
                size = (920, 430) if slot == "landscape" else (1920, 620)
                data, ext = ff_cover(data, ext, *size), "jpg"
            except RuntimeError as e:
                notes.append(f"ffmpeg {slot}: {e}")
        if slot == "logo" and ext != "png":
            notes.append(tr("web.logo_not_png"))
            continue
        images[slot] = (data, ext)
    if not images:
        raise RuntimeError(tr("web.nothing"))
    return images, {"art_source": "steamcdn", "art_id": appid, "art_title": str(appid)}, notes


def griddb_search(q):
    """[{"id", "title", "image"}] from SteamGridDB; needs the key from Settings."""
    key = _key()
    if not key:
        raise ValueError(tr("griddb.no_key"))
    q = (q or "").strip()
    if not q:
        return []
    data = _json(f"{GRIDDB}/search/autocomplete/{urllib.parse.quote(q)}",
                 headers={"Authorization": f"Bearer {key}"})
    out = []
    for item in data.get("data") or []:
        if item.get("id") and item.get("name"):
            out.append({"id": str(item["id"]), "title": item["name"], "image": None})
    return out


def griddb_pick(name):
    q = _query(name)
    if len(norm_title(q)) < 3:
        return None, None
    wanted = norm_title(q)
    for item in griddb_search(q):
        if norm_title(item["title"]) == wanted:
            return int(item["id"]), item["title"]
    return None, None


def _grid(grids, width, height):
    for g in grids or []:
        if g.get("width") == width and g.get("height") == height and g.get("url"):
            if g.get("nsfw") and not STATE.get("vndb_nsfw"):
                continue
            return g["url"]
    return None


def griddb_set(game_id):
    """({slot: (bytes, ext)}, info, notes) from SteamGridDB."""
    key = _key()
    if not key:
        raise ValueError(tr("griddb.no_key"))
    headers = {"Authorization": f"Bearer {key}"}
    images, notes = {}, []
    grids = _json(f"{GRIDDB}/grids/game/{game_id}?dimensions=600x900,920x430", headers=headers).get("data") or []
    specs = {"portrait": _grid(grids, 600, 900), "landscape": _grid(grids, 920, 430)}
    for slot, url in specs.items():
        if not url:
            continue
        data = _download(url, "griddb", game_id, slot, headers)
        if data:
            images[slot] = (data, _ext(data))
    for slot, kind, size, query in (("hero", "heroes", (1920, 620), "?dimensions=1920x620"),
                                    ("logo", "logos", None, "?types=static"),
                                    ("icon", "icons", (256, 256), "")):
        items = _json(f"{GRIDDB}/{kind}/game/{game_id}{query}", headers=headers).get("data") or []
        items = [i for i in items if i.get("url") and not (i.get("nsfw") and not STATE.get("vndb_nsfw"))]
        if not items:
            continue
        data = _download(items[0]["url"], "griddb", game_id, slot, headers)
        if not data:
            continue
        ext = _ext(data)
        if slot == "logo" and ext != "png":
            notes.append(tr("web.logo_not_png"))
            continue
        if slot == "hero" and FFMPEG:
            try:
                data, ext = ff_cover(data, ext, *size), "jpg"
            except RuntimeError as e:
                notes.append(f"ffmpeg hero: {e}")
        if slot == "icon" and FFMPEG:
            try:
                data, ext = ff_cover(data, ext, 256, 256), "jpg"
            except RuntimeError as e:
                notes.append(f"ffmpeg icon: {e}")
        images[slot] = (data, ext)
    if not images:
        raise RuntimeError(tr("web.nothing"))
    return images, {"art_source": "griddb", "art_id": game_id, "art_title": str(game_id)}, notes


def search(source, q):
    if source == "steam":
        return steam_search(q)
    if source == "griddb":
        return griddb_search(q)
    raise ValueError(tr("web.bad_source"))


def pick(source, name):
    if source == "steam":
        return steam_pick(name)
    if source == "griddb":
        return griddb_pick(name)
    raise ValueError(tr("web.bad_source"))


def images_for(source, ident):
    if source == "steam":
        return steam_set(int(ident))
    if source == "griddb":
        return griddb_set(int(ident))
    raise ValueError(tr("web.bad_source"))


def griddb_test():
    key = _key()
    if not key:
        raise ValueError(tr("griddb.no_key"))
    found = griddb_search("portal")
    return {"found": len(found)}
