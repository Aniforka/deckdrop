"""Files inside a game: browse, upload, unpack a patch archive over the game."""

import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from .archives import NeedsPassword, try_extract
from .config import CACHE_DIR, log
from .detect import game_exe_path
from .games import game_dirs
from .i18n import tr
from .paths import archive_ext, safe_name
from .state import STATE, STATE_LOCK, save_state
from .storage import inside


UPLOAD_TMP = ".deckdrop-upload-"


def game_folder(game_dir):
    """The folder of a game DeckDrop lists: one in a DeckDrop root, or one imported by hand."""
    if not (game_dir or "").strip():
        raise ValueError(tr("files.not_game"))
    g = Path(game_dir).resolve()
    if not g.is_dir() or not any(g == Path(d).resolve() for d, _ in game_dirs()):
        raise ValueError(tr("files.not_game"))
    return g


def game_target_dir(game_dir, exe, rel):
    """A folder inside the game, given relative to the executable's folder. Empty means next to the exe."""
    game = game_folder(game_dir)
    exe_path = game_exe_path(str(game), exe)
    if not inside(game, exe_path):
        raise ValueError(tr("files.exe_outside"))
    rel = (rel or "").strip().replace("\\", "/")
    if rel.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", rel):
        raise ValueError(tr("files.need_relative"))
    target = (exe_path.parent / rel).resolve() if rel else exe_path.parent
    if not inside(game, target):
        raise ValueError(tr("files.escape"))
    if target.exists() and not target.is_dir():
        raise ValueError(tr("files.is_file", name=target.name))
    return game, target


def game_dir_list(game_dir, exe, rel, limit=300):
    """Where an upload would go and what already lies there."""
    game, target = game_target_dir(game_dir, exe, rel)
    out = {"path": str(target), "game_rel": target.relative_to(game).as_posix(),
           "exists": target.is_dir(), "entries": [], "more": 0}
    if out["exists"]:
        items = [x for x in target.iterdir() if not x.name.startswith(UPLOAD_TMP)]
        items.sort(key=lambda x: (not x.is_dir(), x.name.lower()))
        for x in items[:limit]:
            try:
                is_dir = x.is_dir()
                out["entries"].append({"name": x.name, "dir": is_dir, "size": 0 if is_dir else x.stat().st_size})
            except OSError:
                continue
        out["more"] = max(0, len(items) - limit)
    return out


def game_file_upload(game_dir, exe, rel, name, replace, write_body):
    """Store an uploaded file inside a game.

    An existing file is replaced only when asked, and its very first version stays next to it
    as <name>.bak, so a patch that broke the game can be rolled back by hand.
    """
    game, target = game_target_dir(game_dir, exe, rel)
    if not (name or "").strip():
        raise ValueError(tr("files.no_name"))
    fname = safe_name(name)
    dest = target / fname
    if dest.is_dir():
        raise ValueError(tr("files.is_dir", name=fname))
    existed = dest.exists()
    if existed and not replace:
        raise FileExistsError(fname)
    created = not target.exists()
    target.mkdir(parents=True, exist_ok=True)
    if not inside(game, target):
        raise ValueError(tr("files.escape"))
    tmp = target / f"{UPLOAD_TMP}{secrets.token_hex(4)}.part"
    backup = None
    try:
        size = write_body(tmp)
        if existed:
            bak = dest.with_name(dest.name + ".bak")
            if not bak.exists():
                os.replace(dest, bak)
                backup = bak.name
        try:
            os.replace(tmp, dest)
        except OSError:
            if backup:
                os.replace(target / backup, dest)
            raise
    finally:
        tmp.unlink(missing_ok=True)
    log(f"game file: {dest} ({size} bytes{', replaced' if existed else ''})")
    return {"name": fname, "path": str(dest), "rel": dest.relative_to(game).as_posix(), "size": size,
            "replaced": existed, "backup": backup, "created_dir": created}


