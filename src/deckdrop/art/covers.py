"""Steam cover art: built from the exe icon or taken from VNDB, applied to a shortcut."""

import json
import re
import time
import urllib.error
import urllib.parse
from pathlib import Path

from ..art.ffmpeg import ff_cover, ff_fit_blur, ff_logo, ff_run
from ..art.images import compose, dominant_color, load_icon, png_encode
from ..config import FFMPEG, PNG_SIG, VNDB_UA, log
from ..detect import clean_title, game_exe_path, norm_title, pretty_name
from ..net import net_open, net_reason, proxy_url, with_retries
from ..state import STATE, added_rec, update_added
from ..steam.cdp import CDP
from ..steam.library import pick_userdata, shortcut_appid, shortcuts_index, userdata_dirs
from ..steam.shortcuts import resolve_appid


# SteamClient.Apps.SetCustomArtworkForApp knows four artwork slots only. There is no type for the
# shortcut icon: sending one used to land in the landscape slot. The icon goes through SetShortcutIcon.
ASSET_TYPE = {"portrait": 0, "hero": 1, "logo": 2, "landscape": 3}
GRID_SUFFIX = {"portrait": "p", "landscape": "", "hero": "_hero", "logo": "_logo", "icon": "_icon"}
SIZES = {"portrait": (600, 900), "landscape": (920, 430), "hero": (1920, 620)}


def capsule_png(icon, cw, ch, frac):
    iw, ih, rgba = icon
    bg = dominant_color(iw, ih, rgba)
    if FFMPEG:
        tw, th = int(cw * frac), int(ch * frac)
        fc = (f"color=c=0x{bg[0]:02x}{bg[1]:02x}{bg[2]:02x}:s={cw}x{ch}:d=1[bg];"
              f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos[ic];"
              f"[bg][ic]overlay=(W-w)/2:(H-h)/2:shortest=1,format=rgb24")
        try:
            return ff_run(png_encode(iw, ih, rgba), "png", ["-filter_complex", fc, "-frames:v", "1"], "png")
        except RuntimeError as e:
            log(f"ffmpeg capsule failed, falling back: {e}")
    return png_encode(cw, ch, compose(cw, ch, iw, ih, rgba, bg, frac))


def vndb_fetch(url, timeout=40):
    """Download a VNDB image through DeckDrop's proxy setting, with retries."""
    def go():
        with net_open(url, headers={"User-Agent": VNDB_UA}, timeout=timeout, proxy=proxy_url()) as r:
            return r.read()
    return with_retries(urllib.parse.urlparse(url).hostname or "VNDB", go)


def vndb_query(filters, results=6):
    body = {"filters": filters, "results": results,
            "fields": "title, alttitle, released, image.url, image.dims, image.sexual, image.violence, "
                      "screenshots.url, screenshots.dims, screenshots.sexual, screenshots.violence"}
    if filters and filters[0] == "search":
        body["sort"] = "searchrank"

    def go():
        with net_open("https://api.vndb.org/kana/vn", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", "User-Agent": VNDB_UA},
                      timeout=25, proxy=proxy_url(), method="POST") as r:
            return json.loads(r.read()).get("results") or []
    return with_retries("api.vndb.org", go)


def proxy_test():
    """Reachability of the VNDB hosts, directly and through the configured proxy."""
    px = proxy_url()
    checks = []
    for host, url in (("api.vndb.org", "https://api.vndb.org/kana/schema"), ("t.vndb.org", "https://t.vndb.org/")):
        for mode, p in [("напрямую", None)] + ([("через прокси", px)] if px else []):
            t0 = time.time()
            try:
                with net_open(url, headers={"User-Agent": VNDB_UA}, timeout=12, proxy=p) as r:
                    r.read(200)
                checks.append({"host": host, "mode": mode, "ok": True, "ms": int((time.time() - t0) * 1000)})
            except urllib.error.HTTPError as e:      # an HTTP answer still means we got through
                checks.append({"host": host, "mode": mode, "ok": True, "ms": int((time.time() - t0) * 1000),
                               "note": f"ответ HTTP {e.code}"})
            except Exception as e:  # noqa: BLE001
                checks.append({"host": host, "mode": mode, "ok": False, "error": net_reason(e)})
    return {"proxy": px or "", "checks": checks}


