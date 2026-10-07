"""Downloads by link: plain HTTP and Mega, queueing and cancelling."""

import re
import threading
import time
import urllib.error

from .archives import finish
from .config import CHUNK
from .i18n import carry, tr
from .jobs import CANCELLABLE, JOBS, LOCK, drop_part, fail, new_job
from .mega import mega_folder_files, mega_parse_link, run_mega_download, run_mega_folder
from .net import dl_proxy, http_get, resolve_url
from .paths import filename_from_response, reserve_path, safe_name
from .storage import root_for


def run_download(job, url):
    part = None
    try:
        if job.cancel:
            raise RuntimeError(tr("status.cancelled"))
        job.status = "resolving"
        real = resolve_url(url)
        with http_get(real, proxy=dl_proxy()) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                raise RuntimeError(tr("dl.html_page"))
            name = filename_from_response(r.headers, r.geturl())
            job.total = int(r.headers.get("Content-Length") or 0)
            dest, part = reserve_path(job.root / "_inbox", name)
            job.work = str(part)
            job.label = name
            job.status = "downloading"
            job.started = time.time()
            with open(part, "wb") as f:
                while True:
                    if job.cancel:
                        raise RuntimeError(tr("status.cancelled"))
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    job.done += len(chunk)
        if job.total and job.done != job.total:
            raise RuntimeError(tr("dl.incomplete", done=job.done, total=job.total))
        part.rename(dest)
        part = None
        job.file = str(dest)
        finish(job, dest)
    except urllib.error.HTTPError as e:
        drop_part(part)
        part = None
        fail(job, f"HTTP {e.code} {e.reason}")
    except Exception as e:  # noqa: BLE001
        drop_part(part)
        part = None
        fail(job, str(e))
    finally:
        drop_part(part)


def start_download(url, disk=None, update=None, root=None):
    """Download a link into the inbox. With `update` ({game, exe, title}) it is a new version of
    that game: it lands on the game's disk and waits on the game page instead of becoming a game."""
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        raise ValueError(tr("err.need_url"))
    link = mega_parse_link(url)         # Mega is encrypted and has its own downloader
    if update and link and link["kind"] == "folder" and not link.get("node"):
        raise ValueError(tr("gup.mega_folder"))
    job = new_job("download", url, root or (root_for(disk) if disk else None))
    job.update = update
    threading.Thread(target=carry(run_mega_download if link else run_download),
                     args=(job, link or url), daemon=True).start()
    return job


def cancel_job(job_id):
    """Stop a download. One still waiting in the queue stops on the spot."""
    with LOCK:
        job = JOBS.get(job_id)
    if not job or job.kind != "download" or job.status not in CANCELLABLE:
        return False
    job.cancel = True
    if job.status == "queued":
        job.status = "cancelled"
    return True


def cancel_all():
    """Stop every download; the ones that never started leave the list straight away."""
    with LOCK:
        jobs = list(JOBS.values())
    stopped = 0
    for job in jobs:
        was = job.status
        if cancel_job(job.id):
            stopped += 1
            if was == "queued":
                with LOCK:
                    JOBS.pop(job.id, None)
    return stopped


def start_mega_downloads(url, nodes, disk=None):
    """Queue what was picked in a Mega folder: one file as usual, several as one game."""
    link = mega_parse_link(url)
    if not link:
        raise ValueError(tr("mega.not_mega"))
    if link["kind"] == "file":
        return [start_download(url, disk).to_dict()]
    data = mega_folder_files(link)
    files = {f["h"]: f for f in data["files"]}
    picked = [files[h] for h in dict.fromkeys(nodes or []) if h in files]
    if not picked:
        raise ValueError(tr("mega.nothing_picked"))
    root = root_for(disk) if disk else None
    if len(picked) == 1:
        f = picked[0]
        job = new_job("download", f["name"], root)
        job.total = f["size"]
        target, args = run_mega_download, (job, dict(link, node=f["h"]))
    else:
        job = new_job("download", safe_name(data["name"]), root)
        job.total = sum(f["size"] for f in picked)
        target, args = run_mega_folder, (job, link, picked)
    threading.Thread(target=carry(target), args=args, daemon=True).start()
    return [job.to_dict()]
