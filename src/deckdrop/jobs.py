"""Download/extract jobs shown on the page."""

import threading
import time
from pathlib import Path

from .config import log
from .storage import default_root, disk_label_for


class Job:
    _seq = 0

    def __init__(self, kind, label, root=None):
        Job._seq += 1
        self.id = Job._seq
        self.kind = kind            # download | upload | extract
        self.label = label
        self.status = "queued"      # queued resolving downloading uploading extracting needs_password done error
        #                             update_ready (a new version unpacked aside, waiting on the game page)
        self.done = 0
        self.total = 0
        self.file = None
        self.game_dir = None
        self.error = None
        self.cancel = False
        self.work = None            # file or folder being written now, spared by the inbox cleanup
        self.update = None          # {game, exe, title}: a new version of that game, not a new game
        self.root = Path(root) if root else default_root()
        self.started = time.time()

    def to_dict(self):
        elapsed = max(time.time() - self.started, 0.001)
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
            "done": self.done, "total": self.total,
            "speed": int(self.done / elapsed) if self.status in ("downloading", "uploading") else 0,
            "file": self.file, "game_dir": self.game_dir, "error": self.error,
            "disk": disk_label_for(self.root),
            "update": (self.update or {}).get("title"),
            "update_game": (self.update or {}).get("game"),
        }


JOBS = {}
LOCK = threading.Lock()
ACTIVE = ("queued", "resolving", "downloading", "uploading", "extracting")
CANCELLABLE = ("queued", "resolving", "downloading")


def new_job(kind, label, root=None):
    with LOCK:
        job = Job(kind, label, root)
        JOBS[job.id] = job
    return job


def drop_part(part):
    """Remove a half-written file. Done before a job is marked failed, never after."""
    if part is not None:
        try:
            Path(part).unlink()
        except OSError:
            pass


def fail(job, msg):
    if job.cancel:
        job.status, job.error = "cancelled", None
        log(f"job {job.id} cancelled")
        return
    job.status = "error"
    job.error = msg
    log(f"job {job.id} error: {msg}")
