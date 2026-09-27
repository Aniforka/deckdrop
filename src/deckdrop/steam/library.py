"""Steam on disk: userdata, VDF files, shortcuts, library folders, compatdata."""

import re
import struct
import zlib
from pathlib import Path

from ..config import STEAM_ROOT, log
from ..i18n import tr


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
        raise RuntimeError(tr("steam.no_userdata"))
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
