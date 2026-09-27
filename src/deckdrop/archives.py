"""Unpacking archives (with passwords) and the archives (inbox) tab."""

import os
import re
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path

from .config import AUTO_EXTRACT, CHUNK, KEEP_ARCHIVE, log
from .jobs import ACTIVE, JOBS, LOCK, fail, new_job
from .paths import archive_stem, archive_volume, unique_dir
from .state import STATE, STATE_LOCK, save_state
from .storage import disk_label_for, game_roots, inside_any


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
