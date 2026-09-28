"""Entry point: start the server and the background loops."""

import sys
import threading
from http.server import ThreadingHTTPServer

from . import __version__
from .config import FFMPEG, GAMES_DIR, PORT, STEAM_ROOT, log
from .diagnostics import PENDING_THREAD
from .service import install, uninstall
from .state import STATE, ensure_pin, load_state
from .steam.cdp import CDP
from .steam.layouts import sync_templates
from .steam.shortcuts import pending_loop
from .storage import default_root
from .web.server import Handler, local_urls


def main():
    if "--install" in sys.argv:
        return install()
    if "--uninstall" in sys.argv:
        return uninstall()
    load_state()
    if ensure_pin():
        log(f"new admin PIN: {STATE['admin_pin']} (change it on the Settings tab)")
    (default_root() / "_inbox").mkdir(parents=True, exist_ok=True)
    CDP.ensure_marker()
    try:
        sync_templates()          # a Steam update may have wiped the layouts DeckDrop offers as templates
    except Exception as e:  # noqa: BLE001
        log(f"layout templates: {e}")
    threading.Thread(target=pending_loop, name=PENDING_THREAD, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    st = CDP.status()
    log(f"DeckDrop {__version__} listening on " + " ".join(local_urls())
        + f"  (games: {GAMES_DIR}, steam: {STEAM_ROOT}, ffmpeg: {'yes' if FFMPEG else 'no'}, "
        + f"steam control: {'live' if st['available'] else ('after Steam restart' if st['marker'] else 'off')})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
