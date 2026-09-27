"""AES for Mega: libcrypto through ctypes, pure Python fallback."""

import os
import sys
from pathlib import Path

from .config import log


# Mega encrypts every file in the browser before uploading it: the link carries the key, the
# server never sees it. So downloading means decrypting here. The stdlib has no AES, so this is
# OpenSSL through ctypes (present on SteamOS - Python itself links it) with a pure-Python
# implementation behind it, used for keys and names even when libcrypto is missing.

ZERO16 = b"\0" * 16
_LIB = None            # ctypes handle to libcrypto, or False once the search has failed
_CT = None


def _load_libcrypto():
    global _LIB, _CT
    if _LIB is not None:
        return _LIB
    import ctypes
    import ctypes.util
    names = []
    try:
        found = ctypes.util.find_library("crypto")
    except Exception:                                             # noqa: BLE001
        found = None
    if found:
        names.append(found)
    names += ["libcrypto.so.3", "libcrypto.so.1.1", "libcrypto.so", "libcrypto.dylib"]
    if os.name == "nt":
        dlls = Path(sys.base_prefix) / "DLLs"
        names += [str(dlls / n) for n in ("libcrypto-3-x64.dll", "libcrypto-3.dll",
                                          "libcrypto-1_1-x64.dll", "libcrypto-1_1.dll")]
    c = ctypes
    for name in names:
        try:
            lib = c.CDLL(name)
            lib.EVP_CIPHER_CTX_new.restype = c.c_void_p
            lib.EVP_CIPHER_CTX_free.argtypes = [c.c_void_p]
            lib.EVP_CIPHER_CTX_set_padding.argtypes = [c.c_void_p, c.c_int]
            for fn in ("EVP_aes_128_ecb", "EVP_aes_128_cbc", "EVP_aes_128_ctr"):
                getattr(lib, fn).restype = c.c_void_p
            for fn in ("EVP_EncryptInit_ex", "EVP_DecryptInit_ex"):
                f = getattr(lib, fn)
                f.argtypes = [c.c_void_p, c.c_void_p, c.c_void_p, c.c_char_p, c.c_char_p]
                f.restype = c.c_int
            for fn in ("EVP_EncryptUpdate", "EVP_DecryptUpdate"):
                f = getattr(lib, fn)
                f.argtypes = [c.c_void_p, c.c_char_p, c.POINTER(c.c_int), c.c_char_p, c.c_int]
                f.restype = c.c_int
            _CT, _LIB = c, lib
            return lib
        except (OSError, AttributeError):
            continue
    _LIB = False
    log("aes: libcrypto not found, Mega decryption will be very slow")
    return False


