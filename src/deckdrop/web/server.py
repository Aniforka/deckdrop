"""HTTP server: the page and the JSON API."""

import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .. import __version__, i18n
from ..archives import (
    archive_cleanup, archive_delete, archive_extract_job, finish, inbox_clear, job_password,
    list_archives,
)
from ..art.covers import (
    art_current, art_file_for, art_from_exe, art_from_url, art_worker, custom_art, proxy_test,
    vndb_image_list, vndb_search,
)
from ..config import CACHE_DIR, CHUNK, FFMPEG, GAMES_DIR, MIME, PORT, UPDATE_URL_DEFAULT, log
from ..detect import game_exe_path
from ..downloads import cancel_all, cancel_job, start_download, start_mega_downloads
from ..games import (
    add_to_steam, delete_game, game_info, import_candidates, import_game, list_games,
    set_hidden, unimport_game,
)
from ..i18n import carry, tr
from ..jobs import JOBS, LOCK, fail, new_job
from ..media import PLACEHOLDER_SVG, clip_mp4, media_delete, media_item, media_thumb, scan_media
from ..mega import mega_probe
from ..net import mask_proxy
from ..patches import (
    _patch_drop, game_dir_list, game_file_upload, patch_apply, patch_unlock, patch_upload,
)
from ..paths import reserve_path, safe_name
from ..saves import build_saves_zip, import_saves_zip, saves_info
from ..state import (
    MEDIA_TOKENS, PROTECTED_KEYS, SETTING_KEYS, STATE, check_pin, media_login, media_token_ok,
    pw_hash, set_state,
)
from ..steam.cdp import CDP
from ..steam.compat import compat_tools
from ..steam.shortcuts import rename_shortcut, set_compat_for
from ..storage import disks, root_for
from ..update import self_update
from .page import csp, render


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


# names a page on the home network is opened by; anything else in Host is a DNS rebinding attempt
LAN_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".intranet", ".localdomain",
                ".localhost", ".box", ".ts.net")
JSON_LIMIT = 1 << 20
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def extra_hosts():
    """DECKDROP_HOSTS: more names the page may be opened by ("deck.example.org", ".example.org", "*")."""
    return [h.strip().lower() for h in re.split(r"[\s,;]+", os.environ.get("DECKDROP_HOSTS", "")) if h.strip()]