def vndb_search(q):
    q = re.sub(r"\b(rus|eng|jpn?|ru|en|jp|russian|english|uncensored|patched|repack|final|full|remake|hd|dl|steam)\b", " ", q, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip()
    return [{"id": v["id"], "title": v.get("title"), "alttitle": v.get("alttitle"), "released": v.get("released"),
             "image": (v.get("image") or {}).get("url"), "sexual": (v.get("image") or {}).get("sexual", 0)}
            for v in vndb_query(["search", "=", q])] if q else []


def vndb_pick(name, vn_id=None):
    """VNDB entry for a game: by id, or by name when the top hit matches the query well enough."""
    if vn_id:
        res = vndb_query(["id", "=", vn_id], 1)
        if not res:
            raise RuntimeError(f"VNDB: {vn_id} не найден")
        return res[0]
    q = re.sub(r"\b(rus|eng|jpn?|ru|en|jp|russian|english|uncensored|patched|repack|final|full|hd)\b", " ", name, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip()
    if len(norm_title(q)) < 3:
        return None
    nq = norm_title(q)
    for v in vndb_query(["search", "=", q], 5):
        for t in (v.get("title") or "", v.get("alttitle") or ""):
            nt = norm_title(t)
            if nt and (nt == nq or (len(nq) >= 5 and (nq in nt or nt in nq))):
                return v
    return None


def vndb_allowed(img):
    limit = 3.0 if STATE.get("vndb_nsfw") else 1.4
    return img and img.get("url") and float(img.get("sexual") or 0) <= limit and float(img.get("violence") or 0) <= limit


def vndb_images(vn, have_icon):
    """{slot: (bytes, ext)} from a VNDB entry, honoring the NSFW setting."""
    imgs, notes = {}, []
    cover = vn.get("image") or {}
    shots = [s for s in (vn.get("screenshots") or []) if vndb_allowed(s)]
    shots.sort(key=lambda s: -(s.get("dims") or [0, 0])[0])
    cov = None
    if vndb_allowed(cover):
        cov = vndb_fetch(cover["url"])
    elif cover.get("url"):
        notes.append("обложка VNDB отфильтрована как NSFW")
    shot = None
    if shots:
        shot = vndb_fetch(shots[0]["url"])
    if FFMPEG:
        if cov:
            imgs["portrait"] = (ff_fit_blur(cov, "jpg", 600, 900), "jpg")
        wide = shot or cov
        if wide:
            imgs["landscape"] = (ff_cover(wide, "jpg", 920, 430), "jpg")
            imgs["hero"] = (ff_cover(wide, "jpg", 1920, 620), "jpg")
        if cov and not have_icon:
            imgs["icon"] = (ff_cover(cov, "jpg", 256, 256), "jpg")
        try:
            imgs["logo"] = (ff_logo(vn.get("title") or ""), "png")
        except RuntimeError as e:
            notes.append(f"логотип: {e}")
    else:
        if cov:
            imgs["portrait"] = (cov, "jpg")
        if shot:
            imgs["landscape"] = (shot, "jpg")
            imgs["hero"] = (shot, "jpg")
        notes.append("без ffmpeg картинки VNDB поставлены как есть")
    return imgs, notes


def art_userdata(exe):
    sc = shortcuts_index().get(str(exe))
    return Path(sc["userdata"]) if sc else pick_userdata()


def art_files(appid, ud):
    """{slot: path} of the cover files currently in userdata/<id>/config/grid (newest per slot)."""
    grid = ud / "config" / "grid"
    out = {}
    if not grid.is_dir():
        return out
    for slot, suf in GRID_SUFFIX.items():
        cands = [f for f in grid.glob(f"{appid}{suf}.*") if f.suffix.lower() in (".png", ".jpg", ".jpeg")]
        if cands:
            out[slot] = max(cands, key=lambda f: f.stat().st_mtime)
    return out


def art_file_for(appid, slot):
    for ud in userdata_dirs():
        f = art_files(appid, ud).get(slot)
        if f:
            return f
    return None


def apply_artwork(appid, ud, images):
    """Write covers into userdata/<id>/config/grid (what Steam reads on start / what the UI previews)
    and, when Steam control is live, push them to the running client as well."""
    ud = ud or pick_userdata()
    grid = ud / "config" / "grid"
    grid.mkdir(parents=True, exist_ok=True)
    for slot, (data, ext) in images.items():
        for old in grid.glob(f"{appid}{GRID_SUFFIX[slot]}.*"):
            if old.suffix.lower() in (".png", ".jpg", ".jpeg"):
                old.unlink()
        (grid / f"{appid}{GRID_SUFFIX[slot]}.{ext}").write_bytes(data)
    if not CDP.available():
        return "files"
    for slot, (data, ext) in images.items():
        if slot in ASSET_TYPE:
            CDP.set_artwork(appid, data, ext, ASSET_TYPE[slot])
    if "icon" in images:
        try:
            CDP.set_icon(appid, grid / f"{appid}_icon.{images['icon'][1]}")
        except Exception as e:  # noqa: BLE001
            log(f"set icon: {e}")
    return "live"


def art_current(game_dir, exe):
    """Cover slots of an added game with URLs for preview."""
    p = game_exe_path(game_dir, exe)
    appid = resolve_appid(p)
    if not appid:
        raise ValueError("игра ещё не добавлена в Steam")
    files = art_files(appid, art_userdata(p))
    rec = added_rec(str(p))
    slots = {}
    for slot in GRID_SUFFIX:
        f = files.get(slot)
        # nanosecond mtime: two replacements in the same second must still bust the browser cache
        slots[slot] = ({"url": f"/art/{appid}/{slot}?v={f.stat().st_mtime_ns}", "size": f.stat().st_size,
                        "ext": f.suffix.lstrip(".").lower()} if f else None)
    return {"appid": appid, "source": rec.get("art_source"), "vndb_title": rec.get("vndb_title"),
            "note": rec.get("art_note"), "error": rec.get("art_error"), "live": CDP.available(), "slots": slots}


def vndb_image_list(vn_id):
    """Cover + screenshots of one VN, for picking a single slot image by hand."""
    res = vndb_query(["id", "=", vn_id], 1)
    if not res:
        raise RuntimeError(f"VNDB: {vn_id} не найден")
    v = res[0]
    out = []
    cover = v.get("image") or {}
    if vndb_allowed(cover):
        out.append({"kind": "cover", "url": cover["url"], "dims": cover.get("dims")})
    for shot in v.get("screenshots") or []:
        if vndb_allowed(shot):
            out.append({"kind": "screenshot", "url": shot["url"], "dims": shot.get("dims")})
    return {"id": v["id"], "title": v.get("title"), "images": out}


def art_from_url(game_dir, exe, slot, url, vn_id=None):
    """Put one VNDB image into one cover slot, fitted/cropped for that slot when ffmpeg is around."""
    if slot not in GRID_SUFFIX:
        raise ValueError("неизвестный слот обложки")
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or not (u.netloc == "vndb.org" or u.netloc.endswith(".vndb.org")):
        raise ValueError("картинки можно брать только с vndb.org")
    p = game_exe_path(game_dir, exe)
    appid = resolve_appid(p)
    if not appid:
        raise ValueError("игра ещё не добавлена в Steam")
    data = vndb_fetch(url)
    ext = "png" if data[:8] == PNG_SIG else "jpg"
    if FFMPEG:
        try:
            if slot == "portrait":
                data, ext = ff_fit_blur(data, ext, 600, 900), "jpg"
            elif slot == "landscape":
                data, ext = ff_cover(data, ext, 920, 430), "jpg"
            elif slot == "hero":
                data, ext = ff_cover(data, ext, 1920, 620), "jpg"
            elif slot == "icon":
                data, ext = ff_cover(data, ext, 256, 256), "jpg"
        except RuntimeError as e:
            log(f"ffmpeg for {slot} failed, using the image as is: {e}")
    how = apply_artwork(appid, art_userdata(p), {slot: (data, ext)})
    kv = {"art": True, "art_error": None, "art_source": "vndb"}
    if vn_id:
        kv["vndb_id"] = vn_id
    update_added(str(p), **kv)
    return {"slot": slot, "how": how}


def art_from_exe(game_dir, exe, slot):
    """Fill one cover slot from the icon inside the executable (or an .ico next to it)."""
    if slot not in GRID_SUFFIX:
        raise ValueError("неизвестный слот обложки")
    p = game_exe_path(game_dir, exe)
    appid = resolve_appid(p)
    if not appid:
        raise ValueError("игра ещё не добавлена в Steam")
    icon = load_icon(p)
    if not icon:
        raise RuntimeError(f"в {p.name} нет иконки, и рядом не нашлось .ico или icon.png")
    if slot in SIZES:
        w, h = SIZES[slot]
        data = capsule_png(icon, w, h, 0.6 if slot == "portrait" else 0.5)
    else:                      # icon and logo keep the transparent original
        data = png_encode(*icon)
    how = apply_artwork(appid, art_userdata(p), {slot: (data, "png")})
    update_added(str(p), art=True, art_error=None)
    return {"slot": slot, "how": how, "size": f"{icon[0]}x{icon[1]}"}


def custom_art(game_dir, exe, slot, data):
    """Replace one cover slot with an image uploaded by the user (PNG or JPEG)."""
    if slot not in GRID_SUFFIX:
        raise ValueError("неизвестный слот обложки")
    if data[:8] == PNG_SIG:
        ext = "png"
    elif data[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    else:
        raise ValueError("нужен PNG или JPEG")
    if len(data) > 25 << 20:
        raise ValueError("файл больше 25 МБ")
    p = game_exe_path(game_dir, exe)
    appid = resolve_appid(p)
    if not appid:
        raise ValueError("игра ещё не добавлена в Steam")
    how = apply_artwork(appid, art_userdata(p), {slot: (data, ext)})
    update_added(str(p), art=True, art_error=None, art_source="custom")
    return {"slot": slot, "how": how}


ART_BUSY = set()


def art_worker(exe, force, wait_for_shortcut, source="auto", vn_id=None):
    if exe in ART_BUSY:
        return
    ART_BUSY.add(exe)
    try:
        note = ensure_art(exe, force, wait_for_shortcut, source, vn_id)
        log(f"art for {Path(exe).name}: {note}")
    except Exception as e:  # noqa: BLE001
        log(f"art for {Path(exe).name} failed: {e}")
        update_added(exe, art=False, art_error=str(e))
    finally:
        ART_BUSY.discard(exe)


def ensure_art(exe, force=False, wait_for_shortcut=False, source="auto", vn_id=None):
    """Build and apply the full cover set (portrait, landscape, hero, logo, icon) for a shortcut."""
    p = Path(exe)
    rec = added_rec(str(p))
    if rec.get("art") and not force and source == "auto":
        return "обложка уже есть"
    appid = resolve_appid(p, wait=20 if wait_for_shortcut else 0)
    sc = shortcuts_index().get(str(p))
    ud = Path(sc["userdata"]) if sc else None
    if not appid:
        appid = shortcut_appid(f'"{p}"', p.stem)
        log(f"shortcut for {p.name} not in shortcuts.vdf yet; using computed appid {appid}")
    images, info, notes = {}, {"art_source": "icon", "vndb_id": None, "vndb_title": None}, []
    icon = load_icon(p)
    if source == "vndb" or (source == "auto" and STATE.get("vndb_auto", True)):
        try:
            query = rec.get("name") or (clean_title(sc["name"]) if sc and sc.get("name") else "") or pretty_name(p, p.parent)
            vn = vndb_pick(clean_title(query) or query, vn_id)
            if vn:
                vimgs, vnotes = vndb_images(vn, bool(icon))
                notes += vnotes
                if vimgs:
                    images.update(vimgs)
                    info = {"art_source": "vndb", "vndb_id": vn["id"], "vndb_title": vn.get("title")}
            else:
                notes.append("VNDB: подходящей новеллы не нашёл")
        except Exception as e:  # noqa: BLE001
            notes.append(f"VNDB: {str(e)[:120]}")
            log(f"vndb for {p.name}: {e}")
    if icon:
        if "icon" not in images:
            images["icon"] = (png_encode(*icon), "png")
        for slot, (w, h) in SIZES.items():
            if slot not in images:
                images[slot] = (capsule_png(icon, w, h, 0.6 if slot == "portrait" else 0.5), "png")
    if not images:
        raise RuntimeError("иконка не найдена ни в exe, ни в папке игры, и VNDB не помог")
    how = apply_artwork(appid, ud, images)
    update_added(str(p), art=True, appid=appid, art_error=None, art_note="; ".join(notes) or None, **info)
    return f"{info['art_source']} -> {', '.join(sorted(images))} ({how})" + (f"; {'; '.join(notes)}" if notes else "")
