"""Media gallery: Steam screenshots and recordings."""

import hashlib
import os
import re
import shutil
import subprocess
import time
import zipfile

from .config import CACHE_DIR, CHUNK, FFMPEG, IMAGE_EXTS, MEDIA_DIRS, VIDEO_EXTS, log
from .i18n import tr
from .paths import safe_name
from .steam.library import _SC_CACHE, library_folders, shortcuts_index, userdata_dirs


_MEDIA = {"at": 0, "items": [], "by_id": {}}
_APP_NAMES = {}


def app_name(appid):
    if appid in _APP_NAMES:
        return _APP_NAMES[appid]
    name = None
    for lib in library_folders():
        acf = lib / "steamapps" / f"appmanifest_{appid}.acf"
        if acf.is_file():
            m = re.search(r'"name"\s+"([^"]*)"', acf.read_text("utf-8", "replace"))
            name = m.group(1) if m else None
            break
    if not name and appid.isdigit():
        shortcuts_index()
        rec = _SC_CACHE["by_appid"].get(int(appid))
        name = rec["name"] if rec else None
    _APP_NAMES[appid] = name or f"app {appid}"
    return _APP_NAMES[appid]


def media_id(path):
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]


def clip_time(name, fallback):
    m = re.search(r"_(\d{8})_(\d{6})", name)
    if m:
        try:
            return int(time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")))
        except ValueError:
            pass
    return int(fallback)


def scan_media(force=False):
    if not force and time.time() - _MEDIA["at"] < 10:
        return _MEDIA["items"]
    items = []
    for ud in userdata_dirs():
        remote = ud / "760" / "remote"
        if remote.is_dir():
            for appdir in remote.iterdir():
                shots = appdir / "screenshots"
                if not shots.is_dir():
                    continue
                game = app_name(appdir.name)
                for f in shots.iterdir():
                    if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                        thumb = shots / "thumbnails" / f.name
                        st = f.stat()
                        items.append({"kind": "image", "path": f, "thumb": thumb if thumb.is_file() else None,
                                      "name": f.name, "game": game, "time": int(st.st_mtime), "size": st.st_size})
        for sub in ("clips", "video"):
            base = ud / "gamerecordings" / sub
            if not base.is_dir():
                continue
            for clip in base.iterdir():
                if not clip.is_dir():
                    continue
                m = re.match(r"(?:clip|bg)_(\d+)_", clip.name)
                game = app_name(m.group(1)) if m else clip.name
                thumb = clip / "thumbnail.jpg"
                size = sum(f.stat().st_size for f in clip.rglob("*.m4s"))
                items.append({"kind": "clip", "path": clip, "thumb": thumb if thumb.is_file() else None,
                              "name": clip.name, "game": game,
                              "time": clip_time(clip.name, clip.stat().st_mtime), "size": size})
    for d in MEDIA_DIRS:
        if not d.is_dir():
            continue
        for f in d.rglob("*"):
            if len(f.relative_to(d).parts) > 3 or not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext in IMAGE_EXTS or ext in VIDEO_EXTS:
                st = f.stat()
                items.append({"kind": "image" if ext in IMAGE_EXTS else "video", "path": f, "thumb": None,
                              "name": f.name, "game": f.parent.name if f.parent != d else d.name,
                              "time": int(st.st_mtime), "size": st.st_size})
    items.sort(key=lambda i: i["time"], reverse=True)
    for it in items:
        it["id"] = media_id(it["path"])
    _MEDIA.update(at=time.time(), items=items, by_id={i["id"]: i for i in items})
    return items


def media_item(mid):
    it = _MEDIA["by_id"].get(mid)
    if it is None:
        scan_media(force=True)
        it = _MEDIA["by_id"].get(mid)
    if it is None:
        raise FileNotFoundError(mid)
    return it


def _concat(files, out):
    with open(out, "wb") as dst:
        for f in files:
            with open(f, "rb") as src:
                shutil.copyfileobj(src, dst, CHUNK)


def clip_mp4(item):
    """Assemble a Steam recording (fragmented m4s chunks) into one mp4, cached."""
    clip = item["path"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"{item['id']}.mp4"
    if out.is_file() and out.stat().st_mtime >= clip.stat().st_mtime and out.stat().st_size > 0:
        return out
    inits = sorted(clip.rglob("init-stream0.m4s"))
    if not inits:
        raise FileNotFoundError(tr("media.clip_empty"))
    vdir = inits[0].parent

    def stream(n):
        init = vdir / f"init-stream{n}.m4s"
        chunks = sorted(vdir.glob(f"chunk-stream{n}-*.m4s"),
                        key=lambda p: int(re.findall(r"(\d+)\.m4s$", p.name)[0]))
        return [init] + chunks if init.is_file() and chunks else None

    video, audio = stream(0), stream(1)
    if not video:
        raise FileNotFoundError(tr("media.clip_empty"))
    tmp_v = out.with_suffix(".v.mp4")
    _concat(video, tmp_v)
    if audio and FFMPEG:
        tmp_a = out.with_suffix(".a.mp4")
        _concat(audio, tmp_a)
        res = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(tmp_v), "-i", str(tmp_a),
                              "-c", "copy", "-movflags", "+faststart", str(out)],
                             capture_output=True, text=True, errors="replace", timeout=600)
        tmp_a.unlink(missing_ok=True)
        if res.returncode == 0:
            tmp_v.unlink(missing_ok=True)
            return out
        log(f"ffmpeg mux failed, serving video only: {res.stderr.strip()[-200:]}")
    os.replace(tmp_v, out)
    return out


