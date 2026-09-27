"""Live control of the Steam client through its CEF remote debugging port."""

import base64
import hashlib
import json
import os
import socket
import struct
import time
import urllib.parse
import urllib.request

from ..config import CEF_ENABLED, CEF_PORT, STEAM_ROOT, log
from ..i18n import tr
from ..state import STATE


def _ws_mask(data, mask):
    n = len(data)
    words = (n + 3) // 4
    m = int.from_bytes(mask * words, "big")
    d = int.from_bytes(data + b"\0" * (words * 4 - n), "big")
    return (d ^ m).to_bytes(words * 4, "big")[:n]


class WS:
    """Minimal RFC 6455 client: text frames, fragmentation, ping/pong, close."""

    def __init__(self, url, timeout=15):
        u = urllib.parse.urlparse(url)
        host, port = u.hostname, u.port or 80
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket: closed during the handshake")
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raise ConnectionError("websocket: " + status.decode(errors="replace"))
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if accept not in head:
            raise ConnectionError("websocket: bad Sec-WebSocket-Accept")

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket: connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, opcode, payload):
        n = len(payload)
        head = bytearray([0x80 | opcode])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(bytes(head) + mask + _ws_mask(payload, mask))

    def send_text(self, text):
        self._send(0x1, text.encode("utf-8"))

    def recv_text(self):
        message, started = bytearray(), False
        while True:
            b1, b2 = self._read(2)
            fin, opcode, masked, n = b1 & 0x80, b1 & 0x0F, b2 & 0x80, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else None
            data = self._read(n)
            if mask:
                data = _ws_mask(data, mask)
            if opcode == 0x8:
                raise ConnectionError("websocket: closed by the server")
            if opcode == 0x9:
                self._send(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                message, started = bytearray(data), True
            elif opcode == 0x0 and started:
                message += data
            if fin and started:
                return message.decode("utf-8", "replace")

    def close(self):
        try:
            self._send(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class SteamCDP:
    """Drive the running Steam client through its CEF remote-debugging port.

    Enabled by the marker file <steam>/.cef-enable-remote-debugging (what Decky
    Loader does); Steam picks it up on its next start. All calls evaluate JS in
    Steam's SharedJSContext, where the SteamClient API lives.
    """

    def __init__(self, port):
        self.port = port
        self._cache = (0.0, None)

    @property
    def enabled(self):
        return bool(STATE.get("cef_enabled", CEF_ENABLED))

    def marker(self):
        return STEAM_ROOT / ".cef-enable-remote-debugging"

    def ensure_marker(self):
        try:
            if self.enabled and STEAM_ROOT.is_dir() and not self.marker().exists():
                self.marker().touch()
                log(f"created {self.marker()} - Steam control activates after the next Steam restart")
            elif not self.enabled and self.marker().exists():
                self.marker().unlink()
        except OSError as e:
            log(f"cef marker: {e}")

    def target(self, force=False):
        now = time.time()
        if not force and now - self._cache[0] < 10:
            return self._cache[1]
        url = None
        if self.enabled:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=1.5) as r:
                    targets = json.load(r)
                for t in targets:
                    if t.get("title") == "SharedJSContext" and t.get("webSocketDebuggerUrl"):
                        url = t["webSocketDebuggerUrl"]
                        break
                if url is None:
                    for t in targets:
                        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                            url = t["webSocketDebuggerUrl"]
                            break
            except Exception:  # noqa: BLE001
                url = None
        self._cache = (now, url)
        return url

    def available(self):
        return self.target() is not None

    def status(self):
        return {"enabled": self.enabled, "marker": self.marker().exists(), "available": self.available()}

    def eval(self, expr, timeout=30):
        url = self.target()
        if not url:
            raise RuntimeError(tr("cdp.unavailable"))
        ws = WS(url, timeout)
        try:
            ws.send_text(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                     "params": {"expression": expr, "awaitPromise": True, "returnByValue": True}}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                msg = json.loads(ws.recv_text())
                if msg.get("id") != 1:
                    continue  # CDP events
                if "error" in msg:
                    raise RuntimeError("Steam: " + str(msg["error"].get("message")))
                res = msg.get("result", {})
                exc = res.get("exceptionDetails")
                if exc:
                    desc = (exc.get("exception") or {}).get("description") or exc.get("text") or "JS error"
                    raise RuntimeError("Steam JS: " + desc.splitlines()[0][:200])
                return (res.get("result") or {}).get("value")
            raise TimeoutError(tr("cdp.no_answer"))
        finally:
            ws.close()

    def call(self, fn, *args):
        return self.eval(f"{fn}({', '.join(json.dumps(a, ensure_ascii=False) for a in args)})")

    # -- SteamClient.Apps wrappers (same calls Decky plugins use)
    def add_shortcut(self, name, exe, start_dir):
        appid = self.call("SteamClient.Apps.AddShortcut", name, exe, start_dir, "")
        if not isinstance(appid, (int, float)) or not appid:
            raise RuntimeError(tr("cdp.no_appid", value=repr(appid)))
        return int(appid) & 0xFFFFFFFF

    def set_name(self, appid, name):
        self.call("SteamClient.Apps.SetShortcutName", appid, name)

    def set_exe(self, appid, exe_quoted, start_dir_quoted):
        self.call("SteamClient.Apps.SetShortcutExe", appid, exe_quoted)
        self.call("SteamClient.Apps.SetShortcutStartDir", appid, start_dir_quoted)

    def set_compat(self, appid, tool):
        self.call("SteamClient.Apps.SpecifyCompatTool", appid, tool or "")

    def set_artwork(self, appid, data, ext, asset_type):
        self.call("SteamClient.Apps.SetCustomArtworkForApp", appid, base64.b64encode(data).decode(), ext, asset_type)

    def set_icon(self, appid, path):
        self.call("SteamClient.Apps.SetShortcutIcon", appid, str(path))

    def remove_shortcut(self, appid):
        self.call("SteamClient.Apps.RemoveShortcut", appid)


CDP = SteamCDP(CEF_PORT)
