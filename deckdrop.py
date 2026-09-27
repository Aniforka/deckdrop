#!/usr/bin/env python3
"""DeckDrop - LAN inbox, Steam helper and media gallery for Steam Deck.

Open http://<deck>.local:8088 from a phone or PC, paste a link or drop a file.
The Deck downloads it (to the internal disk or a microSD, Mega links included,
decrypted on the fly), unpacks archives
(asking for a password when needed), adds the game to Steam under a clean name,
picks the default Proton, and sets every kind of Steam cover art: from VNDB for
visual novels, or built from the exe icon. Extra tabs: archives, and a
password-protected gallery of Steam screenshots and clips. Save games can be
backed up to a zip and imported back.

Steam is driven live through its CEF remote-debugging port (the same mechanism
Decky Loader uses). DeckDrop enables it with a marker file; it becomes active
after one Steam restart (a Deck reboot). Until then, name/Proton changes are
queued and applied automatically later.

Stdlib only, runs on stock SteamOS (nothing to install, survives OS updates).

    python3 deckdrop.py             run in foreground
    python3 deckdrop.py --install   install as a systemd user service (autostart, works in Gaming Mode)
    python3 deckdrop.py --uninstall

Env overrides: DECKDROP_PORT (8088), DECKDROP_GAMES (~/Games), DECKDROP_STATE (state file),
               DECKDROP_STEAM (Steam root), DECKDROP_UPDATE_URL, DECKDROP_PIN (initial admin PIN, random if unset),
               DECKDROP_DISKS (extra roots "label=path;..."), DECKDROP_CEF=0 (no Steam control),
               DECKDROP_CEF_PORT (8080), DECKDROP_EXTRACT=0 (don't unpack), DECKDROP_KEEP=0
"""
__version__ = "0.3.24"

import base64
import hashlib
import http.client
import hmac
import json
import mmap
import os
import re
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import zipfile
import zlib
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("DECKDROP_PORT", "8088"))
CEF_PORT = int(os.environ.get("DECKDROP_CEF_PORT", "8080"))
CEF_ENABLED = os.environ.get("DECKDROP_CEF", "1") != "0"
GAMES_DIR = Path(os.environ.get("DECKDROP_GAMES", str(Path.home() / "Games")))
STATE_FILE = Path(os.environ.get("DECKDROP_STATE", str(Path.home() / ".config" / "deckdrop" / "state.json")))
CACHE_DIR = Path.home() / ".cache" / "deckdrop"
AUTO_EXTRACT = os.environ.get("DECKDROP_EXTRACT", "1") != "0"
KEEP_ARCHIVE = os.environ.get("DECKDROP_KEEP", "1") != "0"
UPDATE_URL_DEFAULT = os.environ.get("DECKDROP_UPDATE_URL",
                                    "https://github.com/Aniforka/deckdrop/releases/latest/download/deckdrop.py")
# where versions up to 0.3.23 updated from; a remembered copy of it is moved to the release link
UPDATE_URL_LEGACY = ("https://raw.githubusercontent.com/aniforka/deckdrop/main/deckdrop.py",)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
VNDB_UA = f"DeckDrop/{__version__} (Steam Deck cover art)"
ARCHIVE_EXTS = (".zip", ".7z", ".rar", ".tar", ".tgz", ".txz", ".tbz2",
                ".tar.gz", ".tar.xz", ".tar.bz2")
# executables that are almost never the game itself
SKIP_EXE = re.compile(r"unins|vc_?redist|dxsetup|dxwebsetup|crashhandler|"
                      r"^python|notification_helper|^setup_?vc|^unitycrash", re.I)
LINUX_EXTS = (".sh", ".x86_64", ".x86")
CHUNK = 1 << 20
PNG_SIG = b"\x89PNG\r\n\x1a\n"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
        ".webp": "image/webp", ".mp4": "video/mp4", ".m4v": "video/mp4", ".mkv": "video/x-matroska",
        ".webm": "video/webm", ".mov": "video/quicktime", ".svg": "image/svg+xml",
        ".zip": "application/zip"}
FFMPEG = shutil.which("ffmpeg")


def _steam_root():
    env = os.environ.get("DECKDROP_STEAM")
    if env:
        return Path(env)
    for cand in (Path.home() / ".local/share/Steam", Path.home() / ".steam/steam", Path.home() / ".steam/root"):
        if (cand / "userdata").is_dir():
            return cand
    return Path.home() / ".local/share/Steam"


STEAM_ROOT = _steam_root()
MEDIA_DIRS = [Path.home() / "Videos", Path.home() / "Pictures"]


def log(msg):
    sys.stderr.write(time.strftime("%H:%M:%S ") + msg + "\n")
    sys.stderr.flush()

# --------------------------------------------------------------------------- persistent state

STATE_LOCK = threading.RLock()
STATE = {
    "admin_pin": os.environ.get("DECKDROP_PIN", ""),   # empty: a random one is made on first start
    "hidden": [],                 # game dirs hidden from the list
    "imported": [],               # single game folders located outside the DeckDrop roots
    "added": {},                  # exe path -> {at, appid, name, art, ...}
    "pending": [],                # steam ops waiting for CEF control: {op, exe, ...}
    "update_url": UPDATE_URL_DEFAULT,
    "media_pw": None,             # {salt, hash}
    "default_compat": "proton_experimental",
    "prefer_linux": True,
    "vndb_auto": True,
    "vndb_nsfw": True,             # False = skip 18+ images from VNDB
    "archive_passwords": [],
    "default_disk": "internal",
    "proxy": "",                  # DeckDrop-only proxy: socks5://host:port or http://host:port
    "proxy_downloads": False,     # also route game downloads through it
    "mega_verify": True,          # check Mega's own checksum after a download
    "cef_enabled": CEF_ENABLED,
}
SETTING_KEYS = ("default_compat", "prefer_linux", "vndb_auto", "vndb_nsfw", "archive_passwords",
                "default_disk", "cef_enabled", "update_url", "proxy", "proxy_downloads",
                "mega_verify")
PROTECTED_KEYS = ("proxy",)   # may carry credentials: PIN required to read or change


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text("utf-8"))
        if isinstance(data, dict):
            STATE.update(data)
    except (OSError, ValueError):
        pass
    if str(STATE.get("update_url") or "").strip().lower() in UPDATE_URL_LEGACY:
        STATE["update_url"] = UPDATE_URL_DEFAULT


def save_state():
    with STATE_LOCK:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(STATE, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, STATE_FILE)


def set_state(**kv):
    with STATE_LOCK:
        STATE.update(kv)
        save_state()


def added_rec(exe, create=False):
    with STATE_LOCK:
        recs = STATE.setdefault("added", {})
        if create and exe not in recs:
            recs[exe] = {"at": int(time.time())}
        return dict(recs.get(exe, {}))


def update_added(exe, **kv):
    with STATE_LOCK:
        rec = STATE.setdefault("added", {}).setdefault(exe, {"at": int(time.time())})
        rec.update(kv)
        save_state()


def check_pin(pin):
    stored = str(STATE.get("admin_pin") or "")
    ok = bool(stored) and hmac.compare_digest(str(pin or ""), stored)
    if not ok:
        time.sleep(1)  # slow down guessing
        raise PermissionError("неверный PIN")


def ensure_pin():
    """Give a fresh install a random admin PIN. Returns True when one was just made."""
    if STATE.get("admin_pin"):
        return False
    set_state(admin_pin=f"{secrets.randbelow(10 ** 4):04d}")
    return True


def pw_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000).hex()
    return {"salt": salt, "hash": digest}


MEDIA_TOKENS = {}  # token -> expiry


def media_login(password):
    rec = STATE.get("media_pw")
    if not rec:
        raise PermissionError("пароль медиа ещё не задан")
    if not hmac.compare_digest(pw_hash(password, rec["salt"])["hash"], rec["hash"]):
        time.sleep(1)
        raise PermissionError("неверный пароль")
    token = secrets.token_urlsafe(24)
    now = time.time()
    for t, exp in list(MEDIA_TOKENS.items()):
        if exp < now:
            del MEDIA_TOKENS[t]
    MEDIA_TOKENS[token] = now + 12 * 3600
    return token


def media_token_ok(token):
    return bool(token) and MEDIA_TOKENS.get(token, 0) > time.time()

# --------------------------------------------------------------------------- disks / roots

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

    add("internal", "Внутренний", GAMES_DIR)
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
    return "Внутренний" if inside(Path.home(), p) else "своя папка"


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

# --------------------------------------------------------------------------- jobs

class Job:
    _seq = 0

    def __init__(self, kind, label, root=None):
        Job._seq += 1
        self.id = Job._seq
        self.kind = kind            # download | upload | extract
        self.label = label
        self.status = "queued"      # queued resolving downloading uploading extracting needs_password done error
        self.done = 0
        self.total = 0
        self.file = None
        self.game_dir = None
        self.error = None
        self.cancel = False
        self.work = None            # file or folder being written now, spared by the inbox cleanup
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

# --------------------------------------------------------------------------- names / paths

def safe_name(name):
    name = urllib.parse.unquote(name)
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip(" .")
    return name or "download.bin"


def archive_ext(name):
    low = name.lower()
    for ext in sorted(ARCHIVE_EXTS, key=len, reverse=True):
        if low.endswith(ext):
            return ext
    return None


def split_ext(name):
    ext = archive_ext(name) or Path(name).suffix
    return (name[:-len(ext)] if ext else name), ext


def archive_volume(name):
    """'primary' for an archive or the first part of a split one, 'secondary' for the other parts."""
    low = name.lower()
    m = re.search(r"\.part(\d+)\.rar$", low) or re.search(r"\.(?:7z|zip|rar|tar)\.(\d{3})$", low)
    if m:
        return "primary" if int(m.group(1)) == 1 else "secondary"
    if re.search(r"\.[rz]\d{2}$", low):
        return "secondary"                    # old-style .r00 / .z01 next to the .rar / .zip
    return "primary" if archive_ext(name) else None


def archive_stem(name):
    """Game folder name for an archive: 'Game.part1.rar' and 'Game.7z.001' both give 'Game'."""
    stem, _ = split_ext(name)
    return re.sub(r"(?:\.part\d+|\.(?:7z|zip|rar|tar))$", "", stem, flags=re.I) or stem


def reserve_path(directory, name):
    """Pick a non-existing path in `directory` and reserve its .part file."""
    directory.mkdir(parents=True, exist_ok=True)
    stem, ext = split_ext(name)
    with LOCK:
        i = 1
        while True:
            cand = directory / (name if i == 1 else f"{stem} ({i}){ext}")
            part = cand.with_name(cand.name + ".part")
            if not cand.exists() and not part.exists():
                part.touch()
                return cand, part
            i += 1


def unique_dir(directory, name):
    i = 1
    while True:
        cand = directory / (name if i == 1 else f"{name} ({i})")
        if not cand.exists():
            return cand
        i += 1


def filename_from_response(headers, url):
    cd = headers.get("Content-Disposition", "") or ""
    m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", cd)
    if m:
        try:
            return safe_name(urllib.parse.unquote(m.group(2).strip(), encoding=m.group(1) or "utf-8"))
        except (UnicodeError, LookupError):
            pass
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd) or re.search(r"filename\s*=\s*([^;]+)", cd)
    if m:
        return safe_name(m.group(1).strip())
    tail = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    return safe_name(tail) if tail else "download.bin"

# --------------------------------------------------------------------------- resolvers

# --------------------------------------------------------------------------- network: DeckDrop-only proxy

SOCKS_ERR = {1: "общая ошибка прокси", 2: "прокси запретил соединение", 3: "сеть недоступна",
             4: "хост недоступен", 5: "соединение отклонено", 6: "истёк TTL",
             7: "команда не поддерживается", 8: "тип адреса не поддерживается"}


def mask_proxy(url):
    """Proxy string with the credentials blanked out, safe to show without the PIN."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return "***"
    if not (u.username or u.password):
        return url
    host = u.hostname or ""
    if u.port:
        host += f":{u.port}"
    return f"{u.scheme}://***:***@{host}"


def proxy_url():
    return (STATE.get("proxy") or "").strip() or None


def dl_proxy():
    return proxy_url() if STATE.get("proxy_downloads") else None


def net_reason(e):
    """Network exception -> short Russian explanation with a hint about blocking."""
    err = e
    while isinstance(err, urllib.error.URLError) and not isinstance(err, urllib.error.HTTPError):
        err = err.reason if isinstance(err.reason, BaseException) else err.reason
        break
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code} {e.reason}"
    text = str(err) or type(e).__name__
    low = text.lower()
    if "reset" in low or "104" in low or "10054" in low:
        return f"соединение сброшено ({text}). Обычно это блокировка со стороны сети: попробуй прокси в настройках"
    if "timed out" in low or "timeout" in low:
        return f"нет ответа ({text}). Похоже на блокировку или медленную сеть: попробуй прокси в настройках"
    if "name or service" in low or "getaddrinfo" in low or "resolve" in low:
        return f"имя хоста не разрешается ({text}): проверь интернет на деке"
    if "refused" in low:
        return f"соединение отклонено ({text})"
    if "certificate" in low or "ssl" in low:
        return f"ошибка TLS ({text})"
    return text


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("прокси закрыл соединение")
        buf += chunk
    return buf


def socks5_connect(px, dest_host, dest_port, timeout):
    """Open a TCP connection to dest through a SOCKS5 proxy (RFC 1928, optional user/password auth).

    Hostnames are sent to the proxy, so DNS is resolved on the proxy side too - that is what
    makes it work when the local resolver or route to the host is blocked.
    """
    sock = socket.create_connection((px.hostname, px.port or 1080), timeout)
    try:
        sock.settimeout(timeout)
        methods = b"\x00\x02" if px.username else b"\x00"
        sock.sendall(bytes([5, len(methods)]) + methods)
        ver, method = _recv_exact(sock, 2)
        if ver != 5:
            raise ConnectionError("это не SOCKS5-прокси")
        if method == 2:
            u = urllib.parse.unquote(px.username or "").encode()
            pw = urllib.parse.unquote(px.password or "").encode()
            sock.sendall(bytes([1, len(u)]) + u + bytes([len(pw)]) + pw)
            if _recv_exact(sock, 2)[1] != 0:
                raise ConnectionError("прокси не принял логин или пароль")
        elif method != 0:
            raise ConnectionError("прокси требует авторизацию, которую я не умею")
        host = dest_host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack(">H", dest_port))
        rep = _recv_exact(sock, 4)
        if rep[1] != 0:
            raise ConnectionError("прокси: " + SOCKS_ERR.get(rep[1], f"код {rep[1]}"))
        atyp = rep[3]
        if atyp == 1:
            _recv_exact(sock, 4)
        elif atyp == 3:
            _recv_exact(sock, _recv_exact(sock, 1)[0])
        elif atyp == 4:
            _recv_exact(sock, 16)
        _recv_exact(sock, 2)
        return sock
    except Exception:
        sock.close()
        raise


class Resp:
    """Uniform response over urllib / http.client so callers can use read / headers / geturl."""

    def __init__(self, raw, url):
        self.raw, self._url, self.headers = raw, url, raw.headers
        self.status = getattr(raw, "status", None) or getattr(raw, "code", None)

    def read(self, *a):
        return self.raw.read(*a)

    def geturl(self):
        return self._url

    def close(self):
        try:
            self.raw.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _via_socks(url, data, headers, timeout, method, px):
    u = urllib.parse.urlparse(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    sock = socks5_connect(px, u.hostname, port, timeout)
    if u.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
    conn = http.client.HTTPConnection(u.hostname, port, timeout=timeout)
    conn.sock = sock
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    conn.request(method or ("POST" if data else "GET"), path, body=data, headers=headers)
    return conn.getresponse()


def net_open(url, data=None, headers=None, timeout=60, proxy=None, method=None, redirects=3):
    """Open a URL directly or through DeckDrop's own proxy (socks5:// or http://)."""
    h = {"User-Agent": UA}
    h.update(headers or {})
    px = urllib.parse.urlparse(proxy) if proxy else None
    if px and px.scheme in ("socks5", "socks5h", "socks"):
        for _ in range(redirects + 1):
            raw = _via_socks(url, data, h, timeout, method, px)
            loc = raw.headers.get("Location")
            if raw.status in (301, 302, 303, 307, 308) and loc:
                raw.read()
                raw.close()
                url = urllib.parse.urljoin(url, loc)
                continue
            if raw.status >= 400:
                raw.read(400)
                raw.close()
                raise urllib.error.HTTPError(url, raw.status, raw.reason, raw.headers, None)
            return Resp(raw, url)
        raise RuntimeError("слишком много перенаправлений")
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if px else {})
    opener = urllib.request.build_opener(handler)
    raw = opener.open(urllib.request.Request(url, data=data, headers=h, method=method), timeout=timeout)
    return Resp(raw, raw.geturl())


def http_get(url, timeout=60, headers=None, proxy=None):
    return net_open(url, headers=headers, timeout=timeout, proxy=proxy)


def with_retries(what, fn, tries=3, delay=1.5):
    """Retry a network call; connection resets from DPI are often intermittent."""
    last = None
    for i in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, ConnectionError, TimeoutError, ssl.SSLError) as e:
            last = e
            if i + 1 < tries:
                time.sleep(delay * (i + 1))
    raise RuntimeError(f"{what}: {net_reason(last)}") from last



def resolve_url(url):
    """Turn share links of known hosts into direct download links (best effort)."""
    u = urllib.parse.urlparse(url)
    host = u.netloc.lower()
    if "disk.yandex" in host or host.endswith("yadi.sk"):
        api = ("https://cloud-api.yandex.net/v1/disk/public/resources/download?public_key="
               + urllib.parse.quote(url, safe=""))
        with http_get(api, 30, proxy=dl_proxy()) as r:
            return json.load(r)["href"]
    if "drive.google.com" in host or "docs.google.com" in host:
        m = re.search(r"/d/([\w-]+)", u.path) or re.search(r"[?&]id=([\w-]+)", url)
        if m:
            return ("https://drive.usercontent.google.com/download?id="
                    + m.group(1) + "&export=download&confirm=t")
    return url

# --------------------------------------------------------------------------- download / upload

def run_download(job, url):
    part = None
    try:
        if job.cancel:
            raise RuntimeError("отменено")
        job.status = "resolving"
        real = resolve_url(url)
        with http_get(real, proxy=dl_proxy()) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                raise RuntimeError("по ссылке отдаётся HTML-страница, а не файл: нужна прямая "
                                   "ссылка, или скачай на ПК и перетащи файл сюда")
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
                        raise RuntimeError("отменено")
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    job.done += len(chunk)
        if job.total and job.done != job.total:
            raise RuntimeError(f"файл скачан не полностью: {job.done} из {job.total} байт")
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


