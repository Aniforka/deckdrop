"""A game that arrived as a folder: move it out of the inbox, then add it to Steam.

Both new ways in - a whole game folder uploaded from a PC and a finished torrent - end with
a folder that is usually the game already. This module is the one place that turns such a
folder into a DeckDrop game: it skips the one directory a download often adds on top
(Games/Foo/Foo.exe), renames the folder to the title the user saw, moves it next to the other
games and, when asked in the settings, adds the recommended executable to Steam.
"""

import re
import shutil
from pathlib import Path

from .config import log
from .detect import find_exes, recommend
from .i18n import tr
from .paths import unique_dir
from .state import STATE
from .storage import inside

JUNK = {"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"}
# a repack arrives as an installer: a shortcut to setup.exe is not a game
INSTALLER = re.compile(r"^(setup|install|installer|setups?[-_ ]?x?(64|32)?|game[-_ ]?setup)$", re.I)


def _children(folder):
    return [e for e in folder.iterdir() if e.name not in JUNK and not e.name.startswith(".")]


def game_source(folder):
    """The folder that really holds the game: a download often adds one level (Games/Foo/Foo.exe)."""
    folder = Path(folder)
    for _ in range(3):
        try:
            if find_exes(folder):
                break
            kids = _children(folder)
        except OSError:
            break
        if len(kids) != 1 or not kids[0].is_dir():
            break
        folder = kids[0]
    return folder


def move_game(job, folder, name=""):
    """Move the game out of `folder` (a part of the inbox) into the games root. Returns the new path."""
    work = Path(folder)
    src = game_source(work)
    title = (name or "").strip() or src.name
    target = unique_dir(job.root, title)
    shutil.move(str(src), str(target))
    job.game_dir = str(target)
    job.file = None
    log(f"job {job.id}: {src} -> {target}")
    return target


def clean_temp(path, keep=None):
    """Remove the empty folders an upload or torrent left in the inbox; files are never touched."""
    path = Path(path)
    keep = Path(keep).resolve() if keep else None
    try:
        dirs = sorted((p for p in path.rglob("*") if p.is_dir() and not p.is_symlink()),
                      key=lambda p: len(p.parts), reverse=True)
    except OSError:
        return
    for d in dirs:
        if keep is not None and (d == keep or keep in d.parents or d in keep.parents):
            continue
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    try:
        if not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


def maybe_add(job):
    """Add the game a job produced to Steam when the setting asks for it. Never fails the job."""
    if not job.game_dir or job.status != "done" or not STATE.get("auto_add_steam", True):
        return None
    game = Path(job.game_dir)
    if not inside(job.root, game):
        return None
    try:
        exes = find_exes(game)
    except OSError:
        exes = []
    rel = recommend(exes)
    if not rel:
        return None
    if INSTALLER.match(Path(rel).stem):
        job.note = tr("adopt.installer")
        log(f"job {job.id}: {rel} looks like an installer, not adding it to Steam")
        return None
    try:
        from .games import add_to_steam          # imported here: games imports the art modules
        job.note = add_to_steam(str(game), rel)
        log(f"job {job.id}: {job.note}")
        return job.note
    except Exception as e:  # noqa: BLE001
        # not fatal and not the job's failure: the game is already on the Deck, it just was not
        # added to Steam (Steam closed, the shortcut queued for later, ...)
        log(f"job {job.id}: add to Steam failed: {e}")
        return None
