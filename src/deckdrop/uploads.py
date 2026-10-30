"""A whole game folder from a PC: the browser sends it file by file into the inbox.

The page picks a folder (or a folder dropped onto it), asks the server for a job, sends every
file with its relative path, and tells the server when it is done. Then the folder is treated
as a finished download: an archive among the files is unpacked as usual, anything else is moved
into the games root as a game and, when the settings ask for it, added to Steam.
"""

import shutil
import threading
from pathlib import Path

from .adopt import clean_temp, maybe_add, move_game
from .archives import finish
from .config import log
from .i18n import carry, tr
from .jobs import JOBS, LOCK, fail, new_job
from .paths import archive_volume, safe_name, safe_rel, unique_file
from .storage import inside, root_for

MAX_FILES = 100_000      # a game has thousands of files; more than this is a whole disk by mistake


def begin(name, disk=None, total=0, files=0):
    """Start an upload: a job that owns a temporary folder in the inbox."""
    if files > MAX_FILES:
        raise ValueError(tr("tree.too_many", n=MAX_FILES))
    name = safe_name((name or "").strip() or "game")
    job = new_job("upload", name, root_for(disk) if disk else None)
    job.total = max(0, int(total or 0))
    job.files_total = int(files or 0)
    job.files_done = 0
    work = Path(job.root) / "_inbox" / f".tree-{job.id}"
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(tr("tree.bad_dir", error=e)) from e
    job.work = str(work)
    probe = Path(job.root) if Path(job.root).exists() else Path(job.root).parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        free = None
    if free is not None and job.total and free < job.total + (200 << 20):
        job.status, job.error = "error", tr("tree.no_space", need=job.total >> 30, free=free >> 30)
        shutil.rmtree(work, ignore_errors=True)
        job.work = None
        raise ValueError(job.error)
    job.status = "uploading"
    log(f"upload {job.id}: '{name}' ({files} files, {job.total} bytes) -> {work}")
    return job


def save_file(job_id, rel, write_body):
    """One file of the folder, at its relative path."""
    with LOCK:
        job = JOBS.get(job_id)
    if not job or job.kind != "upload" or job.status != "uploading" or not job.work:
        raise ValueError(tr("tree.bad_job"))
    rel = safe_rel(rel)
    if not rel:
        raise ValueError(tr("tree.bad_path"))
    root = Path(job.work).resolve()
    dest = (root / rel)
    try:
        resolved = dest.resolve()
    except OSError:
        resolved = dest
    if resolved != root and root not in resolved.parents:
        raise ValueError(tr("tree.bad_path"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_body(dest, job)
    job.files_done += 1
    return dest


def finish_tree(job_id):
    """All files are in: unpack or adopt the folder in the background."""
    with LOCK:
        job = JOBS.get(job_id)
    if not job or job.kind != "upload" or job.status not in ("uploading", "queued"):
        raise ValueError(tr("tree.bad_job"))
    job.status = "importing"
    threading.Thread(target=carry(run_finish), args=(job,), daemon=True).start()
    return job


def abort(job_id, reason=""):
    """The browser gave up (a file failed, the tab was closed): drop what was received."""
    with LOCK:
        job = JOBS.get(job_id)
    if not job or job.kind != "upload" or job.status not in ("uploading", "queued", "importing"):
        return False
    work = Path(job.work) if job.work else None
    if work and work.is_dir() and inside(job.root, work):
        shutil.rmtree(work, ignore_errors=True)
    job.work = None
    if reason:
        job.status, job.error = "error", reason
    else:
        job.status = "cancelled"
    log(f"upload {job.id} aborted: {reason or 'cancelled'}")
    return True


def run_finish(job):
    work = Path(job.work) if job.work else None
    try:
        if work is None or not work.is_dir():
            raise RuntimeError(tr("tree.empty"))
        items = [p for p in work.rglob("*") if p.is_file()]
        if not items:
            raise RuntimeError(tr("tree.empty"))
        top = [p for p in work.iterdir() if p.name not in (".DS_Store", "Thumbs.db")]
        single = top[0] if len(top) == 1 else None
        if single is not None and single.is_file() and archive_volume(single.name):
            dest = unique_file(Path(job.root) / "_inbox", single.name)
            shutil.move(str(single), str(dest))
            job.file = str(dest)
            job.status = "extracting"
            finish(job, dest)             # unpack; may stop and ask for a password
            if job.status == "done":
                maybe_add(job)
        else:
            job.status = "importing"
            move_game(job, work, job.label)
            job.status = "done"
            maybe_add(job)
            log(f"job {job.id} done: {job.game_dir}")
    except Exception as e:  # noqa: BLE001
        fail(job, tr("tree.failed", error=e))
    finally:
        if work is not None and inside(job.root, work) and work.is_dir():
            if job.status in ("done", "needs_password"):
                shutil.rmtree(work, ignore_errors=True)   # everything worth keeping was moved out
            else:
                clean_temp(work)                          # a failed job keeps its files for a look
            if not work.exists():
                job.work = None