def _ossl(cipher, key, iv, data, enc):
    lib, c = _LIB, _CT
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise RuntimeError("openssl: no cipher context")
    try:
        init = lib.EVP_EncryptInit_ex if enc else lib.EVP_DecryptInit_ex
        upd = lib.EVP_EncryptUpdate if enc else lib.EVP_DecryptUpdate
        if init(ctx, getattr(lib, cipher)(), None, key, iv) != 1:
            raise RuntimeError("openssl: key rejected")
        lib.EVP_CIPHER_CTX_set_padding(ctx, 0)
        out = c.create_string_buffer(len(data) + 16)
        n = c.c_int(0)
        if upd(ctx, out, c.byref(n), bytes(data), len(data)) != 1:
            raise RuntimeError("openssl: cipher failed")
        return out.raw[:n.value]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def _aes_tables():
    """S-box and its inverse, built from the GF(2**8) inverse and the AES affine map."""
    exp, lg = [0] * 512, [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        lg[x] = i
        x ^= ((x << 1) & 0xFF) ^ (0x1B if x & 0x80 else 0)     # x *= 3, the generator
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    sbox = [0] * 256
    for i in range(256):
        v = 0 if i == 0 else exp[255 - lg[i]]
        s = v
        for _ in range(4):
            v = ((v << 1) | (v >> 7)) & 0xFF
            s ^= v
        sbox[i] = s ^ 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    return exp, lg, sbox, inv


_EXP, _LG, _SBOX, _ISBOX = _aes_tables()
_MUL = {n: bytes((0 if i == 0 else _EXP[_LG[n] + _LG[i]]) for i in range(256))
        for n in (2, 3, 9, 11, 13, 14)}
_M2, _M3, _M9, _M11, _M13, _M14 = (_MUL[2], _MUL[3], _MUL[9], _MUL[11], _MUL[13], _MUL[14])
# state byte i is row i%4, column i//4; ShiftRows moves row r left by r, its inverse right by r
_SHIFT = [(i % 4) + 4 * (((i // 4) + (i % 4)) % 4) for i in range(16)]
_UNSHIFT = [(i % 4) + 4 * (((i // 4) - (i % 4)) % 4) for i in range(16)]


def _expand_key(key):
    if len(key) != 16:
        raise ValueError(f"an AES key must be 16 bytes, not {len(key)}")
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = [_SBOX[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = (((rcon << 1) & 0xFF) ^ 0x1B) if rcon & 0x80 else rcon << 1
        w.append([a ^ b for a, b in zip(w[i - 4], t)])
    return [bytes(b for word in w[r * 4:r * 4 + 4] for b in word) for r in range(11)]


def _enc_block(rk, blk):
    s = [a ^ b for a, b in zip(blk, rk[0])]
    for rnd in range(1, 11):
        s = [_SBOX[s[j]] for j in _SHIFT]                       # SubBytes + ShiftRows
        if rnd < 10:
            t = []
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
                t += [_M2[a0] ^ _M3[a1] ^ a2 ^ a3,
                      a0 ^ _M2[a1] ^ _M3[a2] ^ a3,
                      a0 ^ a1 ^ _M2[a2] ^ _M3[a3],
                      _M3[a0] ^ a1 ^ a2 ^ _M2[a3]]
            s = t
        s = [a ^ b for a, b in zip(s, rk[rnd])]
    return bytes(s)


def _dec_block(rk, blk):
    s = [a ^ b for a, b in zip(blk, rk[10])]
    for rnd in range(9, -1, -1):
        s = [_ISBOX[s[j]] for j in _UNSHIFT]                    # InvShiftRows + InvSubBytes
        s = [a ^ b for a, b in zip(s, rk[rnd])]
        if rnd:
            t = []
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
                t += [_M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3],
                      _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3],
                      _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3],
                      _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3]]
            s = t
    return bytes(s)


def _blocks(data):
    if len(data) % 16:
        raise ValueError("AES data must be a multiple of 16 bytes")
    return range(0, len(data), 16)


def aes_ecb_encrypt(key, data):
    if _load_libcrypto():
        return _ossl("EVP_aes_128_ecb", key, None, data, True)
    rk = _expand_key(key)
    return b"".join(_enc_block(rk, data[i:i + 16]) for i in _blocks(data))


def aes_ecb_decrypt(key, data):
    if _load_libcrypto():
        return _ossl("EVP_aes_128_ecb", key, None, data, False)
    rk = _expand_key(key)
    return b"".join(_dec_block(rk, data[i:i + 16]) for i in _blocks(data))


def aes_cbc_encrypt(key, data, iv=ZERO16):
    if _load_libcrypto():
        return _ossl("EVP_aes_128_cbc", key, iv, data, True)
    rk, out, prev = _expand_key(key), [], iv
    for i in _blocks(data):
        prev = _enc_block(rk, bytes(a ^ b for a, b in zip(data[i:i + 16], prev)))
        out.append(prev)
    return b"".join(out)


def aes_cbc_decrypt(key, data, iv=ZERO16):
    if _load_libcrypto():
        return _ossl("EVP_aes_128_cbc", key, iv, data, False)
    rk, out, prev = _expand_key(key), [], iv
    for i in _blocks(data):
        blk = data[i:i + 16]
        out.append(bytes(a ^ b for a, b in zip(_dec_block(rk, blk), prev)))
        prev = blk
    return b"".join(out)


class AesCtr:
    """AES-128-CTR kept open for the whole file, so chunks decrypt as one continuous stream."""

    def __init__(self, key, iv):
        self.ctx = None
        if _load_libcrypto():
            lib = _LIB
            ctx = lib.EVP_CIPHER_CTX_new()
            if lib.EVP_EncryptInit_ex(ctx, lib.EVP_aes_128_ctr(), None, key, iv) != 1:
                lib.EVP_CIPHER_CTX_free(ctx)
                raise RuntimeError("openssl: Mega key rejected")
            lib.EVP_CIPHER_CTX_set_padding(ctx, 0)
            self.ctx = ctx
        else:
            self.rk, self.ctr, self.buf = _expand_key(key), int.from_bytes(iv, "big"), b""

    def xor(self, data):
        if self.ctx is not None:
            c = _CT
            out = c.create_string_buffer(len(data) + 16)
            n = c.c_int(0)
            if _LIB.EVP_EncryptUpdate(self.ctx, out, c.byref(n), bytes(data), len(data)) != 1:
                raise RuntimeError("openssl: decryption failed")
            return out.raw[:n.value]
        ks = bytearray(self.buf)
        while len(ks) < len(data):
            ks += _enc_block(self.rk, self.ctr.to_bytes(16, "big"))
            self.ctr = (self.ctr + 1) & ((1 << 128) - 1)
        self.buf = bytes(ks[len(data):])
        return bytes(a ^ b for a, b in zip(data, ks))

    def close(self):
        if self.ctx is not None:
            ctx, self.ctx = self.ctx, None
            _LIB.EVP_CIPHER_CTX_free(ctx)

    def __del__(self):
        try:
            self.close()
        except Exception:                                          # noqa: BLE001
            pass