PATCH_DIR = CACHE_DIR / "patches"
PATCH_TTL = 3600
PATCHES = {}                          # token -> {archive, game, exe, dir, name, at}
PATCH_LOCK = threading.Lock()
PATCH_JUNK = {"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"}


def _patch_drop(tok):
    with PATCH_LOCK:
        PATCHES.pop(tok, None)
    if re.fullmatch(r"[0-9a-f]{16}", tok or ""):            # never a path from outside
        shutil.rmtree(PATCH_DIR / tok, ignore_errors=True)


def _patch_sweep():
    """Forget previews nobody confirmed within an hour, including ones left from before a restart."""
    now = time.time()
    with PATCH_LOCK:
        stale = [t for t, p in PATCHES.items() if now - p["at"] > PATCH_TTL]
        live = set(PATCHES)
    for t in stale:
        _patch_drop(t)
    if PATCH_DIR.is_dir():
        for d in PATCH_DIR.iterdir():
            try:
                if d.name not in live and now - d.stat().st_mtime > PATCH_TTL:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


def _patch_get(tok):
    with PATCH_LOCK:
        info = PATCHES.get(tok or "")
    if not info:
        raise ValueError(tr("patch.gone"))
    return info


def _stage_items(src):
    """Everything in an unpacked archive as (relative path, is_dir), parents first.

    Symlinks are not carried over (returned separately) and OS junk like __MACOSX is ignored.
    """
    items, links = [], []
    for root, dirs, files in os.walk(src):
        r = Path(root)
        dirs[:] = sorted(d for d in dirs if d not in PATCH_JUNK)
        for name in dirs:
            (links if (r / name).is_symlink() else items).append(((r / name).relative_to(src), True))
        dirs[:] = [d for d in dirs if not (r / d).is_symlink()]
        for name in sorted(files):
            if name not in PATCH_JUNK:
                (links if (r / name).is_symlink() else items).append(((r / name).relative_to(src), False))
    return items, [rel.as_posix() for rel, _ in links]


def _patch_plan(src, target):
    """What unpacking src over target would do: new files, replaced files, and what can't go in."""
    items, links = _stage_items(src)
    files, size, conflicts, blocked = 0, 0, [], list(links)
    for rel, is_dir in items:
        dest = target / rel
        if is_dir:
            if dest.exists() and not dest.is_dir():
                blocked.append(rel.as_posix())
            continue
        files += 1
        try:
            size += (src / rel).stat().st_size
        except OSError:
            pass
        par = dest.parent
        while par != target and not par.is_file():
            par = par.parent
        if dest.is_dir() or par != target:
            blocked.append(rel.as_posix())
        elif dest.is_file():
            conflicts.append(rel.as_posix())
    return {"files": files, "size": size, "conflicts": len(conflicts), "sample": conflicts[:8],
            "blocked": blocked[:8], "blocked_count": len(blocked)}


def _stage_top(stage):
    top = sorted((x for x in stage.iterdir() if x.name not in PATCH_JUNK), key=lambda x: (not x.is_dir(), x.name.lower()))
    single = top[0] if len(top) == 1 and top[0].is_dir() and not top[0].is_symlink() else None
    return top, single


def _patch_view(tok):
    info = _patch_get(tok)
    game, target = game_target_dir(info["game"], info["exe"], info["dir"])
    stage = PATCH_DIR / tok / "x"
    top, single = _stage_top(stage)
    return {"token": tok, "name": info["name"], "target_rel": target.relative_to(game).as_posix(),
            "top": [x.name + ("/" if x.is_dir() else "") for x in top[:12]], "top_more": max(0, len(top) - 12),
            "single_top": single.name if single else None, "plain": _patch_plan(stage, target),
            "stripped": _patch_plan(single, target) if single else None}


