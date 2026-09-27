"""mega.nz: link parsing, API, folder listings, decrypting downloads."""

import base64
import hmac
import http.client
import json
import re
import secrets
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.parse
from pathlib import Path

from .aes import AesCtr, ZERO16, aes_cbc_decrypt, aes_cbc_encrypt, aes_ecb_decrypt
from .archives import finish, flatten
from .config import CHUNK, log
from .jobs import drop_part, fail
from .net import dl_proxy, net_open, net_reason, proxy_url
from .paths import archive_volume, reserve_path, safe_name, split_ext, unique_dir
from .state import STATE
from .storage import inside


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
