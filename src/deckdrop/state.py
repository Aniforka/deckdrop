"""Persistent state (state.json), admin PIN, media gallery password and tokens."""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from .config import CEF_ENABLED, STATE_FILE, UPDATE_URL_DEFAULT, UPDATE_URL_LEGACY
from .i18n import tr


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
    try:
        if STATE_FILE.exists() and STATE_FILE.stat().st_mode & 0o077:
            STATE_FILE.chmod(0o600)               # earlier versions left it readable by everyone
    except OSError:
        pass
    if str(STATE.get("update_url") or "").strip().lower() in UPDATE_URL_LEGACY:
        STATE["update_url"] = UPDATE_URL_DEFAULT


def save_state():
    # the file holds the admin PIN, archive passwords and proxy credentials: owner only
    with STATE_LOCK:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(STATE, ensure_ascii=False, indent=1))
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


class Throttle:
    """Slows down guessing a secret, across all connections at once.

    Every wrong answer costs a second. After FREE wrong answers in a row the next try is refused
    outright for a while, doubling up to a minute, so a 4-digit PIN takes days, not an hour.
    The right answer resets it.
    """
    FREE = 5
    MAX_WAIT = 60

    def __init__(self):
        self.lock = threading.Lock()
        self.fails = 0
        self.until = 0.0

    def check(self, ok_fn, wrong_msg):
        with self.lock:
            wait = self.until - time.time()
        if wait > 0:
            time.sleep(1)
            raise PermissionError(tr("err.too_many_attempts", s=int(wait) + 1))
        if ok_fn():
            with self.lock:
                self.fails, self.until = 0, 0.0
            return
        with self.lock:
            self.fails += 1
            if self.fails >= self.FREE:
                self.until = time.time() + min(self.MAX_WAIT, 2 ** (self.fails - self.FREE))
        time.sleep(1)
        raise PermissionError(wrong_msg)


PIN_THROTTLE = Throttle()
MEDIA_THROTTLE = Throttle()


def check_pin(pin):
    stored = str(STATE.get("admin_pin") or "")
    given = str(pin or "")
    PIN_THROTTLE.check(lambda: bool(stored) and hmac.compare_digest(given.encode("utf-8"), stored.encode("utf-8")),
                       tr("err.wrong_pin"))


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
        raise PermissionError(tr("media.pw_not_set"))
    MEDIA_THROTTLE.check(lambda: hmac.compare_digest(pw_hash(password, rec["salt"])["hash"], rec["hash"]),
                         tr("err.wrong_password"))
    token = secrets.token_urlsafe(24)
    now = time.time()
    for t, exp in list(MEDIA_TOKENS.items()):
        if exp < now:
            del MEDIA_TOKENS[t]
    MEDIA_TOKENS[token] = now + 12 * 3600
    return token


def media_token_ok(token):
    return bool(token) and MEDIA_TOKENS.get(token, 0) > time.time()
