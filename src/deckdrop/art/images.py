"""Images without dependencies: PE icons, ICO, PNG, DIB, simple compositing."""

import mmap
import struct
import zlib
from pathlib import Path

from ..config import PNG_SIG, log


def pe_icon(path):
    """Best icon image (raw PNG or DIB bytes) from a PE executable, or None."""
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            return _pe_icon(m)
    except (OSError, ValueError, struct.error, IndexError, KeyError):
        return None


def _pe_icon(m):
    if m[:2] != b"MZ":
        return None
    pe = struct.unpack_from("<I", m, 0x3C)[0]
    if m[pe:pe + 4] != b"PE\0\0":
        return None
    nsec = struct.unpack_from("<H", m, pe + 6)[0]
    opt_size = struct.unpack_from("<H", m, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", m, opt)[0]
    dd = opt + (96 if magic == 0x10B else 112)
    rsrc_rva = struct.unpack_from("<I", m, dd + 2 * 8)[0]
    if not rsrc_rva:
        return None
    sections = []
    sec = opt + opt_size
    for i in range(nsec):
        vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", m, sec + i * 40 + 8)
        sections.append((va, max(vsize, rawsize), rawptr))

    def off(rva):
        for va, size, ptr in sections:
            if va <= rva < va + size:
                return rva - va + ptr
        raise ValueError("rva outside sections")

    base = off(rsrc_rva)

    def entries(dir_off):
        n_named, n_id = struct.unpack_from("<HH", m, dir_off + 12)
        return [struct.unpack_from("<II", m, dir_off + 16 + i * 8) for i in range(n_named + n_id)]

    def leaf(e):
        while e & 0x80000000:
            subs = entries(base + (e & 0x7FFFFFFF))
            if not subs:
                raise ValueError("empty resource dir")
            e = subs[0][1]
        rva, size = struct.unpack_from("<II", m, base + e)
        o = off(rva)
        return bytes(m[o:o + size])

    types = dict(entries(base))
    if 14 not in types or 3 not in types:
        return None
    icons = dict(entries(base + (types[3] & 0x7FFFFFFF)))
    groups = entries(base + (types[14] & 0x7FFFFFFF))
    if not groups:
        return None
    grp = leaf(groups[0][1])
    count = struct.unpack_from("<H", grp, 4)[0]
    best = None
    for i in range(count):
        w, _h, _cc, _res, _planes, bpp, _size, ident = struct.unpack_from("<BBBBHHIH", grp, 6 + i * 14)
        if ident not in icons:
            continue
        score = ((w or 256), bpp)
        if best is None or score > best[0]:
            best = (score, icons[ident])
    return leaf(best[1]) if best else None


def ico_best(data):
    """Best image (raw PNG or DIB bytes) from an .ico file."""
    count = struct.unpack_from("<H", data, 4)[0]
    best = None
    for i in range(count):
        w, _h, _cc, _res, _planes, bpp, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + i * 16)
        score = ((w or 256), bpp)
        if best is None or score > best[0]:
            best = (score, data[offset:offset + size])
    return best[1] if best else None


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def png_decode(data):
    """Minimal PNG decoder (8-bit, non-interlaced) -> (w, h, rgba bytes)."""
    if data[:8] != PNG_SIG:
        raise ValueError("not a png")
    pos, idat, plte, trns = 8, [], b"", b""
    w = h = ct = bd = il = 0
    while pos + 8 <= len(data):
        ln, typ = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, bd, ct, _, _, il = struct.unpack(">IIBBBBB", body)
        elif typ == b"PLTE":
            plte = body
        elif typ == b"tRNS":
            trns = body
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    if bd != 8 or il or ct not in (0, 2, 3, 4, 6):
        raise ValueError("unsupported png")
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    raw = zlib.decompress(b"".join(idat))
    stride = w * ch
    prev = bytearray(stride)
    out = bytearray()
    pos = 0
    for _ in range(h):
        f = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if f == 1:
            for x in range(ch, stride):
                line[x] = (line[x] + line[x - ch]) & 0xFF
        elif f == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif f == 3:
            for x in range(stride):
                line[x] = (line[x] + (((line[x - ch] if x >= ch else 0) + prev[x]) >> 1)) & 0xFF
        elif f == 4:
            for x in range(stride):
                a = line[x - ch] if x >= ch else 0
                c = prev[x - ch] if x >= ch else 0
                line[x] = (line[x] + _paeth(a, prev[x], c)) & 0xFF
        out += line
        prev = line
    if ct == 6:
        return w, h, bytes(out)
    px = bytearray(w * h * 4)
    for i in range(w * h):
        o = i * 4
        if ct == 2:
            px[o:o + 3] = out[i * 3:i * 3 + 3]
            px[o + 3] = 255
        elif ct == 0:
            px[o] = px[o + 1] = px[o + 2] = out[i]
            px[o + 3] = 255
        elif ct == 4:
            px[o] = px[o + 1] = px[o + 2] = out[i * 2]
            px[o + 3] = out[i * 2 + 1]
        else:
            idx = out[i]
            px[o:o + 3] = plte[idx * 3:idx * 3 + 3]
            px[o + 3] = trns[idx] if idx < len(trns) else 255
    return w, h, bytes(px)