PLACEHOLDER_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 90">'
                   b'<rect width="160" height="90" fill="#2a475e"/>'
                   b'<polygon points="65,28 65,62 98,45" fill="#66c0f4"/></svg>')


def media_thumb(item):
    """Path to a thumbnail (Steam's, cached ffmpeg frame, or the image itself); None -> placeholder."""
    if item["thumb"]:
        return item["thumb"]
    if item["kind"] == "image":
        return item["path"]
    if FFMPEG:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        th = CACHE_DIR / f"{item['id']}_thumb.jpg"
        if th.is_file():
            return th
        src = clip_mp4(item) if item["kind"] == "clip" else item["path"]
        res = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", "1", "-i", str(src),
                              "-frames:v", "1", "-vf", "scale=320:-2", str(th)],
                             capture_output=True, text=True, errors="replace", timeout=120)
        if res.returncode == 0 and th.is_file():
            return th
    return None


def media_delete(mid):
    item = media_item(mid)
    p = item["path"]
    if item["kind"] == "clip":
        shutil.rmtree(p)
    else:
        p.unlink()
        if item["thumb"] and item["thumb"].is_file():
            item["thumb"].unlink()
    for f in CACHE_DIR.glob(f"{mid}*"):
        try:
            f.unlink()
        except OSError:
            pass
    scan_media(force=True)
    return item["name"]


def media_delete_many(ids):
    """Delete several items; returns (names removed, errors "name: reason")."""
    removed, errors = [], []
    for mid in dict.fromkeys(ids):
        try:
            removed.append(media_delete(mid))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{_MEDIA['by_id'].get(mid, {}).get('name', mid)}: {e}")
    return removed, errors


def media_zip_names(items):
    """Name inside the zip for each item: <game>/<file>, Steam clips as .mp4, never twice the same."""
    seen, names = set(), []
    for it in items:
        name = it["path"].name + (".mp4" if it["kind"] == "clip" else "")
        base, ext = os.path.splitext(name)
        cand, n = f"{safe_name(it['game'])}/{name}", 1
        while cand.lower() in seen:
            n += 1
            cand = f"{safe_name(it['game'])}/{base} ({n}){ext}"
        seen.add(cand.lower())
        names.append(cand)
    return names


def media_zip(ids, out):
    """Write the items as a zip into the stream `out` (it need not be seekable).

    Stored, not compressed: photos and videos are compressed already, and the Deck starts sending
    at once instead of making the phone wait."""
    items = [media_item(mid) for mid in dict.fromkeys(ids)]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for it, name in zip(items, media_zip_names(items)):
            src = clip_mp4(it) if it["kind"] == "clip" else it["path"]
            info = zipfile.ZipInfo(name, time.localtime(it["time"])[:6])
            with open(src, "rb") as f, z.open(info, "w", force_zip64=True) as dst:
                shutil.copyfileobj(f, dst, CHUNK)
