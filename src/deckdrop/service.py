"""Install/uninstall as a systemd user service."""

import subprocess
import sys
from pathlib import Path

from . import __version__, bundle
from .state import STATE, ensure_pin, load_state
from .web.server import local_urls


UNIT = """[Unit]
Description=DeckDrop LAN inbox
After=network-online.target

[Service]
ExecStart={python} {script}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def install():
    if bundle.PATH is None:
        raise SystemExit("из исходников сервис не ставится: собери файл (python tools/build.py) "
                         "и запусти python3 dist/deckdrop.py --install")
    load_state()
    ensure_pin()
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "deckdrop.service").write_text(
        UNIT.format(python=sys.executable, script=Path(bundle.PATH).resolve()))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "deckdrop.service"], check=True)
    # restart, not `enable --now`: on an upgrade the unit is already active and
    # would keep running the old code
    subprocess.run(["systemctl", "--user", "restart", "deckdrop.service"], check=True)
    print(f"DeckDrop {__version__} installed and running:", " ".join(local_urls()))
    print(f"Admin PIN: {STATE['admin_pin']} (change it on the Settings tab)")


def uninstall():
    subprocess.run(["systemctl", "--user", "disable", "--now", "deckdrop.service"])
    try:
        (Path.home() / ".config" / "systemd" / "user" / "deckdrop.service").unlink()
    except OSError:
        pass
    print("DeckDrop removed")
