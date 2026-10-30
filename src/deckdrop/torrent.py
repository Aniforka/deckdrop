"""Torrents: give a magnet or a .torrent link to qBittorrent on this Deck and take the game.

qBittorrent is driven through its own Web API (Web UI must be enabled in its settings). DeckDrop
asks it to save into a folder under the inbox, watches the progress there, and when the torrent
is complete takes it away from the client (the files stay) and finishes like any other download:
an archive is unpacked, a folder becomes a game next to the others, and - when the settings say
so - the game is added to Steam right away.

Nothing here needs a second downloader: the torrent client the user already runs does the work.
"""

import json
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .adopt import clean_temp, maybe_add, move_game
from .archives import finish
from .config import UA, log
from .i18n import carry, tr
from .jobs import JOBS, LOCK, fail, new_job
from .net import net_reason
from .paths import archive_volume, safe_name, unique_file
from .state import STATE
from .storage import inside, root_for

PORTS = (8080, 8081, 8090)          # the qBittorrent Web UI defaults tried when no address is set
POLL = 3.0
DONE_STATES = {"uploading", "stalledup", "pausedup", "queuedup", "forcedup", "checkingup"}
BAD_STATES = {"error", "missingfiles", "unknown"}
# 100% is not the same as "the files are in place": these states are still work in progress
TRANSIENT = {"checkingdl", "checkingresume", "moving", "allocating", "metadl"}
MAGNET_RE = re.compile(r"^magnet:\?", re.I)
TORRENT_RE = re.compile(r"^https?://\S+\.torrent(?:[?#]\S*)?$", re.I)


def is_torrent(link):
    link = (link or "").strip()
    return bool(MAGNET_RE.match(link) or TORRENT_RE.match(link))


def torrent_name(link):
    if MAGNET_RE.match(link):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        name = (q.get("dn") or [""])[0].strip()
        return safe_name(name) if name else tr("torrent.unnamed")
    return safe_name(urllib.parse.unquote(urllib.parse.urlparse(link).path.rsplit("/", 1)[-1]))


def urls():
    """Addresses to try, most likely first: the one set in the settings, else the local defaults."""
    configured = (STATE.get("torrent_url") or "").strip()
    if configured:
        if not re.match(r"^https?://", configured, re.I):
            configured = "http://" + configured
        return [configured.rstrip("/")]
    return [f"http://127.0.0.1:{p}" for p in PORTS]


def credentials():
    user = (STATE.get("torrent_user") or "admin").strip() or "admin"
    password = STATE.get("torrent_pass") or ""
    if password:
        return [(user, password)]
    # nothing saved yet: the qBittorrent default (admin/adminadmin) and then a Web UI
    # that lets localhost in without a password at all. Two tries, so the client's
    # "ban after 5 failed logins" protection is not tripped by a mere check.
    return [(user, "adminadmin"), (user, "")]


class QB:
    """The part of the qBittorrent Web API (v2) that DeckDrop needs."""

    def __init__(self, url, user="", password=""):
        self.base = url.rstrip("/")
        self.user, self.password = user, password
        self.sid = None

    def call(self, path, fields=None, timeout=25):
        data = urllib.parse.urlencode(fields).encode() if fields is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers={
            "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
        if self.sid:
            req.add_header("Cookie", "SID=" + self.sid)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                m = re.search(r"SID=([^;]+)", r.headers.get("Set-Cookie") or "")
                if m:
                    self.sid = m.group(1)
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read() or b""

    def version(self):
        code, body = self.call("/api/v2/app/version", timeout=6)
        if code == 200:
            return body.decode("utf-8", "replace").strip() or "?"
        raise PermissionError(tr("torrent.need_login"))

    def login(self):
        code, body = self.call("/api/v2/auth/login",
                               {"username": self.user, "password": self.password}, timeout=10)
        text = body.decode("utf-8", "replace").strip()
        if code != 200 or text.lower().startswith("fail"):
            raise PermissionError(tr("torrent.bad_login"))

    def add(self, link, save_dir, tag):
        code, body = self.call("/api/v2/torrents/add", {
            "urls": link, "savepath": str(save_dir), "tags": tag,
            "paused": "false", "stopped": "false", "autoTMM": "false"}, timeout=40)
        text = body.decode("utf-8", "replace").strip()
        if code != 200 or not text.lower().startswith("ok"):
            raise RuntimeError(tr("torrent.add_failed", detail=(text or f"HTTP {code}")[:160]))

    def info(self, tag=None):
        query = ("?" + urllib.parse.urlencode({"tag": tag})) if tag else ""
        code, body = self.call("/api/v2/torrents/info" + query, timeout=15)
        if code != 200:
            raise RuntimeError(f"qBittorrent: HTTP {code}")
        return json.loads(body or b"[]")

    def remove(self, hash_, delete_files=False):
        self.call("/api/v2/torrents/delete",
                  {"hashes": hash_, "deleteFiles": "true" if delete_files else "false"}, timeout=15)


def _identify(url):
    """(client, version) when a qBittorrent Web UI answers there; version is "" when a login is due."""
    c = QB(url)
    try:
        code, body = c.call("/api/v2/app/version", timeout=5)
    except (urllib.error.URLError, OSError) as e:
        return None, net_reason(e)
    if code == 200:
        ver = body.decode("utf-8", "replace").strip()
        # another local server (Steam's own CEF, a router page) answers 200 to any path: only a
        # qBittorrent-looking version makes this the client
        return (c, ver or "?") if re.match(r"^v?\d+\.\d+", ver) else (None, "HTTP 200, not qBittorrent")
    if code == 403:
        return c, ""
    return None, f"HTTP {code}"


def connect():
    """A logged-in qBittorrent: the configured address, or the first local port that answers."""
    errors = []
    for url in urls():
        client, ver = _identify(url)
        if client is None:
            errors.append(f"{url}: {ver}")
            continue
        last = None
        for user, password in credentials():
            client.user, client.password = user, password
            try:
                client.login()
                return client, (ver or client.version())
            except PermissionError as e:
                last = e
            except (urllib.error.URLError, OSError) as e:
                last = RuntimeError(net_reason(e))
        raise RuntimeError(f"{url}: {last}") from last
    raise RuntimeError(tr("torrent.not_found", tried="; ".join(errors[-2:]) or "-"))


def probe():
    """What the Settings tab shows after "check": is the client there and does it let us in."""
    try:
        client, ver = connect()
        return {"ok": True, "url": client.base, "version": ver}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "tried": urls()}