def png_encode(w, h, rgba):
    raw = b"".join(b"\0" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def dib_decode(data):
    """Icon DIB (BITMAPINFOHEADER + XOR bitmap + AND mask) -> (w, h, rgba)."""
    size, w, h2, _planes, bpp, comp = struct.unpack_from("<IiiHHI", data, 0)
    if comp != 0 or bpp not in (1, 4, 8, 24, 32):
        raise ValueError(f"unsupported dib (bpp={bpp}, comp={comp})")
    h = abs(h2)
    if h == 2 * w:      # icon DIBs store XOR+AND bitmaps stacked, so height is doubled
        h //= 2
    off = size
    palette = []
    if bpp <= 8:
        n = 1 << bpp
        palette = [data[off + i * 4:off + i * 4 + 4] for i in range(n)]
        off += n * 4
    row = ((w * bpp + 31) // 32) * 4
    mask_row = ((w + 31) // 32) * 4
    xor_off, and_off = off, off + row * h
    has_mask = and_off + mask_row * h <= len(data)
    use_alpha = False
    if bpp == 32:
        use_alpha = any(data[xor_off + i * 4 + 3] for i in range(w * h))
    px = bytearray(w * h * 4)
    for y in range(h):
        sr = xor_off + (h - 1 - y) * row
        mr = and_off + (h - 1 - y) * mask_row
        for x in range(w):
            a = 255
            if bpp == 32:
                b, g, r, a32 = data[sr + x * 4:sr + x * 4 + 4]
                if use_alpha:
                    a = a32
            elif bpp == 24:
                b, g, r = data[sr + x * 3:sr + x * 3 + 3]
            else:
                if bpp == 8:
                    idx = data[sr + x]
                elif bpp == 4:
                    idx = (data[sr + x // 2] >> (4 if x % 2 == 0 else 0)) & 15
                else:
                    idx = (data[sr + x // 8] >> (7 - x % 8)) & 1
                b, g, r = palette[idx][:3]
            if not use_alpha and has_mask:
                a = 0 if (data[mr + x // 8] >> (7 - x % 8)) & 1 else 255
            o = (y * w + x) * 4
            px[o], px[o + 1], px[o + 2], px[o + 3] = r, g, b, a
    return w, h, bytes(px)


def decode_icon_image(data):
    return png_decode(data) if data[:8] == PNG_SIG else dib_decode(data)


def find_icon_file(game_dir):
    """Fallback for games without an exe icon: an .ico / icon png in the game folder."""
    pats = ("*.ico", "*/*.ico", "icon.png", "*/icon.png", "*/window_icon.png", "*/*/window_icon.png", "*icon*.png")
    for pat in pats:
        for p in sorted(Path(game_dir).glob(pat)):
            try:
                data = p.read_bytes()
                return decode_icon_image(ico_best(data) if p.suffix.lower() == ".ico" else data)
            except (ValueError, struct.error, zlib.error, IndexError):
                continue
    return None


def load_icon(exe_path):
    p = Path(exe_path)
    if p.suffix.lower() == ".exe":
        raw = pe_icon(p)
        if raw:
            try:
                return decode_icon_image(raw)
            except (ValueError, struct.error, zlib.error, IndexError) as e:
                log(f"icon decode failed for {p.name}: {e}")
    return find_icon_file(p.parent)


def dominant_color(w, h, rgba):
    r = g = b = n = 0
    for i in range(0, w * h * 4, 4 * max(1, (w * h) // 4096)):
        if rgba[i + 3] > 128:
            r += rgba[i]
            g += rgba[i + 1]
            b += rgba[i + 2]
            n += 1
    if not n:
        return (0x1B, 0x28, 0x38)
    return tuple(int(c / n * 0.35) for c in (r, g, b))


def compose(cw, ch, iw, ih, rgba, bg, frac):
    """Nearest-neighbour scale the icon to `frac` of the canvas and centre it on a solid bg."""
    scale = min(cw * frac / iw, ch * frac / ih)
    tw, th = max(1, int(iw * scale)), max(1, int(ih * scale))
    xs = [min(iw - 1, int(x / scale)) * 4 for x in range(tw)]
    ys = [min(ih - 1, int(y / scale)) for y in range(th)]
    ox, oy = (cw - tw) // 2, (ch - th) // 2
    bgpx = bytes(bg) + b"\xff"
    canvas = bytearray(bgpx * (cw * ch))
    for ty in range(th):
        srow = rgba[ys[ty] * iw * 4:(ys[ty] + 1) * iw * 4]
        row = bytearray(tw * 4)
        for tx in range(tw):
            sx = xs[tx]
            a = srow[sx + 3]
            o = tx * 4
            if a == 255:
                row[o:o + 4] = srow[sx:sx + 4]
            elif a == 0:
                row[o:o + 4] = bgpx
            else:
                ia = 255 - a
                row[o] = (srow[sx] * a + bg[0] * ia) // 255
                row[o + 1] = (srow[sx + 1] * a + bg[1] * ia) // 255
                row[o + 2] = (srow[sx + 2] * a + bg[2] * ia) // 255
                row[o + 3] = 255
        start = ((oy + ty) * cw + ox) * 4
        canvas[start:start + tw * 4] = row
    return bytes(canvas)
