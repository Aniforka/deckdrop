"""Self update from a release (or any URL) and restart."""

import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path

from . import __version__, bundle
from .config import PORT, log
from .net import http_get


def self_update(url):
    if bundle.PATH is None:
        raise RuntimeError("запущено из исходников (src/): обновляй через git, "
                           "кнопка работает только в собранном deckdrop.py")
    script = Path(bundle.PATH).resolve()
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