def start(link, disk=None):
    link = (link or "").strip()
    if not is_torrent(link):
        raise ValueError(tr("torrent.need_link"))
    job = new_job("torrent", torrent_name(link) or tr("torrent.unnamed"),
                  root_for(disk) if disk else None)
    threading.Thread(target=carry(run), args=(job, link), daemon=True).start()
    return job


def cancel(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job or job.kind != "torrent" or job.status not in ("queued", "downloading"):
        return False
    job.cancel = True
    return True


def wait(client, job, tag, save_dir=None):
    """Follow the torrent until it is complete; returns (path with the content, last info row)."""
    last = None
    while True:
        if job.cancel:
            if last and last.get("hash"):
                try:
                    client.remove(last["hash"], delete_files=True)
                except Exception as e:  # noqa: BLE001
                    log(f"job {job.id}: remove cancelled torrent: {e}")
            raise RuntimeError(tr("status.cancelled"))
        try:
            rows = client.info(tag)
            if not rows and save_dir is not None:
                # no tag support (or the tag was not applied): find it by its own save folder
                rows = [r for r in client.info() if Path(r.get("save_path") or "") == Path(save_dir)]
        except (urllib.error.URLError, OSError, RuntimeError, ValueError) as e:
            log(f"job {job.id}: qBittorrent is not answering: {e}")
            rows = []
        if rows:
            t = last = rows[0]
            size = int(t.get("total_size") or t.get("size") or 0)
            if size:
                job.total = size
                job.done = min(size, int(size * float(t.get("progress") or 0)))
            job.speed = int(t.get("dlspeed") or 0)
            if t.get("name"):
                job.label = t["name"]
            state = (t.get("state") or "").lower()
            finished = (float(t.get("progress") or 0) >= 0.999 and state not in TRANSIENT) or state in DONE_STATES
            if finished:
                path = t.get("content_path")
                if not path:
                    path = str(Path(t.get("save_path") or ".") / (t.get("name") or ""))
                if Path(path).exists() or state in DONE_STATES:
                    return Path(path), t
                log(f"job {job.id}: qBittorrent has not put the files in place yet ({state})")
            if state in BAD_STATES:
                raise RuntimeError(tr("torrent.client_error", state=state))
        time.sleep(POLL)


def run(job, link):
    client, save_dir, content = None, None, None
    try:
        client, ver = connect()
        log(f"job {job.id}: qBittorrent {ver} at {client.base}")
        save_dir = Path(job.root) / "_inbox" / f".torrent-{job.id}"
        save_dir.mkdir(parents=True, exist_ok=True)
        job.work = str(save_dir)
        tag = f"deckdrop-{job.id}"
        job.status = "downloading"
        job.started = time.time()
        client.add(link, save_dir, tag)
        content, row = wait(client, job, tag, save_dir)
        job.done = job.total or job.done
        job.status = "importing"
        job.work = str(content) if content.exists() else job.work
        try:                            # detach first: the files are about to change places
            client.remove(row.get("hash") or "", delete_files=False)
        except Exception as e:  # noqa: BLE001
            log(f"job {job.id}: remove finished torrent: {e}")
        if content.is_dir():
            move_game(job, content, job.label)
            job.status = "done"
            maybe_add(job)
            log(f"job {job.id} done: {job.game_dir}")
        elif content.is_file():
            dest = unique_file(Path(job.root) / "_inbox", content.name)
            shutil.move(str(content), str(dest))
            job.file = str(dest)
            if archive_volume(dest.name):
                job.status = "extracting"
                finish(job, dest)
                if job.status == "done":
                    maybe_add(job)
            else:
                job.status = "done"
                log(f"job {job.id} done: {dest}")
        else:
            raise RuntimeError(tr("torrent.no_files", name=job.label))
    except Exception as e:  # noqa: BLE001
        fail(job, tr("torrent.failed", error=e))
    finally:
        if save_dir is not None and inside(job.root, save_dir) and save_dir.is_dir():
            if job.status in ("done", "needs_password"):
                shutil.rmtree(save_dir, ignore_errors=True)   # what was inside now lives next to the games
            else:
                clean_temp(save_dir)                          # a failed job keeps its files for a look
            if not save_dir.exists():
                job.work = None
