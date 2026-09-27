"""Install/uninstall as a systemd user service."""

import subprocess
import sys
from pathlib import Path

from . import __version__, bundle
from .i18n import tr
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
        raise SystemExit("the service is not installed from src/: build the file (python tools/build.py) "
                         "and run python3 dist/deckdrop.py --install")
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
    print(tr("install.running", v=__version__, urls=" ".join(local_urls())))
    print(tr("install.pin", pin=STATE["admin_pin"]))


def uninstall():
    subprocess.run(["systemctl", "--user", "disable", "--now", "deckdrop.service"])
    try:
        (Path.home() / ".config" / "systemd" / "user" / "deckdrop.service").unlink()
    except OSError:
        pass
    print(tr("install.removed"))
