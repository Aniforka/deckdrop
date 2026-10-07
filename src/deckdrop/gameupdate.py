"""A new version of a game, put over the old one without losing saves, settings or the Steam shortcut.

The new version arrives like any download (a link, Mega, or a file from the phone) and is unpacked
aside, next to the game: <folder above the game>/.deckdrop-updates/stage-<token>. Nothing in the
game changes until the user has seen what the update would do and confirmed it.

Applying it keeps the game where it is. Steam knows a non-Steam game by its shortcut, and the
shortcut's app id names the Proton prefix where most games keep their saves and settings, so the
folder and the executable stay the same paths: the new files go over the old ones. When the new
version renames its executable, the same shortcut is pointed at the new one, which keeps the app
id (and with it the prefix, the name, Proton, artwork and controller layout).

Inside the game folder:
  - a file in a save folder (saves, savedata, Saved, ...) that the user already has is never
    replaced, even if the new version ships one;
  - settings files the user has (*.ini, *.cfg, config.json, ...) are kept when asked (the default);
  - files of the old version the new one does not have stay, unless the user asks to clear them
    (saves and kept settings excepted).
Every file the update replaces or clears is moved, not deleted, into
.deckdrop-updates/<game folder name>/, with manifest.json saying what the update did, so the last
update can be rolled back.
"""

import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from .archives import NeedsPassword, flatten, try_extract
from .config import log
from .detect import find_exes, game_exe_path, recommend
from .i18n import tr
from .jobs import fail
from .patches import PATCH_JUNK, _place, game_folder
from .paths import archive_volume, safe_name
from .saves import SAVE_DIR_RE
from .state import STATE, STATE_LOCK, save_state
from .steam.cdp import CDP
from .steam.shortcuts import queue_pending, resolve_appid
from .storage import game_roots, inside


HOME_NAME = ".deckdrop-updates"
MANIFEST = "manifest.json"
UPDATES = {}                 # game dir -> {token, stage, exe, name, version, at}
UPDATES_LOCK = threading.Lock()
SAVE_EXTRA = {"saved", "savegame", "save games", "saved games"}
SETTINGS_EXTS = {".ini", ".cfg", ".conf"}
SETTINGS_NAME = re.compile(r"(config|settings|options|prefs|preferences)", re.I)
SETTINGS_NAME_EXTS = {".json", ".xml", ".txt", ".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml"}


def update_home(game):
    return Path(game).parent / HOME_NAME


def backup_dir(game):
    return update_home(game) / Path(game).name


def is_save_path(rel):
    """A path inside a save folder, by any of its folder names."""
    parts = Path(rel).parts[:-1]
    return any(SAVE_DIR_RE.match(p) or p.lower() in SAVE_EXTRA for p in parts)


def is_settings_file(rel):
    p = Path(rel)
    ext = p.suffix.lower()
    return ext in SETTINGS_EXTS or (ext in SETTINGS_NAME_EXTS and bool(SETTINGS_NAME.search(p.stem)))


def _protected(rel, keep_settings):
    """Never cleared with the old version's files: saves, and the user's settings when they are kept."""
    return is_save_path(rel) or (keep_settings and is_settings_file(rel))


def game_running(game):
    """True when a process works inside the game folder: Steam starts a game there (Proton too)."""
    game = Path(game).resolve()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return False
    me = str(os.getpid())
    for pid in pids:
        if pid == me:
            continue
        try:
            cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            continue
        if cwd == game or inside(game, cwd):
            return True
    return False


def _files(root):
    """Files below root as relative paths, parents' folders first; OS junk and links left out."""
    out, dirs_out = [], []
    for base, dirs, files in os.walk(root):
        b = Path(base)
        dirs[:] = sorted(d for d in dirs if d not in PATCH_JUNK and d != HOME_NAME and not (b / d).is_symlink())
        dirs_out += [(b / d).relative_to(root) for d in dirs]
        out += [(b / f).relative_to(root) for f in sorted(files)
                if f not in PATCH_JUNK and not (b / f).is_symlink()]
    return dirs_out, out


# ---- getting the new version

def _drop_stage(info):
    if info and info.get("stage"):
        shutil.rmtree(info["stage"], ignore_errors=True)


def forget(game):
    with UPDATES_LOCK:
        info = UPDATES.pop(str(game), None)
    _drop_stage(info)


def discard_update(game_dir):
    forget(game_folder(game_dir))


def sweep_stages():
    """Unpacked updates nobody applied are gone after a restart: free their space."""
    from .games import game_dirs
    seen = set()
    for d, _ in game_dirs():
        home = update_home(d)
        if home in seen or not home.is_dir():
            continue
        seen.add(home)
        for s in home.glob("stage-*"):
            shutil.rmtree(s, ignore_errors=True)


