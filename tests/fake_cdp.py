"""A stand-in for Steam's CEF debugging port: enough of it for DeckDrop's Steam control in tests.

It answers /json with a SharedJSContext target and serves its websocket. It cannot run JavaScript,
so it recognises what DeckDrop asks by the Steam calls in the expression and answers the way the
Deck did (tests/ui_check.py runs the same expressions in a real browser against a stand-in
SteamClient). Every call is recorded in `calls`.
"""
import base64
import hashlib
import json
import re
import socket
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeSteam:
    def __init__(self, configs_dir=None):
        self.configs_dir = configs_dir     # Steam Controller Configs/<account>/config, for autosave URLs
        self.mode = "ok"                   # ok | stuck (Steam does not switch) | no_controller
        self.selected = {}                 # appid -> URL chosen through DeckDrop
        self.current = {}                  # appid -> (URL, title) Steam reports before any choice
        self.calls = []
        self.lock = threading.Lock()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.sock.close()

    # ---- what DeckDrop asks
    def answer(self, expr):
        if "SetSelectedConfigForApp" in expr:
            m = re.search(r"\}\)\((\d+), (\"(?:\\.|[^\"\\])*\")\)\s*$", expr)
            appid, url = int(m.group(1)), json.loads(m.group(2))
            with self.lock:
                self.calls.append(("apply", appid, url))
                if self.mode == "no_controller":
                    return {"ok": False, "reason": "no_controller"}
                if self.mode == "stuck":
                    return {"ok": False, "reason": "not_switched", "index": 15, "url": "", "title": ""}
                self.selected[appid] = url
            return {"ok": True, "index": 15, "url": url, "title": "DeckDrop: x"}
        if "GetConfigForAppAndController" in expr:
            appid = int(re.search(r"\}\)\((\d+)\)\s*$", expr).group(1))
            with self.lock:
                self.calls.append(("current", appid))
                if appid in self.selected:
                    return {"url": self.selected[appid], "title": "DeckDrop", "index": 15}
                url, title = self.current.get(appid, ("", ""))
            return {"url": url, "title": title, "index": 15} if url else None
        m = re.match(r"SteamClient\.Apps\.(\w+)\(", expr)
        if m:
            with self.lock:
                self.calls.append(("apps", m.group(1)))
            return 3000000077 if m.group(1) == "AddShortcut" else None
        return None

    def calls_of(self, kind):
        with self.lock:
            return [c for c in self.calls if c[0] == kind]

    # ---- the protocol
    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            lines = head.split(b"\r\n\r\n", 1)[0].decode().split("\r\n")
            path = lines[0].split()[1]
            headers = {k.strip().lower(): v.strip() for k, v in (h.split(":", 1) for h in lines[1:] if ":" in h)}
            if path == "/json":
                body = json.dumps([{"title": "SharedJSContext", "type": "page",
                                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/1"}]).encode()
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                return
            accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + GUID).encode()).digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            buf = head.split(b"\r\n\r\n", 1)[1]
            while True:
                msg, buf = self._frame(conn, buf)
                if msg is None:
                    return
                req = json.loads(msg)
                value = self.answer(req["params"]["expression"])
                result = {"type": "object", "value": value} if value is not None else {"type": "undefined"}
                self._send(conn, json.dumps({"id": req["id"], "result": {"result": result}}))
        except (OSError, ValueError, KeyError, AttributeError):
            pass
        finally:
            conn.close()

    def _frame(self, conn, buf):
        def need(n):
            nonlocal buf
            while len(buf) < n:
                chunk = conn.recv(65536)
                if not chunk:
                    raise OSError("closed")
                buf += chunk
            out, buf = buf[:n], buf[n:]
            return out
        try:
            b1, b2 = need(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", need(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", need(8))[0]
            mask = need(4) if b2 & 0x80 else b"\0\0\0\0"
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(need(n)))
        except OSError:
            return None, buf
        if b1 & 0x0F == 0x8:
            return None, buf
        return data.decode(), buf

    def _send(self, conn, text):
        data = text.encode()
        n = len(data)
        head = bytes([0x81, n]) if n < 126 else bytes([0x81, 126]) + struct.pack(">H", n) if n < 65536 \
            else bytes([0x81, 127]) + struct.pack(">Q", n)
        conn.sendall(head + data)
