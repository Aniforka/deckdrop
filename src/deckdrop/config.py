"""Settings from the environment, fixed paths and constants, logging."""

import collections
import os
import re
import shutil
import sys
import time
from pathlib import Path

from . import __version__


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
STARTED = time.time()
RECENT_LOG = collections.deque(maxlen=300)   # (time, line) for Settings -> Performance -> self-check


def log(msg):
    RECENT_LOG.append((time.time(), msg))
    sys.stderr.write(time.strftime("%H:%M:%S ") + msg + "\n")
    sys.stderr.flush()