def host_ok(host):
    """Is the Host header an address on the home network, or a name of this Deck?

    Blocks DNS rebinding: a web page on evil.example that re-resolves its own name to the
    Deck's IP could otherwise read and drive the API as if it were on the same site.
    """
    host = (host or "").strip().lower()
    if not host:
        return True                               # HTTP/1.0 clients, curl -H 'Host:'
    m = re.fullmatch(r"\[([0-9a-f:.]+)\](?::\d+)?|([^:\[\]]+)(?::\d+)?", host)
    if not m:
        return False
    name = (m.group(1) or m.group(2)).rstrip(".")
    try:
        ipaddress.ip_address(name)
        return True                               # a bare IP is never a rebinding target
    except ValueError:
        pass
    if not re.fullmatch(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*", name):
        return False
    extra = extra_hosts()
    if "*" in extra:
        return True
    for h in extra:
        if name == h.lstrip(".") or (h.startswith(".") and name.endswith(h)):
            return True
    own = socket.gethostname().lower()
    return ("." not in name or name in (own, own + ".local", "localhost")
            or name.endswith(LAN_SUFFIXES))


def origin_ok(origin, host):
    """A browser request from another site carries its Origin; only this page may change things."""
    if not origin:
        return True                               # not a browser, or a same-origin GET
    try:
        u = urllib.parse.urlparse(origin)
    except ValueError:
        return False
    return bool(u.netloc) and u.netloc.lower() == (host or "").strip().lower()


def update_url_ok(url):
    """Self update runs whatever it downloads: plain http only from the home network."""
    u = urllib.parse.urlparse(url)
    if u.scheme == "https":
        return bool(u.hostname)
    if u.scheme != "http" or not u.hostname:
        return False
    name = u.hostname.lower()
    try:
        ip = ipaddress.ip_address(name)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return "." not in name or name.endswith((".local", ".lan", ".home", ".home.arpa", ".localhost"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 300                                 # a stalled client must not hold a thread forever

    def parse_request(self):
        # every answer, the page and API errors alike, speaks the language of this device
        ok = super().parse_request()
        if ok:
            i18n.set_current(i18n.for_request(self.headers)[0])
            if not self.request_allowed():
                self.close_connection = True
                self.send_json({"error": tr("err.forbidden_origin")}, 403)
                return False
        return ok

    def request_allowed(self):
        host = self.headers.get("Host")
        if not host_ok(host):
            log(f"{self.address_string()} refused: Host {host!r} is not on the home network "
                f"(allow it with DECKDROP_HOSTS)")
            return False
        if self.command not in ("GET", "HEAD") and not origin_ok(self.headers.get("Origin"), host):
            log(f"{self.address_string()} refused: {self.command} from {self.headers.get('Origin')!r}")
            return False
        return True

    def log_message(self, fmt, *args):
        line = args[0] if args else ""
        if "/api/state" not in line and "/thumb" not in line:
            # the gallery token rides in ?t= for <img> and <video>; keep it out of the journal
            msg = re.sub(r"([?&]t=)[^&\s\"]+", r"\1***", fmt % args)
            log(f"{self.address_string()} {msg}")

    # ---- helpers
    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for k, v in dict(SECURITY_HEADERS, **(extra or {})).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def content_length(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0:
            raise ValueError("Content-Length")
        return n

    def read_json(self):
        n = self.content_length()
        if n > JSON_LIMIT:
            raise ValueError(tr("err.too_big"))
        body = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("JSON")
        return body

    def read_body_to(self, path, job=None):
        total = self.content_length()
        remaining = total
        with open(path, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(CHUNK, remaining))
                if not chunk:
                    raise ConnectionError(tr("err.upload_cut"))
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
            self.send_json({"error": tr("media.need_password"), "auth": True}, 401)
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
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
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
                lang = i18n.current()
                self._send(200, render(lang).encode(), "text/html; charset=utf-8",
                           {"Vary": "Cookie", "Cache-Control": "no-cache", "Content-Security-Policy": csp(lang)})
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
                    raise FileNotFoundError(tr("covers.none_in_slot"))
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
                    return self.send_json({"error": tr("saves.not_found")}, 404)
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
            self.send_json({"error": tr("err.not_found", what=e)}, 404)
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
            try:
                body = self.read_json()
            except ValueError:
                self.close_connection = True      # the body may be left unread
                raise
            s = lambda k, d="": str(body.get(k, d) or d)  # noqa: E731
            if path == "/api/download":
                self.send_json(start_download(s("url"), s("disk") or None).to_dict())
            elif path == "/api/mega/list":
                self.send_json({"ok": True, **mega_probe(s("url"))})
            elif path == "/api/mega/download":
                self.send_json({"ok": True, "jobs": start_mega_downloads(
                    s("url"), body.get("nodes") or [], s("disk") or None)})
            elif path == "/api/game/archive/unlock":
                self.send_json({"ok": True, **patch_unlock(s("token"), str(body.get("password") or ""),
                                                           bool(body.get("remember")))})
            elif path == "/api/game/archive/apply":
                self.send_json({"ok": True, **patch_apply(s("token"), bool(body.get("strip")),
                                                          body.get("backup", True) is not False)})
            elif path == "/api/game/archive/discard":
                _patch_drop(s("token"))
                self.send_json({"ok": True})
            elif path == "/api/cancel":
                self.send_json({"ok": cancel_job(int(body.get("id", 0) or 0))})
            elif path == "/api/cancel_all":
                self.send_json({"ok": True, "stopped": cancel_all()})
            elif path == "/api/inbox/clear":
                self.send_json({"ok": True, **inbox_clear()})
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
                threading.Thread(target=carry(art_worker), args=(str(p), True, False, source, s("vn") or None), daemon=True).start()
                self.send_json({"ok": True, "note": tr("covers.making")})
            elif path == "/api/art/from_exe":
                self.send_json({"ok": True, **art_from_exe(s("game"), s("exe"), s("slot"))})
            elif path == "/api/art/from_url":
                self.send_json({"ok": True, **art_from_url(s("game"), s("exe"), s("slot"), s("url"), s("vn") or None)})
            elif path == "/api/game/rename":
                self.send_json({"ok": True, "note": rename_shortcut(s("game"), s("exe"), s("name"))})
            elif path == "/api/game/compat":
                self.send_json({"ok": True, "note": set_compat_for(s("game"), s("exe"), s("tool"))})
            elif path == "/api/game/import":
                self.send_json({"ok": True, **import_game(s("path"))})
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
                    raise PermissionError(tr("media.pw_already_set"))
                pw = s("password")
                if len(pw) < 4:
                    raise ValueError(tr("media.pw_too_short"))
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
                    return self.send_json({"error": tr("media.need_password"), "auth": True}, 401)
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
                    raise ValueError(tr("pin.format"))
                set_state(admin_pin=new)
                self.send_json({"ok": True})
            elif path == "/api/update":
                check_pin(body.get("pin"))
                url = (s("url") or STATE.get("update_url") or UPDATE_URL_DEFAULT).strip()
                if not re.match(r"^https?://", url):
                    raise ValueError(tr("err.need_url"))
                if not update_url_ok(url):
                    raise ValueError(tr("update.need_https"))
                if url != STATE.get("update_url"):
                    set_state(update_url=url)
                note, new_port, updated = self_update(url)
                self.send_json({"ok": True, "note": note, "new_port": new_port, "updated": updated})
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
                job.total = self.content_length()
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
                threading.Thread(target=carry(finish), args=(job, dest), daemon=True).start()
                self.send_json(job.to_dict())
            elif path == "/api/art/upload":
                n = self.content_length()
                if n > 25 << 20:
                    raise ValueError(tr("err.too_big_25"))
                data = self.rfile.read(n)
                self.send_json({"ok": True, **custom_art(self.q1(q, "game"), self.q1(q, "exe"), self.q1(q, "slot"), data)})
            elif path == "/api/game/archive":
                self.send_json({"ok": True, **patch_upload(
                    self.q1(q, "game"), self.q1(q, "exe"), self.q1(q, "dir"), self.q1(q, "name"), self.read_body_to)})
            elif path == "/api/game/file":
                self.send_json({"ok": True, **game_file_upload(
                    self.q1(q, "game"), self.q1(q, "exe"), self.q1(q, "dir"), self.q1(q, "name"),
                    self.q1(q, "replace") == "1", self.read_body_to)})
            elif path == "/api/saves/import":
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = CACHE_DIR / f"import_{secrets.token_hex(4)}.zip"
                try:
                    self.read_body_to(tmp)
                    self.send_json({"ok": True, **import_saves_zip(self.q1(q, "game"), self.q1(q, "exe"), tmp)})
                finally:
                    tmp.unlink(missing_ok=True)
            else:
                self.send_json({"error": "not found"}, 404)
        except FileExistsError as e:
            self.close_connection = True          # the body was not read: don't reuse the connection
            self.send_json({"error": tr("files.exists", name=e), "exists": True}, 409)
        except (ValueError, zipfile.BadZipFile) as e:
            self.close_connection = True
            self.send_json({"error": str(e) or tr("err.bad_zip")}, 400)
        except Exception as e:  # noqa: BLE001
            self.close_connection = True
            log(f"PUT {path} failed: {e!r}")
            try:
                self.send_json({"error": str(e)}, 500)
            except OSError:
                pass                              # the client is already gone, e.g. the phone lost Wi-Fi
