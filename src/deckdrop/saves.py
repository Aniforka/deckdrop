"""Save games: find, back up to a zip, import back."""

import json
import re
import secrets
import shutil
import time
import zipfile
from pathlib import Path

from . import __version__
from .config import CACHE_DIR, CHUNK
from .detect import game_exe_path, is_linux_exe, norm_title, pretty_name
from .i18n import tr
from .paths import dir_size
from .steam.library import compatdata_dir
from .steam.shortcuts import resolve_appid


SAVE_DIR_RE = re.compile(r"^(save|saves|savedata|save_data|savegame|savegames|sav|userdata|profile|profiles)$", re.I)
PREFIX_SUBDIRS = ("AppData/Roaming", "AppData/Local", "AppData/LocalLow", "Documents", "Saved Games")
PREFIX_SKIP = {"microsoft", "temp", "crashdumps", "packages", "d3dscache", "nvidia", "steam", "programs",
               "connecteddevicesplatform", "comms", "placeholdertilelogofolder", "publishers", "google", "mozilla"}


def save_sources(game_dir, exe):
    """[(group, base, path)] - dirs to back up; archive paths are group/<rel to base>."""
    g = Path(game_dir)
    p = g / exe
    srcs = []

    def walk(d, depth):
        for c in d.iterdir():
            if not c.is_dir():
                continue
            if SAVE_DIR_RE.match(c.name):
                srcs.append(("game", g, c))
            elif depth < 3:
                walk(c, depth + 1)

    walk(g, 0)
    appid = resolve_appid(p)
    if appid and not is_linux_exe(p):
        user = compatdata_dir(appid) / "pfx" / "drive_c" / "users" / "steamuser"
        for sub in PREFIX_SUBDIRS:
            base = user / sub
            if base.is_dir():
                for c in base.iterdir():
                    if c.is_dir() and c.name.lower() not in PREFIX_SKIP and not c.name.startswith("."):
                        srcs.append(("prefix", user, c))
    if is_linux_exe(p):
        token = norm_title(pretty_name(p, g))[:6]
        for base in (Path.home() / ".renpy", Path.home() / ".config" / "unity3d"):
            if base.is_dir():
                for c in base.rglob("*"):
                    if c.is_dir() and len(c.relative_to(base).parts) <= 2 and token and token in norm_title(c.name):
                        srcs.append(("home", Path.home(), c))
    return srcs, appid


def saves_info(game_dir, exe):
    p = game_exe_path(game_dir, exe)
    srcs, appid = save_sources(Path(game_dir), p.name if p.parent == Path(game_dir).resolve() else str(p.relative_to(Path(game_dir).resolve())))
    out = []
    for group, base, path in srcs:
        size, files = dir_size(path)
        out.append({"group": group, "path": str(path), "size": size, "files": files})
    return {"appid": appid, "sources": out, "total": sum(s["size"] for s in out),
            "prefix": str(compatdata_dir(appid)) if appid else None}


def build_saves_zip(game_dir, exe):
    p = game_exe_path(game_dir, exe)
    g = Path(game_dir).resolve()
    rel = str(p.relative_to(g))
    srcs, appid = save_sources(g, rel)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"saves_{re.sub(r'[^A-Za-z0-9_-]+', '_', g.name)}_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}.zip"
    manifest = {"deckdrop": __version__, "game": g.name, "exe": rel, "appid": appid,
                "created": int(time.time()), "groups": [], "files": 0}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for group, base, path in srcs:
            manifest["groups"].append({"group": group, "path": str(path.relative_to(base))})
            for f in path.rglob("*"):
                if f.is_file():
                    z.write(f, f"{group}/{f.relative_to(base).as_posix()}")
                    manifest["files"] += 1
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    return out, manifest


def import_saves_zip(game_dir, exe, zip_path):
    p = game_exe_path(game_dir, exe)
    g = Path(game_dir).resolve()
    appid = resolve_appid(p)
    targets = {"game": g, "home": Path.home()}
    if appid:
        targets["prefix"] = compatdata_dir(appid) / "pfx" / "drive_c" / "users" / "steamuser"
    backup, _ = build_saves_zip(game_dir, str(p.relative_to(g)))   # safety copy of what is there now
    written, skipped = 0, 0
    with zipfile.ZipFile(zip_path) as z:
        try:
            manifest = json.loads(z.read("manifest.json"))
        except KeyError:
            raise ValueError(tr("saves.not_backup")) from None
        for info in z.infolist():
            if info.is_dir() or info.filename == "manifest.json":
                continue
            group, _, rel = info.filename.partition("/")
            base = targets.get(group)
            if not base or not rel:
                skipped += 1
                continue
            dest = (base / rel).resolve()
            if base.resolve() not in dest.parents:
                skipped += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            written += 1
    return {"written": written, "skipped": skipped, "from_game": manifest.get("game"),
            "backup_of_previous": backup.name, "prefix_missing": "prefix" not in targets}