def start_download(url, disk=None):
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("нужна ссылка вида http(s)://")
    link = mega_parse_link(url)         # Mega is encrypted and has its own downloader
    job = new_job("download", url, root_for(disk) if disk else None)
    threading.Thread(target=run_mega_download if link else run_download,
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

# --------------------------------------------------------------------------- AES

# Mega encrypts every file in the browser before uploading it: the link carries the key, the
# server never sees it. So downloading means decrypting here. The stdlib has no AES, so this is
# OpenSSL through ctypes (present on SteamOS - Python itself links it) with a pure-Python
# implementation behind it, used for keys and names even when libcrypto is missing.

ZERO16 = b"\0" * 16
_LIB = None            # ctypes handle to libcrypto, or False once the search has failed
_CT = None


def _load_libcrypto():
    global _LIB, _CT
    if _LIB is not None:
        return _LIB
    import ctypes
    import ctypes.util
    names = []
    try:
        found = ctypes.util.find_library("crypto")
    except Exception:                                             # noqa: BLE001
        found = None
    if found:
        names.append(found)
    names += ["libcrypto.so.3", "libcrypto.so.1.1", "libcrypto.so", "libcrypto.dylib"]
    if os.name == "nt":
        dlls = Path(sys.base_prefix) / "DLLs"
        names += [str(dlls / n) for n in ("libcrypto-3-x64.dll", "libcrypto-3.dll",
                                          "libcrypto-1_1-x64.dll", "libcrypto-1_1.dll")]
    c = ctypes
    for name in names:
        try:
            lib = c.CDLL(name)
            lib.EVP_CIPHER_CTX_new.restype = c.c_void_p
            lib.EVP_CIPHER_CTX_free.argtypes = [c.c_void_p]
            lib.EVP_CIPHER_CTX_set_padding.argtypes = [c.c_void_p, c.c_int]
            for fn in ("EVP_aes_128_ecb", "EVP_aes_128_cbc", "EVP_aes_128_ctr"):
                getattr(lib, fn).restype = c.c_void_p
            for fn in ("EVP_EncryptInit_ex", "EVP_DecryptInit_ex"):
                f = getattr(lib, fn)
                f.argtypes = [c.c_void_p, c.c_void_p, c.c_void_p, c.c_char_p, c.c_char_p]
                f.restype = c.c_int
            for fn in ("EVP_EncryptUpdate", "EVP_DecryptUpdate"):
                f = getattr(lib, fn)
                f.argtypes = [c.c_void_p, c.c_char_p, c.POINTER(c.c_int), c.c_char_p, c.c_int]
                f.restype = c.c_int
            _CT, _LIB = c, lib
            return lib
        except (OSError, AttributeError):
            continue
    _LIB = False
    log("aes: libcrypto не найдена, расшифровка Mega будет очень медленной")
    return False


def _ossl(cipher, key, iv, data, enc):
    lib, c = _LIB, _CT
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise RuntimeError("openssl: нет контекста шифрования")
    try:
        init = lib.EVP_EncryptInit_ex if enc else lib.EVP_DecryptInit_ex
        upd = lib.EVP_EncryptUpdate if enc else lib.EVP_DecryptUpdate
        if init(ctx, getattr(lib, cipher)(), None, key, iv) != 1:
            raise RuntimeError("openssl: не приняла ключ")
        lib.EVP_CIPHER_CTX_set_padding(ctx, 0)
        out = c.create_string_buffer(len(data) + 16)
        n = c.c_int(0)
        if upd(ctx, out, c.byref(n), bytes(data), len(data)) != 1:
            raise RuntimeError("openssl: ошибка шифрования")
        return out.raw[:n.value]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def _aes_tables():
    """S-box and its inverse, built from the GF(2**8) inverse and the AES affine map."""
    exp, lg = [0] * 512, [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        lg[x] = i
        x ^= ((x << 1) & 0xFF) ^ (0x1B if x & 0x80 else 0)     # x *= 3, the generator
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    sbox = [0] * 256
    for i in range(256):
        v = 0 if i == 0 else exp[255 - lg[i]]
        s = v
        for _ in range(4):
            v = ((v << 1) | (v >> 7)) & 0xFF
            s ^= v
        sbox[i] = s ^ 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    return exp, lg, sbox, inv


_EXP, _LG, _SBOX, _ISBOX = _aes_tables()
_MUL = {n: bytes((0 if i == 0 else _EXP[_LG[n] + _LG[i]]) for i in range(256))
        for n in (2, 3, 9, 11, 13, 14)}
_M2, _M3, _M9, _M11, _M13, _M14 = (_MUL[2], _MUL[3], _MUL[9], _MUL[11], _MUL[13], _MUL[14])
# state byte i is row i%4, column i//4; ShiftRows moves row r left by r, its inverse right by r
_SHIFT = [(i % 4) + 4 * (((i // 4) + (i % 4)) % 4) for i in range(16)]
_UNSHIFT = [(i % 4) + 4 * (((i // 4) - (i % 4)) % 4) for i in range(16)]


def _expand_key(key):
    if len(key) != 16:
        raise ValueError(f"ключ AES должен быть 16 байт, а не {len(key)}")
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = [_SBOX[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = (((rcon << 1) & 0xFF) ^ 0x1B) if rcon & 0x80 else rcon << 1
        w.append([a ^ b for a, b in zip(w[i - 4], t)])
    return [bytes(b for word in w[r * 4:r * 4 + 4] for b in word) for r in range(11)]


def _enc_block(rk, blk):
    s = [a ^ b for a, b in zip(blk, rk[0])]
    for rnd in range(1, 11):
        s = [_SBOX[s[j]] for j in _SHIFT]                       # SubBytes + ShiftRows
        if rnd < 10:
            t = []
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
                t += [_M2[a0] ^ _M3[a1] ^ a2 ^ a3,
                      a0 ^ _M2[a1] ^ _M3[a2] ^ a3,
                      a0 ^ a1 ^ _M2[a2] ^ _M3[a3],
                      _M3[a0] ^ a1 ^ a2 ^ _M2[a3]]
            s = t
        s = [a ^ b for a, b in zip(s, rk[rnd])]
    return bytes(s)


def _dec_block(rk, blk):
    s = [a ^ b for a, b in zip(blk, rk[10])]
    for rnd in range(9, -1, -1):
        s = [_ISBOX[s[j]] for j in _UNSHIFT]                    # InvShiftRows + InvSubBytes
        s = [a ^ b for a, b in zip(s, rk[rnd])]
        if rnd:
            t = []
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
                t += [_M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3],
                      _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3],
                      _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3],
                      _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3]]
            s = t
    return bytes(s)


def _blocks(data):
    if len(data) % 16:
        raise ValueError("данные для AES должны быть кратны 16 байтам")
    return range(0, len(data), 16)


def aes_ecb_encrypt(key, data):
    if _load_libcrypto():
        return _ossl("EVP_aes_128_ecb", key, None, data, True)
    rk = _expand_key(key)
    return b"".join(_enc_block(rk, data[i:i + 16]) for i in _blocks(data))


def aes_ecb_decrypt(key, data):
    if _load_libcrypto():
        return _ossl("EVP_aes_128_ecb", key, None, data, False)
    rk = _expand_key(key)
    return b"".join(_dec_block(rk, data[i:i + 16]) for i in _blocks(data))


def aes_cbc_encrypt(key, data, iv=ZERO16):
    if _load_libcrypto():
        return _ossl("EVP_aes_128_cbc", key, iv, data, True)
    rk, out, prev = _expand_key(key), [], iv
    for i in _blocks(data):
        prev = _enc_block(rk, bytes(a ^ b for a, b in zip(data[i:i + 16], prev)))
        out.append(prev)
    return b"".join(out)


def aes_cbc_decrypt(key, data, iv=ZERO16):
    if _load_libcrypto():
        return _ossl("EVP_aes_128_cbc", key, iv, data, False)
    rk, out, prev = _expand_key(key), [], iv
    for i in _blocks(data):
        blk = data[i:i + 16]
        out.append(bytes(a ^ b for a, b in zip(_dec_block(rk, blk), prev)))
        prev = blk
    return b"".join(out)


class AesCtr:
    """AES-128-CTR kept open for the whole file, so chunks decrypt as one continuous stream."""

    def __init__(self, key, iv):
        self.ctx = None
        if _load_libcrypto():
            lib = _LIB
            ctx = lib.EVP_CIPHER_CTX_new()
            if lib.EVP_EncryptInit_ex(ctx, lib.EVP_aes_128_ctr(), None, key, iv) != 1:
                lib.EVP_CIPHER_CTX_free(ctx)
                raise RuntimeError("openssl: не приняла ключ Mega")
            lib.EVP_CIPHER_CTX_set_padding(ctx, 0)
            self.ctx = ctx
        else:
            self.rk, self.ctr, self.buf = _expand_key(key), int.from_bytes(iv, "big"), b""

    def xor(self, data):
        if self.ctx is not None:
            c = _CT
            out = c.create_string_buffer(len(data) + 16)
            n = c.c_int(0)
            if _LIB.EVP_EncryptUpdate(self.ctx, out, c.byref(n), bytes(data), len(data)) != 1:
                raise RuntimeError("openssl: ошибка расшифровки")
            return out.raw[:n.value]
        ks = bytearray(self.buf)
        while len(ks) < len(data):
            ks += _enc_block(self.rk, self.ctr.to_bytes(16, "big"))
            self.ctr = (self.ctr + 1) & ((1 << 128) - 1)
        self.buf = bytes(ks[len(data):])
        return bytes(a ^ b for a, b in zip(data, ks))

    def close(self):
        if self.ctx is not None:
            ctx, self.ctx = self.ctx, None
            _LIB.EVP_CIPHER_CTX_free(ctx)

    def __del__(self):
        try:
            self.close()
        except Exception:                                          # noqa: BLE001
            pass

# --------------------------------------------------------------------------- mega.nz

MEGA_API = "https://g.api.mega.co.nz/cs"
MEGA_HOSTS = ("mega.nz", "mega.co.nz", "mega.io")
MEGA_CHUNK_MAX = 1 << 20
MEGA_STEP = 1 << 17
MEGA_GATE = threading.Semaphore(1)        # Mega drops parallel transfers from one address
MEGA_FOLDERS = {}                         # folder handle -> (fetched at, listing)
MEGA_LOCK = threading.Lock()
MEGA_TTL = 600
MEGA_ERR = {
    -1: "внутренняя ошибка Mega, попробуй ещё раз",
    -2: "ссылка составлена неверно",
    -3: "Mega просит подождать и повторить",
    -4: "слишком много запросов, подожди пару минут",
    -5: "передача не удалась",
    -6: "слишком много попыток, подожди",
    -8: "ссылка больше не действует",
    -9: "файл не найден: ссылку удалили или она набрана с ошибкой",
    -11: "нет доступа к этой ссылке",
    -12: "такой файл уже есть",
    -14: "не подошёл ключ: скопируй ссылку целиком, вместе с частью после решётки",
    -15: "нужен вход в аккаунт Mega",
    -16: "аккаунт заблокирован",
    -17: "исчерпан лимит трафика: подожди несколько часов или включи прокси в настройках",
    -18: "файл временно недоступен, попробуй позже",
    -19: "слишком много одновременных соединений",
}


def b64u_decode(s):
    """Mega's base64: URL alphabet, padding stripped."""
    s = re.sub(r"\s+", "", str(s or "")).replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def b64u_encode(b):
    return base64.b64encode(b).decode().replace("+", "-").replace("/", "_").rstrip("=")


def mega_error(code):
    return "Mega: " + MEGA_ERR.get(code, f"ошибка {code}")


def mega_parse_link(url):
    """Recognise a mega.nz link -> {kind, handle, key, node} or None for everything else."""
    try:
        u = urllib.parse.urlparse((url or "").strip())
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    if host not in MEGA_HOSTS:
        return None
    frag = u.fragment or ""
    if frag.startswith("P!"):
        raise ValueError("это ссылка Mega под паролем, DeckDrop такие пока не умеет: "
                         "открой её в браузере, введи пароль и скопируй обычную ссылку")
    m = re.match(r"^/(file|folder|embed)/([\w-]+)/?$", u.path or "/")
    if m:
        kind = "folder" if m.group(1) == "folder" else "file"
        key = frag.split("/")[0]
        node = None
        if kind == "folder":
            sub = re.findall(r"/file/([\w-]+)", frag)
            node = sub[-1] if sub else None
    else:
        m = re.match(r"^(F?)!([\w-]+)!([\w-]+)(?:!([\w-]+))?", frag)
        if not m or (u.path or "/") not in ("/", ""):
            return None
        kind = "folder" if m.group(1) else "file"
        key, node = m.group(3), m.group(4)
    if not m.group(2):
        return None
    if not key:
        raise ValueError("в ссылке Mega нет ключа: скопируй её целиком, вместе с частью после решётки")
    return {"kind": kind, "handle": m.group(2), "key": key, "node": node}


def _mega_post(payload, folder=None):
    q = {"id": str(secrets.randbelow(1 << 30))}
    if folder:
        q["n"] = folder
    url = MEGA_API + "?" + urllib.parse.urlencode(q)
    body = json.dumps([payload]).encode()
    with net_open(url, data=body, headers={"Content-Type": "application/json"},
                  timeout=40, proxy=proxy_url()) as r:
        return json.loads(r.read(4 << 20).decode("utf-8", "replace"))


def mega_api(payload, folder=None, tries=5):
    """One request to Mega's API. Retries the "come back later" codes and network hiccups."""
    delay = 1.0
    for attempt in range(tries):
        try:
            res = _mega_post(payload, folder)
        except urllib.error.HTTPError as e:
            if e.code == 509:
                raise RuntimeError(mega_error(-17)) from e
            if attempt + 1 >= tries:
                raise RuntimeError(f"Mega: {net_reason(e)}") from e
            res = -3
        except (urllib.error.URLError, OSError, ConnectionError, TimeoutError,
                ssl.SSLError, ValueError) as e:
            if attempt + 1 >= tries:
                raise RuntimeError(f"Mega: {net_reason(e)}") from e
            res = -3
        if isinstance(res, list) and res:
            res = res[0]
        if isinstance(res, dict):
            if isinstance(res.get("e"), int):
                raise RuntimeError(mega_error(res["e"]))
            return res
        if not isinstance(res, int):
            raise RuntimeError("Mega: непонятный ответ сервера")
        if res in (-1, -3, -4, -19) and attempt + 1 < tries:
            time.sleep(delay)
            delay *= 2
            continue
        raise RuntimeError(mega_error(res))
    raise RuntimeError(mega_error(-3))


def mega_file_key(raw):
    """Split a 32-byte file key into the AES key, the CTR nonce and the expected MAC."""
    if len(raw) != 32:
        raise ValueError("ключ файла в ссылке неполный: скопируй ссылку целиком")
    return bytes(a ^ b for a, b in zip(raw[:16], raw[16:])), raw[16:24], raw[24:32]


def mega_attrs(key, b64):
    """Decrypt a node's attribute blob: 'MEGA' followed by JSON with the file name."""
    data = b64u_decode(b64)
    data = data[:len(data) - len(data) % 16]
    if not data:
        return {}
    raw = aes_cbc_decrypt(key, data, ZERO16)
    if not raw.startswith(b"MEGA"):
        raise ValueError("не подошёл ключ из ссылки")
    txt = raw[4:].split(b"\0")[0].decode("utf-8", "replace").strip()
    try:
        return json.loads(txt)
    except ValueError:
        i = txt.rfind("}")
        if i > 0:
            try:
                return json.loads(txt[:i + 1])
            except ValueError:
                pass
    return {}


def _node_key(node, shared, folder):
    """A folder link's nodes carry their key encrypted with the folder key."""
    enc = None
    for part in (node.get("k") or "").split("/"):
        if ":" not in part:
            continue
        owner, val = part.split(":", 1)
        if owner == folder:
            enc = val
            break
        if enc is None:
            enc = val
    if not enc:
        return None
    raw = b64u_decode(enc)
    raw = raw[:len(raw) - len(raw) % 16]
    return aes_ecb_decrypt(shared, raw) if raw else None


def mega_folder_files(link, refresh=False):
    """List a shared folder: names, sizes and per-file keys. Cached for a few minutes."""
    handle = link["handle"]
    with MEGA_LOCK:
        hit = MEGA_FOLDERS.get(handle)
    if hit and not refresh and time.time() - hit[0] < MEGA_TTL:
        return hit[1]
    shared = b64u_decode(link["key"])
    if len(shared) != 16:
        raise ValueError("ключ папки в ссылке неполный: скопируй ссылку целиком")
    res = mega_api({"a": "f", "c": 1, "r": 1}, folder=handle)
    nodes = res.get("f") or []
    names, parents, kinds, files = {}, {}, {}, []
    for n in nodes:
        h, t = n.get("h"), n.get("t")
        if not h or t not in (0, 1):
            continue
        parents[h] = n.get("p")
        kinds[h] = t
        try:
            nk = _node_key(n, shared, handle)
            if not nk:
                continue
            key, nonce, mac = mega_file_key(nk) if t == 0 else (nk[:16], b"", b"")
            attrs = mega_attrs(key, n.get("a") or "")
        except (ValueError, RuntimeError) as e:                    # noqa: PERF203
            log(f"mega: узел {h} пропущен ({e})")
            continue
        names[h] = safe_name(attrs.get("n") or h)
        if t == 0:
            files.append({"h": h, "name": names[h], "size": int(n.get("s") or 0),
                          "key": key, "nonce": nonce, "mac": mac})

    def rel_dir(h):
        parts, p, seen = [], parents.get(h), set()
        while p and p != handle and p in names and p not in seen:
            seen.add(p)
            parts.append(names[p])
            p = parents.get(p)
        return "/".join(reversed(parts))

    for f in files:
        f["dir"] = rel_dir(f["h"])
    files.sort(key=lambda f: (f["dir"], f["name"]))
    dirs = sorted("/".join(x for x in (rel_dir(h), names[h]) if x)
                  for h, t in kinds.items() if t == 1 and h != handle and h in names)
    out = {"name": names.get(handle) or "Папка Mega", "files": files, "dirs": dirs}
    with MEGA_LOCK:
        MEGA_FOLDERS[handle] = (time.time(), out)
    return out


def mega_node_info(link, want_url=True):
    """Everything needed to download one file: name, size, keys and a fresh transfer URL."""
    name = url = None
    if link["kind"] == "file":
        key, nonce, mac = mega_file_key(b64u_decode(link["key"]))
        req = {"a": "g", "p": link["handle"]}
        if want_url:
            req["g"] = 1
        res = mega_api(req)
        size = int(res.get("s") or 0)
        if res.get("at"):
            name = mega_attrs(key, res["at"]).get("n")
        url = res.get("g")
    else:
        files = mega_folder_files(link)["files"]
        if link.get("node"):
            f = next((x for x in files if x["h"] == link["node"]), None)
            if not f:
                raise RuntimeError("Mega: этого файла нет в папке по ссылке")
        elif len(files) == 1:
            f = files[0]
        elif not files:
            raise RuntimeError("Mega: в папке по ссылке нет файлов")
        else:
            raise RuntimeError(f"по ссылке папка Mega с {len(files)} файлами: выбери нужные в списке")
        key, nonce, mac, size, name = f["key"], f["nonce"], f["mac"], f["size"], f["name"]
        if want_url:
            res = mega_api({"a": "g", "g": 1, "n": f["h"]}, folder=link["handle"])
            url = res.get("g")
            if res.get("s"):
                size = int(res["s"])
    if isinstance(url, list):
        url = url[0] if url else None
    if want_url and size and not isinstance(url, str):
        raise RuntimeError("Mega не дала ссылку на файл: возможно, исчерпан лимит трафика")
    return {"name": safe_name(name or (link.get("node") or link["handle"])), "size": size,
            "key": key, "nonce": nonce, "mac": mac, "url": url}


def mega_probe(url):
    """What is behind a link, for the picker: one file or a list of them."""
    link = mega_parse_link(url)
    if not link:
        raise ValueError("это не ссылка на Mega")
    if link["kind"] == "file":
        i = mega_node_info(link, want_url=False)
        files = [{"h": link["handle"], "name": i["name"], "size": i["size"], "dir": ""}]
        return {"kind": "file", "name": i["name"], "files": files, "total": i["size"]}
    data = mega_folder_files(link, refresh=True)
    files = data["files"]
    if link.get("node"):
        files = [f for f in files if f["h"] == link["node"]]
    out = [{"h": f["h"], "name": f["name"], "size": f["size"], "dir": f["dir"]} for f in files]
    return {"kind": "folder", "name": data["name"], "files": out,
            "total": sum(f["size"] for f in out)}


def mega_chunks(size):
    """Mega's own chunk boundaries: 128K, 256K ... 1M, then 1M each. The MAC is per chunk."""
    pos, step = 0, 0
    while pos < size:
        step = min(step + MEGA_STEP, MEGA_CHUNK_MAX)
        n = min(step, size - pos)
        yield pos, n
        pos += n


def mega_chunk_mac(key, nonce, data):
    pad = -len(data) % 16
    return aes_cbc_encrypt(key, bytes(data) + b"\0" * pad, nonce + nonce)[-16:]


def mega_meta_mac(key, chunk_macs):
    """Condense the chunk MACs the way Mega does, down to the 8 bytes kept in the link."""
    if not chunk_macs:
        return b"\0" * 8
    fm = aes_cbc_encrypt(key, b"".join(chunk_macs), ZERO16)[-16:]
    return (bytes(a ^ b for a, b in zip(fm[0:4], fm[4:8]))
            + bytes(a ^ b for a, b in zip(fm[8:12], fm[12:16])))


NET_ERRORS = (urllib.error.URLError, OSError, ConnectionError, TimeoutError,
              ssl.SSLError, http.client.HTTPException)


class MegaSource:
    """The encrypted bytes of one file, reconnecting from the current offset if the link drops.

    Mega hands out a transfer URL that dies now and then on a long download; a game archive is
    several gigabytes, so retrying the whole file is not an option.
    """

    def __init__(self, url, size, job=None, tries=6):
        self.url, self.size, self.job, self.tries = url, size, job, tries
        self.pos, self.r, self.fails = 0, None, 0

    def _cancelled(self):
        return bool(self.job and self.job.cancel)

    def _discard(self, r, n):
        while n > 0:
            if self._cancelled():
                raise RuntimeError("отменено")
            b = r.read(min(n, CHUNK))
            if not b:
                raise ConnectionError("Mega оборвала передачу")
            n -= len(b)

    def _connect(self):
        px = dl_proxy()
        if not self.pos:
            self.r = net_open(self.url, timeout=60, proxy=px)
            return
        r = net_open(self.url, headers={"Range": f"bytes={self.pos}-"}, timeout=60, proxy=px)
        if getattr(r, "status", 0) == 206:
            self.r = r
            return
        r.close()
        r = net_open(f"{self.url}/{self.pos}-{self.size - 1}", timeout=60, proxy=px)
        if int(r.headers.get("Content-Length") or 0) == self.size - self.pos:
            self.r = r
            return
        r.close()
        r = net_open(self.url, timeout=60, proxy=px)        # last resort: from the top
        self._discard(r, self.pos)
        self.r = r

    def _drop(self):
        if self.r is not None:
            try:
                self.r.close()
            except OSError:
                pass
            self.r = None

    def read(self, n):
        """Exactly n more bytes, reconnecting as many times as it takes."""
        out = bytearray()
        while len(out) < n:
            if self._cancelled():
                raise RuntimeError("отменено")
            try:
                if self.r is None:
                    self._connect()
                b = self.r.read(min(n - len(out), CHUNK))
                if not b:
                    raise ConnectionError("Mega оборвала передачу раньше времени")
            except NET_ERRORS as e:
                self._drop()
                self.fails += 1
                if self.fails > self.tries:
                    raise RuntimeError(f"Mega: {net_reason(e)}") from e
                log(f"mega: обрыв на {self.pos} байте ({e}), продолжаю")
                time.sleep(min(2 * self.fails, 10))
                continue
            out += b
            self.pos += len(b)
        return bytes(out)

    def close(self):
        self._drop()


def mega_fetch(job, info, part, base=0):
    """Stream one file into `part`, decrypting and checking Mega's own MAC as it goes."""
    ctr = AesCtr(info["key"], info["nonce"] + b"\0" * 8)
    src = MegaSource(info["url"], info["size"], job)
    macs = []
    try:
        with open(part, "wb") as f:
            for pos, n in mega_chunks(info["size"]):
                plain = ctr.xor(src.read(n))
                f.write(plain)
                macs.append(mega_chunk_mac(info["key"], info["nonce"], plain))
                job.done = base + pos + n
    finally:
        src.close()
        ctr.close()
    if not STATE.get("mega_verify", True) or not info["mac"] or not info["size"]:
        return
    if not hmac.compare_digest(mega_meta_mac(info["key"], macs), info["mac"]):
        raise RuntimeError("файл скачался с ошибкой: не сошлась контрольная сумма Mega. "
                           "Попробуй ещё раз, а если повторяется - выключи проверку в настройках")


class MegaSlot:
    """The single Mega transfer slot. Waiting for it gives up at once when the job is cancelled."""

    def __init__(self, job):
        self.job = job

    def __enter__(self):
        while not MEGA_GATE.acquire(timeout=0.25):
            if self.job.cancel:
                raise RuntimeError("отменено")
        return self

    def __exit__(self, *exc):
        MEGA_GATE.release()


def run_mega_download(job, link):
    part = None
    try:
        with MegaSlot(job):
            if job.cancel:
                raise RuntimeError("отменено")
            job.status = "resolving"
            info = mega_node_info(link)
            dest, part = reserve_path(job.root / "_inbox", info["name"])
            job.work = str(part)
            job.label, job.total, job.done = info["name"], info["size"], 0
            job.status = "downloading"
            job.started = time.time()
            mega_fetch(job, info, part)
        part.rename(dest)
        part = None
        job.file = str(dest)
        finish(job, dest)
    except Exception as e:                                         # noqa: BLE001
        drop_part(part)
        part = None
        fail(job, str(e))
    finally:
        drop_part(part)


# files that ride along with archives and do not turn a folder into "an unpacked game"
MEGA_SIDE = {".txt", ".nfo", ".url", ".md", ".pdf", ".htm", ".html", ".jpg", ".jpeg", ".png",
             ".gif", ".webp", ".sfv", ".md5", ".sha1", ".sha256"}


def mega_layout(names):
    """'archives' for a folder of archives (plus a readme), 'game' for an unpacked game."""
    core = [n for n in names if Path(n).suffix.lower() not in MEGA_SIDE]
    return "archives" if core and all(archive_volume(n) for n in core) else "game"


def mega_rel_path(root, rel):
    """A path from the folder listing, kept strictly inside `root` whatever the names say."""
    parts = [safe_name(x) for x in str(rel).replace("\\", "/").split("/") if x.strip(" .")]
    p = root.joinpath(*parts)
    if not inside(root, p):
        raise ValueError("путь в папке Mega выходит за её пределы")
    return p


def free_path(p):
    """p itself, or 'name (2).ext' next to it when p is taken."""
    stem, ext = split_ext(p.name)
    cand, i = p, 1
    while cand.exists():
        i += 1
        cand = p.with_name(f"{stem} ({i}){ext}")
    return cand


def mega_place(job, stage, title):
    """Put a finished folder where it belongs: a game folder as is, or archives to unpack."""
    files = [p for p in stage.rglob("*") if p.is_file()]
    if mega_layout([p.name for p in files]) == "game":
        target = unique_dir(job.root, title)
        stage.rename(target)
        flatten(target)
        job.game_dir = str(target)
        job.status = "done"
        log(f"job {job.id} done: {target}")
        return
    moved = []
    for p in sorted(files, key=lambda p: p.stat().st_size, reverse=True):
        dest = free_path(stage.parent / p.name)
        p.rename(dest)
        moved.append(dest)
    shutil.rmtree(stage, ignore_errors=True)
    primary = next((p for p in moved if archive_volume(p.name) == "primary"), None)
    if primary is None:
        job.status = "done"
        return
    job.file = str(primary)
    finish(job, primary)


def run_mega_folder(job, link, picked):
    """Several files from one shared folder: one job, the folder tree kept, one game at the end."""
    stage = None
    try:
        with MegaSlot(job):
            if job.cancel:
                raise RuntimeError("отменено")
            job.status = "resolving"
            data = mega_folder_files(link)
            title = safe_name(data["name"])
            stage = job.root / "_inbox" / f".mega-{job.id}-{secrets.token_hex(3)}"
            stage.mkdir(parents=True)
            job.work = str(stage)
            if len(picked) == len(data["files"]):          # the whole folder: empty dirs too
                for d in data.get("dirs") or []:
                    mega_rel_path(stage, d).mkdir(parents=True, exist_ok=True)
            job.total, job.done = sum(f["size"] for f in picked), 0
            job.status = "downloading"
            job.started = time.time()
            for i, f in enumerate(picked, 1):
                if job.cancel:
                    raise RuntimeError("отменено")
                job.label = f"{title} · файл {i} из {len(picked)}"
                dest = free_path(mega_rel_path(stage, f"{f['dir']}/{f['name']}"))
                dest.parent.mkdir(parents=True, exist_ok=True)
                info = mega_node_info(dict(link, node=f["h"]), want_url=f["size"] > 0)
                start = job.done
                mega_fetch(job, info, dest, start)
                job.done = start + info["size"]
        job.label = title
        mega_place(job, stage, title)
        stage = None
    except Exception as e:                                         # noqa: BLE001
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
            stage = None
        fail(job, str(e))
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


def start_mega_downloads(url, nodes, disk=None):
    """Queue what was picked in a Mega folder: one file as usual, several as one game."""
    link = mega_parse_link(url)
    if not link:
        raise ValueError("это не ссылка на Mega")
    if link["kind"] == "file":
        return [start_download(url, disk).to_dict()]
    data = mega_folder_files(link)
    files = {f["h"]: f for f in data["files"]}
    picked = [files[h] for h in dict.fromkeys(nodes or []) if h in files]
    if not picked:
        raise ValueError("не выбрано ни одного файла")
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
    threading.Thread(target=target, args=args, daemon=True).start()
    return [job.to_dict()]

# --------------------------------------------------------------------------- extraction

class NeedsPassword(Exception):
    """Archive is encrypted and the given password (if any) did not work."""


PW_ERR = re.compile(r"passphrase|password|encrypt|wrong pass|incorrect pass", re.I)


def zip_member_name(info):
    name = info.filename
    if not (info.flag_bits & 0x800):
        # No UTF-8 flag: Python decoded the name as cp437. Russian archives made on
        # Windows use cp866 (OEM), so re-decode; pure-ASCII names are unaffected.
        try:
            name = name.encode("cp437").decode("cp866")
        except UnicodeError:
            pass
    return name


def zip_encrypted(path):
    try:
        with zipfile.ZipFile(path) as z:
            return any(i.flag_bits & 0x1 for i in z.infolist() if not i.is_dir())
    except (zipfile.BadZipFile, OSError):
        return False


def extract_zip_python(path, target, password=None):
    root = target.resolve()
    pwd = password.encode("utf-8") if password else None
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            name = zip_member_name(info).replace("\\", "/")
            dest = (root / name).resolve()
            if root != dest and root not in dest.parents:
                continue  # zip-slip guard
            if info.is_dir() or name.endswith("/"):
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                with z.open(info, pwd=pwd) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
            except RuntimeError as e:  # "Bad password for file" / "File is encrypted"
                if "password" in str(e).lower() or "encrypted" in str(e).lower():
                    raise NeedsPassword("неверный пароль" if password else "архив зашифрован") from e
                raise


def run_tool(cmd):
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip()
        if PW_ERR.search(msg):
            raise NeedsPassword("неверный пароль" if any(a.startswith(("-p", "--passphrase")) for a in cmd[1:])
                                else "архив зашифрован")
        raise RuntimeError(f"{Path(cmd[0]).name}: {msg[-300:]}")


def extract(path, target, password=None):
    target.mkdir(parents=True, exist_ok=True)
    low = path.name.lower()
    if low.endswith(".zip"):
        if zip_encrypted(path) and not password:
            raise NeedsPassword("архив зашифрован")
        try:
            extract_zip_python(path, target, password)
            return
        except NotImplementedError:
            # AES-encrypted zip: Python can't, bsdtar / 7z can
            if not password:
                raise NeedsPassword("архив зашифрован") from None
    tools = []
    if shutil.which("bsdtar"):
        tools.append(["bsdtar"] + (["--passphrase", password] if password else []) + ["-xf", str(path), "-C", str(target)])
    if shutil.which("7z"):
        tools.append(["7z", "x", "-y", "-p" + (password or ""), "-o" + str(target), str(path)])
    if shutil.which("unrar") and low.endswith(".rar"):
        tools.append(["unrar", "x", "-y", "-p" + (password or "-"), str(path), str(target) + os.sep])
    if not tools:
        raise RuntimeError("не найден распаковщик (bsdtar / 7z / unrar)")
    last = None
    for cmd in tools:
        try:
            run_tool(cmd)
            return
        except NeedsPassword:
            raise
        except RuntimeError as e:
            last = e
    raise last


def flatten(d):
    """archive.zip -> Game/Game/*  becomes  Game/*"""
    for _ in range(3):
        entries = [e for e in d.iterdir() if e.name not in ("__MACOSX", ".DS_Store")]
        if len(entries) == 1 and entries[0].is_dir():
            tmp = d / (".flatten_" + entries[0].name)
            entries[0].rename(tmp)
            for e in tmp.iterdir():
                shutil.move(str(e), str(d / e.name))
            tmp.rmdir()
        else:
            break


def try_extract(path, target, password):
    """Extract into a fresh dir; on failure remove the partial dir and re-raise."""
    try:
        extract(path, target, password)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


def finish(job, path, password=None):
    """Post-download: unpack archives (with remembered passwords), locate game dir."""
    try:
        if not (AUTO_EXTRACT and archive_volume(path.name) == "primary"):
            job.status = "done"
            log(f"job {job.id} done: {job.file}")
            return
        job.status = "extracting"
        job.file = str(path)
        stem = archive_stem(path.name)
        stem = re.sub(r"\s*\(\d+\)\s*$", "", stem).strip() or stem   # 'Game (1).zip' from a PC re-download
        target = unique_dir(job.root, stem)
        candidates = [password] if password else [None] + list(STATE.get("archive_passwords") or [])
        err = None
        for cand in candidates:
            try:
                try_extract(path, target, cand)
                err = None
                break
            except NeedsPassword as e:
                err = e
        if err is not None:
            job.status = "needs_password"
            job.error = str(err)
            return
        flatten(target)
        job.game_dir = str(target)
        if not KEEP_ARCHIVE:
            path.unlink()
            job.file = None
        job.status = "done"
        log(f"job {job.id} done: {job.game_dir}")
    except Exception as e:  # noqa: BLE001
        fail(job, f"ошибка распаковки: {e}")


def job_password(job_id, password, remember=False):
    with LOCK:
        job = JOBS.get(job_id)
    if not job or job.status != "needs_password" or not job.file:
        raise ValueError("это задание не ждёт пароль")
    if remember and password:
        with STATE_LOCK:
            pws = list(STATE.get("archive_passwords") or [])
            if password not in pws:
                pws.append(password)
            STATE["archive_passwords"] = pws
            save_state()
    job.status = "extracting"
    job.error = None
    threading.Thread(target=finish, args=(job, Path(job.file), password), daemon=True).start()
    return job

# --------------------------------------------------------------------------- archives (inbox) tab

def list_archives():
    out = []
    for root in game_roots():
        inbox = root / "_inbox"
        if not inbox.is_dir():
            continue
        label = disk_label_for(root)
        for f in inbox.iterdir():
            if not f.is_file() or f.name.endswith(".part"):
                continue
            stem, vol = archive_stem(f.name), archive_volume(f.name)
            game = root / stem
            st = f.stat()
            out.append({"name": f.name, "path": str(f), "size": st.st_size, "time": int(st.st_mtime),
                        "disk": label, "archive": vol == "primary", "part": vol == "secondary",
                        "extracted": game.is_dir(), "game_dir": str(game) if game.is_dir() else None})
    out.sort(key=lambda a: a["time"], reverse=True)
    return out


def archive_path(path):
    p = Path(path).resolve()
    if not p.is_file() or p.parent.name != "_inbox" or not inside_any(p):
        raise ValueError("неверный путь")
    return p


def archive_extract_job(path):
    p = archive_path(path)
    if archive_volume(p.name) != "primary":
        raise ValueError("это не архив или не первая его часть")
    job = new_job("extract", p.name, p.parent.parent)
    job.file = str(p)
    threading.Thread(target=finish, args=(job, p), daemon=True).start()
    return job


def archive_delete(path):
    p = archive_path(path)
    p.unlink()
    return p.name


def archive_cleanup():
    removed = []
    for a in list_archives():
        if a["extracted"]:
            Path(a["path"]).unlink()
            removed.append(a["name"])
    return removed


def _tree_size(p):
    if p.is_symlink() or not p.is_dir():
        return p.lstat().st_size
    return sum(f.lstat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())


def inbox_clear(dry_run=False):
    """Empty every _inbox: archives unpacked or not, loose files, leftovers of stopped downloads.

    Game folders are never touched, and neither is whatever a running job is writing or
    unpacking at this moment. With dry_run it only counts what would go.
    """
    with LOCK:
        jobs = list(JOBS.values())
    busy = {Path(p).resolve() for j in jobs if j.status in ACTIVE for p in (j.work, j.file) if p}
    out = {"removed": 0, "skipped": 0, "freed": 0}
    for root in game_roots():
        inbox = root / "_inbox"
        if not inbox.is_dir():
            continue
        for e in list(inbox.iterdir()):
            try:
                if e.resolve() in busy:
                    out["skipped"] += 1
                    continue
                size = _tree_size(e)
                if not dry_run:
                    if e.is_dir() and not e.is_symlink():
                        shutil.rmtree(e)
                    else:
                        e.unlink()
                out["removed"] += 1
                out["freed"] += size
            except OSError as ex:
                log(f"inbox clear: {e}: {ex}")
                out["skipped"] += 1
    if not dry_run:
        for j in jobs:                         # a job waiting for a password just lost its archive
            if j.status == "needs_password" and j.file and not Path(j.file).exists():
                j.status, j.error = "error", "архив удалён при очистке входящих"
        log(f"inbox cleared: {out}")
    return out

# --------------------------------------------------------------------------- steam: userdata / vdf

def userdata_dirs():
    base = STEAM_ROOT / "userdata"
    if not base.is_dir():
        return []
    out = [d for d in base.iterdir() if d.is_dir() and d.name.isdigit() and d.name != "0"]
    out.sort(key=lambda d: (d / "config").stat().st_mtime if (d / "config").exists() else 0, reverse=True)
    return out


def vdf_parse(buf):
    """Parse binary VDF (shortcuts.vdf). Keys are lower-cased."""
    pos = 0

    def read_str():
        nonlocal pos
        end = buf.index(b"\0", pos)
        s = buf[pos:end].decode("utf-8", "replace")
        pos = end + 1
        return s

    def read_map():
        nonlocal pos
        d = {}
        while pos < len(buf):
            t = buf[pos]
            pos += 1
            if t == 8:
                return d
            key = read_str().lower()
            if t == 0:
                d[key] = read_map()
            elif t == 1:
                d[key] = read_str()
            elif t == 2:
                d[key] = struct.unpack_from("<i", buf, pos)[0]
                pos += 4
            else:
                raise ValueError(f"unknown vdf type {t}")
        return d

    return read_map()


def text_vdf(text):
    """Parse text VDF (libraryfolders.vdf, compatibilitytool.vdf). Key case is preserved."""
    root, stack, key = {}, [], None
    stack.append(root)
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*', text):
        if m.group(2) == "{":
            d = {}
            stack[-1][key or ""] = d
            stack.append(d)
            key = None
        elif m.group(2) == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif m.group(1) is not None:
            val = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
            if key is None:
                key = val
            else:
                stack[-1][key] = val
                key = None
    return root


def vget(d, *keys):
    """Case-insensitive lookup through nested text-VDF dicts; None when missing."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = next((v for kk, v in d.items() if kk.lower() == k.lower()), None)
    return d


def shortcut_appid(exe_quoted, appname):
    """Steam's shortcut appid (32-bit) used for grid art filenames."""
    return (zlib.crc32((exe_quoted + appname).encode("utf-8")) | 0x80000000) & 0xFFFFFFFF


_SC_CACHE = {"key": None, "index": {}, "by_appid": {}}


def shortcuts_index():
    """{exe_path: {appid, name, userdata}} across all Steam users, cached by vdf mtimes."""
    files = []
    for ud in userdata_dirs():
        f = ud / "config" / "shortcuts.vdf"
        if f.is_file():
            files.append((ud, f, f.stat().st_mtime_ns))
    key = tuple((str(f), m) for _, f, m in files)
    if key == _SC_CACHE["key"]:
        return _SC_CACHE["index"]
    index, by_appid = {}, {}
    for ud, f, _ in files:
        try:
            data = vdf_parse(f.read_bytes())
        except (ValueError, OSError, struct.error) as e:
            log(f"shortcuts.vdf unreadable ({f}): {e}")
            continue
        for entry in (data.get("shortcuts") or {}).values():
            if not isinstance(entry, dict):
                continue
            exe_q = entry.get("exe") or ""
            name = entry.get("appname") or ""
            appid = entry.get("appid")
            appid = (appid & 0xFFFFFFFF) if isinstance(appid, int) and appid else shortcut_appid(exe_q, name)
            rec = {"appid": appid, "name": name, "userdata": str(ud)}
            index[exe_q.strip('"')] = rec
            by_appid[appid] = rec
    _SC_CACHE.update(key=key, index=index, by_appid=by_appid)
    return index


def pick_userdata():
    idx = shortcuts_index()
    if idx:
        return Path(next(iter(idx.values()))["userdata"])
    dirs = userdata_dirs()
    if not dirs:
        raise RuntimeError("не нашёл папку userdata Steam")
    return dirs[0]


def library_folders():
    libs = [STEAM_ROOT]
    vdf = STEAM_ROOT / "steamapps" / "libraryfolders.vdf"
    if vdf.is_file():
        try:
            data = text_vdf(vdf.read_text("utf-8", "replace"))
            for v in (vget(data, "libraryfolders") or {}).values():
                if isinstance(v, dict) and vget(v, "path"):
                    p = Path(vget(v, "path"))
                    if p.is_dir() and p not in libs:
                        libs.append(p)
        except Exception as e:  # noqa: BLE001
            log(f"libraryfolders.vdf: {e}")
    return libs


def compatdata_dir(appid):
    for lib in library_folders():
        d = lib / "steamapps" / "compatdata" / str(appid)
        if d.is_dir():
            return d
    return STEAM_ROOT / "steamapps" / "compatdata" / str(appid)

# --------------------------------------------------------------------------- steam: compat tools (Proton)

_CT_CACHE = {"at": 0, "tools": []}


def compat_tools():
    """[{name, label}] - installed Valve Protons (from appmanifests) + compatibilitytools.d."""
    if time.time() - _CT_CACHE["at"] < 60:
        return _CT_CACHE["tools"]
    tools, seen = [], set()

    def add(name, label, rank):
        if name and name not in seen:
            seen.add(name)
            tools.append((rank, {"name": name, "label": label}))

    for lib in library_folders():
        for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
            try:
                m = re.search(r'"name"\s+"([^"]*)"', acf.read_text("utf-8", "replace"))
            except OSError:
                continue
            title = m.group(1) if m else ""
            m = re.match(r"Proton (Experimental|Hotfix|(\d+)\.(\d+))", title)
            if not m:
                continue
            if m.group(1) == "Experimental":
                add("proton_experimental", title, (0, 0))
            elif m.group(1) == "Hotfix":
                add("proton_hotfix", title, (1, 0))
            else:
                major, minor = int(m.group(2)), int(m.group(3))
                add(f"proton_{major}" if minor == 0 else f"proton_{major}{minor}", title, (2, -(major * 100 + minor)))
    for d in (STEAM_ROOT / "compatibilitytools.d", Path.home() / ".steam" / "root" / "compatibilitytools.d"):
        for vdf in d.glob("*/compatibilitytool.vdf"):
            try:
                data = text_vdf(vdf.read_text("utf-8", "replace"))
                ct = vget(data, "compatibilitytools", "compat_tools") or {}
                for name, info in ct.items():
                    label = vget(info, "display_name") if isinstance(info, dict) else None
                    add(name, label or name, (3, 0))
            except Exception as e:  # noqa: BLE001
                log(f"{vdf}: {e}")
    tools.sort(key=lambda t: t[0])
    _CT_CACHE.update(at=time.time(), tools=[t[1] for t in tools])
    return _CT_CACHE["tools"]


_CT_MAP = {"key": None, "map": {}}


def compat_mapping():
    """appid -> compat tool name, straight from Steam's config.vdf.

    This is the setting Steam actually uses, so a game added to Steam by hand (or imported into
    DeckDrop) shows its real Proton instead of an empty box.
    """
    f = STEAM_ROOT / "config" / "config.vdf"
    try:
        key = (str(f), f.stat().st_mtime_ns)
    except OSError:
        return {}
    if key == _CT_MAP["key"]:
        return _CT_MAP["map"]
    out = {}
    try:
        data = text_vdf(f.read_text("utf-8", "replace"))
        mapping = vget(data, "InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping") or {}
        for appid, info in mapping.items():
            if appid.isdigit() and isinstance(info, dict):
                out[int(appid)] = vget(info, "name") or ""
    except Exception as e:  # noqa: BLE001
        log(f"config.vdf: {e}")
        return _CT_MAP["map"]
    _CT_MAP.update(key=key, map=out)
    return out


def compat_for(appid, rec):
    """Proton of a shortcut: our own record wins (it may be newer than the file), else Steam's."""
    if "compat" in rec:
        return rec.get("compat"), "deckdrop"
    if appid is not None:
        m = compat_mapping()
        if appid in m:
            return (m[appid] or None), "steam"
    return None, None


def compat_label(name):
    for t in compat_tools():
        if t["name"] == name:
            return t["label"]
    return name or "без Proton"

# --------------------------------------------------------------------------- steam: session env (fallback path)

STEAM_PROCS = ("steam", "steamwebhelper", "gamescope")  # ranked: best env source first
SESSION_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE")


def proc_env(d, keys):
    try:
        raw = (d / "environ").read_bytes()
    except OSError:
        return {}
    got = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.decode("utf-8", "replace").partition("=")
        if sep and k in keys and v:
            got[k] = v
    return got


def session_env(proc="/proc"):
    """Return (env, steam_running), borrowing session vars from the running Steam.

    A systemd user service starts with a bare environment: no DISPLAY,
    WAYLAND_DISPLAY, XDG_RUNTIME_DIR or DBUS_SESSION_BUS_ADDRESS. Without them
    `steam steam://...` cannot reach the client running in Gaming Mode, so read
    those values out of Steam's own /proc entry.
    """
    proc = Path(proc)
    env = dict(os.environ)
    if not proc.is_dir():
        return env, False
    uid = getattr(os, "getuid", lambda: None)()
    running, ranked = False, []
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            if uid is not None and d.stat().st_uid != uid:
                continue
            comm = (d / "comm").read_text(errors="replace").strip()
        except OSError:
            continue
        if comm not in STEAM_PROCS:
            continue
        running = running or comm in ("steam", "steamwebhelper")
        got = proc_env(d, SESSION_KEYS)
        if got.get("XDG_RUNTIME_DIR") and (got.get("DISPLAY") or got.get("WAYLAND_DISPLAY")):
            ranked.append((STEAM_PROCS.index(comm), got))
    if ranked:
        env.update(min(ranked, key=lambda r: r[0])[1])
    return env, running

# --------------------------------------------------------------------------- steam: live control via CEF remote debugging

def _ws_mask(data, mask):
    n = len(data)
    words = (n + 3) // 4
    m = int.from_bytes(mask * words, "big")
    d = int.from_bytes(data + b"\0" * (words * 4 - n), "big")
    return (d ^ m).to_bytes(words * 4, "big")[:n]


class WS:
    """Minimal RFC 6455 client: text frames, fragmentation, ping/pong, close."""

    def __init__(self, url, timeout=15):
        u = urllib.parse.urlparse(url)
        host, port = u.hostname, u.port or 80
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket: соединение закрыто при рукопожатии")
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raise ConnectionError("websocket: " + status.decode(errors="replace"))
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if accept not in head:
            raise ConnectionError("websocket: неверный Sec-WebSocket-Accept")

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket: соединение закрыто")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, opcode, payload):
        n = len(payload)
        head = bytearray([0x80 | opcode])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(bytes(head) + mask + _ws_mask(payload, mask))

    def send_text(self, text):
        self._send(0x1, text.encode("utf-8"))

    def recv_text(self):
        message, started = bytearray(), False
        while True:
            b1, b2 = self._read(2)
            fin, opcode, masked, n = b1 & 0x80, b1 & 0x0F, b2 & 0x80, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else None
            data = self._read(n)
            if mask:
                data = _ws_mask(data, mask)
            if opcode == 0x8:
                raise ConnectionError("websocket: закрыто сервером")
            if opcode == 0x9:
                self._send(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                message, started = bytearray(data), True
            elif opcode == 0x0 and started:
                message += data
            if fin and started:
                return message.decode("utf-8", "replace")

    def close(self):
        try:
            self._send(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class SteamCDP:
    """Drive the running Steam client through its CEF remote-debugging port.

    Enabled by the marker file <steam>/.cef-enable-remote-debugging (what Decky
    Loader does); Steam picks it up on its next start. All calls evaluate JS in
    Steam's SharedJSContext, where the SteamClient API lives.
    """

    def __init__(self, port):
        self.port = port
        self._cache = (0.0, None)

    @property
    def enabled(self):
        return bool(STATE.get("cef_enabled", CEF_ENABLED))

    def marker(self):
        return STEAM_ROOT / ".cef-enable-remote-debugging"

    def ensure_marker(self):
        try:
            if self.enabled and STEAM_ROOT.is_dir() and not self.marker().exists():
                self.marker().touch()
                log(f"created {self.marker()} - Steam control activates after the next Steam restart")
            elif not self.enabled and self.marker().exists():
                self.marker().unlink()
        except OSError as e:
            log(f"cef marker: {e}")

    def target(self, force=False):
        now = time.time()
        if not force and now - self._cache[0] < 10:
            return self._cache[1]
        url = None
        if self.enabled:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=1.5) as r:
                    targets = json.load(r)
                for t in targets:
                    if t.get("title") == "SharedJSContext" and t.get("webSocketDebuggerUrl"):
                        url = t["webSocketDebuggerUrl"]
                        break
                if url is None:
                    for t in targets:
                        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                            url = t["webSocketDebuggerUrl"]
                            break
            except Exception:  # noqa: BLE001
                url = None
        self._cache = (now, url)
        return url

    def available(self):
        return self.target() is not None

    def status(self):
        return {"enabled": self.enabled, "marker": self.marker().exists(), "available": self.available()}

    def eval(self, expr, timeout=30):
        url = self.target()
        if not url:
            raise RuntimeError("управление Steam недоступно")
        ws = WS(url, timeout)
        try:
            ws.send_text(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                     "params": {"expression": expr, "awaitPromise": True, "returnByValue": True}}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                msg = json.loads(ws.recv_text())
                if msg.get("id") != 1:
                    continue  # CDP events
                if "error" in msg:
                    raise RuntimeError("Steam: " + str(msg["error"].get("message")))
                res = msg.get("result", {})
                exc = res.get("exceptionDetails")
                if exc:
                    desc = (exc.get("exception") or {}).get("description") or exc.get("text") or "ошибка JS"
                    raise RuntimeError("Steam JS: " + desc.splitlines()[0][:200])
                return (res.get("result") or {}).get("value")
            raise TimeoutError("Steam не ответил")
        finally:
            ws.close()

    def call(self, fn, *args):
        return self.eval(f"{fn}({', '.join(json.dumps(a, ensure_ascii=False) for a in args)})")

    # -- SteamClient.Apps wrappers (same calls Decky plugins use)
    def add_shortcut(self, name, exe, start_dir):
        appid = self.call("SteamClient.Apps.AddShortcut", name, exe, start_dir, "")
        if not isinstance(appid, (int, float)) or not appid:
            raise RuntimeError(f"Steam не вернул appid ({appid!r})")
        return int(appid) & 0xFFFFFFFF

    def set_name(self, appid, name):
        self.call("SteamClient.Apps.SetShortcutName", appid, name)

    def set_exe(self, appid, exe_quoted, start_dir_quoted):
        self.call("SteamClient.Apps.SetShortcutExe", appid, exe_quoted)
        self.call("SteamClient.Apps.SetShortcutStartDir", appid, start_dir_quoted)

    def set_compat(self, appid, tool):
        self.call("SteamClient.Apps.SpecifyCompatTool", appid, tool or "")

    def set_artwork(self, appid, data, ext, asset_type):
        self.call("SteamClient.Apps.SetCustomArtworkForApp", appid, base64.b64encode(data).decode(), ext, asset_type)

    def set_icon(self, appid, path):
        self.call("SteamClient.Apps.SetShortcutIcon", appid, str(path))

    def remove_shortcut(self, appid):
        self.call("SteamClient.Apps.RemoveShortcut", appid)


CDP = SteamCDP(CEF_PORT)

# --------------------------------------------------------------------------- steam: names, add, rename, pending ops

GENERIC_STEMS = {"game", "start", "launcher", "play", "run", "main", "app", "launch", "bin", "engine",
                 "client", "nscript", "nscr", "kirikiri", "krkr", "krkrz", "siglus", "siglusengine",
                 "bgi", "cmvs32", "cmvs64", "yuris", "advhd", "malie", "rugp", "system", "ayame",
                 "game-32", "game-64", "exe", "win", "windows", "x64", "x86"}
NAME_EXT_RE = re.compile(r"\.(exe|sh|x86_64|x86)$", re.I)


def strip_exe_ext(name):
    return NAME_EXT_RE.sub("", (name or "").strip()).strip()


def is_generic_stem(stem):
    return (stem.lower() in GENERIC_STEMS or len(stem) < 3
            or bool(re.fullmatch(r"(game|start|launcher|play)[-_ ]?\d*", stem, re.I)))


def clean_title(raw):
    """Library-name cleanup: no extension, no (1)/[RUS]/(2019) tags, no version numbers,
    underscores -> spaces, first letter capitalised."""
    base = strip_exe_ext(raw)
    base = re.sub(r"[\[\(].*?[\]\)]", " ", base)
    base = re.sub(r"(?<![A-Za-z0-9])[vV]\.?\d+(\.\d+)*[a-z]?(?![A-Za-z0-9])", " ", base)
    base = re.sub(r"[_]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip(" -_.")
    return base[:1].upper() + base[1:] if base else ""


def pretty_name(exe, game_dir):
    """Library name: the exe stem when it says something, else the folder name; both cleaned."""
    stem = Path(exe).stem
    return clean_title(Path(game_dir).name if is_generic_stem(stem) else stem) or clean_title(stem) or stem


def name_candidates(game_dir, rels, steam_names=()):
    """Possible library names, best first: existing Steam names, exe stems (recommended exe first), folder name."""
    out = []

    def add(n):
        if n and n.lower() not in {o.lower() for o in out}:
            out.append(n)

    for n in steam_names:
        add(clean_title(n) or strip_exe_ext(n))
    rec = recommend(rels) if rels else None
    for r in ([rec] if rec else []) + [r for r in rels if r != rec]:
        stem = Path(r).stem
        if not is_generic_stem(stem):
            add(clean_title(stem))
    add(clean_title(Path(game_dir).name))
    return out

def is_linux_exe(path):
    return str(path).lower().endswith(LINUX_EXTS)


def game_exe_path(game_dir, exe):
    p = (Path(game_dir) / exe).resolve()
    if not inside_any(p) or not p.is_file():
        raise ValueError("неверный путь")
    return p


def queue_pending(**op):
    with STATE_LOCK:
        pend = list(STATE.get("pending") or [])
        pend = [o for o in pend if not (o.get("op") == op["op"] and o.get("exe") == op["exe"])]
        pend.append(op)
        STATE["pending"] = pend
        save_state()


def resolve_appid(exe, wait=0):
    """appid of the shortcut for exe: from state, else shortcuts.vdf (polling up to `wait` s)."""
    rec = added_rec(str(exe))
    if rec.get("appid"):
        return rec["appid"]
    deadline = time.time() + wait
    while True:
        sc = shortcuts_index().get(str(exe))
        if sc:
            update_added(str(exe), appid=sc["appid"])
            return sc["appid"]
        if time.time() >= deadline:
            return None
        time.sleep(1)


def add_to_steam(game_dir, exe, name=None, tool=None):
    """Add the executable to Steam under `name` (default: pretty_name) with compat `tool`
    (None = default from settings, "" = none). Returns a note for the UI."""
    p = game_exe_path(game_dir, exe)
    linux = is_linux_exe(p)
    if linux:
        try:
            p.chmod(p.stat().st_mode | 0o111)
        except OSError:
            pass
    name = (name or "").strip() or pretty_name(p, game_dir)
    tool = None if linux else ((STATE.get("default_compat") if tool is None else tool) or None)
    if CDP.available():
        appid = CDP.add_shortcut(name, str(p), str(p.parent))
        try:
            CDP.set_exe(appid, f'"{p}"', f'"{p.parent}"')
        except Exception as e:  # noqa: BLE001
            log(f"set exe/startdir after add: {e}")
        if tool:
            CDP.set_compat(appid, tool)
        update_added(str(p), appid=appid, name=name, compat=tool, via="cdp", art=False)
        threading.Thread(target=art_worker, args=(str(p), False, False), daemon=True).start()
        return f"добавлено как «{name}»" + (f", {compat_label(tool)}" if tool else ", нативно без Proton")
    # fallback: steamos-add-to-steam names the shortcut after the file; fix it up later via CEF
    env, running = session_env()
    if not running:
        raise RuntimeError("Steam не запущен: открой библиотеку на деке и нажми ещё раз")
    ok = False
    if shutil.which("steamos-add-to-steam"):
        res = subprocess.run(["steamos-add-to-steam", str(p)], capture_output=True, text=True, env=env, timeout=60)
        ok = res.returncode == 0
        if not ok:
            log(f"steamos-add-to-steam failed ({res.returncode}): {(res.stderr or res.stdout).strip()}")
    if not ok:
        steam = shutil.which("steam")
        if not steam:
            raise RuntimeError("Steam не найден: добавь вручную в Desktop Mode")
        subprocess.Popen([steam, "steam://addnonsteamgame/" + urllib.parse.quote(str(p))],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    update_added(str(p), name=name, compat=tool, via="steamos", art=False)
    queue_pending(op="rename", exe=str(p), name=name)
    if tool:
        queue_pending(op="compat", exe=str(p), tool=tool)
    threading.Thread(target=art_worker, args=(str(p), False, True), daemon=True).start()
    return ("добавлено; имя «" + name + "» и Proton применятся, когда включится управление Steam "
            "(после перезагрузки дека)")


def rename_shortcut(game_dir, exe, name):
    p = game_exe_path(game_dir, exe)
    name = name.strip()
    if not name:
        raise ValueError("пустое имя")
    update_added(str(p), name=name)
    appid = resolve_appid(p)
    if CDP.available() and appid:
        CDP.set_name(appid, name)
        return f"переименовано в «{name}»"
    queue_pending(op="rename", exe=str(p), name=name)
    return "переименование в очереди до включения управления Steam"


def set_compat_for(game_dir, exe, tool):
    p = game_exe_path(game_dir, exe)
    update_added(str(p), compat=tool or None)
    appid = resolve_appid(p)
    if CDP.available() and appid:
        CDP.set_compat(appid, tool or "")
        return f"Proton: {compat_label(tool)}"
    queue_pending(op="compat", exe=str(p), tool=tool or "")
    return "смена Proton в очереди до включения управления Steam"


def drain_pending():
    with STATE_LOCK:
        ops = list(STATE.get("pending") or [])
    if not ops or not CDP.available():
        return 0
    remaining, done = [], 0
    for op in ops:
        try:
            appid = resolve_appid(op["exe"])
            if not appid:
                remaining.append(op)
                continue
            if op["op"] == "rename":
                CDP.set_name(appid, op["name"])
            elif op["op"] == "compat":
                CDP.set_compat(appid, op.get("tool") or "")
            done += 1
            log(f"pending {op['op']} applied for {Path(op['exe']).name}")
        except Exception as e:  # noqa: BLE001
            op["error"] = str(e)
            op["tries"] = op.get("tries", 0) + 1
            if op["tries"] < 20:
                remaining.append(op)
            log(f"pending {op['op']} failed: {e}")
    with STATE_LOCK:
        STATE["pending"] = remaining
        save_state()
    return done


def pending_loop():
    while True:
        time.sleep(20)
        try:
            drain_pending()
        except Exception as e:  # noqa: BLE001
            log(f"pending loop: {e}")

# --------------------------------------------------------------------------- images: PE icon, ICO, PNG, DIB

def pe_icon(path):
    """Best icon image (raw PNG or DIB bytes) from a PE executable, or None."""
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            return _pe_icon(m)
    except (OSError, ValueError, struct.error, IndexError, KeyError):
        return None


def _pe_icon(m):
    if m[:2] != b"MZ":
        return None
    pe = struct.unpack_from("<I", m, 0x3C)[0]
    if m[pe:pe + 4] != b"PE\0\0":
        return None
    nsec = struct.unpack_from("<H", m, pe + 6)[0]
    opt_size = struct.unpack_from("<H", m, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", m, opt)[0]
    dd = opt + (96 if magic == 0x10B else 112)
    rsrc_rva = struct.unpack_from("<I", m, dd + 2 * 8)[0]
    if not rsrc_rva:
        return None
    sections = []
    sec = opt + opt_size
    for i in range(nsec):
        vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", m, sec + i * 40 + 8)
        sections.append((va, max(vsize, rawsize), rawptr))

    def off(rva):
        for va, size, ptr in sections:
            if va <= rva < va + size:
                return rva - va + ptr
        raise ValueError("rva outside sections")

    base = off(rsrc_rva)

    def entries(dir_off):
        n_named, n_id = struct.unpack_from("<HH", m, dir_off + 12)
        return [struct.unpack_from("<II", m, dir_off + 16 + i * 8) for i in range(n_named + n_id)]

    def leaf(e):
        while e & 0x80000000:
            subs = entries(base + (e & 0x7FFFFFFF))
            if not subs:
                raise ValueError("empty resource dir")
            e = subs[0][1]
        rva, size = struct.unpack_from("<II", m, base + e)
        o = off(rva)
        return bytes(m[o:o + size])

    types = dict(entries(base))
    if 14 not in types or 3 not in types:
        return None
    icons = dict(entries(base + (types[3] & 0x7FFFFFFF)))
    groups = entries(base + (types[14] & 0x7FFFFFFF))
    if not groups:
        return None
    grp = leaf(groups[0][1])
    count = struct.unpack_from("<H", grp, 4)[0]
    best = None
    for i in range(count):
        w, _h, _cc, _res, _planes, bpp, _size, ident = struct.unpack_from("<BBBBHHIH", grp, 6 + i * 14)
        if ident not in icons:
            continue
        score = ((w or 256), bpp)
        if best is None or score > best[0]:
            best = (score, icons[ident])
    return leaf(best[1]) if best else None


def ico_best(data):
    """Best image (raw PNG or DIB bytes) from an .ico file."""
    count = struct.unpack_from("<H", data, 4)[0]
    best = None
    for i in range(count):
        w, _h, _cc, _res, _planes, bpp, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + i * 16)
        score = ((w or 256), bpp)
        if best is None or score > best[0]:
            best = (score, data[offset:offset + size])
    return best[1] if best else None


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def png_decode(data):
    """Minimal PNG decoder (8-bit, non-interlaced) -> (w, h, rgba bytes)."""
    if data[:8] != PNG_SIG:
        raise ValueError("not a png")
    pos, idat, plte, trns = 8, [], b"", b""
    w = h = ct = bd = il = 0
    while pos + 8 <= len(data):
        ln, typ = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, bd, ct, _, _, il = struct.unpack(">IIBBBBB", body)
        elif typ == b"PLTE":
            plte = body
        elif typ == b"tRNS":
            trns = body
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    if bd != 8 or il or ct not in (0, 2, 3, 4, 6):
        raise ValueError("unsupported png")
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    raw = zlib.decompress(b"".join(idat))
    stride = w * ch
    prev = bytearray(stride)
    out = bytearray()
    pos = 0
    for _ in range(h):
        f = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if f == 1:
            for x in range(ch, stride):
                line[x] = (line[x] + line[x - ch]) & 0xFF
        elif f == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif f == 3:
            for x in range(stride):
                line[x] = (line[x] + (((line[x - ch] if x >= ch else 0) + prev[x]) >> 1)) & 0xFF
        elif f == 4:
            for x in range(stride):
                a = line[x - ch] if x >= ch else 0
                c = prev[x - ch] if x >= ch else 0
                line[x] = (line[x] + _paeth(a, prev[x], c)) & 0xFF
        out += line
        prev = line
    if ct == 6:
        return w, h, bytes(out)
    px = bytearray(w * h * 4)
    for i in range(w * h):
        o = i * 4
        if ct == 2:
            px[o:o + 3] = out[i * 3:i * 3 + 3]
            px[o + 3] = 255
        elif ct == 0:
            px[o] = px[o + 1] = px[o + 2] = out[i]
            px[o + 3] = 255
        elif ct == 4:
            px[o] = px[o + 1] = px[o + 2] = out[i * 2]
            px[o + 3] = out[i * 2 + 1]
        else:
            idx = out[i]
            px[o:o + 3] = plte[idx * 3:idx * 3 + 3]
            px[o + 3] = trns[idx] if idx < len(trns) else 255
    return w, h, bytes(px)


def png_encode(w, h, rgba):
    raw = b"".join(b"\0" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def dib_decode(data):
    """Icon DIB (BITMAPINFOHEADER + XOR bitmap + AND mask) -> (w, h, rgba)."""
    size, w, h2, _planes, bpp, comp = struct.unpack_from("<IiiHHI", data, 0)
    if comp != 0 or bpp not in (1, 4, 8, 24, 32):
        raise ValueError(f"unsupported dib (bpp={bpp}, comp={comp})")
    h = abs(h2)
    if h == 2 * w:      # icon DIBs store XOR+AND bitmaps stacked, so height is doubled
        h //= 2
    off = size
    palette = []
    if bpp <= 8:
        n = 1 << bpp
        palette = [data[off + i * 4:off + i * 4 + 4] for i in range(n)]
        off += n * 4
    row = ((w * bpp + 31) // 32) * 4
    mask_row = ((w + 31) // 32) * 4
    xor_off, and_off = off, off + row * h
    has_mask = and_off + mask_row * h <= len(data)
    use_alpha = False
    if bpp == 32:
        use_alpha = any(data[xor_off + i * 4 + 3] for i in range(w * h))
    px = bytearray(w * h * 4)
    for y in range(h):
        sr = xor_off + (h - 1 - y) * row
        mr = and_off + (h - 1 - y) * mask_row
        for x in range(w):
            a = 255
            if bpp == 32:
                b, g, r, a32 = data[sr + x * 4:sr + x * 4 + 4]
                if use_alpha:
                    a = a32
            elif bpp == 24:
                b, g, r = data[sr + x * 3:sr + x * 3 + 3]
            else:
                if bpp == 8:
                    idx = data[sr + x]
                elif bpp == 4:
                    idx = (data[sr + x // 2] >> (4 if x % 2 == 0 else 0)) & 15
                else:
                    idx = (data[sr + x // 8] >> (7 - x % 8)) & 1
                b, g, r = palette[idx][:3]
            if not use_alpha and has_mask:
                a = 0 if (data[mr + x // 8] >> (7 - x % 8)) & 1 else 255
            o = (y * w + x) * 4
            px[o], px[o + 1], px[o + 2], px[o + 3] = r, g, b, a
    return w, h, bytes(px)


def decode_icon_image(data):
    return png_decode(data) if data[:8] == PNG_SIG else dib_decode(data)


def find_icon_file(game_dir):
    """Fallback for games without an exe icon: an .ico / icon png in the game folder."""
    pats = ("*.ico", "*/*.ico", "icon.png", "*/icon.png", "*/window_icon.png", "*/*/window_icon.png", "*icon*.png")
    for pat in pats:
        for p in sorted(Path(game_dir).glob(pat)):
            try:
                data = p.read_bytes()
                return decode_icon_image(ico_best(data) if p.suffix.lower() == ".ico" else data)
            except (ValueError, struct.error, zlib.error, IndexError):
                continue
    return None


def load_icon(exe_path):
    p = Path(exe_path)
    if p.suffix.lower() == ".exe":
        raw = pe_icon(p)
        if raw:
            try:
                return decode_icon_image(raw)
            except (ValueError, struct.error, zlib.error, IndexError) as e:
                log(f"icon decode failed for {p.name}: {e}")
    return find_icon_file(p.parent)


def dominant_color(w, h, rgba):
    r = g = b = n = 0
    for i in range(0, w * h * 4, 4 * max(1, (w * h) // 4096)):
        if rgba[i + 3] > 128:
            r += rgba[i]
            g += rgba[i + 1]
            b += rgba[i + 2]
            n += 1
    if not n:
        return (0x1B, 0x28, 0x38)
    return tuple(int(c / n * 0.35) for c in (r, g, b))


def compose(cw, ch, iw, ih, rgba, bg, frac):
    """Nearest-neighbour scale the icon to `frac` of the canvas and centre it on a solid bg."""
    scale = min(cw * frac / iw, ch * frac / ih)
    tw, th = max(1, int(iw * scale)), max(1, int(ih * scale))
    xs = [min(iw - 1, int(x / scale)) * 4 for x in range(tw)]
    ys = [min(ih - 1, int(y / scale)) for y in range(th)]
    ox, oy = (cw - tw) // 2, (ch - th) // 2
    bgpx = bytes(bg) + b"\xff"
    canvas = bytearray(bgpx * (cw * ch))
    for ty in range(th):
        srow = rgba[ys[ty] * iw * 4:(ys[ty] + 1) * iw * 4]
        row = bytearray(tw * 4)
        for tx in range(tw):
            sx = xs[tx]
            a = srow[sx + 3]
            o = tx * 4
            if a == 255:
                row[o:o + 4] = srow[sx:sx + 4]
            elif a == 0:
                row[o:o + 4] = bgpx
            else:
                ia = 255 - a
                row[o] = (srow[sx] * a + bg[0] * ia) // 255
                row[o + 1] = (srow[sx + 1] * a + bg[1] * ia) // 255
                row[o + 2] = (srow[sx + 2] * a + bg[2] * ia) // 255
                row[o + 3] = 255
        start = ((oy + ty) * cw + ox) * 4
        canvas[start:start + tw * 4] = row
    return bytes(canvas)

# --------------------------------------------------------------------------- ffmpeg helpers (optional, better quality)

def ff_run(inp, in_ext, args, out_ext, timeout=120):
    """Run ffmpeg on bytes -> bytes (via temp files). Raises RuntimeError on failure."""
    if not FFMPEG:
        raise RuntimeError("ffmpeg не найден")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tag = secrets.token_hex(4)
    src = CACHE_DIR / f"ff_{tag}_in.{in_ext}"
    dst = CACHE_DIR / f"ff_{tag}_out.{out_ext}"
    try:
        src.write_bytes(inp)
        res = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(src)] + args + [str(dst)],
                             capture_output=True, text=True, errors="replace", timeout=timeout)
        if res.returncode != 0 or not dst.is_file():
            raise RuntimeError("ffmpeg: " + res.stderr.strip()[-300:])
        return dst.read_bytes()
    finally:
        for f in (src, dst):
            try:
                f.unlink()
            except OSError:
                pass


def ff_cover(inp, in_ext, w, h):
    """Scale-to-fill and centre-crop to exactly w x h (jpg)."""
    return ff_run(inp, in_ext, ["-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
                                "-frames:v", "1", "-q:v", "3"], "jpg")


def ff_fit_blur(inp, in_ext, w, h):
    """Fit inside w x h over a blurred, filled copy of itself (jpg)."""
    vf = (f"split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:2[bg];"
          f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    return ff_run(inp, in_ext, ["-vf", vf, "-frames:v", "1", "-q:v", "3"], "jpg")


def find_font(text=""):
    cjk = bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", text))
    dirs = [Path("/usr/share/fonts"), Path.home() / ".local/share/fonts", Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"]
    prefs = (["NotoSansCJK-Bold.ttc", "NotoSansCJKjp-Bold.otf", "NotoSansCJK-Regular.ttc"] if cjk else []) + \
            ["DejaVuSans-Bold.ttf", "NotoSans-Bold.ttf", "LiberationSans-Bold.ttf", "arialbd.ttf", "segoeuib.ttf"]
    found = {}
    for d in dirs:
        if d.is_dir():
            for f in d.rglob("*"):
                if f.suffix.lower() in (".ttf", ".otf", ".ttc") and f.name not in found:
                    found[f.name] = f
    for name in prefs:
        if name in found:
            return found[name]
    return next(iter(found.values()), None)


def ff_logo(title):
    """Transparent PNG with the title as text (Steam 'logo' asset)."""
    font = find_font(title)
    if not font:
        raise RuntimeError("шрифт не найден")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tag = secrets.token_hex(4)
    txt = CACHE_DIR / f"logo_{tag}.txt"
    out = CACHE_DIR / f"logo_{tag}.png"
    size = max(48, min(120, int(1700 / max(len(title), 1))))
    fontfile = str(font).replace("\\", "/").replace(":", "\\:")
    try:
        txt.write_text(title, "utf-8")
        vf = (f"drawtext=fontfile='{fontfile}':textfile='{str(txt).replace(chr(92), '/').replace(':', chr(92) + ':')}'"
              f":fontsize={size}:fontcolor=white:borderw=4:bordercolor=black@0.55:x=(w-text_w)/2:y=(h-text_h)/2")
        res = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black@0.0:s=1280x400,format=rgba",
                              "-vf", vf, "-frames:v", "1", str(out)], capture_output=True, text=True, errors="replace", timeout=60)
        if res.returncode != 0 or not out.is_file():
            raise RuntimeError("ffmpeg drawtext: " + res.stderr.strip()[-200:])
        return out.read_bytes()
    finally:
        for f in (txt, out):
            try:
                f.unlink()
            except OSError:
                pass

# --------------------------------------------------------------------------- cover art: icon-based, VNDB, apply

# SteamClient.Apps.SetCustomArtworkForApp knows four artwork slots only. There is no type for the
# shortcut icon: sending one used to land in the landscape slot. The icon goes through SetShortcutIcon.
ASSET_TYPE = {"portrait": 0, "hero": 1, "logo": 2, "landscape": 3}
GRID_SUFFIX = {"portrait": "p", "landscape": "", "hero": "_hero", "logo": "_logo", "icon": "_icon"}
SIZES = {"portrait": (600, 900), "landscape": (920, 430), "hero": (1920, 620)}


def capsule_png(icon, cw, ch, frac):
    iw, ih, rgba = icon
    bg = dominant_color(iw, ih, rgba)
    if FFMPEG:
        tw, th = int(cw * frac), int(ch * frac)
        fc = (f"color=c=0x{bg[0]:02x}{bg[1]:02x}{bg[2]:02x}:s={cw}x{ch}:d=1[bg];"
              f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos[ic];"
              f"[bg][ic]overlay=(W-w)/2:(H-h)/2:shortest=1,format=rgb24")
        try:
            return ff_run(png_encode(iw, ih, rgba), "png", ["-filter_complex", fc, "-frames:v", "1"], "png")
        except RuntimeError as e:
            log(f"ffmpeg capsule failed, falling back: {e}")
    return png_encode(cw, ch, compose(cw, ch, iw, ih, rgba, bg, frac))


def norm_title(s):
    return re.sub(r"[^a-z0-9а-яё]+", "", s.lower())


def vndb_fetch(url, timeout=40):
    """Download a VNDB image through DeckDrop's proxy setting, with retries."""
    def go():
        with net_open(url, headers={"User-Agent": VNDB_UA}, timeout=timeout, proxy=proxy_url()) as r:
            return r.read()
    return with_retries(urllib.parse.urlparse(url).hostname or "VNDB", go)


def vndb_query(filters, results=6):
    body = {"filters": filters, "results": results,
            "fields": "title, alttitle, released, image.url, image.dims, image.sexual, image.violence, "
                      "screenshots.url, screenshots.dims, screenshots.sexual, screenshots.violence"}
    if filters and filters[0] == "search":
        body["sort"] = "searchrank"

    def go():
        with net_open("https://api.vndb.org/kana/vn", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", "User-Agent": VNDB_UA},
                      timeout=25, proxy=proxy_url(), method="POST") as r:
            return json.loads(r.read()).get("results") or []
    return with_retries("api.vndb.org", go)


def proxy_test():
    """Reachability of the VNDB hosts, directly and through the configured proxy."""
    px = proxy_url()
    checks = []
    for host, url in (("api.vndb.org", "https://api.vndb.org/kana/schema"), ("t.vndb.org", "https://t.vndb.org/")):
        for mode, p in [("напрямую", None)] + ([("через прокси", px)] if px else []):
            t0 = time.time()
            try:
                with net_open(url, headers={"User-Agent": VNDB_UA}, timeout=12, proxy=p) as r:
                    r.read(200)
                checks.append({"host": host, "mode": mode, "ok": True, "ms": int((time.time() - t0) * 1000)})
            except urllib.error.HTTPError as e:      # an HTTP answer still means we got through
                checks.append({"host": host, "mode": mode, "ok": True, "ms": int((time.time() - t0) * 1000),
                               "note": f"ответ HTTP {e.code}"})
            except Exception as e:  # noqa: BLE001
                checks.append({"host": host, "mode": mode, "ok": False, "error": net_reason(e)})
    return {"proxy": px or "", "checks": checks}


def vndb_search(q):
    q = re.sub(r"\b(rus|eng|jpn?|ru|en|jp|russian|english|uncensored|patched|repack|final|full|remake|hd|dl|steam)\b", " ", q, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip()
    return [{"id": v["id"], "title": v.get("title"), "alttitle": v.get("alttitle"), "released": v.get("released"),
             "image": (v.get("image") or {}).get("url"), "sexual": (v.get("image") or {}).get("sexual", 0)}
            for v in vndb_query(["search", "=", q])] if q else []


def vndb_pick(name, vn_id=None):
    """VNDB entry for a game: by id, or by name when the top hit matches the query well enough."""
    if vn_id:
        res = vndb_query(["id", "=", vn_id], 1)
        if not res:
            raise RuntimeError(f"VNDB: {vn_id} не найден")
        return res[0]
    q = re.sub(r"\b(rus|eng|jpn?|ru|en|jp|russian|english|uncensored|patched|repack|final|full|hd)\b", " ", name, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip()
    if len(norm_title(q)) < 3:
        return None
    nq = norm_title(q)
    for v in vndb_query(["search", "=", q], 5):
        for t in (v.get("title") or "", v.get("alttitle") or ""):
            nt = norm_title(t)
            if nt and (nt == nq or (len(nq) >= 5 and (nq in nt or nt in nq))):
                return v
    return None


def vndb_allowed(img):
    limit = 3.0 if STATE.get("vndb_nsfw") else 1.4
    return img and img.get("url") and float(img.get("sexual") or 0) <= limit and float(img.get("violence") or 0) <= limit


def vndb_images(vn, have_icon):
    """{slot: (bytes, ext)} from a VNDB entry, honoring the NSFW setting."""
    imgs, notes = {}, []
    cover = vn.get("image") or {}
    shots = [s for s in (vn.get("screenshots") or []) if vndb_allowed(s)]
    shots.sort(key=lambda s: -(s.get("dims") or [0, 0])[0])
    cov = None
    if vndb_allowed(cover):
        cov = vndb_fetch(cover["url"])
    elif cover.get("url"):
        notes.append("обложка VNDB отфильтрована как NSFW")
    shot = None
    if shots:
        shot = vndb_fetch(shots[0]["url"])
    if FFMPEG:
        if cov:
            imgs["portrait"] = (ff_fit_blur(cov, "jpg", 600, 900), "jpg")
        wide = shot or cov
        if wide:
            imgs["landscape"] = (ff_cover(wide, "jpg", 920, 430), "jpg")
            imgs["hero"] = (ff_cover(wide, "jpg", 1920, 620), "jpg")
        if cov and not have_icon:
            imgs["icon"] = (ff_cover(cov, "jpg", 256, 256), "jpg")
        try:
            imgs["logo"] = (ff_logo(vn.get("title") or ""), "png")
        except RuntimeError as e:
            notes.append(f"логотип: {e}")
    else:
        if cov:
            imgs["portrait"] = (cov, "jpg")
        if shot:
            imgs["landscape"] = (shot, "jpg")
            imgs["hero"] = (shot, "jpg")
        notes.append("без ffmpeg картинки VNDB поставлены как есть")
    return imgs, notes


def art_userdata(exe):
    sc = shortcuts_index().get(str(exe))
    return Path(sc["userdata"]) if sc else pick_userdata()


def art_files(appid, ud):
    """{slot: path} of the cover files currently in userdata/<id>/config/grid (newest per slot)."""
    grid = ud / "config" / "grid"
    out = {}
    if not grid.is_dir():
        return out
    for slot, suf in GRID_SUFFIX.items():
        cands = [f for f in grid.glob(f"{appid}{suf}.*") if f.suffix.lower() in (".png", ".jpg", ".jpeg")]
        if cands:
            out[slot] = max(cands, key=lambda f: f.stat().st_mtime)
    return out


def art_file_for(appid, slot):
    for ud in userdata_dirs():
        f = art_files(appid, ud).get(slot)
        if f:
            return f
    return None


def apply_artwork(appid, ud, images):
    """Write covers into userdata/<id>/config/grid (what Steam reads on start / what the UI previews)
    and, when Steam control is live, push them to the running client as well."""
    ud = ud or pick_userdata()
    grid = ud / "config" / "grid"
    grid.mkdir(parents=True, exist_ok=True)
    for slot, (data, ext) in images.items():
        for old in grid.glob(f"{appid}{GRID_SUFFIX[slot]}.*"):
            if old.suffix.lower() in (".png", ".jpg", ".jpeg"):
                old.unlink()
        (grid / f"{appid}{GRID_SUFFIX[slot]}.{ext}").write_bytes(data)
    if not CDP.available():
        return "files"
    for slot, (data, ext) in images.items():
        if slot in ASSET_TYPE:
            CDP.set_artwork(appid, data, ext, ASSET_TYPE[slot])
    if "icon" in images:
        try:
            CDP.set_icon(appid, grid / f"{appid}_icon.{images['icon'][1]}")
        except Exception as e:  # noqa: BLE001
            log(f"set icon: {e}")
    return "live"


def art_current(game_dir, exe):
    """Cover slots of an added game with URLs for preview."""
    p = game_exe_path(game_dir, exe)
    appid = resolve_appid(p)
    if not appid:
        raise ValueError("игра ещё не добавлена в Steam")
    files = art_files(appid, art_userdata(p))
    rec = added_rec(str(p))
    slots = {}
    for slot in GRID_SUFFIX:
        f = files.get(slot)
        # nanosecond mtime: two replacements in the same second must still bust the browser cache
        slots[slot] = ({"url": f"/art/{appid}/{slot}?v={f.stat().st_mtime_ns}", "size": f.stat().st_size,
                        "ext": f.suffix.lstrip(".").lower()} if f else None)
    return {"appid": appid, "source": rec.get("art_source"), "vndb_title": rec.get("vndb_title"),
            "note": rec.get("art_note"), "error": rec.get("art_error"), "live": CDP.available(), "slots": slots}


def vndb_image_list(vn_id):
    """Cover + screenshots of one VN, for picking a single slot image by hand."""
    res = vndb_query(["id", "=", vn_id], 1)
    if not res:
        raise RuntimeError(f"VNDB: {vn_id} не найден")
    v = res[0]
    out = []
    cover = v.get("image") or {}
    if vndb_allowed(cover):
        out.append({"kind": "cover", "url": cover["url"], "dims": cover.get("dims")})
    for shot in v.get("screenshots") or []:
        if vndb_allowed(shot):
            out.append({"kind": "screenshot", "url": shot["url"], "dims": shot.get("dims")})
    return {"id": v["id"], "title": v.get("title"), "images": out}


def art_from_url(game_dir, exe, slot, url, vn_id=None):
    """Put one VNDB image into one cover slot, fitted/cropped for that slot when ffmpeg is around."""
    if slot not in GRID_SUFFIX:
        raise ValueError("неизвестный слот обложки")
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or not (u.netloc == "vndb.org" or u.netloc.endswith(".vndb.org")):
        raise ValueError("картинки можно брать только с vndb.org")
    p = game_exe_path(game_dir, exe)
    appid = resolve_appid(p)
    if not appid:
        raise ValueError("игра ещё не добавлена в Steam")
    data = vndb_fetch(url)
    ext = "png" if data[:8] == PNG_SIG else "jpg"
    if FFMPEG:
        try:
            if slot == "portrait":
                data, ext = ff_fit_blur(data, ext, 600, 900), "jpg"
            elif slot == "landscape":
                data, ext = ff_cover(data, ext, 920, 430), "jpg"
            elif slot == "hero":
                data, ext = ff_cover(data, ext, 1920, 620), "jpg"
            elif slot == "icon":
                data, ext = ff_cover(data, ext, 256, 256), "jpg"
        except RuntimeError as e:
            log(f"ffmpeg for {slot} failed, using the image as is: {e}")
    how = apply_artwork(appid, art_userdata(p), {slot: (data, ext)})
    kv = {"art": True, "art_error": None, "art_source": "vndb"}
    if vn_id:
        kv["vndb_id"] = vn_id
    update_added(str(p), **kv)
    return {"slot": slot, "how": how}


def art_from_exe(game_dir, exe, slot):
    """Fill one cover slot from the icon inside the executable (or an .ico next to it)."""
    if slot not in GRID_SUFFIX:
        raise ValueError("неизвестный слот обложки")
    p = game_exe_path(game_dir, exe)
    appid = resolve_appid(p)
    if not appid:
        raise ValueError("игра ещё не добавлена в Steam")
    icon = load_icon(p)
    if not icon:
        raise RuntimeError(f"в {p.name} нет иконки, и рядом не нашлось .ico или icon.png")
    if slot in SIZES:
        w, h = SIZES[slot]
        data = capsule_png(icon, w, h, 0.6 if slot == "portrait" else 0.5)
    else:                      # icon and logo keep the transparent original
        data = png_encode(*icon)
    how = apply_artwork(appid, art_userdata(p), {slot: (data, "png")})
    update_added(str(p), art=True, art_error=None)
    return {"slot": slot, "how": how, "size": f"{icon[0]}x{icon[1]}"}


def custom_art(game_dir, exe, slot, data):
    """Replace one cover slot with an image uploaded by the user (PNG or JPEG)."""
    if slot not in GRID_SUFFIX:
        raise ValueError("неизвестный слот обложки")
    if data[:8] == PNG_SIG:
        ext = "png"
    elif data[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    else:
        raise ValueError("нужен PNG или JPEG")
    if len(data) > 25 << 20:
        raise ValueError("файл больше 25 МБ")
    p = game_exe_path(game_dir, exe)
    appid = resolve_appid(p)
    if not appid:
        raise ValueError("игра ещё не добавлена в Steam")
    how = apply_artwork(appid, art_userdata(p), {slot: (data, ext)})
    update_added(str(p), art=True, art_error=None, art_source="custom")
    return {"slot": slot, "how": how}


ART_BUSY = set()


def art_worker(exe, force, wait_for_shortcut, source="auto", vn_id=None):
    if exe in ART_BUSY:
        return
    ART_BUSY.add(exe)
    try:
        note = ensure_art(exe, force, wait_for_shortcut, source, vn_id)
        log(f"art for {Path(exe).name}: {note}")
    except Exception as e:  # noqa: BLE001
        log(f"art for {Path(exe).name} failed: {e}")
        update_added(exe, art=False, art_error=str(e))
    finally:
        ART_BUSY.discard(exe)


def ensure_art(exe, force=False, wait_for_shortcut=False, source="auto", vn_id=None):
    """Build and apply the full cover set (portrait, landscape, hero, logo, icon) for a shortcut."""
    p = Path(exe)
    rec = added_rec(str(p))
    if rec.get("art") and not force and source == "auto":
        return "обложка уже есть"
    appid = resolve_appid(p, wait=20 if wait_for_shortcut else 0)
    sc = shortcuts_index().get(str(p))
    ud = Path(sc["userdata"]) if sc else None
    if not appid:
        appid = shortcut_appid(f'"{p}"', p.stem)
        log(f"shortcut for {p.name} not in shortcuts.vdf yet; using computed appid {appid}")
    images, info, notes = {}, {"art_source": "icon", "vndb_id": None, "vndb_title": None}, []
    icon = load_icon(p)
    if source == "vndb" or (source == "auto" and STATE.get("vndb_auto", True)):
        try:
            query = rec.get("name") or (clean_title(sc["name"]) if sc and sc.get("name") else "") or pretty_name(p, p.parent)
            vn = vndb_pick(clean_title(query) or query, vn_id)
            if vn:
                vimgs, vnotes = vndb_images(vn, bool(icon))
                notes += vnotes
                if vimgs:
                    images.update(vimgs)
                    info = {"art_source": "vndb", "vndb_id": vn["id"], "vndb_title": vn.get("title")}
            else:
                notes.append("VNDB: подходящей новеллы не нашёл")
        except Exception as e:  # noqa: BLE001
            notes.append(f"VNDB: {str(e)[:120]}")
            log(f"vndb for {p.name}: {e}")
    if icon:
        if "icon" not in images:
            images["icon"] = (png_encode(*icon), "png")
        for slot, (w, h) in SIZES.items():
            if slot not in images:
                images[slot] = (capsule_png(icon, w, h, 0.6 if slot == "portrait" else 0.5), "png")
    if not images:
        raise RuntimeError("иконка не найдена ни в exe, ни в папке игры, и VNDB не помог")
    how = apply_artwork(appid, ud, images)
    update_added(str(p), art=True, appid=appid, art_error=None, art_note="; ".join(notes) or None, **info)
    return f"{info['art_source']} -> {', '.join(sorted(images))} ({how})" + (f"; {'; '.join(notes)}" if notes else "")

# --------------------------------------------------------------------------- games list / hide / delete

def find_exes(d):
    exes = []
    for pattern in ("*.exe", "*/*.exe", "*.sh", "*/*.sh", "*.x86_64", "*/*.x86_64"):
        for p in d.glob(pattern):
            if not SKIP_EXE.search(p.name):
                exes.append(p.relative_to(d).as_posix())
    return sorted(set(exes))[:25]


def recommend(exes):
    linux = [e for e in exes if is_linux_exe(e)]
    win = [e for e in exes if not is_linux_exe(e)]
    pool = (linux or win) if STATE.get("prefer_linux", True) else (win or linux)
    return min(pool, key=lambda e: (e.count("/"), len(e))) if pool else None


def game_dirs():
    """(dir, imported) for every card on the Игры tab: folders inside the roots, plus imported games."""
    out = []
    for root in game_roots():
        if not root.is_dir():
            continue
        try:
            kids = sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        out += [(d, False) for d in kids if d.is_dir() and not d.name.startswith((".", "_"))]
    out += [(d, True) for d in imported_dirs()]
    return out


def list_games():
    out = []
    try:
        idx = shortcuts_index()
    except Exception as e:  # noqa: BLE001
        log(f"shortcuts index failed: {e}")
        idx = {}
    with STATE_LOCK:
        added = dict(STATE.get("added", {}))
        hidden = set(STATE.get("hidden", []))
        pending = STATE.get("pending") or []
    pending_exes = {op.get("exe") for op in pending}
    if True:
        for d, is_imported in game_dirs():
            disk = disk_label_for(d)
            rels = find_exes(d)
            rec_exe = recommend(rels)
            steam_names = [idx[str((d / r).resolve())]["name"] for r in rels if str((d / r).resolve()) in idx]
            names = name_candidates(d, rels, steam_names)
            exes = []
            for rel in rels:
                full = str((d / rel).resolve())
                sc = idx.get(full)
                rec = added.get(full, {})
                appid = (sc or {}).get("appid") or rec.get("appid")
                compat_info = compat_for(appid, rec)
                exes.append({"exe": rel, "linux": is_linux_exe(rel), "recommended": rel == rec_exe,
                             "in_steam": bool(sc) or bool(rec.get("appid") or rec.get("via")),
                             "name": (sc or {}).get("name") or rec.get("name") or pretty_name(rel, d),
                             "clean_name": clean_title((sc or {}).get("name") or rec.get("name") or "") or pretty_name(rel, d),
                             "art": bool(rec.get("art")), "art_error": rec.get("art_error"),
                             "art_source": rec.get("art_source"), "vndb_title": rec.get("vndb_title"),
                             "art_note": rec.get("art_note"),
                             "compat": compat_info[0], "compat_from": compat_info[1],
                             "pending": full in pending_exes,
                             "appid": appid})
            chosen = [x for x in exes if x["in_steam"]]
            title = chosen[0]["name"] if chosen else (names[0] if names else d.name)
            out.append({"name": d.name, "title": title, "names": names, "path": str(d), "disk": disk,
                        "exes": exes, "hidden": str(d) in hidden, "imported": is_imported})
    return out


# an exe often sits in a subfolder; the game folder is the one above it
NESTED_DIRS = {"bin", "bin64", "binaries", "game", "x64", "x86", "win", "win32", "win64",
               "windows", "data", "app", "runtime", "release"}


def adopt_steam_settings(d):
    """Copy what Steam already knows about the games in this folder into DeckDrop's own record.

    Reads shortcuts.vdf, config.vdf and the grid folder; writes only DeckDrop's state file.
    """
    try:
        idx = shortcuts_index()
        ctmap = compat_mapping()
    except Exception as e:  # noqa: BLE001
        log(f"adopt: {e}")
        return []
    adopted = []
    for rel in find_exes(d):
        full = str((d / rel).resolve())
        sc = idx.get(full)
        if not sc:
            continue
        appid = sc["appid"]
        kv = {"appid": appid, "via": "steam"}
        name = clean_title(sc.get("name") or "") or strip_exe_ext(sc.get("name") or "")
        if name:
            kv["name"] = name
        if appid in ctmap:
            kv["compat"] = ctmap[appid] or None
        try:
            covers = sorted(art_files(appid, Path(sc["userdata"])))
        except OSError:
            covers = []
        if covers:
            kv.update(art=True, art_source="steam", art_note="обложки взяты из Steam")
        update_added(full, **kv)
        adopted.append({"exe": rel, "name": kv.get("name"), "compat": kv.get("compat"),
                        "covers": covers, "appid": appid})
    return adopted


def import_game(path):
    """Add one game that already lives somewhere on the Deck.

    Nothing is copied, moved or installed: the folder stays where it is and simply becomes a card.
    """
    raw = (path or "").strip().strip('"').strip("'")
    if not raw:
        raise ValueError("укажи путь к файлу запуска игры")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ValueError("нужен полный путь, начиная с /")
    try:
        p = p.resolve()
    except OSError as e:
        raise ValueError(f"не смог разобрать путь: {e}") from e
    if not p.exists():
        raise ValueError(f"по этому пути ничего нет: {p}")
    if not import_allowed(p):
        raise ValueError("добавлять можно только из домашней папки или с подключённых носителей")
    d = p if p.is_dir() else p.parent
    if not p.is_dir() and d.name.lower() in NESTED_DIRS and d.parent != d and import_allowed(d.parent):
        d = d.parent                      # .../Fate/bin/Fate.exe -> the game folder is .../Fate
    if any(r.resolve() == d or inside(r, d) for r in game_roots() if r.exists()):
        raise ValueError("эта игра и так внутри папки игр DeckDrop, она уже есть в списке")
    if d in imported_dirs():
        raise ValueError("эта игра уже добавлена")
    exes = find_exes(d)
    if not exes:
        raise ValueError(f"в папке {d.name} не нашёл ни одного exe, sh или x86_64. "
                         "Укажи путь к самому файлу запуска")
    with STATE_LOCK:
        STATE["imported"] = sorted({*(STATE.get("imported") or []), str(d)})
        save_state()
    adopted = adopt_steam_settings(d)
    log(f"imported game {d} ({len(exes)} executables, {len(adopted)} already in Steam)")
    return {"path": str(d), "name": d.name, "exes": exes, "disk": disk_label_for(d), "adopted": adopted}


def unimport_game(path):
    """Stop showing an imported game. The folder itself is left untouched."""
    p = Path(path).resolve()
    with STATE_LOCK:
        cur = list(STATE.get("imported") or [])
        keep = [x for x in cur if Path(x).resolve() != p]
        if len(keep) == len(cur):
            raise ValueError("эта игра не из добавленных вручную")
        STATE["imported"] = keep
        STATE["hidden"] = [h for h in STATE.get("hidden", []) if Path(h).resolve() != p]
        STATE["added"] = {k: v for k, v in STATE.get("added", {}).items() if not inside(p, k)}
        STATE["pending"] = [o for o in STATE.get("pending") or [] if not inside(p, o.get("exe", ""))]
        save_state()
    return p.name


def import_candidates():
    """Non-Steam shortcuts pointing outside DeckDrop's folders: one tap to show them here too."""
    out, seen = [], set()
    try:
        idx = shortcuts_index()
    except Exception as e:  # noqa: BLE001
        log(f"import scan: {e}")
        return out
    imported = imported_dirs()
    for exe, sc in idx.items():
        if not exe or not Path(exe).is_absolute():
            continue
        p = Path(exe)
        d = p.parent
        if str(d) in seen or any(inside(r, p) for r in game_roots() if r.exists()):
            continue
        if d in imported or not import_allowed(d):
            continue
        seen.add(str(d))
        out.append({"exe": exe, "dir": str(d), "name": sc.get("name"), "appid": sc.get("appid"),
                    "exists": p.exists(), "disk": disk_label_for(d)})
    out.sort(key=lambda c: (not c["exists"], (c["name"] or "").lower()))
    return out


def game_dir_path(path):
    p = Path(path).resolve()
    if p in imported_dirs():
        return p
    if not inside_any(p) or p.name.startswith("_") or not p.is_dir() or any(p == r.resolve() for r in game_roots()):
        raise ValueError("неверный путь")
    return p


def game_info(path):
    p = game_dir_path(path)
    size, files = dir_size(p)
    return {"size": size, "files": files, "mtime": int(p.stat().st_mtime)}


def set_hidden(path, hidden):
    p = str(game_dir_path(path))
    with STATE_LOCK:
        h = set(STATE.get("hidden", []))
        (h.add if hidden else h.discard)(p)
        STATE["hidden"] = sorted(h)
        save_state()


def delete_game(path, remove_shortcut=False):
    p = game_dir_path(path)
    removed, notes = [p.name], []
    if remove_shortcut:
        with STATE_LOCK:
            recs = {k: v for k, v in STATE.get("added", {}).items() if inside(p, k)}
        idx = shortcuts_index()
        appids = {v.get("appid") for v in recs.values() if v.get("appid")}
        appids |= {v["appid"] for k, v in idx.items() if inside(p, k)}
        if appids and CDP.available():
            for appid in appids:
                try:
                    CDP.remove_shortcut(appid)
                    notes.append(f"ярлык {appid} убран из Steam")
                except Exception as e:  # noqa: BLE001
                    notes.append(f"ярлык {appid}: {e}")
        elif appids:
            notes.append("ярлык в Steam остался: управление Steam недоступно")
    shutil.rmtree(p)
    inbox = p.parent / "_inbox"
    if inbox.is_dir():
        for f in inbox.iterdir():
            if f.is_file() and split_ext(f.name)[0] == p.name:
                f.unlink()
                removed.append(f.name)
    with STATE_LOCK:
        STATE["hidden"] = [h for h in STATE.get("hidden", []) if h != str(p)]
        STATE["imported"] = [x for x in STATE.get("imported") or [] if Path(x).resolve() != p]
        STATE["added"] = {k: v for k, v in STATE.get("added", {}).items() if not inside(p, k)}
        STATE["pending"] = [o for o in STATE.get("pending") or [] if not inside(p, o.get("exe", ""))]
        save_state()
    return removed, notes

# --------------------------------------------------------------------------- save games: backup / import

SAVE_DIR_RE = re.compile(r"^(save|saves|savedata|save_data|savegame|savegames|sav|userdata|profile|profiles)$", re.I)
PREFIX_SUBDIRS = ("AppData/Roaming", "AppData/Local", "AppData/LocalLow", "Documents", "Saved Games")
PREFIX_SKIP = {"microsoft", "temp", "crashdumps", "packages", "d3dscache", "nvidia", "steam", "programs",
               "connecteddevicesplatform", "comms", "placeholdertilelogofolder", "publishers", "google", "mozilla"}


def dir_size(path):
    total, files = 0, 0
    for f in Path(path).rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
                files += 1
            except OSError:
                pass
    return total, files


def save_sources(game_dir, exe):
    """[(group, base, path)] - dirs to back up; archive paths are group/<rel to base>."""
    g = Path(game_dir)
    p = g / exe
    srcs = []

    def walk(d, depth):
        for c in d.iterdir():
            if not c.is_dir():
                continue
            if SAVE_DIR_RE.match(c.name):
                srcs.append(("game", g, c))
            elif depth < 3:
                walk(c, depth + 1)

    walk(g, 0)
    appid = resolve_appid(p)
    if appid and not is_linux_exe(p):
        user = compatdata_dir(appid) / "pfx" / "drive_c" / "users" / "steamuser"
        for sub in PREFIX_SUBDIRS:
            base = user / sub
            if base.is_dir():
                for c in base.iterdir():
                    if c.is_dir() and c.name.lower() not in PREFIX_SKIP and not c.name.startswith("."):
                        srcs.append(("prefix", user, c))
    if is_linux_exe(p):
        token = norm_title(pretty_name(p, g))[:6]
        for base in (Path.home() / ".renpy", Path.home() / ".config" / "unity3d"):
            if base.is_dir():
                for c in base.rglob("*"):
                    if c.is_dir() and len(c.relative_to(base).parts) <= 2 and token and token in norm_title(c.name):
                        srcs.append(("home", Path.home(), c))
    return srcs, appid


def saves_info(game_dir, exe):
    p = game_exe_path(game_dir, exe)
    srcs, appid = save_sources(Path(game_dir), p.name if p.parent == Path(game_dir).resolve() else str(p.relative_to(Path(game_dir).resolve())))
    out = []
    for group, base, path in srcs:
        size, files = dir_size(path)
        out.append({"group": group, "path": str(path), "size": size, "files": files})
    return {"appid": appid, "sources": out, "total": sum(s["size"] for s in out),
            "prefix": str(compatdata_dir(appid)) if appid else None}


def build_saves_zip(game_dir, exe):
    p = game_exe_path(game_dir, exe)
    g = Path(game_dir).resolve()
    rel = str(p.relative_to(g))
    srcs, appid = save_sources(g, rel)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"saves_{re.sub(r'[^A-Za-z0-9_-]+', '_', g.name)}_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}.zip"
    manifest = {"deckdrop": __version__, "game": g.name, "exe": rel, "appid": appid,
                "created": int(time.time()), "groups": [], "files": 0}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for group, base, path in srcs:
            manifest["groups"].append({"group": group, "path": str(path.relative_to(base))})
            for f in path.rglob("*"):
                if f.is_file():
                    z.write(f, f"{group}/{f.relative_to(base).as_posix()}")
                    manifest["files"] += 1
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    return out, manifest


def import_saves_zip(game_dir, exe, zip_path):
    p = game_exe_path(game_dir, exe)
    g = Path(game_dir).resolve()
    appid = resolve_appid(p)
    targets = {"game": g, "home": Path.home()}
    if appid:
        targets["prefix"] = compatdata_dir(appid) / "pfx" / "drive_c" / "users" / "steamuser"
    backup, _ = build_saves_zip(game_dir, str(p.relative_to(g)))   # safety copy of what is there now
    written, skipped = 0, 0
    with zipfile.ZipFile(zip_path) as z:
        try:
            manifest = json.loads(z.read("manifest.json"))
        except KeyError:
            raise ValueError("это не бэкап сейвов DeckDrop (нет manifest.json)") from None
        for info in z.infolist():
            if info.is_dir() or info.filename == "manifest.json":
                continue
            group, _, rel = info.filename.partition("/")
            base = targets.get(group)
            if not base or not rel:
                skipped += 1
                continue
            dest = (base / rel).resolve()
            if base.resolve() not in dest.parents:
                skipped += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            written += 1
    return {"written": written, "skipped": skipped, "from_game": manifest.get("game"),
            "backup_of_previous": backup.name, "prefix_missing": "prefix" not in targets}

# --------------------------------------------------------------------------- media gallery

_MEDIA = {"at": 0, "items": [], "by_id": {}}
_APP_NAMES = {}


def app_name(appid):
    if appid in _APP_NAMES:
        return _APP_NAMES[appid]
    name = None
    for lib in library_folders():
        acf = lib / "steamapps" / f"appmanifest_{appid}.acf"
        if acf.is_file():
            m = re.search(r'"name"\s+"([^"]*)"', acf.read_text("utf-8", "replace"))
            name = m.group(1) if m else None
            break
    if not name and appid.isdigit():
        shortcuts_index()
        rec = _SC_CACHE["by_appid"].get(int(appid))
        name = rec["name"] if rec else None
    _APP_NAMES[appid] = name or f"app {appid}"
    return _APP_NAMES[appid]


def media_id(path):
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]


def clip_time(name, fallback):
    m = re.search(r"_(\d{8})_(\d{6})", name)
    if m:
        try:
            return int(time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")))
        except ValueError:
            pass
    return int(fallback)


def scan_media(force=False):
    if not force and time.time() - _MEDIA["at"] < 10:
        return _MEDIA["items"]
    items = []
    for ud in userdata_dirs():
        remote = ud / "760" / "remote"
        if remote.is_dir():
            for appdir in remote.iterdir():
                shots = appdir / "screenshots"
                if not shots.is_dir():
                    continue
                game = app_name(appdir.name)
                for f in shots.iterdir():
                    if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                        thumb = shots / "thumbnails" / f.name
                        st = f.stat()
                        items.append({"kind": "image", "path": f, "thumb": thumb if thumb.is_file() else None,
                                      "name": f.name, "game": game, "time": int(st.st_mtime), "size": st.st_size})
        for sub, label in (("clips", "клип"), ("video", "запись")):
            base = ud / "gamerecordings" / sub
            if not base.is_dir():
                continue
            for clip in base.iterdir():
                if not clip.is_dir():
                    continue
                m = re.match(r"(?:clip|bg)_(\d+)_", clip.name)
                game = app_name(m.group(1)) if m else clip.name
                thumb = clip / "thumbnail.jpg"
                size = sum(f.stat().st_size for f in clip.rglob("*.m4s"))
                items.append({"kind": "clip", "path": clip, "thumb": thumb if thumb.is_file() else None,
                              "name": f"{label} {clip.name}", "game": game,
                              "time": clip_time(clip.name, clip.stat().st_mtime), "size": size})
    for d in MEDIA_DIRS:
        if not d.is_dir():
            continue
        for f in d.rglob("*"):
            if len(f.relative_to(d).parts) > 3 or not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext in IMAGE_EXTS or ext in VIDEO_EXTS:
                st = f.stat()
                items.append({"kind": "image" if ext in IMAGE_EXTS else "video", "path": f, "thumb": None,
                              "name": f.name, "game": f.parent.name if f.parent != d else d.name,
                              "time": int(st.st_mtime), "size": st.st_size})
    items.sort(key=lambda i: i["time"], reverse=True)
    for it in items:
        it["id"] = media_id(it["path"])
    _MEDIA.update(at=time.time(), items=items, by_id={i["id"]: i for i in items})
    return items


def media_item(mid):
    it = _MEDIA["by_id"].get(mid)
    if it is None:
        scan_media(force=True)
        it = _MEDIA["by_id"].get(mid)
    if it is None:
        raise FileNotFoundError(mid)
    return it


def _concat(files, out):
    with open(out, "wb") as dst:
        for f in files:
            with open(f, "rb") as src:
                shutil.copyfileobj(src, dst, CHUNK)


def clip_mp4(item):
    """Assemble a Steam recording (fragmented m4s chunks) into one mp4, cached."""
    clip = item["path"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"{item['id']}.mp4"
    if out.is_file() and out.stat().st_mtime >= clip.stat().st_mtime and out.stat().st_size > 0:
        return out
    inits = sorted(clip.rglob("init-stream0.m4s"))
    if not inits:
        raise FileNotFoundError("в клипе нет фрагментов видео")
    vdir = inits[0].parent

    def stream(n):
        init = vdir / f"init-stream{n}.m4s"
        chunks = sorted(vdir.glob(f"chunk-stream{n}-*.m4s"),
                        key=lambda p: int(re.findall(r"(\d+)\.m4s$", p.name)[0]))
        return [init] + chunks if init.is_file() and chunks else None

    video, audio = stream(0), stream(1)
    if not video:
        raise FileNotFoundError("в клипе нет фрагментов видео")
    tmp_v = out.with_suffix(".v.mp4")
    _concat(video, tmp_v)
    if audio and FFMPEG:
        tmp_a = out.with_suffix(".a.mp4")
        _concat(audio, tmp_a)
        res = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(tmp_v), "-i", str(tmp_a),
                              "-c", "copy", "-movflags", "+faststart", str(out)],
                             capture_output=True, text=True, errors="replace", timeout=600)
        tmp_a.unlink(missing_ok=True)
        if res.returncode == 0:
            tmp_v.unlink(missing_ok=True)
            return out
        log(f"ffmpeg mux failed, serving video only: {res.stderr.strip()[-200:]}")
    os.replace(tmp_v, out)
    return out


PLACEHOLDER_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 90">'
                   b'<rect width="160" height="90" fill="#2a475e"/>'
                   b'<polygon points="65,28 65,62 98,45" fill="#66c0f4"/></svg>')


def media_thumb(item):
    """Path to a thumbnail (Steam's, cached ffmpeg frame, or the image itself); None -> placeholder."""
    if item["thumb"]:
        return item["thumb"]
    if item["kind"] == "image":
        return item["path"]
    if FFMPEG:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        th = CACHE_DIR / f"{item['id']}_thumb.jpg"
        if th.is_file():
            return th
        src = clip_mp4(item) if item["kind"] == "clip" else item["path"]
        res = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", "1", "-i", str(src),
                              "-frames:v", "1", "-vf", "scale=320:-2", str(th)],
                             capture_output=True, text=True, errors="replace", timeout=120)
        if res.returncode == 0 and th.is_file():
            return th
    return None


def media_delete(mid):
    item = media_item(mid)
    p = item["path"]
    if item["kind"] == "clip":
        shutil.rmtree(p)
    else:
        p.unlink()
        if item["thumb"] and item["thumb"].is_file():
            item["thumb"].unlink()
    for f in CACHE_DIR.glob(f"{mid}*"):
        try:
            f.unlink()
        except OSError:
            pass
    scan_media(force=True)
    return item["name"]

# --------------------------------------------------------------------------- self update

def self_update(url):
    script = Path(__file__).resolve()
    try:
        with http_get(url, 30) as r:
            data = r.read()
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"не удалось скачать {url}: {e}. Запущена ли раздача на ПК?") from e
    if b"DeckDrop" not in data[:3000]:
        raise RuntimeError("по ссылке лежит не deckdrop.py")
    m = re.search(rb'__version__\s*=\s*"([^"]+)"', data)
    ver = m.group(1).decode() if m else "?"
    m = re.search(rb'DECKDROP_PORT",\s*"(\d+)"', data)
    new_port = int(m.group(1)) if m else PORT
    if data == script.read_bytes():
        return f"уже стоит актуальная версия {ver}", new_port
    new = script.with_suffix(".py.new")
    new.write_bytes(data)
    res = subprocess.run([sys.executable, "-m", "py_compile", str(new)], capture_output=True, text=True)
    if res.returncode != 0:
        new.unlink(missing_ok=True)
        raise RuntimeError("новая версия не компилируется: " + res.stderr.strip()[-300:])
    shutil.copy2(script, script.with_suffix(".py.bak"))
    os.replace(new, script)
    try:
        script.chmod(0o755)
    except OSError:
        pass
    if os.environ.get("DECKDROP_NO_RESTART") != "1":
        threading.Timer(1.0, restart_self).start()
    return f"обновлено {__version__} -> {ver}, перезапускаюсь", new_port


def restart_self():
    log("restarting")
    if os.environ.get("INVOCATION_ID") and shutil.which("systemctl"):
        subprocess.Popen(["systemctl", "--user", "restart", "deckdrop.service"])
        return
    os.execv(sys.executable, [sys.executable] + sys.argv)

# --------------------------------------------------------------------------- files inside a game (patches)

UPLOAD_TMP = ".deckdrop-upload-"


def game_folder(game_dir):
    """The folder of a game DeckDrop lists: one in a DeckDrop root, or one imported by hand."""
    if not (game_dir or "").strip():
        raise ValueError("это не папка игры")
    g = Path(game_dir).resolve()
    if not g.is_dir() or not any(g == Path(d).resolve() for d, _ in game_dirs()):
        raise ValueError("это не папка игры")
    return g


def game_target_dir(game_dir, exe, rel):
    """A folder inside the game, given relative to the executable's folder. Empty means next to the exe."""
    game = game_folder(game_dir)
    exe_path = game_exe_path(str(game), exe)
    if not inside(game, exe_path):
        raise ValueError("этот exe не из папки игры")
    rel = (rel or "").strip().replace("\\", "/")
    if rel.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", rel):
        raise ValueError("путь нужен относительный, от папки с exe: например data/patch или ../")
    target = (exe_path.parent / rel).resolve() if rel else exe_path.parent
    if not inside(game, target):
        raise ValueError("путь выходит за пределы папки игры")
    if target.exists() and not target.is_dir():
        raise ValueError(f"«{target.name}» это файл, а не папка")
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
        raise ValueError("у файла нет имени")
    fname = safe_name(name)
    dest = target / fname
    if dest.is_dir():
        raise ValueError(f"«{fname}» здесь уже папка, файл с таким именем не положить")
    existed = dest.exists()
    if existed and not replace:
        raise FileExistsError(fname)
    created = not target.exists()
    target.mkdir(parents=True, exist_ok=True)
    if not inside(game, target):
        raise ValueError("путь выходит за пределы папки игры")
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

# --------------------------------------------------------------------------- archives unpacked over a game (patches)

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
        raise ValueError("архив уже убран: загрузи его ещё раз")
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
                    "error": "неверный пароль" if password else None}
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
        raise ValueError("это не архив: подойдут zip, 7z, rar и tar")
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
        raise ValueError("введи пароль")
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
        raise ValueError("архив ещё не распакован: сначала нужен пароль")
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

# --------------------------------------------------------------------------- http

def local_urls():
    urls = [f"http://{socket.gethostname()}.local:{PORT}"]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        urls.append(f"http://{s.getsockname()[0]}:{PORT}")
        s.close()
    except OSError:
        pass
    return urls


def public_settings():
    """Settings for the page: protected values are replaced by a masked preview."""
    out = {k: STATE.get(k) for k in SETTING_KEYS if k not in PROTECTED_KEYS}
    out["proxy_masked"] = mask_proxy(STATE.get("proxy"))
    out["proxy_set"] = bool((STATE.get("proxy") or "").strip())
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        line = args[0] if args else ""
        if "/api/state" not in line and "/thumb" not in line:
            log(f"{self.address_string()} {fmt % args}")

    # ---- helpers
    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def read_body_to(self, path, job=None):
        total = int(self.headers.get("Content-Length") or 0)
        remaining = total
        with open(path, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(CHUNK, remaining))
                if not chunk:
                    raise ConnectionError("загрузка прервана")
                f.write(chunk)
                remaining -= len(chunk)
                if job:
                    job.done += len(chunk)
        return total

    def query(self):
        u = urllib.parse.urlparse(self.path)
        return u.path, urllib.parse.parse_qs(u.query)

    def q1(self, q, key, default=""):
        return q.get(key, [default])[0]

    def media_auth(self, q):
        token = self.headers.get("X-Media-Token") or self.q1(q, "t")
        if not media_token_ok(token):
            self.send_json({"error": "нужен пароль медиа", "auth": True}, 401)
            return False
        return True

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def send_file(self, path, download_name=None):
        path = Path(path)
        size = path.stat().st_size
        ctype = MIME.get(path.suffix.lower(), "application/octet-stream")
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("Connection", "close")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(download_name))
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    # ---- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, q = self.query()
        try:
            if path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/state":
                with LOCK:
                    jobs = [j.to_dict() for j in JOBS.values()]
                self.send_json({"jobs": jobs[::-1], "games": list_games(), "urls": local_urls(),
                                "disks": disks(), "games_dir": str(GAMES_DIR),
                                "version": __version__, "ffmpeg": bool(FFMPEG),
                                "media_set": bool(STATE.get("media_pw")),
                                "settings": public_settings(), "compat_tools": compat_tools(),
                                "cdp": CDP.status(), "pending": len(STATE.get("pending") or [])})
            elif path == "/add":  # GET /add?url=... for share shortcuts / bookmarklets
                start_download(self.q1(q, "url"), self.q1(q, "disk") or None)
                self.redirect("/")
            elif path == "/api/game/import/scan":
                self.send_json({"candidates": import_candidates()})
            elif path == "/api/game/info":
                self.send_json(game_info(self.q1(q, "path")))
            elif path == "/api/game/dir":
                self.send_json(game_dir_list(self.q1(q, "game"), self.q1(q, "exe"), self.q1(q, "dir")))
            elif path == "/api/art/current":
                self.send_json(art_current(self.q1(q, "game"), self.q1(q, "exe")))
            elif path.startswith("/art/"):
                parts = path.split("/")
                f = art_file_for(int(parts[2]), parts[3]) if len(parts) > 3 and parts[2].isdigit() else None
                if not f:
                    raise FileNotFoundError("обложки нет")
                self.send_file(f)
            elif path == "/api/inbox/stats":
                self.send_json(inbox_clear(dry_run=True))
            elif path == "/api/archives":
                self.send_json({"archives": list_archives()})
            elif path == "/api/vndb/test":
                self.send_json(proxy_test())
            elif path == "/api/vndb/images":
                self.send_json(vndb_image_list(self.q1(q, "vn")))
            elif path == "/api/vndb/search":
                self.send_json({"results": vndb_search(self.q1(q, "q"))})
            elif path == "/api/saves/info":
                self.send_json(saves_info(self.q1(q, "game"), self.q1(q, "exe")))
            elif path == "/api/saves/backup":
                out, manifest = build_saves_zip(self.q1(q, "game"), self.q1(q, "exe"))
                if not manifest["files"]:
                    out.unlink(missing_ok=True)
                    return self.send_json({"error": "сейвы не найдены: игра ещё не запускалась или хранит их в другом месте"}, 404)
                self.send_file(out, download_name=out.name)
            elif path == "/api/media/status":
                self.send_json({"set": bool(STATE.get("media_pw"))})
            elif path == "/api/media/list":
                if not self.media_auth(q):
                    return
                items = scan_media(force="refresh" in q)
                self.send_json({"items": [{k: v for k, v in it.items() if k not in ("path", "thumb")}
                                          for it in items], "ffmpeg": bool(FFMPEG)})
            elif path.startswith("/media/"):
                if not self.media_auth(q):
                    return
                parts = path.split("/")
                item = media_item(parts[2])
                if len(parts) > 3 and parts[3] == "thumb":
                    th = media_thumb(item)
                    if th:
                        self.send_file(th)
                    else:
                        self._send(200, PLACEHOLDER_SVG, "image/svg+xml")
                else:
                    f = clip_mp4(item) if item["kind"] == "clip" else item["path"]
                    name = (item["path"].name + ".mp4") if item["kind"] == "clip" else item["path"].name
                    self.send_file(f, download_name=name if "dl" in q else None)
            else:
                self._send(404, b"not found", "text/plain")
        except FileNotFoundError as e:
            self.send_json({"error": f"не найдено: {e}"}, 404)
        except (ValueError, PermissionError) as e:
            self.send_json({"error": str(e)}, 400)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # noqa: BLE001
            log(f"GET {path} failed: {e!r}")
            try:
                self.send_json({"error": str(e)}, 500)
            except OSError:
                pass

    # ---- POST
    def do_POST(self):
        path, _ = self.query()
        try:
            body = self.read_json()
            s = lambda k, d="": str(body.get(k, d) or d)  # noqa: E731
            if path == "/api/download":
                self.send_json(start_download(s("url"), s("disk") or None).to_dict())
            elif path == "/api/mega/list":
                self.send_json({"ok": True} | mega_probe(s("url")))
            elif path == "/api/mega/download":
                self.send_json({"ok": True, "jobs": start_mega_downloads(
                    s("url"), body.get("nodes") or [], s("disk") or None)})
            elif path == "/api/game/archive/unlock":
                self.send_json({"ok": True} | patch_unlock(s("token"), str(body.get("password") or ""),
                                                           bool(body.get("remember"))))
            elif path == "/api/game/archive/apply":
                self.send_json({"ok": True} | patch_apply(s("token"), bool(body.get("strip")),
                                                          body.get("backup", True) is not False))
            elif path == "/api/game/archive/discard":
                _patch_drop(s("token"))
                self.send_json({"ok": True})
            elif path == "/api/cancel":
                self.send_json({"ok": cancel_job(int(body.get("id", 0) or 0))})
            elif path == "/api/cancel_all":
                self.send_json({"ok": True, "stopped": cancel_all()})
            elif path == "/api/inbox/clear":
                self.send_json({"ok": True} | inbox_clear())
            elif path == "/api/clear":
                with LOCK:
                    for k in [k for k, j in JOBS.items() if j.status in ("done", "error", "cancelled")]:
                        del JOBS[k]
                self.send_json({"ok": True})
            elif path == "/api/job/password":
                job = job_password(int(body.get("id", 0)), s("password"), bool(body.get("remember")))
                self.send_json(job.to_dict())
            elif path == "/api/add_to_steam":
                tool = body.get("tool")
                self.send_json({"ok": True, "note": add_to_steam(s("game"), s("exe"), s("name") or None,
                                                                  None if tool is None else str(tool))})
            elif path == "/api/art":
                p = game_exe_path(s("game"), s("exe"))
                source = s("source", "auto")
                if source not in ("auto", "icon", "vndb"):
                    raise ValueError("source")
                threading.Thread(target=art_worker, args=(str(p), True, False, source, s("vn") or None), daemon=True).start()
                self.send_json({"ok": True, "note": "делаю обложки"})
            elif path == "/api/art/from_exe":
                self.send_json({"ok": True} | art_from_exe(s("game"), s("exe"), s("slot")))
            elif path == "/api/art/from_url":
                self.send_json({"ok": True} | art_from_url(s("game"), s("exe"), s("slot"), s("url"), s("vn") or None))
            elif path == "/api/game/rename":
                self.send_json({"ok": True, "note": rename_shortcut(s("game"), s("exe"), s("name"))})
            elif path == "/api/game/compat":
                self.send_json({"ok": True, "note": set_compat_for(s("game"), s("exe"), s("tool"))})
            elif path == "/api/game/import":
                self.send_json({"ok": True} | import_game(s("path")))
            elif path == "/api/game/unimport":
                self.send_json({"ok": True, "removed": unimport_game(s("path"))})
            elif path == "/api/game/hide":
                set_hidden(s("path"), bool(body.get("hidden", True)))
                self.send_json({"ok": True})
            elif path == "/api/game/delete":
                check_pin(body.get("pin"))
                removed, notes = delete_game(s("path"), bool(body.get("remove_shortcut")))
                self.send_json({"ok": True, "removed": removed, "notes": notes})
            elif path == "/api/archive/extract":
                self.send_json(archive_extract_job(s("path")).to_dict())
            elif path == "/api/archive/delete":
                self.send_json({"ok": True, "removed": [archive_delete(s("path"))]})
            elif path == "/api/archive/cleanup":
                self.send_json({"ok": True, "removed": archive_cleanup()})
            elif path == "/api/media/setup":
                if STATE.get("media_pw"):
                    raise PermissionError("пароль уже задан")
                pw = s("password")
                if len(pw) < 4:
                    raise ValueError("пароль короче 4 символов")
                set_state(media_pw=pw_hash(pw))
                self.send_json({"ok": True, "token": media_login(pw)})
            elif path == "/api/media/login":
                self.send_json({"ok": True, "token": media_login(s("password"))})
            elif path == "/api/media/reset":
                check_pin(body.get("pin"))
                set_state(media_pw=None)
                MEDIA_TOKENS.clear()
                self.send_json({"ok": True})
            elif path == "/api/media/delete":
                if not media_token_ok(self.headers.get("X-Media-Token") or s("token")):
                    return self.send_json({"error": "нужен пароль медиа", "auth": True}, 401)
                check_pin(body.get("pin"))
                self.send_json({"ok": True, "removed": media_delete(s("id"))})
            elif path == "/api/settings":
                if any(k in body for k in PROTECTED_KEYS):
                    check_pin(body.get("pin"))
                changes = {}
                for k in SETTING_KEYS:
                    if k not in body:
                        continue
                    v = body[k]
                    if k in ("prefer_linux", "vndb_auto", "vndb_nsfw", "cef_enabled",
                             "proxy_downloads", "mega_verify"):
                        v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "on", "yes")
                    elif k == "archive_passwords":
                        v = [str(x).strip() for x in (v if isinstance(v, list) else str(v).splitlines()) if str(x).strip()]
                    else:
                        v = str(v or "").strip()
                    changes[k] = v
                if changes:
                    set_state(**changes)
                    if "cef_enabled" in changes:
                        CDP.ensure_marker()
                        CDP.target(force=True)
                self.send_json({"ok": True, "settings": public_settings(), "cdp": CDP.status()})
            elif path == "/api/settings/reveal":
                check_pin(body.get("pin"))
                self.send_json({"ok": True, "proxy": STATE.get("proxy") or ""})
            elif path == "/api/settings/pin":
                check_pin(body.get("old"))
                new = s("new")
                if not re.fullmatch(r"\d{4,8}", new):
                    raise ValueError("PIN: от 4 до 8 цифр")
                set_state(admin_pin=new)
                self.send_json({"ok": True})
            elif path == "/api/update":
                check_pin(body.get("pin"))
                url = (s("url") or STATE.get("update_url") or UPDATE_URL_DEFAULT).strip()
                if not re.match(r"^https?://", url):
                    raise ValueError("нужна ссылка вида http(s)://")
                if url != STATE.get("update_url"):
                    set_state(update_url=url)
                note, new_port = self_update(url)
                self.send_json({"ok": True, "note": note, "new_port": new_port})
            else:
                self.send_json({"error": "not found"}, 404)
        except PermissionError as e:
            self.send_json({"error": str(e)}, 403)
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": str(e)}, 400)

    # ---- PUT (uploads)
    def do_PUT(self):
        path, q = self.query()
        try:
            if path.startswith("/api/upload/"):
                name = safe_name(path[len("/api/upload/"):])
                job = new_job("upload", name, root_for(self.q1(q, "disk")) if self.q1(q, "disk") else None)
                job.total = int(self.headers.get("Content-Length") or 0)
                job.status = "uploading"
                dest, part = reserve_path(job.root / "_inbox", name)
                job.work = str(part)
                try:
                    self.read_body_to(part, job)
                    part.rename(dest)
                    job.file = str(dest)
                except Exception as e:  # noqa: BLE001
                    part.unlink(missing_ok=True)
                    fail(job, str(e))
                    return self.send_json({"error": str(e)}, 400)
                threading.Thread(target=finish, args=(job, dest), daemon=True).start()
                self.send_json(job.to_dict())
            elif path == "/api/art/upload":
                n = int(self.headers.get("Content-Length") or 0)
                if n > 25 << 20:
                    raise ValueError("файл больше 25 МБ")
                data = self.rfile.read(n)
                self.send_json({"ok": True} | custom_art(self.q1(q, "game"), self.q1(q, "exe"), self.q1(q, "slot"), data))
            elif path == "/api/game/archive":
                self.send_json({"ok": True} | patch_upload(
                    self.q1(q, "game"), self.q1(q, "exe"), self.q1(q, "dir"), self.q1(q, "name"), self.read_body_to))
            elif path == "/api/game/file":
                self.send_json({"ok": True} | game_file_upload(
                    self.q1(q, "game"), self.q1(q, "exe"), self.q1(q, "dir"), self.q1(q, "name"),
                    self.q1(q, "replace") == "1", self.read_body_to))
            elif path == "/api/saves/import":
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = CACHE_DIR / f"import_{secrets.token_hex(4)}.zip"
                try:
                    self.read_body_to(tmp)
                    self.send_json({"ok": True} | import_saves_zip(self.q1(q, "game"), self.q1(q, "exe"), tmp))
                finally:
                    tmp.unlink(missing_ok=True)
            else:
                self.send_json({"error": "not found"}, 404)
        except FileExistsError as e:
            self.close_connection = True          # the body was not read: don't reuse the connection
            self.send_json({"error": f"«{e}» уже есть в этой папке", "exists": True}, 409)
        except (ValueError, zipfile.BadZipFile) as e:
            self.close_connection = True
            self.send_json({"error": str(e) or "битый zip"}, 400)
        except Exception as e:  # noqa: BLE001
            self.close_connection = True
            log(f"PUT {path} failed: {e!r}")
            try:
                self.send_json({"error": str(e)}, 500)
            except OSError:
                pass                              # the client is already gone, e.g. the phone lost Wi-Fi


PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#0e1621"><title>DeckDrop</title>
<style>
:root{--bg:#0e1621;--panel:#16212e;--panel2:#1c2937;--line:#27394d;--text:#e8eef5;--muted:#8fa1b7;--accent:#4cc2ff;--accent2:#7c6cff;--ok:#3ddc97;--warn:#ffcf70;--danger:#ff6b7a;--r:16px}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;
 background-image:radial-gradient(900px 500px at 10% -10%,#1b3350 0%,transparent 60%),radial-gradient(800px 500px at 110% 10%,#2a1f4d 0%,transparent 55%);background-attachment:fixed}
a{color:var(--accent);text-decoration:none}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92em;color:#c9d7e6;overflow-wrap:anywhere}
.wrap{max-width:820px;margin:0 auto;padding:0 16px 96px}
header{position:sticky;top:0;z-index:5;transition:transform .25s ease;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);background:#0e1621b8;border-bottom:1px solid #ffffff10}
header.hide{transform:translateY(-100%)}
.hin{max-width:820px;margin:0 auto;padding:12px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.logo{width:36px;height:36px;border-radius:11px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:grid;place-items:center;font-weight:800;color:#0b1220;box-shadow:0 6px 18px #4cc2ff44}
.brand{font-weight:700;font-size:1.15em;letter-spacing:.2px}
.chip{font-size:.78em;color:var(--muted);background:#ffffff0d;border:1px solid #ffffff12;border-radius:999px;padding:4px 10px;white-space:nowrap}
.chip.click{cursor:pointer}.chip.click:hover{color:var(--text);border-color:#ffffff30}
.seg{margin-left:auto;display:flex;background:#ffffff0c;border:1px solid #ffffff12;border-radius:12px;padding:3px}
.seg button{background:transparent;color:var(--muted);padding:8px 14px;border-radius:9px;font-weight:600;box-shadow:none}
.seg button.on{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#0b1220;box-shadow:0 4px 14px #4cc2ff33}
button{font:inherit;font-size:.95em;padding:10px 16px;border:0;border-radius:12px;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#0b1220;cursor:pointer;font-weight:600;transition:transform .08s,filter .15s,background .15s;white-space:nowrap}
button:hover{filter:brightness(1.08)}button:active{transform:translateY(1px) scale(.99)}
button:disabled{opacity:.5;cursor:default;filter:none}
button.ghost{background:#ffffff0c;color:var(--text);border:1px solid #ffffff14;font-weight:500}
button.ghost:hover{background:#ffffff16}
button.sm{padding:7px 12px;font-size:.85em;border-radius:10px}
button.icon{padding:8px 10px;font-size:1em;line-height:1}
button.danger{background:#ff6b7a1a;color:var(--danger);border:1px solid #ff6b7a33;font-weight:500}
button.danger:hover{background:#ff6b7a2e}
input[type=text],input[type=password],input[type=search],select,textarea{font:inherit;font-size:1em;padding:12px 14px;border-radius:12px;border:1px solid var(--line);background:#0b1320;color:var(--text);min-width:0;flex:1;outline:none;transition:border-color .15s,box-shadow .15s}
input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px #4cc2ff22}
select{cursor:pointer}
select.sel{padding:6px 10px;font-size:.85em;border-radius:10px;flex:0 1 auto;max-width:240px}
.card{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:var(--r);padding:16px;margin:14px 0;box-shadow:0 12px 32px #00000055;overflow-wrap:anywhere}
.card h2{margin:0 0 12px;font-size:.8em;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.1em;display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.row{display:flex;gap:10px;align-items:center}
.row.wrap{flex-wrap:wrap}
.drop{margin-top:12px;padding:22px;border:1.5px dashed var(--line);border-radius:14px;text-align:center;color:var(--muted);transition:all .15s;background:#ffffff05;font-size:.92em;line-height:1.6}
.drop.over{border-color:var(--accent);background:#4cc2ff12;color:var(--text)}
.drop .ic{font-size:1.7em;line-height:1;margin-bottom:6px;color:var(--accent)}
.drop label{color:var(--accent);cursor:pointer;font-weight:600}
.drop input{display:none}
.item{background:#ffffff07;border:1px solid #ffffff0c;border-radius:12px;padding:12px 14px;margin:10px 0;word-break:break-word}
.item.hid{opacity:.5}
.top{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.top b{font-size:1.02em}
.acts{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.path{color:var(--muted);font-size:.76em;margin-top:2px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;word-break:break-all}
.bar{height:6px;background:#ffffff10;border-radius:999px;overflow:hidden;margin:10px 0 8px}
.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:999px;transition:width .4s}
.st{font-size:.88em}.err{color:var(--danger)}.ok{color:var(--ok)}.busy{color:var(--warn)}
.muted{color:var(--muted)}.small{font-size:.85em}
.exe{padding:8px 0;border-top:1px solid #ffffff0a}
.exeh{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.exeh .n{flex:1;min-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.86em;color:var(--muted)}
.exed{margin-top:8px;display:flex;flex-direction:column;gap:8px}
.exed .line{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:.88em}
.exed .lbl{color:var(--muted);min-width:64px}
.gname{font-weight:700;font-size:1.02em}
.pill{font-size:.78em;padding:4px 10px;border-radius:999px;background:#3ddc9718;color:var(--ok);border:1px solid #3ddc9730;white-space:nowrap}
.pill.warn{background:#ffcf7018;color:var(--warn);border-color:#ffcf7030}
.pill.k{background:#ffffff0c;color:var(--muted);border-color:#ffffff14}
.pill.acc{background:#4cc2ff18;color:var(--accent);border-color:#4cc2ff30}
.pill.err{background:#ff6b7a18;color:var(--danger);border-color:#ff6b7a30}
.empty{color:var(--muted);text-align:center;padding:18px 0;font-size:.92em}
.chips{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.chips button{padding:7px 14px;font-size:.85em;background:#ffffff0c;color:var(--muted);border:1px solid #ffffff12;font-weight:500}
.chips button.on{background:#4cc2ff22;color:var(--accent);border-color:#4cc2ff55}
.chips input{padding:8px 12px;font-size:.9em;flex:1;min-width:140px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(138px,1fr));gap:10px}
.tile{position:relative;border-radius:14px;overflow:hidden;cursor:pointer;background:#0b1320;border:1px solid #ffffff0c;transition:transform .15s,border-color .15s}
.tile:hover{transform:translateY(-2px);border-color:#ffffff30}
.tile img{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:#0b1320}
.tile .cap{padding:8px 10px;font-size:.78em;line-height:1.35}
.tile .cap b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.tile .k{position:absolute;top:8px;left:8px;background:#0b1320cc;backdrop-filter:blur(6px);padding:3px 8px;border-radius:999px;font-size:.72em;color:#dfe8f2}
.tile .k.v{background:#7c6cffcc}
#viewer{position:fixed;inset:0;background:#05090fe6;backdrop-filter:blur(10px);display:none;flex-direction:column;z-index:20}
#viewer.on{display:flex}
.vbar{display:flex;gap:8px;padding:12px 14px;align-items:center;flex-wrap:wrap}
.vbar .t{flex:1;font-size:.92em;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:120px}
.vbody{flex:1;display:flex;align-items:center;justify-content:center;min-height:0;padding:0 12px 14px}
.vbody img,.vbody video{max-width:100%;max-height:100%;object-fit:contain;border-radius:12px;box-shadow:0 20px 60px #000a}
.auth{max-width:420px;margin:30px auto;text-align:center}
.auth .lock{font-size:2.2em;margin-bottom:6px}
.auth p{color:var(--muted);font-size:.9em;margin:6px 0 16px;line-height:1.5}
.auth .row{margin-top:10px}
footer{margin-top:26px;padding-top:14px;border-top:1px solid #ffffff10;display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;font-size:.85em;color:var(--muted)}
#toasts{position:fixed;left:0;right:0;bottom:18px;display:flex;flex-direction:column;align-items:center;gap:8px;pointer-events:none;z-index:50}
.toast{background:#1c2937f2;border:1px solid #ffffff1a;color:var(--text);padding:10px 16px;border-radius:12px;box-shadow:0 10px 30px #0008;font-size:.92em;max-width:90vw;animation:up .2s ease-out;transition:opacity .3s}
.toast.err{border-color:#ff6b7a66}.toast.ok{border-color:#3ddc9766}
@keyframes up{from{transform:translateY(12px);opacity:0}to{transform:none;opacity:1}}
#modal{position:fixed;inset:0;background:#05090fb0;backdrop-filter:blur(6px);display:none;align-items:center;justify-content:center;z-index:40;padding:16px;overflow:auto}
#modal.on{display:flex}
.mbox{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:18px;padding:20px;width:min(520px,100%);box-shadow:0 30px 80px #000a;max-height:92vh;overflow:auto}
.mbox h3{margin:0 0 6px;font-size:1.05em}.mbox p{margin:0 0 6px;color:var(--muted);font-size:.9em;line-height:1.5}
.mbox label{display:block;font-size:.78em;color:var(--muted);margin:12px 0 4px}
.mbox label.chk{display:flex;align-items:center;gap:10px;font-size:.92em;color:var(--text);margin:10px 0;cursor:pointer}
.mbox label.chk input{width:18px;height:18px;accent-color:var(--accent)}
.mbox input[type=text],.mbox input[type=password],.mbox select,.mbox textarea{width:100%}
.mbox .btns{display:flex;gap:8px;justify-content:flex-end;margin-top:18px;flex-wrap:wrap}
.vn{display:flex;gap:10px;align-items:center;padding:8px;border-radius:12px;border:1px solid #ffffff0c;background:#ffffff06;cursor:pointer;margin-top:8px}
.vn:hover{border-color:#4cc2ff66}
.vn img{width:54px;height:76px;object-fit:cover;border-radius:8px;background:#0b1320;flex:none}
.vn .ph{width:54px;height:76px;border-radius:8px;background:#0b1320;flex:none;display:grid;place-items:center;color:var(--muted);font-size:.7em}
.vn b{display:block}.vn small{color:var(--muted)}
.mlist{max-height:46vh;overflow:auto;margin:6px -4px 0;padding:0 4px}
.mlist .vn input{flex:none;width:18px;height:18px}
label.chk{display:flex;align-items:center;gap:10px;font-size:.92em;color:var(--text);margin:10px 0;cursor:pointer;line-height:1.4}
label.chk input{width:18px;height:18px;accent-color:var(--accent);flex:none}
.frow{margin:12px 0}.frow>label{display:block;font-size:.78em;color:var(--muted);margin-bottom:5px}
.frow select,.frow input[type=text]{width:100%}
.gcard{cursor:pointer;transition:border-color .15s,transform .1s}.gcard:hover{border-color:#ffffff30}.gcard:active{transform:scale(.995)}
.chev{color:var(--muted);font-size:1.4em;line-height:1;margin-left:2px}
.gtitle{font-weight:800;font-size:1.3em;display:flex;align-items:center;gap:8px;flex-wrap:wrap;word-break:break-word}
.covers{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-top:10px}
.cov{background:#0b1320;border:1px solid #ffffff0c;border-radius:12px;padding:8px;text-align:center}
.cov .im{width:100%;aspect-ratio:var(--ar,2/3);display:grid;place-items:center;border-radius:8px;overflow:hidden;background:repeating-conic-gradient(#ffffff0a 0 25%,transparent 0 50%) 0 0/16px 16px #0b1320}
.cov img{max-width:100%;max-height:100%;object-fit:contain;display:block}
.cov b{display:block;font-size:.8em;margin-top:6px}.cov small{display:block;color:var(--muted);font-size:.72em;margin-bottom:6px}
.vimgs{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px;margin-top:8px}
.vimg{cursor:pointer;border:1px solid #ffffff0c;border-radius:10px;overflow:hidden;background:#0b1320}.vimg:hover{border-color:#4cc2ff66}
.vimg img{width:100%;aspect-ratio:4/3;object-fit:cover;display:block}.vimg small{display:block;padding:4px 6px;color:var(--muted);font-size:.7em}
.chipsel{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 4px}
.chipsel button{padding:6px 10px;font-size:.85em;background:#ffffff0c;color:var(--text);border:1px solid #ffffff14;font-weight:500}
.chipsel button:hover{border-color:#4cc2ff66}
.arch{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 0;border-top:1px solid #ffffff0a}
.arch .nm{flex:1;min-width:160px;word-break:break-all}
.hint{font-size:.82em;color:var(--muted);line-height:1.5;overflow-wrap:anywhere}
.list{margin:6px 0;padding-left:18px}.list li{margin:3px 0;overflow-wrap:anywhere}
@media (max-width:560px){.seg{margin-left:0;width:100%}.seg button{flex:1;padding:8px 4px;font-size:.85em}.hin{gap:8px}.chip.addr{display:none}.wrap{padding:0 12px 96px}.card{padding:14px}.exed .lbl{min-width:100%}}
</style></head><body>
<header><div class="hin"><div class="logo">D</div><span class="brand">DeckDrop</span><span class="chip" id="ver"></span><span class="chip click addr" id="addr" title="скопировать адрес"></span>
<button class="ghost icon" id="settings" title="настройки">⚙</button>
<div class="seg"><button id="tabGames" class="on">Игры</button><button id="tabArch">Архивы</button><button id="tabMedia">Медиа</button><button id="tabSettings">Настройки</button></div></div></header>
<div class="wrap">
<div id="pgGames">
<div id="gameList">
 <div class="card"><h2>Добавить <select class="sel" id="disk" title="куда скачивать"></select></h2>
  <div class="row"><input id="url" type="text" placeholder="Ссылка на файл: прямая, Mega, Яндекс.Диск, Google Drive" autocomplete="off"><button id="go">Скачать</button></div>
  <div class="drop" id="drop"><div class="ic">⤓</div>Перетащи сюда файл или ссылку, либо просто Ctrl+V<br><label>выбрать файлы<input id="file" type="file" multiple></label></div>
 </div>
 <div class="card"><h2>Задания <span class="acts"><button class="danger sm" id="stopAll" hidden>остановить всё</button><button class="ghost sm" id="clear">очистить</button></span></h2><div id="jobs"></div></div>
 <div class="card"><h2>Игры <span class="acts"><button class="ghost sm" id="importGame">+ своя игра</button><button class="ghost sm" id="toggleHidden"></button></span></h2><div id="games"></div></div>
</div>
<div id="gamePage" hidden>
 <div class="row" style="margin-top:14px"><button class="ghost sm" id="gpBack">← к списку</button></div>
 <div class="card" id="gpHead"></div>
 <div class="card"><h2>Запуск</h2><div class="hint" style="margin-bottom:4px">Файл, добавленный в Steam, отмечен галочкой. Proton меняется прямо здесь.</div><div id="gpExes"></div></div>
 <div class="card" id="gpCoversCard" hidden><h2>Обложки <span class="acts"><button class="ghost sm" id="gpVndb">VNDB…</button><button class="ghost sm" id="gpIcon">из иконки exe</button></span></h2><div class="hint" id="gpCvHint"></div><div class="covers" id="gpCovers"></div></div>
 <div class="card" id="gpSavesCard" hidden><h2>Сейвы <span class="acts"><a id="gpSvDl"><button class="ghost sm" id="gpSvDlB">скачать бэкап</button></a><button class="ghost sm" id="gpSvImp">импортировать zip</button></span></h2><div class="hint" id="gpSaves"></div></div>
 <div class="card" id="gpFilesCard" hidden><h2>Файлы игры <span class="acts"><button class="ghost sm" id="gpFArch">распаковать архив…</button><button class="ghost sm" id="gpFUp">загрузить…</button></span></h2>
  <div class="hint">Положить патч или любой другой файл в папку игры. Путь считается от папки с exe: пусто — рядом с exe, <code>data/patch</code> — в подпапку, <code>../</code> — на уровень выше, но не за пределы игры. Если такой файл уже есть, DeckDrop спросит, а самый первый вариант сохранит рядом как <code>.bak</code>. Архив с патчем можно распаковать сюда же: DeckDrop сначала покажет, что он поменяет.</div>
  <div class="frow" id="gpFExeRow" hidden><label for="gpFExe">От какого exe</label><select id="gpFExe"></select></div>
  <div class="frow"><label for="gpFDir">Путь от exe</label><input id="gpFDir" type="text" placeholder="пусто — рядом с exe" autocomplete="off" autocapitalize="off" spellcheck="false"></div>
  <div class="hint" id="gpFWhere"></div><div id="gpFList"></div>
 </div>
 <div class="card"><h2>Действия</h2><div class="acts" id="gpActs"></div></div>
 <div class="card"><h2>Сведения</h2><div class="hint" id="gpInfo"></div></div>
</div>
</div>
<div id="pgArch" hidden>
 <div class="card"><h2>Скачанные архивы <span class="acts"><span class="chip" id="archTotal"></span><button class="ghost sm" id="archCleanup">удалить распакованные</button><button class="danger sm" id="inboxClear">очистить всё</button></span></h2>
 <div class="hint" style="margin-bottom:6px">Архивы лежат в папке <code>_inbox</code> на каждом диске и после распаковки больше не нужны. Распакованные можно удалить одним нажатием, нераспакованные распаковать отсюда.</div>
 <div id="archives"></div></div>
</div>
<div id="pgMedia" hidden>
 <div id="mediaAuth"></div>
 <div id="mediaBody" hidden>
  <div class="card"><div class="chips"><button data-f="all" class="on">Все</button><button data-f="image">Скриншоты</button><button data-f="video">Видео</button>
   <input id="mq" type="search" placeholder="поиск по игре"><button class="ghost sm" id="mrefresh" title="пересканировать">⟳</button></div>
   <div class="muted" id="mhint" style="font-size:.85em;margin-bottom:10px"></div>
   <div class="grid" id="mgrid"></div></div>
 </div>
</div>
<div id="pgSettings" hidden>
 <div class="card"><h2>Steam</h2>
  <div class="hint" id="sCdpTxt" style="margin-bottom:6px"></div>
  <div class="frow"><label for="sCompat">Proton по умолчанию для всех новых игр</label><select id="sCompat" data-set="default_compat"></select>
   <div class="hint" style="margin-top:6px">Применяется к играм, которые добавляются после смены. У уже добавленных Proton меняется в карточке игры.</div></div>
  <label class="chk"><input type="checkbox" id="sLinux" data-set="prefer_linux">Предпочитать Linux-сборку, если она есть в архиве: идёт нативно, без Proton</label>
  <label class="chk"><input type="checkbox" id="sCef" data-set="cef_enabled">Управлять Steam через отладочный порт, как Decky</label>
 </div>
 <div class="card"><h2>Обложки</h2>
  <label class="chk"><input type="checkbox" id="sVndb" data-set="vndb_auto">Искать обложки на VNDB автоматически (экспериментально)</label>
  <label class="chk"><input type="checkbox" id="sSafe" data-set="vndb_nsfw" data-invert="1">Пропускать картинки 18+ с VNDB</label>
 </div>
 <div class="card"><h2>Сеть</h2>
  <div class="hint">Прокси используется <b>только самим DeckDrop</b>, остальной дек ходит в интернет как обычно.
   Нужен, когда провайдер рвёт соединение с VNDB. Формат: <code>socks5://хост:порт</code> или <code>http://хост:порт</code>,
   можно с логином: <code>socks5://user:pass@хост:порт</code>. Если на домашнем ПК уже стоит VPN-клиент с локальным
   входом SOCKS5, подойдёт адрес этого ПК, например <code>socks5://192.168.1.10:10808</code> (в клиенте надо разрешить
   подключения из локальной сети).</div>
  <div class="frow"><label>Прокси для запросов DeckDrop</label><div class="row wrap"><code id="sProxyView" style="flex:1;min-width:160px;word-break:break-all"></code><button class="ghost sm" id="sProxyEdit">изменить 🔒</button></div><div class="hint" style="margin-top:6px">Логин и пароль прокси не показываются и не уходят на страницу без PIN.</div></div>
  <label class="chk"><input type="checkbox" id="sProxyDl" data-set="proxy_downloads">Через прокси качать и сами игры (медленнее, но обходит блокировки файлохостингов)</label>
  <div class="row"><button class="ghost sm" id="sTest">Проверить связь с VNDB</button></div>
  <div class="hint" id="sTestRes"></div>
 </div>
 <div class="card"><h2>Mega</h2>
  <div class="hint">Ссылки <code>mega.nz</code> работают как обычные. Mega шифрует файлы у себя в браузере,
   ключ лежит в самой ссылке после решётки — копируй её целиком, иначе расшифровать нечем. Файл приходит
   зашифрованным и расшифровывается прямо на деке. Ссылка на папку откроет список: отметь, что качать, и отмеченное скачается одной игрой со всеми папками.
   Файлы качаются по одному — Mega не любит несколько соединений с одного адреса.</div>
  <label class="chk"><input type="checkbox" id="sMega" data-set="mega_verify">Проверять контрольную сумму Mega после скачивания</label>
 </div>
 <div class="card"><h2>Архивы</h2>
  <div class="frow"><label for="sPw">Пароли, которые пробовать автоматически (через запятую)</label><input id="sPw" type="text" data-set="archive_passwords" placeholder="anivisual, 1234" autocomplete="off"></div>
 </div>
 <div class="card"><h2>Обновление и PIN</h2>
  <div class="frow"><label for="sUpd">Откуда обновлять утилиту</label><input id="sUpd" type="text" data-set="update_url" autocomplete="off"></div>
  <div class="frow"><label>Сменить PIN</label><div class="row wrap"><input id="sPinOld" type="password" inputmode="numeric" placeholder="текущий" autocomplete="off"><input id="sPinNew" type="password" inputmode="numeric" placeholder="новый, 4–8 цифр" autocomplete="off"><button class="sm" id="sPinGo">Сменить</button></div></div>
 </div>
 <div class="card"><h2>О системе</h2><div class="hint" id="sInfo"></div></div>
</div>
<footer><span id="ffm"></span><span class="acts"><button class="ghost sm" id="mediaReset">сбросить пароль медиа</button><button class="ghost sm" id="update">Обновить утилиту</button></span></footer>
</div>
<div id="viewer"><div class="vbar"><span class="t" id="vtitle"></span><a id="vopen" target="_blank" rel="noopener"><button class="ghost sm">открыть</button></a><a id="vdl"><button class="ghost sm">скачать</button></a><button class="danger sm" id="vdel">удалить с дека 🔒</button><button class="ghost sm" id="vclose">✕</button></div><div class="vbody" id="vbody"></div></div>
<div id="modal"><div class="mbox" id="mbox"></div></div>
<div id="toasts"></div>
<script>
const $=s=>document.querySelector(s);
const isUrl=t=>/^https?:\/\/\S+$/i.test(t);
const busy=new Set(),expanded=new Set();let showHidden=false,lastState=null,tab='games',archTimer=null;
let sigGames='',sigJobs='',sigDisks='',holdGames=0;
// Re-rendering a list while a <select> is open (or an input is focused) destroys the element and closes the
// picker on phones, so lists are redrawn only when their data changed and never while they are being used.
function focusWithin(sel){const a=document.activeElement,c=document.querySelector(sel);return !!(a&&c&&a!==document.body&&c.contains(a));}
document.addEventListener('pointerdown',e=>{if(e.target.closest('#games select'))holdGames=Date.now()+6000;},true);
document.addEventListener('focusin',e=>{if(e.target.closest('#games select'))holdGames=Date.now()+6000;});
document.addEventListener('change',e=>{if(e.target.closest('#games select'))holdGames=0;});
document.addEventListener('focusout',e=>{if(e.target.closest('#games select'))holdGames=Math.min(holdGames,Date.now()+400);});
let mediaToken=null,mediaItems=[],mediaFilter='all',viewing=null;
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function fmt(n){if(n==null)return '?';return n<1e6?(n/1e3).toFixed(0)+' KB':n<1e9?(n/1e6).toFixed(1)+' MB':(n/1e9).toFixed(2)+' GB';}
function when(t){const d=new Date(t*1000);return d.toLocaleDateString('ru-RU')+' '+d.toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});}
function toast(msg,kind){const t=document.createElement('div');t.className='toast'+(kind?' '+kind:'');t.textContent=msg;$('#toasts').appendChild(t);setTimeout(()=>{t.style.opacity='0';setTimeout(()=>t.remove(),320);},kind==='err'?5000:3200);}
// ---- modal helpers
function openModal(html){const m=$('#modal'),b=$('#mbox');b.innerHTML=html;m.classList.add('on');m.onclick=e=>{if(e.target===m)closeModal();};const f=b.querySelector('input,select,textarea');if(f)setTimeout(()=>f.focus(),60);return b;}
function closeModal(){$('#modal').classList.remove('on');$('#mbox').innerHTML='';}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();closeViewer();}});
function ask(o){return new Promise(res=>{const b=openModal(`<h3>${esc(o.title)}</h3>${o.text?`<p>${esc(o.text)}</p>`:''}${o.html||''}`
  +(o.fields||[]).map((f,i)=>f.type==='check'?`<label class="chk"><input id="mf${i}" type="checkbox" ${f.value?'checked':''}>${esc(f.label)}</label>`
   :f.type==='select'?`<label>${esc(f.label)}</label><select id="mf${i}">${f.options.map(op=>`<option value="${esc(op.value)}" ${op.value===f.value?'selected':''}>${esc(op.label)}</option>`).join('')}</select>`
   :`<label>${esc(f.label)}</label><input id="mf${i}" type="${f.type||'text'}" value="${esc(f.value||'')}" placeholder="${esc(f.placeholder||'')}" autocomplete="off" ${f.numeric?'inputmode="numeric"':''}>`).join('')
  +`<div class="btns"><button class="ghost" id="mcancel">Отмена</button><button id="mok" class="${o.danger?'danger':''}">${esc(o.ok||'ОК')}</button></div>`);
 const done=v=>{closeModal();res(v);};
 $('#mcancel').onclick=()=>done(null);$('#mok').onclick=()=>done((o.fields||[]).map((f,i)=>{const el=$('#mf'+i);return f.type==='check'?el.checked:el.value;}));
 b.querySelectorAll('input').forEach(inp=>inp.addEventListener('keydown',e=>{if(e.key==='Enter'&&inp.type!=='checkbox')$('#mok').click();}));
 $('#modal').onclick=e=>{if(e.target===$('#modal'))done(null);};});}
async function api(path,body,method){const r=await fetch(path,{method:method||'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let j={};try{j=await r.json();}catch(e){}if(!r.ok)toast(j.error||('ошибка '+r.status),'err');refresh();return j;}
// ---- tabs
function showTab(t){tab=t;$('#pgGames').hidden=t!=='games';$('#pgArch').hidden=t!=='arch';$('#pgMedia').hidden=t!=='media';$('#pgSettings').hidden=t!=='settings';
 [['tabGames','games'],['tabArch','arch'],['tabMedia','media'],['tabSettings','settings']].forEach(([id,k])=>$('#'+id).classList.toggle('on',t===k));
 hdr.classList.remove('hide');window.scrollTo(0,0);
 if(t==='media')mediaEnter();if(t==='arch')loadArchives();if(t==='settings')fillSettings();clearInterval(archTimer);if(t==='arch')archTimer=setInterval(loadArchives,5000);}
$('#tabGames').onclick=()=>showTab('games');$('#tabArch').onclick=()=>showTab('arch');$('#tabMedia').onclick=()=>showTab('media');$('#tabSettings').onclick=()=>showTab('settings');$('#settings').onclick=()=>showTab('settings');
// header slides away when scrolling down on a phone and comes back on scroll up
const hdr=document.querySelector('header');let lastY=0;const mobile=matchMedia('(max-width:560px)');
addEventListener('scroll',()=>{const y=scrollY;if(!mobile.matches){hdr.classList.remove('hide');lastY=y;return;}
 if(y>lastY+8&&y>90)hdr.classList.add('hide');else if(y<lastY-8||y<40)hdr.classList.remove('hide');lastY=y;},{passive:true});
$('#addr').onclick=()=>{const u=lastState&&lastState.urls[0];if(!u)return;if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(u).then(()=>toast('Адрес скопирован','ok'),()=>toast(u));else toast(u);};
// ---- downloads / uploads
const diskSel=()=>$('#disk').value||undefined;
function download(u){u=(u||$('#url').value).trim();if(!isUrl(u)){toast('Нужна ссылка вида http(s)://…','err');return;}$('#url').value='';
 if(isMegaFolder(u)){megaPick(u);return;}api('/api/download',{url:u,disk:diskSel()});}
const MEGA_RE=/^https?:\/\/(?:www\.)?mega(?:\.co)?\.nz\//i;
function isMegaFolder(u){if(!MEGA_RE.test(u))return false;const h=u.split('#')[1]||'';return /\/folder\//i.test(u.split('#')[0])||/^F!/.test(h);}
async function megaPick(url){const j=await api('/api/mega/list',{url});const fs=(j&&j.files)||[];
 if(!j.ok||!fs.length){$('#url').value=url;if(j.ok)toast('В этой папке Mega нет файлов','err');return;}
 if(fs.length===1){const r=await api('/api/mega/download',{url,nodes:[fs[0].h],disk:diskSel()});if(r.ok)toast('Скачиваю: '+fs[0].name,'ok');return;}
 const b=openModal(`<h3>Папка на Mega</h3><p>${esc(j.name||'')} · файлов: ${fs.length} · ${fmt(j.total)}</p>`
  +`<div class="row" style="gap:8px"><button class="ghost sm" id="mAll">выбрать все</button><button class="ghost sm" id="mNone">снять все</button></div>`
  +`<div class="mlist">${fs.map((f,i)=>`<label class="vn"><input type="checkbox" data-mi="${i}" checked><div style="flex:1;min-width:0"><b>${esc(f.name)}</b><small>${f.dir?esc(f.dir)+' · ':''}${fmt(f.size)}</small></div></label>`).join('')}</div>`
  +`<div class="hint" id="mHint" style="margin-top:8px"></div><div class="btns"><button class="ghost" id="mCancel">Отмена</button><button id="mOk">Скачать</button></div>`);
 const boxes=[...b.querySelectorAll('[data-mi]')];
 const upd=()=>{const n=boxes.filter(x=>x.checked).length;$('#mOk').textContent=n?`Скачать (${n})`:'Скачать';$('#mOk').disabled=!n;
  $('#mHint').textContent=n>1?`Отмеченное скачается одним заданием в папку «${j.name||'Mega'}» со всей структурой. Если там только архивы, они распакуются.`:n===1?'Один файл попадёт во входящие, как обычная ссылка.':'';};
 boxes.forEach(x=>x.onchange=upd);$('#mAll').onclick=()=>{boxes.forEach(x=>x.checked=true);upd();};$('#mNone').onclick=()=>{boxes.forEach(x=>x.checked=false);upd();};
 $('#mCancel').onclick=closeModal;
 $('#mOk').onclick=async()=>{const nodes=boxes.filter(x=>x.checked).map(x=>fs[+x.dataset.mi].h);closeModal();
  const r=await api('/api/mega/download',{url,nodes,disk:diskSel()});if(r.ok)toast(nodes.length>1?`Скачиваю папку «${j.name||'Mega'}» · файлов: ${nodes.length}`:'Скачиваю: '+((r.jobs[0]||{}).label||''),'ok');};
 upd();}
$('#go').onclick=()=>download();
$('#url').addEventListener('keydown',e=>{if(e.key==='Enter')download();});
$('#clear').onclick=()=>api('/api/clear',{});
$('#stopAll').onclick=async()=>{const n=lastJobs.filter(j=>j.kind==='download'&&CANCELLABLE.includes(j.status)).length;
 const r=await ask({title:'Остановить скачивание',text:`Остановить все скачивания (${n})? Задания из очереди исчезнут сразу, текущее остановится, а недокачанное удалится.`,ok:'Остановить',danger:true});if(!r)return;
 const j=await api('/api/cancel_all',{});if(j.ok)toast(j.stopped?`Остановлено: ${j.stopped}`:'Нечего останавливать','ok');};
$('#toggleHidden').onclick=()=>{showHidden=!showHidden;render(lastState);};
$('#disk').onchange=()=>api('/api/settings',{default_disk:$('#disk').value});
function upload(f){const x=new XMLHttpRequest();x.open('PUT','/api/upload/'+encodeURIComponent(f.name)+(diskSel()?'?disk='+encodeURIComponent(diskSel()):''));x.onerror=()=>toast('Ошибка загрузки: '+f.name,'err');x.onload=refresh;x.send(f);setTimeout(refresh,300);}
$('#file').onchange=e=>{[...e.target.files].forEach(upload);e.target.value='';};
const d=$('#drop');
['dragenter','dragover'].forEach(ev=>d.addEventListener(ev,e=>{e.preventDefault();d.classList.add('over');}));
['dragleave','drop'].forEach(ev=>d.addEventListener(ev,e=>{e.preventDefault();d.classList.remove('over');}));
d.addEventListener('drop',e=>{if(e.dataTransfer.files.length){[...e.dataTransfer.files].forEach(upload);return;}
 const t=(e.dataTransfer.getData('text/uri-list')||e.dataTransfer.getData('text/plain')||'').split('\n')[0].trim();if(isUrl(t))download(t);});
document.addEventListener('paste',e=>{if(['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName))return;const t=(e.clipboardData.getData('text')||'').trim();if(isUrl(t))download(t);});
// ---- game actions (event delegation; lists re-render only when their data changes)
document.addEventListener('change',e=>{const el=e.target;if(el.dataset.compat!==undefined)api('/api/game/compat',{game:el.dataset.game,exe:el.dataset.exe,tool:el.value}).then(j=>{if(j.ok)toast(j.note,'ok');});});
document.addEventListener('click',async e=>{const card=e.target.closest('[data-open]');if(card&&!e.target.closest('button,select,input,a')){openGame(card.dataset.open);return;}
 const b=e.target.closest('button');if(!b)return;const ds=b.dataset;
 if(ds.cancel)api('/api/cancel',{id:+ds.cancel});
 if(ds.pwgo!==undefined){const box=b.closest('.item');const pw=box.querySelector('input[type=password]').value;const rem=box.querySelector('input[type=checkbox]').checked;if(!pw){toast('Введи пароль','err');return;}api('/api/job/password',{id:+ds.pwgo,password:pw,remember:rem});}
 if(ds.add!==undefined)addFlow(ds.game,ds.add);
 if(ds.rename!==undefined){const r=await ask({title:'Имя в библиотеке Steam',fields:[{label:'Название',value:ds.name}],ok:'Переименовать'});if(!r)return;const j=await api('/api/game/rename',{game:ds.game,exe:ds.rename,name:r[0]});if(j.ok)toast(j.note,'ok');}
 if(ds.hide!==undefined)api('/api/game/hide',{path:ds.hide,hidden:ds.hidden==='1'});
 if(ds.unimport!==undefined){const r=await ask({title:'Убрать из DeckDrop',text:`«${ds.name}» пропадёт из списка. Файлы на деке и ярлык в Steam останутся на месте.`,ok:'Убрать',danger:true});if(!r)return;
  const j=await api('/api/game/unimport',{path:ds.unimport});if(j.ok){toast('Убрано из списка: '+j.removed,'ok');if(view.kind==='game')closeGame();}}
 if(ds.imppick!==undefined){const j=await api('/api/game/import',{path:ds.imppick});if(j.ok){closeModal();toast(importNote(j),'ok');openGame(j.path);}}
 if(ds.del!==undefined){const r=await ask({title:'Удалить с дека',text:`«${ds.name}» будет удалена вместе со скачанным архивом.`,fields:[{label:'Убрать ярлык и из Steam',type:'check',value:true},{label:'PIN',type:'password',numeric:true}],ok:'Удалить',danger:true});if(!r)return;
  const j=await api('/api/game/delete',{path:ds.del,pin:r[1],remove_shortcut:r[0]});if(j.ok){toast('Удалено: '+j.removed.join(', ')+(j.notes&&j.notes.length?'. '+j.notes.join('; '):''),'ok');if(view.kind==='game')closeGame();}}
 if(ds.aextract!==undefined)api('/api/archive/extract',{path:ds.aextract}).then(j=>{if(j.id){toast('Распаковываю: '+j.label,'ok');showTab('games');}});
 if(ds.adel!==undefined){const r=await ask({title:'Удалить архив',text:ds.name,ok:'Удалить',danger:true});if(!r)return;const j=await api('/api/archive/delete',{path:ds.adel});if(j.ok){toast('Удалено: '+j.removed.join(', '),'ok');loadArchives();}}
});
const CANCELLABLE=['queued','resolving','downloading'];let lastJobs=[];
const RU={cancelled:'отменено',queued:'в очереди',resolving:'ищу файл',downloading:'скачиваю',uploading:'принимаю',extracting:'распаковываю',needs_password:'нужен пароль',done:'готово',error:'ошибка'};
function compatOptions(cur){const tools=(lastState&&lastState.compat_tools)||[];let opts=tools.map(t=>({value:t.name,label:t.label}));if(cur&&!opts.some(o=>o.value===cur))opts.unshift({value:cur,label:cur});opts.push({value:'',label:'без Proton (нативно)'});return opts;}
function compatLabel(v){const t=((lastState&&lastState.compat_tools)||[]).find(t=>t.name===v);return t?t.label:(v||'без Proton');}
const chosenExe=g=>g.exes.find(x=>x.in_steam)||g.exes.find(x=>x.recommended)||g.exes[0];
// ---- list <-> game page routing (hash, so the phone's Back button works)
let view={kind:'list',path:null},sigGame='',extrasFor=null,gameInfo=null;
function parseHash(){const m=location.hash.match(/^#g=(.+)$/);return m?{kind:'game',path:decodeURIComponent(m[1])}:{kind:'list',path:null};}
function applyView(v){view=v;sigGames='';sigGame='';extrasFor=null;$('#gameList').hidden=view.kind==='game';$('#gamePage').hidden=view.kind!=='game';if(view.kind==='game'&&tab!=='games')showTab('games');window.scrollTo(0,0);render(lastState);}
// history is an enhancement (phone Back button returns to the list); the view switches even where it is unavailable
function openGame(path){try{history.pushState({g:path},'','#g='+encodeURIComponent(path));}catch(e){}applyView({kind:'game',path});}
function closeGame(){if(history.state&&history.state.g){history.back();return;}try{history.replaceState(null,'',location.pathname+location.search);}catch(e){}applyView({kind:'list',path:null});}
addEventListener('popstate',()=>applyView(parseHash()));
$('#gpBack').onclick=closeGame;
function exeRow(g,x){const k=g.path+'|'+x.exe;let right;
 if(busy.has(k))right='<span class="pill warn">добавляю…</span>';
 else if(x.in_steam)right=`<span class="pill">✓ в Steam</span>${x.pending?'<span class="pill warn" title="применится, когда включится управление Steam">в очереди</span>':''}`+(x.linux?'':`<select class="sel" data-compat data-game="${esc(g.path)}" data-exe="${esc(x.exe)}">${compatOptions(x.compat).map(o=>`<option value="${esc(o.value)}" ${o.value===(x.compat||'')?'selected':''}>${esc(o.label)}</option>`).join('')}</select>`);
 else right=`<button class="sm" data-add="${esc(x.exe)}" data-game="${esc(g.path)}">в Steam</button>`;
 return `<div class="exe"><div class="exeh"><span class="n">${esc(x.exe)}</span><span class="pill k">${x.linux?'Linux':'Windows'}</span>${x.recommended?'<span class="pill acc">рекомендуется</span>':''}<span class="acts">${right}</span></div></div>`;}
function listCard(g){const x=g.exes.find(e=>e.in_steam);
 return `<div class="item gcard${g.hidden?' hid':''}" data-open="${esc(g.path)}"><div class="top"><b>${esc(g.title||g.name)}</b><span class="acts"><span class="pill k">${esc(g.disk)}</span>${g.imported?'<span class="pill acc" title="игра лежит вне папок DeckDrop">своя</span>':''}${x?'<span class="pill">✓ в Steam</span>':'<span class="pill k">не добавлена</span>'}${x&&x.pending?'<span class="pill warn">в очереди</span>':''}${x&&x.art?'<span class="pill">обложки ✓</span>':''}<span class="chev">›</span></span></div><div class="path">${esc(g.path)}</div></div>`;}
function renderList(s){lastJobs=s.jobs||[];
 const running=lastJobs.filter(j=>j.kind==='download'&&CANCELLABLE.includes(j.status)).length;$('#stopAll').hidden=!running;$('#stopAll').textContent=running>1?`остановить всё (${running})`:'остановить всё';
 const jsig=JSON.stringify(s.jobs);
 if(jsig!==sigJobs&&!focusWithin('#jobs')){sigJobs=jsig;
 $('#jobs').innerHTML=s.jobs.map(j=>{const act=j.status==='downloading'||j.status==='uploading';const pct=j.total?Math.round(j.done*100/j.total):0;
  let st=RU[j.status]||j.status;if(act)st+=' · '+fmt(j.done)+(j.total?' / '+fmt(j.total)+' · '+pct+'%':'')+(j.speed?' · '+fmt(j.speed)+'/с':'');
  const cls=j.status==='error'?'err':j.status==='done'?'ok':j.status==='cancelled'?'':'busy';const can=j.kind==='download'&&CANCELLABLE.includes(j.status);
  return `<div class="item"><div class="top"><b>${esc(j.label)}</b><span class="acts"><span class="pill k">${esc(j.disk||'')}</span>${can?`<button class="ghost sm" data-cancel="${j.id}">отмена</button>`:''}</span></div>`
   +(act?`<div class="bar"><i style="width:${pct}%"></i></div>`:'')+`<div class="st ${cls}">${esc(st)}${j.error?' — '+esc(j.error):''}</div>`
   +(j.status==='needs_password'?`<div class="row wrap" style="margin-top:8px"><input type="password" placeholder="пароль архива" style="flex:1;min-width:140px;padding:8px 12px;font-size:.95em"><label class="small muted" style="display:flex;align-items:center;gap:6px"><input type="checkbox" checked>запомнить</label><button class="sm" data-pwgo="${j.id}">распаковать</button></div>`:'')
   +(j.game_dir?`<div class="path">→ ${esc(j.game_dir)}</div>`:j.status==='done'&&j.file?`<div class="path">→ ${esc(j.file)}</div>`:'')+'</div>';}).join('')||'<div class="empty">Пока пусто. Кинь ссылку или файл выше.</div>';}
 const gsig=JSON.stringify([s.games,showHidden]);
 if(gsig!==sigGames){sigGames=gsig;const nh=s.games.filter(g=>g.hidden).length;$('#toggleHidden').textContent=showHidden?'убрать скрытые':`показать скрытые (${nh})`;$('#toggleHidden').hidden=!nh&&!showHidden;
  $('#games').innerHTML=s.games.filter(g=>showHidden||!g.hidden).map(listCard).join('')||'<div class="empty">Игр пока нет.</div>';}}
function renderGame(s){const g=(s.games||[]).find(x=>x.path===view.path);
 if(!g){$('#gpHead').innerHTML='<div class="empty">Игра не найдена, возможно уже удалена.</div>';$('#gpExes').innerHTML='';$('#gpCoversCard').hidden=$('#gpSavesCard').hidden=$('#gpFilesCard').hidden=true;$('#gpActs').innerHTML='';$('#gpInfo').innerHTML='';return;}
 if(extrasFor!==g.path){extrasFor=g.path;loadGameExtras(g);}
 const sig=JSON.stringify([g,[...busy],s.compat_tools,gameInfo]);if(sig===sigGame||focusWithin('#gamePage')||Date.now()<holdGames)return;sigGame=sig;
 const ch=chosenExe(g);const inSteam=g.exes.some(x=>x.in_steam);
 $('#gpHead').innerHTML=`<div class="top"><div style="min-width:0"><div class="gtitle">${esc(g.title||g.name)}${inSteam?`<button class="ghost sm" data-rename="${esc(ch.exe)}" data-game="${esc(g.path)}" data-name="${esc(ch.clean_name||ch.name)}" title="переименовать">✎</button>`:''}</div><div class="path">${esc(g.path)}</div></div><span class="acts"><span class="pill k">${esc(g.disk)}</span>${inSteam?'<span class="pill">✓ в Steam</span>':'<span class="pill k">не добавлена</span>'}${g.imported?'<span class="pill acc" title="папка вне DeckDrop, добавлена вручную">своя</span>':''}${g.hidden?'<span class="pill warn">скрыта из списка</span>':''}</span></div>`;
 $('#gpExes').innerHTML=g.exes.map(x=>exeRow(g,x)).join('')||'<div class="empty">исполняемых файлов не найдено</div>';
 $('#gpCoversCard').hidden=!inSteam;$('#gpSavesCard').hidden=!inSteam;$('#gpFilesCard').hidden=!g.exes.length;
 $('#gpActs').innerHTML=(g.hidden?`<button class="ghost sm" data-hide="${esc(g.path)}" data-hidden="0">вернуть в список</button>`:`<button class="ghost sm" data-hide="${esc(g.path)}" data-hidden="1">скрыть из списка</button>`)+(g.imported?`<button class="ghost sm" data-unimport="${esc(g.path)}" data-name="${esc(g.title||g.name)}">убрать из DeckDrop</button>`:'')
  +`<button class="danger sm" data-del="${esc(g.path)}" data-name="${esc(g.title||g.name)}">удалить с дека 🔒</button>`
  +(g.imported?'<span class="hint" style="flex-basis:100%">Игра добавлена вручную и лежит вне папок DeckDrop. «Убрать из DeckDrop» только прячет её из списка, файлы остаются на месте.</span>':'');
 const info=[`Папка: <code>${esc(g.name)}</code>`,`Диск: ${esc(g.disk)}`];
 if(gameInfo&&gameInfo.path===g.path)info.push(`Размер: ${fmt(gameInfo.size)} · файлов: ${gameInfo.files}`);
 if(ch&&ch.in_steam){info.push(`Имя в Steam: ${esc(ch.name)}`);if(ch.appid)info.push(`Steam AppID: <code>${ch.appid}</code>`);if(!ch.linux)info.push(`Proton: ${esc(compatLabel(ch.compat))}${ch.compat_from==='steam'?' · из настроек Steam':''}`);
  if(ch.art_source)info.push(`Обложки: ${ch.art_source==='vndb'?'VNDB · '+esc(ch.vndb_title||''):ch.art_source==='custom'?'свои картинки':ch.art_source==='steam'?'уже были в Steam':'из иконки exe'}`);if(ch.art_note)info.push(esc(ch.art_note));if(ch.art_error)info.push(`<span class="err">${esc(ch.art_error)}</span>`);if(ch.pending)info.push('<span class="busy">Имя и Proton применятся, когда включится управление Steam</span>');}
 $('#gpInfo').innerHTML=info.join('<br>');}
function render(s){if(!s)return;
 $('#ver').textContent='v'+s.version;
 const c=s.cdp||{};const cdpTxt=!c.enabled?'управление Steam выключено':c.available?'управление Steam активно':c.marker?'управление Steam включится после перезагрузки дека':'управление Steam: нет папки Steam';
 $('#ffm').textContent=(s.ffmpeg?'ffmpeg найден':'ffmpeg не найден: обложки попроще, VNDB частично, клипы без звука')+' · '+cdpTxt+(s.pending?` · в очереди: ${s.pending}`:'');
 $('#addr').textContent=s.urls[0]||'';$('#addr').title=s.urls.join('\n');
 const dsel=$('#disk');const dsig=JSON.stringify([s.disks,(s.settings||{}).default_disk]);
 if(dsig!==sigDisks&&document.activeElement!==dsel){const cur=dsel.value||(s.settings&&s.settings.default_disk)||'internal';dsel.innerHTML=(s.disks||[]).map(d=>`<option value="${esc(d.id)}" ${d.id===cur?'selected':''}>${esc(d.label)} · ${fmt(d.free)} свободно</option>`).join('');dsel.hidden=(s.disks||[]).length<2;sigDisks=dsig;}
 if(view.kind==='game')renderGame(s);else renderList(s);}
async function refresh(){try{lastState=await(await fetch('/api/state')).json();render(lastState);}catch(e){}}
applyView(parseHash());refresh();setInterval(refresh,1000);
// ---- game page extras: folder size, covers with preview/replace, saves
const LAB={portrait:['Вертикальная','600×900, Big Picture','2/3'],landscape:['Широкая','920×430','920/430'],hero:['Баннер hero','1920×620','1920/620'],logo:['Логотип','прозрачный PNG','16/5'],icon:['Иконка','квадрат','1/1']};
function loadGameExtras(g){gameInfo=null;const ch=chosenExe(g);if(g.exes.length)setupFiles(g);
 fetch('/api/game/info?path='+encodeURIComponent(g.path)).then(r=>r.json()).then(j=>{if(!j.error){gameInfo={path:g.path,...j};sigGame='';render(lastState);}}).catch(()=>{});
 if(ch&&ch.in_steam){loadCovers(g,ch);loadSaves(g,ch);}else{$('#gpCovers').innerHTML='';$('#gpSaves').innerHTML='';}}
// ---- files inside the game: a patch next to the exe, or anywhere below the game folder
let filesTimer=0;
function filesQ(g){const exe=$('#gpFExe').value||((chosenExe(g)||{}).exe)||'';return `game=${encodeURIComponent(g.path)}&exe=${encodeURIComponent(exe)}&dir=${encodeURIComponent($('#gpFDir').value.trim())}`;}
function setupFiles(g){const ch=chosenExe(g);
 $('#gpFExe').innerHTML=g.exes.map(x=>`<option value="${esc(x.exe)}" ${ch&&x.exe===ch.exe?'selected':''}>${esc(x.exe)}</option>`).join('');$('#gpFExeRow').hidden=g.exes.length<2;
 $('#gpFDir').value='';$('#gpFWhere').textContent='';$('#gpFList').innerHTML='';
 $('#gpFDir').oninput=()=>{clearTimeout(filesTimer);filesTimer=setTimeout(()=>loadFiles(g),350);};
 $('#gpFExe').onchange=()=>loadFiles(g);
 $('#gpFUp').onclick=()=>{const inp=document.createElement('input');inp.type='file';inp.multiple=true;inp.onchange=()=>uploadGameFiles(g,[...inp.files]);inp.click();};
 $('#gpFArch').onclick=()=>{const inp=document.createElement('input');inp.type='file';inp.accept='.zip,.7z,.rar,.tar,.tgz,.txz,.tbz2,.gz,.xz,.bz2';inp.onchange=()=>{const f=inp.files[0];if(f)unpackArchive(g,f);};inp.click();};
 loadFiles(g);}
async function loadFiles(g){let j;try{j=await(await fetch('/api/game/dir?'+filesQ(g))).json();}catch(e){j={error:'нет связи с деком'};}
 if(j.error){$('#gpFWhere').innerHTML=`<span class="err">${esc(j.error)}</span>`;$('#gpFList').innerHTML='';$('#gpFUp').disabled=$('#gpFArch').disabled=true;return null;}
 $('#gpFUp').disabled=$('#gpFArch').disabled=false;
 $('#gpFWhere').innerHTML=`Куда: <code>${esc(j.game_rel==='.'?'корень папки игры':j.game_rel)}</code>`+(j.exists?'':' · <span class="busy">такой папки ещё нет, будет создана</span>');
 const n=j.entries.length+j.more;
 $('#gpFList').innerHTML=j.exists?(n?`<details class="hint"><summary>Сейчас в этой папке: ${n}</summary><ul class="list">${j.entries.map(e=>`<li>${e.dir?'📁 ':''}<code>${esc(e.name)}</code>${e.dir?'':' · '+fmt(e.size)}</li>`).join('')}${j.more?`<li>и ещё ${j.more}</li>`:''}</ul></details>`:'<div class="hint">папка пустая</div>'):'';
 return j;}
function putGameFile(q,f,replace){return new Promise(res=>{const xh=new XMLHttpRequest();
 xh.open('PUT',`/api/game/file?${q}&name=${encodeURIComponent(f.name)}${replace?'&replace=1':''}`);
 xh.upload.onprogress=e=>{if(e.lengthComputable&&e.total)$('#gpFWhere').textContent=`Загружаю ${f.name}: ${Math.round(e.loaded*100/e.total)}% · ${fmt(e.loaded)} из ${fmt(e.total)}`;};
 xh.onload=async()=>{let r={};try{r=JSON.parse(xh.responseText);}catch(e){}
  if(xh.status===409&&r.exists&&!replace){const ok=await ask({title:'Заменить файл',text:`«${f.name}» уже есть в этой папке. Заменить? Самый первый вариант DeckDrop сохранит рядом как .bak.`,ok:'Заменить',danger:true});return res(ok?await putGameFile(q,f,true):false);}
  if(xh.status===200){toast(`${f.name} → ${r.rel}${r.backup?' · прежний сохранён как '+r.backup:''}`,'ok');res(true);}else{toast(r.error||'ошибка загрузки','err');res(false);}};
 xh.onerror=()=>{toast('ошибка загрузки: '+f.name,'err');res(false);};xh.send(f);});}
async function uploadGameFiles(g,files){if(!files.length)return;const j=await loadFiles(g);if(!j)return;
 const dirs=new Set(j.entries.filter(e=>e.dir).map(e=>e.name)),have=new Set(j.entries.filter(e=>!e.dir).map(e=>e.name));
 const asDir=files.find(f=>dirs.has(f.name));if(asDir){toast(`«${asDir.name}» здесь уже папка, файл с таким именем не положить`,'err');return;}
 const clash=files.filter(f=>have.has(f.name)).map(f=>f.name);
 if(clash.length){const r=await ask({title:'Заменить файлы',text:`Уже есть: ${clash.join(', ')}. Заменить? Самый первый вариант каждого DeckDrop сохранит рядом как .bak.`,ok:'Заменить',danger:true});if(!r)return;}
 const q=filesQ(g);$('#gpFUp').disabled=true;let done=0;
 try{for(const f of files){if(!await putGameFile(q,f,clash.includes(f.name)))break;done++;}}finally{$('#gpFUp').disabled=false;}
 if(done>1)toast(`Загружено файлов: ${done}`,'ok');loadFiles(g);}
// ---- an archive unpacked over the game: upload, unpack aside, preview, then apply
const ARCH_RE=/\.(zip|7z|rar|tar|tgz|txz|tbz2|tar\.(gz|xz|bz2))$/i;
async function unpackArchive(g,f){
 if(!ARCH_RE.test(f.name)){toast('Это не архив: подойдут zip, 7z, rar и tar','err');return;}
 if(!await loadFiles(g))return;
 $('#gpFUp').disabled=$('#gpFArch').disabled=true;
 try{let r=await new Promise(res=>{const xh=new XMLHttpRequest();xh.open('PUT',`/api/game/archive?${filesQ(g)}&name=${encodeURIComponent(f.name)}`);
   xh.upload.onprogress=e=>{if(e.lengthComputable&&e.total)$('#gpFWhere').textContent=`Загружаю ${f.name}: ${Math.round(e.loaded*100/e.total)}%`;};
   xh.upload.onload=()=>{$('#gpFWhere').textContent='Распаковываю на деке и смотрю, что поменяется…';};
   xh.onload=()=>{let j={};try{j=JSON.parse(xh.responseText);}catch(e){}res(xh.status===200?j:{error:j.error||'ошибка '+xh.status});};
   xh.onerror=()=>res({error:'ошибка загрузки: '+f.name});xh.send(f);});
  if(r.error&&!r.needs_password){toast(r.error,'err');return;}
  while(r.needs_password){const p=await ask({title:'Архив с паролем',text:r.error?`«${r.name}»: неверный пароль, попробуй ещё раз.`:`«${r.name}» закрыт паролем.`,fields:[{label:'Пароль',type:'password'},{label:'Запомнить для других архивов',type:'check',value:true}],ok:'Открыть'});
   if(!p||!p[0]){api('/api/game/archive/discard',{token:r.token});return;}
   $('#gpFWhere').textContent='Распаковываю…';r=await api('/api/game/archive/unlock',{token:r.token,password:p[0],remember:p[1]});if(!r.ok)return;}
  await patchPreview(g,r);
 }finally{$('#gpFUp').disabled=$('#gpFArch').disabled=false;loadFiles(g);}}
function patchPreview(g,r){return new Promise(done=>{
 const where=r.target_rel==='.'?'корень папки игры':r.target_rel;
 const stat=v=>{let h=`Файлов: ${v.files} · ${fmt(v.size)}. `+(v.conflicts?`Заменит существующих: ${v.conflicts} (${v.sample.map(esc).join(', ')}${v.conflicts>v.sample.length?', …':''}).`:'Существующие файлы не затронет.');
  if(v.blocked_count)h+=`<br><span class="err">Не ляжет, мешают папки или файлы с тем же именем: ${v.blocked_count} (${v.blocked.map(esc).join(', ')}${v.blocked_count>v.blocked.length?', …':''})</span>`;return h;};
 openModal(`<h3>Распаковать в игру</h3><p>«${esc(r.name)}» → <code>${esc(where)}</code></p>`
  +`<div class="hint">Внутри: ${r.top.map(t=>`<code>${esc(t)}</code>`).join(' ')||'пусто'}${r.top_more?` и ещё ${r.top_more}`:''}</div>`
  +(r.single_top?`<label class="chk"><input type="checkbox" id="pvStrip">Без верхней папки «${esc(r.single_top)}»: её содержимое ляжет прямо в ${esc(where)}</label>`:'')
  +`<div class="hint" id="pvStat"></div>`
  +`<label class="chk"><input type="checkbox" id="pvBak" checked>Сохранить заменяемые файлы как .bak (только самый первый вариант)</label>`
  +`<div class="btns"><button class="ghost" id="pvCancel">Отмена</button><button id="pvOk">Распаковать</button></div>`);
 const cur=()=>$('#pvStrip')&&$('#pvStrip').checked?r.stripped:r.plain;
 const upd=()=>{const v=cur();$('#pvStat').innerHTML=stat(v);$('#pvOk').disabled=!v.files;$('#pvOk').className=v.conflicts?'danger':'';
  $('#pvOk').textContent=!v.files?'Нечего распаковывать':v.conflicts?`Распаковать и заменить (${v.conflicts})`:'Распаковать';};
 if($('#pvStrip'))$('#pvStrip').onchange=upd;upd();
 const cancel=()=>{closeModal();api('/api/game/archive/discard',{token:r.token});done(false);};
 $('#pvCancel').onclick=cancel;$('#modal').onclick=e=>{if(e.target===$('#modal'))cancel();};
 $('#pvOk').onclick=async()=>{const strip=!!($('#pvStrip')&&$('#pvStrip').checked),backup=$('#pvBak').checked;closeModal();
  $('#gpFWhere').textContent='Распаковываю в игру…';
  const a=await api('/api/game/archive/apply',{token:r.token,strip,backup});
  if(a.ok)toast(`Распаковано в ${a.target_rel==='.'?'корень игры':a.target_rel}: файлов ${a.written}${a.replaced?`, заменено ${a.replaced}`:''}${a.backups?`, в .bak: ${a.backups}`:''}${a.skipped_count?`, пропущено ${a.skipped_count}`:''}`,'ok');
  done(!!a.ok);};});}
async function loadCovers(g,x){const q=`game=${encodeURIComponent(g.path)}&exe=${encodeURIComponent(x.exe)}`;$('#gpCvHint').textContent='смотрю…';
 $('#gpVndb').onclick=()=>vndbPicker(g.path,x.exe,x.clean_name||x.name,()=>loadCovers(g,x));
 $('#gpIcon').onclick=async()=>{const j=await api('/api/art',{game:g.path,exe:x.exe,source:'icon'});if(j.ok){toast('Рисую обложки из иконки…','ok');setTimeout(()=>loadCovers(g,x),5000);}};
 try{const j=await(await fetch('/api/art/current?'+q)).json();if(j.error){$('#gpCvHint').innerHTML=`<span class="err">${esc(j.error)}</span>`;$('#gpCovers').innerHTML='';return;}
  $('#gpCvHint').textContent=(j.source==='vndb'?'Источник: VNDB · '+(j.vndb_title||''):j.source==='custom'?'Источник: свои картинки':j.source==='icon'?'Источник: иконка exe':j.source==='steam'?'Обложки уже были в Steam':'Обложек ещё нет')+(j.live?'':' · Steam покажет новые обложки после перезапуска')+(j.note?' · '+j.note:'')+(j.error?' · '+j.error:'')+'. Нажми «заменить…», чтобы поставить свою картинку в слот.';
  $('#gpCovers').innerHTML=Object.keys(LAB).map(sl=>{const it=j.slots[sl];const [t,sz,ar]=LAB[sl];return `<div class="cov"><div class="im" style="--ar:${ar}">${it?`<img src="${esc(it.url)}" alt="">`:'<span class="muted small">нет</span>'}</div><b>${t}</b><small>${sz}${it?' · '+fmt(it.size):''}</small><div class="acts" style="justify-content:center"><button class="ghost sm" data-cup="${sl}">заменить…</button><button class="ghost sm" data-cvn="${sl}">VNDB…</button>${sl==='icon'?`<button class="ghost sm" data-cexe="${sl}">из exe</button>`:''}</div></div>`;}).join('');
  $('#gpCovers').querySelectorAll('[data-cvn]').forEach(bt=>bt.onclick=()=>vndbPicker(g.path,x.exe,x.clean_name||x.name,()=>loadCovers(g,x),bt.dataset.cvn));
  $('#gpCovers').querySelectorAll('[data-cexe]').forEach(bt=>bt.onclick=async()=>{bt.disabled=true;bt.textContent='беру…';
   const jj=await api('/api/art/from_exe',{game:g.path,exe:x.exe,slot:bt.dataset.cexe});if(jj.ok)toast(`Иконка взята из ${x.exe} · ${jj.size}`,'ok');setTimeout(()=>loadCovers(g,x),600);});
  $('#gpCovers').querySelectorAll('[data-cup]').forEach(bt=>bt.onclick=()=>{const inp=document.createElement('input');inp.type='file';inp.accept='image/png,image/jpeg';inp.onchange=()=>{const f=inp.files[0];if(!f)return;bt.disabled=true;bt.textContent='загружаю…';
   const xh=new XMLHttpRequest();xh.open('PUT','/api/art/upload?'+q+'&slot='+bt.dataset.cup);xh.onload=()=>{let r={};try{r=JSON.parse(xh.responseText);}catch(e){}if(xh.status===200)toast('Обложка заменена','ok');else toast(r.error||'ошибка загрузки','err');setTimeout(()=>loadCovers(g,x),600);refresh();};xh.onerror=()=>{toast('ошибка загрузки','err');loadCovers(g,x);};xh.send(f);};inp.click();});
 }catch(e){$('#gpCvHint').textContent='ошибка';}}
async function loadSaves(g,x){const q=`game=${encodeURIComponent(g.path)}&exe=${encodeURIComponent(x.exe)}`;$('#gpSaves').textContent='смотрю…';$('#gpSvDl').href='/api/saves/backup?'+q;
 $('#gpSvDl').onclick=e=>{if($('#gpSvDlB').disabled){e.preventDefault();toast('Сохранений пока не найдено','err');}};
 $('#gpSvImp').onclick=()=>{const inp=document.createElement('input');inp.type='file';inp.accept='.zip,application/zip';inp.onchange=async()=>{const f=inp.files[0];if(!f)return;const r=await ask({title:'Импорт сейвов',text:`Файлы из «${f.name}» заменят текущие сохранения. Перед этим DeckDrop сам сделает резервную копию текущих.`,ok:'Импортировать',danger:true});if(!r)return;
   const xh=new XMLHttpRequest();xh.open('PUT','/api/saves/import?'+q);xh.onload=()=>{let j={};try{j=JSON.parse(xh.responseText);}catch(e){}if(xh.status===200)toast(`Импортировано файлов: ${j.written}${j.prefix_missing?'. Префикс Proton ещё не создан, запусти игру один раз':''}`,'ok');else toast(j.error||'ошибка импорта','err');loadSaves(g,x);};xh.onerror=()=>toast('ошибка загрузки','err');xh.send(f);toast('Загружаю бэкап…');};inp.click();};
 try{const j=await(await fetch('/api/saves/info?'+q)).json();if(j.error){$('#gpSaves').innerHTML=`<span class="err">${esc(j.error)}</span>`;$('#gpSvDlB').disabled=true;return;}
  $('#gpSaves').innerHTML=(j.sources.length?`Что войдёт в бэкап (${fmt(j.total)}):<ul class="list">${j.sources.map(s=>`<li><code>${esc(s.path)}</code> · ${s.files} файлов · ${fmt(s.size)}</li>`).join('')}</ul>`:'Сохранений не найдено: игра ещё не запускалась или хранит их в необычном месте. Импорт всё равно доступен.')+(j.prefix?`Префикс Proton: <code>${esc(j.prefix)}</code>`:'');
  $('#gpSvDlB').disabled=!j.total;}catch(e){$('#gpSaves').textContent='ошибка';}}
// ---- import one game that already lives elsewhere on the Deck
function importNote(j){const a=(j.adopted||[])[0];if(!a)return `Добавлена: ${j.name} · файлов запуска: ${j.exes.length}`;
 const bits=[];if(a.name)bits.push(`имя «${a.name}»`);if(a.compat)bits.push(compatLabel(a.compat));if(a.covers&&a.covers.length)bits.push(`обложек: ${a.covers.length}`);
 return `Добавлена: ${j.name}. Из Steam подтянул ${bits.join(', ')||'ярлык'}`;}
$('#importGame').onclick=async()=>{
 openModal(`<h3>Своя игра</h3><p>Путь к файлу запуска <b>одной игры</b>. DeckDrop ничего не копирует и не устанавливает: игра остаётся там, где лежит, и просто появляется в списке. Имя, Proton, обложки и сейвы у неё меняются так же, как у остальных.</p>
  <label>Путь к файлу запуска</label><input id="ipath" type="text" placeholder="/home/deck/Games/MyGame/Game.exe" autocomplete="off" spellcheck="false">
  <div class="hint" style="margin-top:6px">Можно указать и папку самой игры. Это одна игра, а не папка со списком игр. Добавлять можно из домашней папки и с подключённых носителей.</div>
  <div id="icand"></div><div class="btns"><button class="ghost" id="icancel">Отмена</button><button id="iok">Добавить</button></div>`);
 $('#icancel').onclick=closeModal;
 const go=async()=>{const v=$('#ipath').value.trim();if(!v){toast('Введи путь','err');return;}const j=await api('/api/game/import',{path:v});if(j.ok){closeModal();toast(importNote(j),'ok');openGame(j.path);}};
 $('#iok').onclick=go;$('#ipath').addEventListener('keydown',e=>{if(e.key==='Enter')go();});
 try{const r=await(await fetch('/api/game/import/scan')).json();const c=r.candidates||[];
  if(c.length)$('#icand').innerHTML=`<div class="sect">Уже в Steam, но не в DeckDrop</div>`
   +c.map(x=>`<div class="vn" style="cursor:default"><div style="flex:1;min-width:0"><b>${esc(x.name||x.dir.split('/').pop())}</b><small>${esc(x.dir)}${x.exists?'':' · файла нет на месте'}</small></div><button class="ghost sm" data-imppick="${esc(x.dir)}" ${x.exists?'':'disabled'}>добавить</button></div>`).join('');
 }catch(e){}};
// ---- add to Steam: pick a name (when there are several candidates) and Proton for this game
async function addFlow(game,exe){const g=((lastState&&lastState.games)||[]).find(x=>x.path===game);const x=g&&g.exes.find(e=>e.exe===exe);const own=(x&&(x.clean_name||x.name))||'';
 const names=[own,...((g&&g.names)||[])].filter((n,i,a)=>n&&a.findIndex(m=>m.toLowerCase()===n.toLowerCase())===i);let name=own,tool=null;
 if(names.length>1||!own){const st=(lastState&&lastState.settings)||{};const linux=!!(x&&x.linux);
  const b=openModal(`<h3>Добавить в Steam</h3><p>Имя в библиотеке. Варианты: из имени файла, других файлов игры и папки.</p><label>Название</label><input id="anm" type="text" value="${esc(own)}" autocomplete="off"><div class="chipsel">${names.map(n=>`<button type="button" data-pick="${esc(n)}">${esc(n)}</button>`).join('')}</div>`
   +(linux?'<div class="hint">Linux-сборка: пойдёт нативно, без Proton.</div>':`<label>Proton для этой игры</label><select id="atool">${compatOptions(st.default_compat).map(o=>`<option value="${esc(o.value)}" ${o.value===(st.default_compat||'')?'selected':''}>${esc(o.label)}</option>`).join('')}</select>`)
   +`<div class="btns"><button class="ghost" id="acancel">Отмена</button><button id="aok">Добавить</button></div>`);
  b.querySelectorAll('[data-pick]').forEach(c=>c.onclick=()=>{$('#anm').value=c.dataset.pick;$('#anm').focus();});
  const res=await new Promise(r=>{$('#acancel').onclick=()=>{closeModal();r(null);};$('#aok').onclick=()=>{const v={name:$('#anm').value.trim(),tool:linux?'':$('#atool').value};closeModal();r(v);};
   $('#anm').addEventListener('keydown',e=>{if(e.key==='Enter')$('#aok').click();});$('#modal').onclick=e=>{if(e.target===$('#modal')){closeModal();r(null);}};});
  if(!res)return;name=res.name||own;tool=res.tool;}
 const k=game+'|'+exe;busy.add(k);render(lastState);const body={game,exe,name};if(tool!==null)body.tool=tool;const j=await api('/api/add_to_steam',body);busy.delete(k);if(j.ok){toast(j.note,'ok');if(view.kind==='game'){extrasFor=null;sigGame='';}}}
// ---- VNDB picker: a whole cover set for the game, or one picked image for one slot
async function vndbPicker(game,exe,name,after,slot){
 openModal(`<h3>${slot?`VNDB: картинка для слота «${LAB[slot][0]}»`:'Обложки с VNDB'}</h3><p>${slot?'Найди новеллу, затем выбери обложку или любой скриншот.':'Найди новеллу и выбери её. Обложка встанет в вертикальный слот, скриншот в широкий и hero, логотип нарисуется текстом, иконка возьмётся из exe.'} Экспериментально.</p><div class="row"><input id="vq" type="text" value="${esc(name)}"><button id="vgo" class="sm">Искать</button></div><div id="vres"></div><div class="btns"><button class="ghost" id="vcancel">Закрыть</button></div>`);
 $('#vcancel').onclick=closeModal;
 const applyAll=async(vn)=>{closeModal();const k='art'+game+'|'+exe;busy.add(k);render(lastState);await api('/api/art',{game,exe,source:'vndb',vn});toast('Ставлю обложки с VNDB…','ok');setTimeout(()=>{busy.delete(k);refresh();if(after)after();},8000);};
 const showImages=async(vn,vtitle)=>{$('#vres').innerHTML='<div class="empty">загружаю картинки…</div>';
  try{const r=await(await fetch('/api/vndb/images?vn='+encodeURIComponent(vn))).json();if(r.error){$('#vres').innerHTML=`<div class="empty err">${esc(r.error)}</div>`;return;}
   $('#vres').innerHTML=`<div class="hint" style="margin:10px 0 4px">${esc(vtitle)} · нажми картинку, она встанет в слот «${LAB[slot][0]}»</div><div class="vimgs">${(r.images||[]).map(im=>`<div class="vimg" data-url="${esc(im.url)}"><img src="${esc(im.url)}" loading="lazy" alt=""><small>${im.kind==='cover'?'обложка':'скриншот'}${im.dims?' · '+im.dims.join('×'):''}</small></div>`).join('')||'<div class="empty">картинок нет</div>'}</div><div class="btns"><button class="ghost sm" id="vback">← другая новелла</button><button class="ghost sm" id="vall">все слоты с этой новеллы</button></div>`;
   $('#vback').onclick=search;$('#vall').onclick=()=>applyAll(vn);
   $('#vres').querySelectorAll('.vimg').forEach(el=>el.onclick=async()=>{closeModal();toast('Ставлю картинку…');const j=await api('/api/art/from_url',{game,exe,slot,url:el.dataset.url,vn});if(j.ok){toast('Обложка заменена','ok');if(after)after();}});
  }catch(e){$('#vres').innerHTML='<div class="empty err">ошибка сети</div>';}};
 const search=async()=>{$('#vres').innerHTML='<div class="empty">ищу…</div>';try{const r=await(await fetch('/api/vndb/search?q='+encodeURIComponent($('#vq').value))).json();if(r.error){$('#vres').innerHTML=`<div class="empty err">${esc(r.error)}</div>`;return;}
  $('#vres').innerHTML=(r.results||[]).map(v=>`<div class="vn" data-vn="${esc(v.id)}" data-title="${esc(v.title||'')}">${v.image?`<img src="${esc(v.image)}" loading="lazy" alt="">`:'<div class="ph">нет</div>'}<div><b>${esc(v.title)}</b><small>${esc(v.alttitle||'')}${v.released?' · '+esc(v.released):''} · ${esc(v.id)}</small></div></div>`).join('')||'<div class="empty">ничего не нашёл</div>';
  $('#vres').querySelectorAll('.vn').forEach(el=>el.onclick=()=>slot?showImages(el.dataset.vn,el.dataset.title):applyAll(el.dataset.vn));}catch(e){$('#vres').innerHTML='<div class="empty err">ошибка сети</div>';}};
 $('#vgo').onclick=search;$('#vq').addEventListener('keydown',e=>{if(e.key==='Enter')search();});search();}
// ---- archives tab
async function loadArchives(){try{const j=await(await fetch('/api/archives')).json();const a=j.archives||[];$('#archTotal').textContent=a.length?`${a.length} · ${fmt(a.reduce((s,x)=>s+x.size,0))}`:'пусто';
 $('#archives').innerHTML=a.map(x=>`<div class="arch"><div class="nm"><b>${esc(x.name)}</b><div class="path">${esc(x.disk)} · ${fmt(x.size)} · ${when(x.time)}</div></div><span class="acts">${x.extracted?'<span class="pill">распакован</span>':x.archive?`<button class="ghost sm" data-aextract="${esc(x.path)}">распаковать</button>`:x.part?'<span class="pill k">часть архива</span>':'<span class="pill k">файл</span>'}<button class="danger sm" data-adel="${esc(x.path)}" data-name="${esc(x.name)}">удалить</button></span></div>`).join('')||'<div class="empty">Архивов нет.</div>';}catch(e){}}
$('#archCleanup').onclick=async()=>{const r=await ask({title:'Удалить распакованные архивы',text:'Будут удалены только те архивы, для которых уже есть папка игры.',ok:'Удалить',danger:true});if(!r)return;const j=await api('/api/archive/cleanup',{});if(j.ok){toast(j.removed.length?'Удалено: '+j.removed.join(', '):'Нечего удалять','ok');loadArchives();}};
$('#inboxClear').onclick=async()=>{let st={};try{st=await(await fetch('/api/inbox/stats')).json();}catch(e){}
 if(!st.removed){toast(st.skipped?'Во входящих только то, что качается прямо сейчас':'Входящие и так пустые','ok');return;}
 const r=await ask({title:'Очистить входящие',text:`Удалить из _inbox всё: ${st.removed} шт., ${fmt(st.freed)}. Это архивы (распакованные и нет), отдельные файлы и остатки недокачанного. Папки игр не трогаются.`+(st.skipped?` То, что качается прямо сейчас (${st.skipped}), останется.`:''),ok:'Удалить всё',danger:true});if(!r)return;
 const j=await api('/api/inbox/clear',{});if(j.ok){toast(`Удалено: ${j.removed} · освобождено ${fmt(j.freed)}`,'ok');loadArchives();}};
// ---- settings tab (each control saves on change)
function fillSettings(){const s=lastState;if(!s)return;const st=s.settings||{},c=s.cdp||{};
 $('#sCdpTxt').textContent=c.available?'Управление Steam активно: имена, Proton и обложки применяются сразу.':c.marker&&c.enabled?'Управление Steam включится после одной перезагрузки дека. До этого имена и Proton встают в очередь и применятся сами.':c.enabled?'Папка Steam не найдена, управление Steam недоступно.':'Управление Steam выключено: игры добавляются через steamos-add-to-steam, имя и Proton придётся ставить вручную.';
 $('#sCompat').innerHTML=compatOptions(st.default_compat).map(o=>`<option value="${esc(o.value)}" ${o.value===(st.default_compat||'')?'selected':''}>${esc(o.label)}</option>`).join('');
 $('#sLinux').checked=!!st.prefer_linux;$('#sCef').checked=!!st.cef_enabled;$('#sVndb').checked=!!st.vndb_auto;$('#sSafe').checked=!st.vndb_nsfw;
 if(document.activeElement!==$('#sPw'))$('#sPw').value=(st.archive_passwords||[]).join(', ');if(document.activeElement!==$('#sUpd'))$('#sUpd').value=st.update_url||'';
 $('#sProxyView').textContent=st.proxy_masked||'не задан';$('#sProxyDl').checked=!!st.proxy_downloads;$('#sMega').checked=!!st.mega_verify;
 $('#sInfo').innerHTML=`DeckDrop v${esc(s.version)} · ${s.ffmpeg?'ffmpeg найден':'ffmpeg не найден'}<br>${(s.urls||[]).map(esc).join(' · ')}<br>`+(s.disks||[]).map(d=>`${esc(d.label)}: ${fmt(d.free)} свободно из ${fmt(d.total)} · <code>${esc(d.root)}</code>`).join('<br>')+(s.pending?`<br>В очереди до включения управления Steam: ${s.pending}`:'');}
document.querySelectorAll('#pgSettings [data-set]').forEach(el=>el.addEventListener('change',async()=>{const k=el.dataset.set;let v=el.type==='checkbox'?el.checked:el.value;if(el.dataset.invert)v=!v;
 if(k==='archive_passwords')v=v.split(',').map(x=>x.trim()).filter(Boolean);const j=await api('/api/settings',{[k]:v});if(j.ok){toast('Сохранено','ok');lastState.settings=j.settings;lastState.cdp=j.cdp;fillSettings();}}));
$('#sProxyEdit').onclick=async()=>{const p=await ask({title:'Настройки прокси',text:'Адрес прокси может содержать логин и пароль, поэтому он под PIN.',fields:[{label:'PIN',type:'password',numeric:true}],ok:'Показать'});if(!p)return;
 const rv=await api('/api/settings/reveal',{pin:p[0]});if(!rv.ok)return;
 const r=await ask({title:'Прокси для запросов DeckDrop',text:'socks5://хост:порт или http://хост:порт, можно с логином. Пустое поле выключает прокси.',fields:[{label:'Адрес',value:rv.proxy||'',placeholder:'socks5://192.168.1.10:10808'}],ok:'Сохранить'});if(!r)return;
 const j=await api('/api/settings',{proxy:r[0],pin:p[0]});if(j.ok){toast(r[0]?'Прокси сохранён':'Прокси выключен','ok');lastState.settings=j.settings;fillSettings();}};
$('#sTest').onclick=async()=>{const b=$('#sTest');b.disabled=true;b.textContent='проверяю…';$('#sTestRes').textContent='';
 try{const j=await(await fetch('/api/vndb/test')).json();
  $('#sTestRes').innerHTML=(j.proxy?`Прокси: <code>${esc(j.proxy)}</code><br>`:'Прокси не задан<br>')
   +j.checks.map(c=>`${c.ok?'✅':'❌'} ${esc(c.host)} ${esc(c.mode)}: ${c.ok?(c.ms+' мс'+(c.note?' · '+esc(c.note):'')):esc(c.error)}`).join('<br>');
  }catch(e){$('#sTestRes').textContent='не смог проверить';}
 b.disabled=false;b.textContent='Проверить связь с VNDB';};
$('#sPinGo').onclick=async()=>{const j=await api('/api/settings/pin',{old:$('#sPinOld').value,new:$('#sPinNew').value});if(j.ok){toast('PIN изменён','ok');$('#sPinOld').value=$('#sPinNew').value='';}};
// ---- media
async function mediaEnter(){if(mediaToken){loadMedia();return;}const st=await(await fetch('/api/media/status')).json();const a=$('#mediaAuth');$('#mediaBody').hidden=true;
 if(!st.set){a.innerHTML=`<div class="card auth"><div class="lock">🔐</div><h3 style="margin:0">Первый вход в галерею</h3><p>Придумай пароль. Его будут спрашивать при каждом открытии вкладки. Сбросить можно кнопкой внизу страницы по PIN.</p>
  <div class="row"><input id="pw1" type="password" placeholder="пароль, от 4 символов"></div><div class="row"><input id="pw2" type="password" placeholder="ещё раз"><button id="pwset">Сохранить</button></div></div>`;
  $('#pwset').onclick=async()=>{const p1=$('#pw1').value,p2=$('#pw2').value;if(p1!==p2){toast('Пароли не совпадают','err');return;}const r=await fetch('/api/media/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:p1})});const j=await r.json();if(!r.ok){toast(j.error,'err');return;}mediaToken=j.token;mediaOpen();};
  $('#pw2').addEventListener('keydown',e=>{if(e.key==='Enter')$('#pwset').click();});}
 else{a.innerHTML=`<div class="card auth"><div class="lock">🔒</div><h3 style="margin:0">Галерея дека</h3><p>Скриншоты и записи Steam, экспортированные видео и картинки.</p><div class="row"><input id="pw" type="password" placeholder="пароль медиа"><button id="pwgo">Войти</button></div></div>`;
  const go=async()=>{const r=await fetch('/api/media/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('#pw').value})});const j=await r.json();if(!r.ok){toast(j.error,'err');return;}mediaToken=j.token;mediaOpen();};
  $('#pwgo').onclick=go;$('#pw').addEventListener('keydown',e=>{if(e.key==='Enter')go();});setTimeout(()=>$('#pw').focus(),60);}}
async function mediaOpen(){$('#mediaAuth').innerHTML='';$('#mediaBody').hidden=false;await loadMedia();}
async function loadMedia(rescan){const r=await fetch('/api/media/list'+(rescan?'?refresh=1':''),{headers:{'X-Media-Token':mediaToken}});if(r.status===401){mediaToken=null;mediaEnter();return;}const j=await r.json();mediaItems=j.items;
 $('#mhint').textContent=mediaItems.length+' файлов'+(j.ffmpeg?'':' · клипы Steam без ffmpeg склеиваются без звука');renderMedia();}
function renderMedia(){const q=$('#mq').value.trim().toLowerCase();const list=mediaItems.filter(i=>(mediaFilter==='all'||(mediaFilter==='image'?i.kind==='image':i.kind!=='image'))&&(!q||(i.game+' '+i.name).toLowerCase().includes(q)));
 $('#mgrid').innerHTML=list.slice(0,400).map(i=>`<div class="tile" data-id="${i.id}"><img loading="lazy" src="/media/${i.id}/thumb?t=${mediaToken}" alt=""><span class="k${i.kind==='image'?'':' v'}">${i.kind==='image'?'фото':i.kind==='clip'?'клип':'видео'}</span><div class="cap"><b>${esc(i.game)}</b><span class="muted">${when(i.time)} · ${fmt(i.size)}</span></div></div>`).join('')||'<div class="empty">Ничего не найдено.</div>';
 if(list.length>400)$('#mhint').textContent+=' · показаны первые 400';}
$('#mq').oninput=renderMedia;$('#mrefresh').onclick=()=>loadMedia(true);
document.querySelectorAll('.chips button[data-f]').forEach(b=>b.onclick=()=>{mediaFilter=b.dataset.f;document.querySelectorAll('.chips button[data-f]').forEach(x=>x.classList.toggle('on',x===b));renderMedia();});
$('#mgrid').addEventListener('click',e=>{const t=e.target.closest('.tile');if(!t)return;const i=mediaItems.find(x=>x.id===t.dataset.id);if(!i)return;viewing=i;
 const src=`/media/${i.id}?t=${mediaToken}`;$('#vtitle').textContent=i.game+' · '+i.name;$('#vopen').href=src;$('#vdl').href=src+'&dl=1';$('#vdl').setAttribute('download',i.name);
 $('#vbody').innerHTML=i.kind==='image'?`<img src="${src}">`:`<video src="${src}" controls playsinline autoplay></video>`;$('#viewer').classList.add('on');});
function closeViewer(){$('#viewer').classList.remove('on');$('#vbody').innerHTML='';viewing=null;}
$('#vclose').onclick=closeViewer;$('#viewer').addEventListener('click',e=>{if(e.target.id==='viewer'||e.target.id==='vbody')closeViewer();});
$('#vdel').onclick=async()=>{if(!viewing)return;const i=viewing;const r=await ask({title:'Удалить с дека',text:`${i.game} · ${i.name} (${fmt(i.size)}) будет удалён безвозвратно.${i.kind==='clip'?' Steam может показывать пустую запись до перезапуска.':''}`,fields:[{label:'PIN',type:'password',numeric:true}],ok:'Удалить',danger:true});if(!r)return;
 const j=await api('/api/media/delete',{id:i.id,pin:r[0],token:mediaToken});if(j.ok){toast('Удалено: '+j.removed,'ok');closeViewer();loadMedia(true);}};
$('#mediaReset').onclick=async()=>{const r=await ask({title:'Сбросить пароль галереи',text:'При следующем входе попросит придумать новый.',fields:[{label:'PIN',type:'password',numeric:true}],ok:'Сбросить',danger:true});if(!r)return;
 const j=await api('/api/media/reset',{pin:r[0]});if(j.ok){mediaToken=null;toast('Пароль сброшен','ok');if(tab==='media')mediaEnter();}};
// ---- self update
$('#update').onclick=async()=>{const r=await ask({title:'Обновить утилиту',text:'Ссылка на свежий deckdrop.py: GitHub или своя раздача с ПК (python -m http.server 8000 в папке с файлом).',fields:[{label:'Откуда взять свежий deckdrop.py',value:(lastState&&lastState.settings&&lastState.settings.update_url)||''},{label:'PIN',type:'password',numeric:true}],ok:'Обновить'});if(!r)return;
 const b=$('#update');b.disabled=true;b.textContent='обновляю…';const old=lastState&&lastState.version;
 const j=await api('/api/update',{url:r[0],pin:r[1]});
 if(!j.ok){b.disabled=false;b.textContent='Обновить утилиту';return;}
 toast(j.note,'ok');b.textContent=j.note;if(!/->/.test(j.note)){setTimeout(()=>{b.disabled=false;b.textContent='Обновить утилиту';},3000);return;}
 const np=j.new_port&&String(j.new_port)!==(location.port||'80')?j.new_port:null;const target=np?`${location.protocol}//${location.hostname}:${np}/`:null;
 if(target){toast('Новый адрес: '+target,'ok');setTimeout(()=>location.href=target,4000);return;}
 let n=0;const t=setInterval(async()=>{n++;try{const s=await(await fetch('/api/state')).json();if(s.version!==old){clearInterval(t);b.textContent='готово: v'+s.version;toast('Обновлено до v'+s.version,'ok');setTimeout(()=>location.reload(),1500);}}catch(e){}if(n>40){clearInterval(t);b.disabled=false;b.textContent='Обновить утилиту';}},1000);};
</script></body></html>"""

# --------------------------------------------------------------------------- service install

UNIT = """[Unit]
Description=DeckDrop LAN inbox
After=network-online.target

[Service]
ExecStart={python} {script}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def install():
    load_state()
    ensure_pin()
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "deckdrop.service").write_text(
        UNIT.format(python=sys.executable, script=Path(__file__).resolve()))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "deckdrop.service"], check=True)
    # restart, not `enable --now`: on an upgrade the unit is already active and
    # would keep running the old code
    subprocess.run(["systemctl", "--user", "restart", "deckdrop.service"], check=True)
    print(f"DeckDrop {__version__} installed and running:", " ".join(local_urls()))
    print(f"Admin PIN: {STATE['admin_pin']} (change it on the Settings tab)")


def uninstall():
    subprocess.run(["systemctl", "--user", "disable", "--now", "deckdrop.service"])
    try:
        (Path.home() / ".config" / "systemd" / "user" / "deckdrop.service").unlink()
    except OSError:
        pass
    print("DeckDrop removed")


def main():
    if "--install" in sys.argv:
        return install()
    if "--uninstall" in sys.argv:
        return uninstall()
    load_state()
    if ensure_pin():
        log(f"new admin PIN: {STATE['admin_pin']} (change it on the Settings tab)")
    (default_root() / "_inbox").mkdir(parents=True, exist_ok=True)
    CDP.ensure_marker()
    threading.Thread(target=pending_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    st = CDP.status()
    log(f"DeckDrop {__version__} listening on " + " ".join(local_urls())
        + f"  (games: {GAMES_DIR}, steam: {STEAM_ROOT}, ffmpeg: {'yes' if FFMPEG else 'no'}, "
        + f"steam control: {'live' if st['available'] else ('after Steam restart' if st['marker'] else 'off')})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