def update_target(game_dir, exe):
    """The game to update and its executable Steam starts, checked before anything is downloaded."""
    game = game_folder(game_dir)
    p = game_exe_path(str(game), exe)
    if not inside(game, p):
        raise ValueError(tr("files.exe_outside"))
    return game, p.relative_to(game).as_posix()


def update_root(game):
    """The DeckDrop folder on the game's disk, where its new version is downloaded; None = the default."""
    roots = [r for r in game_roots() if inside(r, game)]
    return roots[0] if roots else None


def new_version_job(game_dir, exe, title):
    """{game, exe, title} for a download or upload of a new version, and the folder it goes to."""
    game, rel = update_target(game_dir, exe)
    return {"game": str(game), "exe": rel, "title": (title or "").strip() or game.name}, update_root(game)


def stage_update(job, path, password=None):
    """Called for a finished download or upload of a new version: unpack it aside and wait."""
    game, exe = Path(job.update["game"]), job.update["exe"]
    stage = update_home(game) / f"stage-{secrets.token_hex(6)}"
    try:
        job.status = "extracting"
        job.file = str(path)
        stage.parent.mkdir(parents=True, exist_ok=True)
        if archive_volume(path.name) == "primary":
            candidates = [password] if password else [None] + list(STATE.get("archive_passwords") or [])
            err = None
            for cand in candidates:
                try:
                    try_extract(path, stage, cand)
                    err = None
                    break
                except NeedsPassword as e:
                    err = e
            if err is not None:
                job.status, job.error = "needs_password", str(err)
                return
            version = None
            top = [x for x in stage.iterdir() if x.name not in PATCH_JUNK]
            if len(top) == 1 and top[0].is_dir():
                version = top[0].name            # "Game-1.3-pc": often the only place a version is named
            flatten(stage)
            path.unlink(missing_ok=True)         # all of it is in the stage now
        else:
            stage.mkdir(parents=True)            # a single file: an executable, a .pck, ...
            shutil.move(str(path), str(stage / safe_name(path.name)))
            version = path.name
        if not any(stage.iterdir()):
            raise ValueError(tr("gup.empty"))
        with UPDATES_LOCK:
            old = UPDATES.get(str(game))
            UPDATES[str(game)] = {"token": secrets.token_hex(8), "stage": str(stage), "exe": exe,
                                  "name": path.name, "version": version or path.name, "at": time.time()}
        _drop_stage(old)
        job.file = None
        job.game_dir = str(game)
        job.status = "update_ready"
        log(f"job {job.id}: update for {game.name} unpacked to {stage}")
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(stage, ignore_errors=True)
        fail(job, tr("gup.unpack_failed", error=e))


# ---- what it would do

def _align(stage, exe):
    """Where the new files go: (folder in the stage, folder in the game, new path of the exe or None).

    Archives differ in how deep they put the game: "Game-1.3/bin/Game.exe" against the old
    "bin/Game.exe". The executable Steam starts is looked for by name, and the deepest match of its
    path decides which stage folder lines up with which game folder.
    """
    old = Path(exe)
    best = None
    for c in stage.rglob("*"):
        if c.name.lower() != old.name.lower() or not c.is_file() or c.is_symlink():
            continue
        rel = c.relative_to(stage)
        k = 0
        while (k < len(rel.parts) and k < len(old.parts)
               and rel.parts[-1 - k].lower() == old.parts[-1 - k].lower()):
            k += 1
        cand = (k, -len(rel.parts), rel)
        if best is None or cand[:2] > best[:2]:
            best = cand
    if best:
        k, _, rel = best
        src = stage.joinpath(*rel.parts[:len(rel.parts) - k])
        dst_rel = Path(*old.parts[:len(old.parts) - k]) if len(old.parts) > k else Path(".")
        new_exe = Path(*old.parts[:len(old.parts) - k], *rel.parts[len(rel.parts) - k:]).as_posix()
        return src, dst_rel, new_exe
    return stage, Path("."), None


