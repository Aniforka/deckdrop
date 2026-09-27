"""What a game folder holds: executables and a human name for the game."""

import os
import re
import threading
import time
from pathlib import Path

from .config import LINUX_EXTS, SKIP_EXE
from .i18n import tr
from .state import STATE
from .storage import inside_any


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
        raise ValueError(tr("err.bad_path"))
    return p


def norm_title(s):
    return re.sub("[^a-z0-9\u0430-\u044f\u0451]+", "", s.lower())   # keeps Cyrillic titles


EXE_PATTERNS = ("*.exe", "*/*.exe", "*.sh", "*/*.sh", "*.x86_64", "*/*.x86_64")
# The page asks for the game list every second, and globbing a game means listing every folder
# right under it (a Unity game's _Data alone holds thousands of files). The patterns only look at
# the game folder and its direct subfolders, and a folder's mtime changes whenever an entry is
# added, removed or renamed in it, so those mtimes tell exactly when a game must be looked at again.
_EXES = {}                  # game dir -> (fingerprint, exes)
_EXES_LOCK = threading.Lock()
SETTLE = 3.0                # a folder changed this recently is not cached: coarse clocks (FAT: 2 s)


def _fingerprint(d):
    """mtimes of the game folder and its subfolders, or None when it cannot be cached yet."""
    stamps = [os.stat(d).st_mtime_ns]
    with os.scandir(d) as entries:
        for e in entries:
            try:
                if e.is_dir():
                    stamps.append((e.name, e.stat().st_mtime_ns))
            except OSError:
                continue
    newest = max(s if isinstance(s, int) else s[1] for s in stamps)
    if time.time() - newest / 1e9 < SETTLE:
        return None
    return tuple(stamps[:1] + sorted(stamps[1:]))


def _glob_exes(d):
    exes = []
    for pattern in EXE_PATTERNS:
        for p in d.glob(pattern):
            if not SKIP_EXE.search(p.name):
                exes.append(p.relative_to(d).as_posix())
    return sorted(set(exes))[:25]


def find_exes(d):
    """Launchable files of a game, relative to its folder; looked up again only when it changed."""
    d = Path(d)
    key = str(d)
    try:
        fp = _fingerprint(d)
    except OSError:
        fp = None
    if fp is not None:
        with _EXES_LOCK:
            hit = _EXES.get(key)
        if hit and hit[0] == fp:
            return list(hit[1])
    exes = _glob_exes(d)
    with _EXES_LOCK:
        if fp is None:
            _EXES.pop(key, None)
        else:
            if len(_EXES) > 5000:             # games come and go; never let it grow without bound
                _EXES.clear()
            _EXES[key] = (fp, exes)
    return list(exes)


def recommend(exes):
    linux = [e for e in exes if is_linux_exe(e)]
    win = [e for e in exes if not is_linux_exe(e)]
    pool = (linux or win) if STATE.get("prefer_linux", True) else (win or linux)
    return min(pool, key=lambda e: (e.count("/"), len(e))) if pool else None
