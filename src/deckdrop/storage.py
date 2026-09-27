"""Disks and game roots: internal disk, microSD, extra roots, imported folders."""

import os
import shutil
from pathlib import Path

from .config import GAMES_DIR
from .i18n import tr
from .state import STATE


def sd_mounts():
    """Removable media mounted by SteamOS (/run/media/...) plus DECKDROP_DISKS extras."""
    out = []
    for base in (Path("/run/media"), Path("/run/media/deck")):
        if not base.is_dir():
            continue
        for d in base.iterdir():
            try:
                if d.is_dir() and d.name != "deck" and os.path.ismount(d):
                    out.append((d.name if d.name != "mmcblk0p1" else "microSD", d))
            except OSError:
                continue
    for extra in os.environ.get("DECKDROP_DISKS", "").split(";"):
        if "=" in extra:
            label, p = extra.split("=", 1)
            if Path(p).is_dir():
                out.append((label.strip(), Path(p)))
    return out


def disks():
    """[{id, label, root, free, total}] - internal first, then removable media."""
    res = []

    def add(disk_id, label, root):
        probe = root if root.exists() else root.parent
        try:
            u = shutil.disk_usage(probe)
            free, total = u.free, u.total
        except OSError:
            free = total = None
        res.append({"id": disk_id, "label": label, "root": str(root), "free": free, "total": total})

    add("internal", tr("disk.internal"), GAMES_DIR)
    for label, mount in sd_mounts():
        add("sd:" + mount.name, label, mount / "Games")
    return res


def root_for(disk_id):
    for d in disks():
        if d["id"] == disk_id:
            return Path(d["root"])
    return GAMES_DIR


def default_root():
    return root_for(STATE.get("default_disk") or "internal")


def game_roots():
    return [Path(d["root"]) for d in disks()]


def disk_label_for(path):
    p = Path(path).resolve()
    best = None
    for d in disks():
        r = Path(d["root"]).resolve()
        if (r == p or r in p.parents) and (best is None or len(str(r)) > len(str(best[0]))):
            best = (r, d["label"])
    if best:
        return best[1]
    for label, mount in sd_mounts():          # imported game on a card, outside <mount>/Games
        try:
            if mount.resolve() in p.parents:
                return label
        except OSError:
            continue
    return tr("disk.internal") if inside(Path.home(), p) else tr("disk.own_folder")


def inside(root, path):
    root = Path(root).resolve()
    path = Path(path).resolve()
    return root == path or root in path.parents


def imported_dirs():
    """Game folders the user pointed DeckDrop at. Each entry is one game, not a folder of games."""
    out = []
    for raw in STATE.get("imported") or []:
        try:
            p = Path(raw).resolve()
        except OSError:
            continue
        if p.is_dir():
            out.append(p)
    return out


# import is limited to places that belong to the user, so a typo cannot point DeckDrop at /etc
IMPORT_ROOTS = (Path.home(), Path("/run/media"), Path("/media"), Path("/mnt"))


def import_allowed(path):
    return any(inside(r, path) for r in IMPORT_ROOTS if r.exists())


def inside_any(path):
    return any(inside(r, path) for r in game_roots()) or any(inside(d, path) for d in imported_dirs())