def _plan(game, info, keep_settings=True, cleanup=False):
    stage = Path(info["stage"])
    src, dst_rel, same_exe = _align(stage, info["exe"])
    dst = (game / dst_rel).resolve()
    if not inside(game, dst) and dst != game:
        raise ValueError(tr("files.escape"))
    _, files = _files(src)
    added, replaced, kept_saves, settings, blocked = [], [], [], [], []
    for rel in files:
        dest = dst / rel
        game_rel = dest.relative_to(game).as_posix()
        par = dest.parent
        while par != dst and not par.is_file():
            par = par.parent
        if dest.is_dir() or par != dst:
            blocked.append(game_rel)
        elif not dest.exists():
            added.append(game_rel)
        elif is_save_path(game_rel):
            kept_saves.append(game_rel)
        elif is_settings_file(game_rel) and not _same(src / rel, dest):
            settings.append(game_rel)          # the user's own settings against the new defaults
        else:
            replaced.append(game_rel)
    new_set = {(dst / r).relative_to(game).as_posix() for r in files}
    _, old_files = _files(game)
    old_only = [r.as_posix() for r in old_files if r.as_posix() not in new_set]
    removable = [r for r in old_only if not _protected(r, keep_settings)] if cleanup else []
    new_exes = [(dst_rel / e).as_posix() for e in find_exes(src)]
    new_exes = [e[2:] if e.startswith("./") else e for e in new_exes]
    if not keep_settings:
        replaced += settings
    size = _size([(game / r).relative_to(dst) for r in added + replaced], src)
    return {"src": src, "dst": dst, "added": added, "replaced": replaced, "kept_saves": kept_saves,
            "settings": settings, "kept_settings": settings if keep_settings else [], "blocked": blocked, "old_only": old_only, "removable": removable,
            "same_exe": same_exe, "new_exes": new_exes, "size": size}


def _same(a, b):
    """Same content; only small files are compared, a big one counts as changed."""
    try:
        sa, sb = a.stat().st_size, b.stat().st_size
        if sa != sb:
            return False
        if sa > 1 << 20:
            return False
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _size(paths, base):
    total = 0
    for r in paths:
        try:
            total += (base / r).stat().st_size
        except OSError:
            pass
    return total


def _pending(game):
    with UPDATES_LOCK:
        info = UPDATES.get(str(game))
    if info and not Path(info["stage"]).is_dir():
        forget(game)
        return None
    return info


def update_status(game_dir):
    """What the game page shows: an update waiting to be applied, and the last one to roll back."""
    game = game_folder(game_dir)
    out = {"pending": None, "backup": None, "running": game_running(game)}
    info = _pending(game)
    if info:
        plan = _plan(game, info)
        settings = plan["settings"]
        new_exe = plan["same_exe"] or recommend(plan["new_exes"])
        out["pending"] = {
            "token": info["token"], "name": info["name"], "version": info["version"], "exe": info["exe"],
            "exe_found": bool(plan["same_exe"]), "same_exe": plan["same_exe"], "new_exe": new_exe,
            "new_exes": plan["new_exes"],
            "added": len(plan["added"]), "replaced": len(plan["replaced"]),
            "size": plan["size"],
            "replaced_sample": plan["replaced"][:8],
            "kept_saves": len(plan["kept_saves"]), "kept_saves_sample": plan["kept_saves"][:5],
            "settings": settings[:20], "settings_count": len(settings),
            "blocked": plan["blocked"][:8], "blocked_count": len(plan["blocked"]),
            "old_only": len(plan["old_only"]),
            "old_only_kept": sum(1 for r in plan["old_only"] if _protected(r, True)),
            "old_only_sample": [r for r in plan["old_only"] if not _protected(r, True)][:8],
        }
    m = read_manifest(game)
    if m:
        out["backup"] = {"at": m.get("at"), "version": m.get("version"), "added": len(m.get("added", [])),
                         "replaced": len(m.get("replaced", [])), "removed": len(m.get("removed", [])),
                         "exe_change": m.get("exe_change")}
    return out


def read_manifest(game):
    try:
        return json.loads((backup_dir(game) / MANIFEST).read_text("utf-8"))
    except (OSError, ValueError):
        return None


# ---- applying and rolling back

def _point_shortcut(game, old_rel, new_rel):
    """Make the game's Steam shortcut start new_rel instead of old_rel, keeping its app id."""
    old, new = str((game / old_rel).resolve()), str((game / new_rel).resolve())
    appid = resolve_appid(old)
    with STATE_LOCK:
        recs = STATE.setdefault("added", {})
        if old in recs:
            recs[new] = dict(recs.pop(old), **({"appid": appid} if appid else {}))
        elif appid:
            recs[new] = {"at": int(time.time()), "appid": appid}
        pend = STATE.get("pending") or []
        for op in pend:                                  # renames and Proton waiting for Steam follow
            if op.get("exe") == old and op.get("op") != "exe":
                op["exe"] = new
        save_state()
    if CDP.available() and appid:
        CDP.set_exe(appid, f'"{new}"', f'"{Path(new).parent}"')
        return "live"
    queue_pending(op="exe", exe=new, old=old, appid=appid)
    return "queued"


