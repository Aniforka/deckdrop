"""Where the space went: games, Proton prefixes, inbox archives and update backups per disk.

The report walks big trees, so it is built in the background and cached: the page asks for it
while the panel is open, and a manual refresh starts a new walk. Nothing here writes anything.
"""

import os
import threading
import time
from pathlib import Path

from .detect import norm_title
from .gameupdate import HOME_NAME
from .games import list_games
from .steam.library import compatdata_dir
from .storage import disks, sd_mounts

CACHE_TTL = 300          # a built report is reused for this long, unless refreshed by hand
SIZE_TTL = 600           # one folder's size is measured again after this long
TOP = 30
PREFIX_TOP = 10
PARTS = ("games", "prefixes", "inbox", "backups", "other")

_SIZE_LOCK = threading.Lock()
_SIZES = {}              # path -> (monotonic, size, files)
_REPORT_LOCK = threading.Lock()
_REPORT = {"at": 0.0, "data": None, "building": False, "error": None}


def tree_size(path):
    """(bytes, files) below path; links are not followed and unreadable entries are skipped."""
    key = str(path)
    now = time.monotonic()
    with _SIZE_LOCK:
        hit = _SIZES.get(key)
        if hit and now - hit[0] < SIZE_TTL:
            return hit[1], hit[2]
    total = files = 0
    stack = [Path(path)]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    with _SIZE_LOCK:
        _SIZES[key] = (now, total, files)
    return total, files


def _mounts():
    out = []
    for _label, mount in sd_mounts():
        try:
            out.append((mount, mount.resolve()))
        except OSError:
            continue
    return out


def _path_disk(path, mounts):
    """Disk id (as in storage.disks) a path lives on: a removable mount, else the internal one."""
    try:
        p = Path(path).resolve()
    except OSError:
        return "internal"
    for mount, resolved in mounts:
        if p == resolved or resolved in p.parents:
            return "sd:" + mount.name
    return "internal"


def _game_appid(game):
    for exe in game.get("exes") or []:
        if exe.get("in_steam") and exe.get("appid"):
            return exe["appid"]
    return None


def _prefix_paths(appids):
    """appid -> existing compatdata folder (looked up once per report)."""
    out = {}
    for appid in appids:
        try:
            folder = compatdata_dir(appid)
        except Exception:  # noqa: BLE001
            continue
        if folder.is_dir():
            out[appid] = folder
    return out


def build():
    """The whole report, synchronously. Called from the background thread and by the tests."""
    disk_list = disks()
    mounts = _mounts()
    parts = {d["id"]: dict.fromkeys(PARTS, 0) for d in disk_list}
    if "internal" not in parts:                     # tests and odd setups may only pass one disk
        parts["internal"] = dict.fromkeys(PARTS, 0)
    rows = {disk_id: [] for disk_id in parts}
    backups = {disk_id: {} for disk_id in parts}
    games = list_games()

    for disk in disk_list:
        disk_id = disk["id"]
        root = Path(disk["root"])
        inbox = root / "_inbox"
        if inbox.is_dir():
            parts[disk_id]["inbox"] += tree_size(inbox)[0]
        home = root / HOME_NAME
        if home.is_dir():
            try:
                for sub in home.iterdir():
                    if not sub.is_dir():
                        continue
                    size = tree_size(sub)[0]
                    parts[disk_id]["backups"] += size
                    backups[disk_id][sub.name] = size
            except OSError:
                pass

    appids = sorted({a for a in (_game_appid(g) for g in games) if a})
    prefixes = _prefix_paths(appids)

    for game in games:
        size = tree_size(game["path"])[0]
        disk_id = _path_disk(game["path"], mounts)
        if disk_id not in parts:
            disk_id = "internal"
        appid = _game_appid(game)
        prefix_size = 0
        prefix_path = None
        if appid and appid in prefixes:
            folder = prefixes[appid]
            prefix_size = tree_size(folder)[0]
            prefix_path = str(folder)
            prefix_disk = _path_disk(folder, mounts)
            if prefix_disk not in parts:
                prefix_disk = "internal"
            parts[prefix_disk]["prefixes"] += prefix_size
        parts[disk_id]["games"] += size
        rows[disk_id].append({"name": game.get("name") or Path(game["path"]).name,
                              "title": game.get("title") or game.get("name") or Path(game["path"]).name,
                              "path": game["path"], "disk": game.get("disk") or disk_id,
                              "size": size, "prefix": prefix_size, "prefix_path": prefix_path,
                              "backup": backups.get(disk_id, {}).get(Path(game["path"]).name, 0),
                              "appid": appid})

    duplicates = _duplicates(rows)
    out = []
    for disk in disk_list:
        disk_id = disk["id"]
        free, total = disk.get("free"), disk.get("total")
        used = total - free if total is not None and free is not None else None
        known = sum(parts[disk_id][k] for k in PARTS if k != "other")
        parts[disk_id]["other"] = max(0, used - known) if used is not None else 0
        out.append({"id": disk_id, "label": disk.get("label") or disk_id, "root": disk.get("root"),
                    "free": free, "total": total, "used": used,
                    "parts": parts[disk_id],
                    "games": sorted(rows[disk_id], key=lambda r: -r["size"])[:TOP],
                    "prefixes": sorted(({"title": r["title"], "appid": r["appid"], "size": r["prefix"],
                                         "path": r["prefix_path"]}
                                        for r in rows[disk_id] if r["prefix"]),
                                       key=lambda r: -r["size"])[:PREFIX_TOP],
                    "duplicates": [d for d in duplicates if disk_id in d["disks"]]})
    return {"at": int(time.time()), "disks": out}


def _duplicates(rows):
    """Games with the same normalized title on different disks: likely copies, only a hint."""
    groups = {}
    for disk_id, items in rows.items():
        for row in items:
            key = norm_title(row["title"] or row["name"])
            if not key:
                continue
            groups.setdefault(key, []).append((disk_id, row))
    out = []
    for items in groups.values():
        disk_ids = sorted({disk_id for disk_id, _ in items})
        if len(disk_ids) < 2:
            continue
        paths = [row["path"] for _, row in items]
        out.append({"title": items[0][1]["title"], "size": sum(row["size"] for _, row in items),
                    "disks": disk_ids, "paths": paths})
    out.sort(key=lambda d: -d["size"])
    return out[:10]


def _run():
    try:
        data = build()
        with _REPORT_LOCK:
            _REPORT.update(at=time.time(), data=data, error=None)
    except Exception as e:  # noqa: BLE001
        with _REPORT_LOCK:
            _REPORT.update(at=time.time(), error=str(e))
    finally:
        with _REPORT_LOCK:
            _REPORT["building"] = False


def report(refresh=False):
    """The last report; starts a new walk when there is none, it is old, or refresh is asked for."""
    now = time.time()
    with _REPORT_LOCK:
        if refresh:
            _SIZES.clear()
            _REPORT["at"] = 0.0
        age = now - _REPORT["at"]
        if not _REPORT["building"] and (_REPORT["data"] is None or age > CACHE_TTL):
            _REPORT["building"] = True
            threading.Thread(target=_run, daemon=True).start()
        data = _REPORT["data"]
        return {"at": int(_REPORT["at"]), "building": _REPORT["building"], "error": _REPORT["error"],
                "stale": bool(data and now - _REPORT["at"] > CACHE_TTL),
                "disks": (data or {}).get("disks") or []}
