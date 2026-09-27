#!/usr/bin/env python3
"""Smoke test: start deckdrop.py in a throwaway home and poke the read-only endpoints.

Also guards the contract that already-installed copies rely on when they update
themselves: they accept a new file only if it says "DeckDrop" near the top, and
they read __version__ and the default port from it with regexes.

    python3 tests/smoke.py [path/to/deckdrop.py]
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

SCRIPT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "deckdrop.py")


def check_update_contract(data):
    assert b"DeckDrop" in data[:3000], "self_update of old copies needs 'DeckDrop' in the first 3000 bytes"
    assert re.search(rb'__version__\s*=\s*"([^"]+)"', data), "__version__ line not found"
    assert re.search(rb'DECKDROP_PORT",\s*"(\d+)"', data), "default port line not found"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return r.status, r.read()


def main():
    check_update_contract(SCRIPT.read_bytes())
    port = free_port()
    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ, HOME=home, DECKDROP_PORT=str(port), DECKDROP_CEF="0",
                   DECKDROP_STATE=str(Path(home) / "state.json"), DECKDROP_GAMES=str(Path(home) / "Games"),
                   DECKDROP_STEAM=str(Path(home) / "no-steam"), DECKDROP_PIN="1234")
        proc = subprocess.Popen([sys.executable, str(SCRIPT)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 20
            while True:
                try:
                    get(port, "/api/state")
                    break
                except OSError:
                    if proc.poll() is not None or time.time() > deadline:
                        raise SystemExit("server did not start:\n" + proc.stdout.read().decode(errors="replace"))
                    time.sleep(0.3)
            status, body = get(port, "/")
            assert status == 200 and b"DeckDrop" in body, "main page"
            for path in ("/api/state", "/api/archives", "/api/inbox/stats", "/api/media/status"):
                status, body = get(port, path)
                assert status == 200, path
                json.loads(body)
            print(f"smoke ok: {SCRIPT.name} on port {port}")
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
            if proc.returncode not in (0, -15, None):
                print(proc.stdout.read().decode(errors="replace"))


if __name__ == "__main__":
    main()
