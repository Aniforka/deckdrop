"""Networking: DeckDrop-only proxy (SOCKS5/HTTP), HTTP helpers, retries, link resolvers."""

import http.client
import json
import re
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import UA
from .i18n import tr
from .state import STATE


SOCKS_ERR = {1: "socks.general", 2: "socks.not_allowed", 3: "socks.net_unreachable", 4: "socks.host_unreachable",
             5: "socks.refused", 6: "socks.ttl", 7: "socks.command", 8: "socks.address_type"}


def mask_proxy(url):
    """Proxy string with the credentials blanked out, safe to show without the PIN."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return "***"
    if not (u.username or u.password):
        return url
    host = u.hostname or ""
    if u.port:
        host += f":{u.port}"
    return f"{u.scheme}://***:***@{host}"


def proxy_url():
    return (STATE.get("proxy") or "").strip() or None


def dl_proxy():
    return proxy_url() if STATE.get("proxy_downloads") else None


def net_reason(e):
    """Network exception -> a short explanation for the user, with a hint about blocking."""
    err = e
    while isinstance(err, urllib.error.URLError) and not isinstance(err, urllib.error.HTTPError):
        err = err.reason if isinstance(err.reason, BaseException) else err.reason
        break
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code} {e.reason}"
    text = str(err) or type(e).__name__
    low = text.lower()
    if "reset" in low or "104" in low or "10054" in low:
        return tr("net.reset", detail=text)
    if "timed out" in low or "timeout" in low:
        return tr("net.timeout", detail=text)
    if "name or service" in low or "getaddrinfo" in low or "resolve" in low:
        return tr("net.dns", detail=text)
    if "refused" in low:
        return tr("net.refused", detail=text)
    if "certificate" in low or "ssl" in low:
        return tr("net.tls", detail=text)
    return text


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(tr("socks.closed"))
        buf += chunk
    return buf


def socks5_connect(px, dest_host, dest_port, timeout):
    """Open a TCP connection to dest through a SOCKS5 proxy (RFC 1928, optional user/password auth).

    Hostnames are sent to the proxy, so DNS is resolved on the proxy side too - that is what
    makes it work when the local resolver or route to the host is blocked.
    """
    sock = socket.create_connection((px.hostname, px.port or 1080), timeout)
    try:
        sock.settimeout(timeout)
        methods = b"\x00\x02" if px.username else b"\x00"
        sock.sendall(bytes([5, len(methods)]) + methods)
        ver, method = _recv_exact(sock, 2)
        if ver != 5:
            raise ConnectionError(tr("socks.not_socks5"))
        if method == 2:
            u = urllib.parse.unquote(px.username or "").encode()
            pw = urllib.parse.unquote(px.password or "").encode()
            sock.sendall(bytes([1, len(u)]) + u + bytes([len(pw)]) + pw)
            if _recv_exact(sock, 2)[1] != 0:
                raise ConnectionError(tr("socks.auth_failed"))
        elif method != 0:
            raise ConnectionError(tr("socks.auth_unsupported"))
        host = dest_host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack(">H", dest_port))
        rep = _recv_exact(sock, 4)
        if rep[1] != 0:
            raise ConnectionError(tr("socks.error", reason=tr(SOCKS_ERR[rep[1]]) if rep[1] in SOCKS_ERR else tr("socks.code", code=rep[1])))
        atyp = rep[3]
        if atyp == 1:
            _recv_exact(sock, 4)
        elif atyp == 3:
            _recv_exact(sock, _recv_exact(sock, 1)[0])
        elif atyp == 4:
            _recv_exact(sock, 16)
        _recv_exact(sock, 2)
        return sock
    except Exception:
        sock.close()
        raise


class Resp:
    """Uniform response over urllib / http.client so callers can use read / headers / geturl."""

    def __init__(self, raw, url):
        self.raw, self._url, self.headers = raw, url, raw.headers
        self.status = getattr(raw, "status", None) or getattr(raw, "code", None)

    def read(self, *a):
        return self.raw.read(*a)

    def geturl(self):
        return self._url

    def close(self):
        try:
            self.raw.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _via_socks(url, data, headers, timeout, method, px):
    u = urllib.parse.urlparse(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    sock = socks5_connect(px, u.hostname, port, timeout)
    if u.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
    conn = http.client.HTTPConnection(u.hostname, port, timeout=timeout)
    conn.sock = sock
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    conn.request(method or ("POST" if data else "GET"), path, body=data, headers=headers)
    return conn.getresponse()


def net_open(url, data=None, headers=None, timeout=60, proxy=None, method=None, redirects=3):
    """Open a URL directly or through DeckDrop's own proxy (socks5:// or http://)."""
    h = {"User-Agent": UA}
    h.update(headers or {})
    px = urllib.parse.urlparse(proxy) if proxy else None
    if px and px.scheme in ("socks5", "socks5h", "socks"):
        for _ in range(redirects + 1):
            raw = _via_socks(url, data, h, timeout, method, px)
            loc = raw.headers.get("Location")
            if raw.status in (301, 302, 303, 307, 308) and loc:
                raw.read()
                raw.close()
                url = urllib.parse.urljoin(url, loc)
                continue
            if raw.status >= 400:
                raw.read(400)
                raw.close()
                raise urllib.error.HTTPError(url, raw.status, raw.reason, raw.headers, None)
            return Resp(raw, url)
        raise RuntimeError(tr("net.redirects"))
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if px else {})
    opener = urllib.request.build_opener(handler)
    raw = opener.open(urllib.request.Request(url, data=data, headers=h, method=method), timeout=timeout)
    return Resp(raw, raw.geturl())


def http_get(url, timeout=60, headers=None, proxy=None):
    return net_open(url, headers=headers, timeout=timeout, proxy=proxy)


def with_retries(what, fn, tries=3, delay=1.5):
    """Retry a network call; connection resets from DPI are often intermittent."""
    last = None
    for i in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, ConnectionError, TimeoutError, ssl.SSLError) as e:
            last = e
            if i + 1 < tries:
                time.sleep(delay * (i + 1))
    raise RuntimeError(f"{what}: {net_reason(last)}") from last


def resolve_url(url):
    """Turn share links of known hosts into direct download links (best effort)."""
    u = urllib.parse.urlparse(url)
    host = u.netloc.lower()
    if "disk.yandex" in host or host.endswith("yadi.sk"):
        api = ("https://cloud-api.yandex.net/v1/disk/public/resources/download?public_key="
               + urllib.parse.quote(url, safe=""))
        with http_get(api, 30, proxy=dl_proxy()) as r:
            return json.load(r)["href"]
    if "drive.google.com" in host or "docs.google.com" in host:
        m = re.search(r"/d/([\w-]+)", u.path) or re.search(r"[?&]id=([\w-]+)", url)
        if m:
            return ("https://drive.usercontent.google.com/download?id="
                    + m.group(1) + "&export=download&confirm=t")
    return url
