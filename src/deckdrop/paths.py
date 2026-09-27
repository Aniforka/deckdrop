"""File names and paths: safe names, archive volumes, unique targets, sizes."""

import re
import urllib.parse
from pathlib import Path

from .config import ARCHIVE_EXTS
from .jobs import LOCK


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
