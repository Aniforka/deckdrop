"""Move a game to another disk, keeping its Steam shortcut, AppID and Proton prefix.

The folder is renamed on the same disk and copied file by file across disks. Only after the copy
is verified does the old folder go away. The shortcut is pointed at the new executable through
Steam control, so its AppID (and with it the Proton prefix, artwork and controller layout) stays.
"""

import copy
import os
import shutil
import threading
import time
from pathlib import Path

from .config import CHUNK, log
from .gameupdate import UPDATES, backup_dir, game_running
from .games import game_dir_path
from .i18n import carry, tr
from .jobs import ACTIVE, JOBS, LOCK, new_job
from .state import STATE, STATE_LOCK, save_state
from .steam.cdp import CDP
from .steam.library import shortcuts_index
from .steam.shortcuts import queue_pending
from .storage import disks, game_roots, inside, root_for
from .usage import tree_size


class _Cancelled(Exception):
    pass


def _human(n):
    return f"{n / (1 << 30):.1f} GB" if n >= 1 << 30 else f"{n / (1 << 20):.0f} MB"


def _relative(path, root):
    return Path(path).resolve().relative_to(Path(root).resolve())


def _steam_links(src):
    """{absolute exe: appid} for the shortcuts that start inside the game folder.

    A shortcut whose app id is not known yet cannot be moved safely: Steam would keep pointing
    at a folder that is no longer there. The caller refuses the move instead.
    """
    src = Path(src).resolve()
    try:
        index = shortcuts_index()
    except Exception as e:  # noqa: BLE001
        log(f"move: shortcuts: {e}")
        index = {}
    with STATE_LOCK:
        recs = {k: dict(v) for k, v in (STATE.get("added") or {}).items() if inside(src, k)}
    links = {}
    for exe, rec in recs.items():
        appid = rec.get("appid") or ((index.get(exe) or {}).get("appid"))
        if not (rec.get("appid") or rec.get("via") or exe in index):
            continue
        if not appid:
            raise ValueError(tr("move.no_appid", name=Path(exe).name))
        links[str(Path(exe).resolve())] = appid
    for exe, sc in index.items():
        if inside(src, exe):
            links[str(Path(exe).resolve())] = sc["appid"]
    if links and not CDP.available():
        raise ValueError(tr("move.need_steam"))
    return links


def _plan(game, disk_id):
    src = game_dir_path(game)
    known = {d["id"] for d in disks()}
    if disk_id not in known:
        raise ValueError(tr("move.no_disk"))
    root = root_for(disk_id)
    if not root.parent.is_dir():
        raise ValueError(tr("move.no_disk"))
    if src.parent.resolve() == root.resolve():
        raise ValueError(tr("move.same"))
    target = root / src.name
    if target.exists() or target.is_symlink():
        raise ValueError(tr("move.exists", name=src.name))
    if game_running(src):
        raise ValueError(tr("move.running"))
    if UPDATES.get(str(src)):
        raise ValueError(tr("move.update"))
    with LOCK:
        jobs = list(JOBS.values())
    if any(j.kind == "move" and j.game == str(src) and j.status in ACTIVE for j in jobs):
        raise ValueError(tr("move.busy"))
    if any(j.update and j.update.get("game") == str(src) and j.status in ACTIVE for j in jobs):
        raise ValueError(tr("move.update"))
    links = _steam_links(src)
    size, files = tree_size(src)
    try:
        root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(root).free
    except OSError as e:
        raise ValueError(tr("move.no_disk")) from e
    need = size + max(200 << 20, int(size * 0.05))
    if free < need:
        raise ValueError(tr("move.no_space", need=_human(need), free=_human(free)))
    old_backup, new_backup = backup_dir(src), backup_dir(target)
    if old_backup.exists() and new_backup.exists():
        raise ValueError(tr("move.backup_exists"))
    return {"src": src, "target": target, "root": root, "links": links,
            "size": size, "files": files, "backup": old_backup.exists()}


def _same_device(src, parent):
    try:
        return os.stat(src).st_dev == os.stat(parent).st_dev
    except OSError:
        return False


def _copy_file(job, src, dest):
    with open(src, "rb") as fi, open(dest, "wb") as fo:
        while True:
            if job.cancel:
                raise _Cancelled()
            chunk = fi.read(CHUNK)
            if not chunk:
                break
            fo.write(chunk)
            job.done += len(chunk)
    try:
        shutil.copystat(src, dest)
    except OSError:
        pass


def _copy_tree(job, src, target):
    target.mkdir(parents=True, exist_ok=True)
    for base, dirs, files in os.walk(src, followlinks=False):
        base = Path(base)
        rel = base.relative_to(src)
        (target / rel).mkdir(exist_ok=True)
        dirs[:] = [d for d in dirs if not (base / d).is_symlink()]
        for name in files:
            source, dest = base / name, target / rel / name
            if job.cancel:
                raise _Cancelled()
            if source.is_symlink():
                try:
                    os.symlink(os.readlink(source), dest)
                except OSError as e:
                    log(f"move: symlink {source}: {e}")
                continue
            _copy_file(job, source, dest)