def apply_update(game_dir, token, new_exe=None, keep_settings=True, cleanup=False):
    game = game_folder(game_dir)
    info = _pending(game)
    if not info or info["token"] != token:
        raise ValueError(tr("gup.gone"))
    if game_running(game):
        raise ValueError(tr("gup.running"))
    plan = _plan(game, info, keep_settings, cleanup)
    new_exe = (new_exe or plan["same_exe"] or info["exe"]).strip()
    if new_exe != info["exe"] and new_exe not in plan["new_exes"] and not (game / new_exe).is_file():
        raise ValueError(tr("gup.bad_exe", name=new_exe))
    bdir = backup_dir(game)
    shutil.rmtree(bdir, ignore_errors=True)              # one update can be rolled back: the last one
    bdir.mkdir(parents=True)
    manifest = {"at": int(time.time()), "version": info["version"], "name": info["name"],
                "added": [], "replaced": [], "removed": [], "dirs": [], "exe_change": None}
    src, dst = plan["src"], plan["dst"]
    skip = set(plan["kept_saves"]) | set(plan["kept_settings"]) | set(plan["blocked"])
    problems = []

    def save_manifest():
        (bdir / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")

    try:
        dirs, files = _files(src)
        for d in dirs:
            target = dst / d
            if not target.exists():
                target.mkdir(parents=True)
                manifest["dirs"].append(target.relative_to(game).as_posix())
        for rel in files:
            dest = dst / rel
            game_rel = dest.relative_to(game).as_posix()
            if game_rel in skip:
                continue
            try:
                if dest.exists():
                    keep = bdir / "files" / game_rel
                    keep.parent.mkdir(parents=True, exist_ok=True)
                    _place(dest, keep)
                    manifest["replaced"].append(game_rel)
                else:
                    manifest["added"].append(game_rel)
                _place(src / rel, dest)
            except OSError as e:
                problems.append(f"{game_rel}: {e}")
                log(f"update {game.name}: {game_rel}: {e}")
        for game_rel in plan["removable"]:
            try:
                keep = bdir / "files" / game_rel
                keep.parent.mkdir(parents=True, exist_ok=True)
                _place(game / game_rel, keep)
                manifest["removed"].append(game_rel)
            except OSError as e:
                problems.append(f"{game_rel}: {e}")
        if new_exe != info["exe"]:
            how = _point_shortcut(game, info["exe"], new_exe)
            manifest["exe_change"] = {"old": info["exe"], "new": new_exe, "how": how}
    finally:
        save_manifest()
        forget(game)
    log(f"update {game.name} -> {info['version']}: {len(manifest['added'])} added, "
        f"{len(manifest['replaced'])} replaced, {len(manifest['removed'])} cleared, "
        f"{len(skip)} kept, {len(problems)} problems")
    return {"added": len(manifest["added"]), "replaced": len(manifest["replaced"]),
            "removed": len(manifest["removed"]), "kept_saves": len(plan["kept_saves"]),
            "kept_settings": len(plan["kept_settings"]), "skipped": len(plan["blocked"]),
            "problems": problems[:10], "problems_count": len(problems),
            "exe_change": manifest["exe_change"], "version": info["version"]}


def rollback_update(game_dir):
    """Put the game back as it was before its last update. Saves made since then are not touched."""
    game = game_folder(game_dir)
    m = read_manifest(game)
    if not m:
        raise ValueError(tr("gup.no_backup"))
    if game_running(game):
        raise ValueError(tr("gup.running"))
    bdir = backup_dir(game)
    problems = []
    for rel in m.get("added", []):
        p = game / rel
        try:
            if inside(game, p) and p.is_file():
                p.unlink()
        except OSError as e:
            problems.append(f"{rel}: {e}")
    for rel in m.get("replaced", []) + m.get("removed", []):
        keep, p = bdir / "files" / rel, game / rel
        try:
            if keep.is_file() and inside(game, p):
                p.parent.mkdir(parents=True, exist_ok=True)
                _place(keep, p)
        except OSError as e:
            problems.append(f"{rel}: {e}")
    for rel in reversed(m.get("dirs", [])):
        try:
            (game / rel).rmdir()                         # only if the update made it and it is empty again
        except OSError:
            pass
    ch = m.get("exe_change")
    if ch and (game / ch["old"]).is_file():
        _point_shortcut(game, ch["new"], ch["old"])
    if problems:
        log(f"rollback {game.name}: {problems}")
        raise RuntimeError(tr("gup.rollback_partial", n=len(problems), first=problems[0]))
    shutil.rmtree(bdir, ignore_errors=True)
    log(f"rollback {game.name}: back to before {m.get('version')}")
    return {"version": m.get("version"), "exe": ch["old"] if ch else None}


def drop_backup(game_dir):
    game = game_folder(game_dir)
    shutil.rmtree(backup_dir(game), ignore_errors=True)
