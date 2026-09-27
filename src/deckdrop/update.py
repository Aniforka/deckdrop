"""Self update from a release (or any URL) and restart."""

import ipaddress
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
from pathlib import Path

from . import __version__, bundle
from .config import PORT, log
from .i18n import tr
from .net import http_get


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


def self_update(url):
    if bundle.PATH is None:
        raise RuntimeError(tr("update.from_source"))
    script = Path(bundle.PATH).resolve()
    try:
        with http_get(url, 30) as r:
            if not update_url_ok(r.geturl()):        # redirected from https to plain http elsewhere
                raise RuntimeError(tr("update.need_https"))
            data = r.read()
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(tr("update.download_failed", url=url, error=e)) from e
    if b"DeckDrop" not in data[:3000]:
        raise RuntimeError(tr("update.not_deckdrop"))
    m = re.search(rb'__version__\s*=\s*"([^"]+)"', data)
    ver = m.group(1).decode() if m else "?"
    m = re.search(rb'DECKDROP_PORT",\s*"(\d+)"', data)
    new_port = int(m.group(1)) if m else PORT
    if data == script.read_bytes():
        return tr("update.up_to_date", v=ver), new_port, False
    new = script.with_suffix(".py.new")
    new.write_bytes(data)
    res = subprocess.run([sys.executable, "-m", "py_compile", str(new)], capture_output=True, text=True)
    if res.returncode != 0:
        new.unlink(missing_ok=True)
        raise RuntimeError(tr("update.bad_file", error=res.stderr.strip()[-300:]))
    shutil.copy2(script, script.with_suffix(".py.bak"))
    os.replace(new, script)
    try:
        script.chmod(0o755)
    except OSError:
        pass
    if os.environ.get("DECKDROP_NO_RESTART") != "1":
        threading.Timer(1.0, restart_self).start()
    return tr("update.updated", old=__version__, new=ver), new_port, True


def restart_self():
    log("restarting")
    if os.environ.get("INVOCATION_ID") and shutil.which("systemctl"):
        subprocess.Popen(["systemctl", "--user", "restart", "deckdrop.service"])
        return
    os.execv(sys.executable, [sys.executable] + sys.argv)