def _verify(job, target):
    size, files = tree_size(target)
    if size != job.total or files != getattr(job, "files_total", files):
        raise RuntimeError(tr("move.verify", done=_human(size), total=_human(job.total)))


def _move_backup(src, target):
    old, new = backup_dir(src), backup_dir(target)
    if not old.exists():
        return False
    if new.exists():
        raise ValueError(tr("move.backup_exists"))
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(new))
    try:
        old.parent.rmdir()
    except OSError:
        pass
    return True


def _snapshot():
    with STATE_LOCK:
        return {k: copy.deepcopy(STATE.get(k)) for k in ("added", "hidden", "imported", "pending")}


def _restore(snapshot):
    with STATE_LOCK:
        STATE.update(snapshot)
        save_state()


def _migrate_state(src, target, links):
    src, target = Path(src).resolve(), Path(target).resolve()
    with STATE_LOCK:
        added = STATE.setdefault("added", {})
        for old in [k for k in added if inside(src, k)]:
            rec = added.pop(old)
            added[str(target / _relative(old, src))] = rec
        for old, appid in links.items():
            new = str(target / _relative(old, src))
            added.setdefault(new, {"at": int(time.time()), "appid": appid})
        STATE["hidden"] = [str(target) if Path(h).resolve() == src else h for h in STATE.get("hidden") or []]
        imported = []
        for path in STATE.get("imported") or []:
            path = str(target) if Path(path).resolve() == src else path
            imported.append(path)
        if any(inside(root, target) for root in game_roots()):
            imported = [p for p in imported if Path(p).resolve() != target]
        STATE["imported"] = imported
        for op in STATE.get("pending") or []:
            for key in ("exe", "old"):
                value = op.get(key)
                if value and inside(src, value):
                    op[key] = str(target / _relative(value, src))
        save_state()


def _point_shortcuts(src, target, links):
    src, target = Path(src).resolve(), Path(target).resolve()
    for old, appid in links.items():
        new = str(target / _relative(old, src))
        try:
            if CDP.available():
                CDP.set_exe(appid, f'"{new}"', f'"{Path(new).parent}"')
            else:
                queue_pending(op="exe", exe=new, old=old, appid=appid)
        except Exception as e:  # noqa: BLE001
            log(f"move: shortcut {appid}: {e}")
            queue_pending(op="exe", exe=new, old=old, appid=appid)


def _rollback(src, target, same, backup_moved, snapshot):
    """Best effort: put the folders and the state back the way they were before the move."""
    try:
        _restore(snapshot)
    except Exception as e:  # noqa: BLE001
        log(f"move: state rollback: {e}")
    try:
        if same and target.exists() and not src.exists():
            os.rename(target, src)
        elif target.exists() and src.exists():
            shutil.rmtree(target, ignore_errors=True)
    except OSError as e:
        log(f"move: folder rollback: {e}")
    if backup_moved:
        try:
            old, new = backup_dir(src), backup_dir(target)
            if new.exists() and not old.exists():
                old.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(new), str(old))
        except OSError as e:
            log(f"move: backup rollback: {e}")


def _run(job, plan):
    src, target = plan["src"], plan["target"]
    same = _same_device(src, target.parent)
    backup_moved = False
    snapshot = _snapshot()
    try:
        if same:
            os.rename(src, target)
        else:
            _copy_tree(job, src, target)
            _verify(job, target)
        backup_moved = _move_backup(src, target)
        _migrate_state(src, target, plan["links"])
        _point_shortcuts(src, target, plan["links"])
        job.game_dir = str(target)
        job.new_path = str(target)
        if not same:
            try:
                shutil.rmtree(src)
            except OSError as e:
                # the game is already at its new place and Steam points there; the old folder is
                # only leftovers, so this is a warning rather than a failed move
                job.note = tr("move.old_left", error=e)
                log(f"job {job.id}: old folder left behind: {e}")
        job.status = "done"
        job.note = job.note or tr("move.done", name=target.name)
        log(f"job {job.id}: moved {src} -> {target}")
    except _Cancelled:
        _rollback(src, target, same, backup_moved, snapshot)
        job.status = "cancelled"
        log(f"job {job.id}: move cancelled")
    except Exception as e:  # noqa: BLE001
        _rollback(src, target, same, backup_moved, snapshot)
        job.status = "error"
        job.error = str(e)
        log(f"job {job.id}: move failed: {e}")


def start(game, disk_id):
    plan = _plan(game, disk_id)
    job = new_job("move", tr("move.label", name=plan["src"].name), plan["root"])
    job.game = str(plan["src"])
    job.total = plan["size"]
    job.files_total = plan["files"]
    job.status = "moving"
    job.started = time.time()
    threading.Thread(target=carry(_run), args=(job, plan), daemon=True).start()
    return job


def cancel(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job or job.kind != "move" or job.status not in ("queued", "moving"):
        return False
    job.cancel = True
    return True