def _patch_unpack(tok, password=None):
    """Unpack the uploaded archive aside, trying remembered passwords when none is given."""
    info = _patch_get(tok)
    stage = PATCH_DIR / tok / "x"
    shutil.rmtree(stage, ignore_errors=True)
    tries = [password] if password else [None] + list(STATE.get("archive_passwords") or [])
    try:
        for cand in tries:
            try:
                try_extract(info["archive"], stage, cand)
                break
            except NeedsPassword:
                continue
        else:
            return {"token": tok, "name": info["name"], "needs_password": True,
                    "error": tr("err.wrong_password") if password else None}
        info["archive"].unlink(missing_ok=True)             # everything is in the stage now
        return _patch_view(tok)
    except Exception:
        _patch_drop(tok)
        raise


def patch_upload(game_dir, exe, rel, name, write_body):
    """Take an archive for a game and unpack it aside, so the user sees what it would change first."""
    game_target_dir(game_dir, exe, rel)                     # refuse a bad path before reading the body
    fname = safe_name(name or "")
    if not (name or "").strip() or not archive_ext(fname):
        raise ValueError(tr("files.not_archive"))
    _patch_sweep()
    tok = secrets.token_hex(8)
    (PATCH_DIR / tok).mkdir(parents=True)
    arch = PATCH_DIR / tok / fname
    with PATCH_LOCK:
        PATCHES[tok] = {"archive": arch, "game": game_dir, "exe": exe, "dir": rel or "", "name": fname,
                        "at": time.time()}
    try:
        write_body(arch)
    except Exception:
        _patch_drop(tok)
        raise
    return _patch_unpack(tok)


def patch_unlock(tok, password, remember=False):
    if not password:
        raise ValueError(tr("err.enter_password"))
    res = _patch_unpack(tok, password)
    if remember and not res.get("needs_password"):
        with STATE_LOCK:
            pws = list(STATE.get("archive_passwords") or [])
            if password not in pws:
                pws.append(password)
                STATE["archive_passwords"] = pws
                save_state()
    return res


def _place(src, dest):
    """Move a file into place atomically, copying when the game sits on another disk."""
    try:
        os.replace(src, dest)
        return
    except OSError:
        pass
    tmp = dest.with_name(f"{UPLOAD_TMP}{secrets.token_hex(4)}.part")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    src.unlink(missing_ok=True)


def patch_apply(tok, strip=False, backup=True):
    """Unpack a previewed archive over the game.

    A replaced file keeps its very first version as <name>.bak (unless backups are off), the
    same rule as for single uploaded files. Whatever collides with a folder, or would need a
    folder where a file lies, is skipped and reported.
    """
    info = _patch_get(tok)
    stage = PATCH_DIR / tok / "x"
    if not stage.is_dir():
        raise ValueError(tr("patch.need_password_first"))
    try:
        game, target = game_target_dir(info["game"], info["exe"], info["dir"])
        _, single = _stage_top(stage)
        src = single if (strip and single) else stage
        items, links = _stage_items(src)
        out = {"written": 0, "replaced": 0, "backups": 0}
        skipped = list(links)
        target.mkdir(parents=True, exist_ok=True)
        for rel, is_dir in items:
            dest = target / rel
            try:
                if not inside(game, dest):
                    skipped.append(rel.as_posix())
                elif is_dir:
                    if dest.exists() and not dest.is_dir():
                        skipped.append(rel.as_posix())
                    else:
                        dest.mkdir(exist_ok=True)
                elif dest.is_dir() or not dest.parent.is_dir():
                    skipped.append(rel.as_posix())
                else:
                    if dest.exists():
                        out["replaced"] += 1
                        bak = dest.with_name(dest.name + ".bak")
                        if backup and not bak.exists():
                            os.replace(dest, bak)
                            out["backups"] += 1
                    _place(src / rel, dest)
                    out["written"] += 1
            except OSError as e:
                log(f"patch apply: {rel}: {e}")
                skipped.append(rel.as_posix())
    finally:
        _patch_drop(tok)
    out.update(skipped=skipped[:10], skipped_count=len(skipped), target_rel=target.relative_to(game).as_posix())
    log(f"patch {info['name']} -> {target}: {out['written']} written, {out['replaced']} replaced, "
        f"{out['backups']} backups, {len(skipped)} skipped")
    return out
