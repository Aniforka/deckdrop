"""Unit tests for pure helpers: python -m unittest discover -s tests"""
import ast
import contextlib
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deckdrop import aes  # noqa: E402
from deckdrop.art.images import png_decode, png_encode  # noqa: E402
from deckdrop.detect import clean_title, recommend  # noqa: E402
from deckdrop.paths import archive_stem, archive_volume, safe_name  # noqa: E402
from deckdrop.steam.library import text_vdf, vdf_parse  # noqa: E402

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")


class PureAes:
    """Run a test body with libcrypto switched off, so the pure Python AES is used."""

    def __enter__(self):
        self.saved = aes._LIB
        aes._LIB = False
        return self

    def __exit__(self, *exc):
        aes._LIB = self.saved


class AesTest(unittest.TestCase):
    def test_fips197_vector(self):
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        want = "69c4e0d86a7b0430d8cdb78070b4c55a"
        self.assertEqual(aes.aes_ecb_encrypt(KEY, plain).hex(), want)
        with PureAes():
            self.assertEqual(aes.aes_ecb_encrypt(KEY, plain).hex(), want)
            self.assertEqual(aes.aes_ecb_decrypt(KEY, bytes.fromhex(want)), plain)

    def test_cbc_roundtrip(self):
        data = os.urandom(64)
        for pure in (False, True):
            with PureAes() if pure else contextlib.nullcontext():
                enc = aes.aes_cbc_encrypt(KEY, data)
                self.assertNotEqual(enc, data)
                self.assertEqual(aes.aes_cbc_decrypt(KEY, enc), data)

    def test_ctr_stream_matches_between_backends(self):
        iv = bytes(range(16))
        data = os.urandom(1000)
        fast = aes.AesCtr(KEY, iv)
        whole = fast.xor(data)
        fast.close()
        with PureAes():
            slow = aes.AesCtr(KEY, iv)
            # odd chunk sizes: the keystream must continue across calls
            parts = slow.xor(data[:7]) + slow.xor(data[7:300]) + slow.xor(data[300:])
        self.assertEqual(parts, whole)


class PngTest(unittest.TestCase):
    def test_roundtrip(self):
        rgba = bytes([255, 0, 0, 255, 0, 255, 0, 128, 0, 0, 255, 0, 9, 9, 9, 9])
        w, h, out = png_decode(png_encode(2, 2, rgba))
        self.assertEqual((w, h), (2, 2))
        self.assertEqual(bytes(out), rgba)


class VdfTest(unittest.TestCase):
    def test_text(self):
        self.assertEqual(text_vdf('"a"\n{\n "b" "c"\n "d" { "e" "f" }\n}\n'),
                         {"a": {"b": "c", "d": {"e": "f"}}})

    def test_binary(self):
        # map "shortcuts" { map "0" { string AppName "X", int32 appid 7 } }
        buf = (b"\x00shortcuts\x00" + b"\x000\x00" + b"\x01AppName\x00X\x00"
               + b"\x02appid\x00" + (7).to_bytes(4, "little") + b"\x08" + b"\x08" + b"\x08")
        # keys come back lower-cased: Steam itself treats them case-insensitively
        self.assertEqual(vdf_parse(buf), {"shortcuts": {"0": {"appname": "X", "appid": 7}}})


class NamesTest(unittest.TestCase):
    def test_archive_volumes(self):
        cases = {"Game.zip": "primary", "Game.part1.rar": "primary", "Game.part2.rar": "secondary",
                 "Game.7z.001": "primary", "Game.7z.002": "secondary", "Game.z01": "secondary"}
        for name, kind in cases.items():
            self.assertEqual(archive_volume(name), kind, name)
            self.assertEqual(archive_stem(name), "Game", name)

    def test_safe_name(self):
        self.assertEqual(safe_name("a/b:c?.zip"), "b_c_.zip")

    def test_titles(self):
        self.assertEqual(clean_title("Cool_Game-v1.2 (x64)"), "Cool Game")
        self.assertEqual(clean_title("game"), "Game")
        self.assertEqual(recommend(["unins000.exe", "Game.exe", "bin/Launcher.exe"]), "Game.exe")


class Python38Test(unittest.TestCase):
    """SteamOS ships 3.11, but 3.8 is the promised minimum and CI runs it."""

    def test_no_dict_union_operator(self):
        # {...} | x needs 3.9; pyflakes and a 3.8 syntax check do not catch it
        found = []
        for path in sorted((Path(__file__).resolve().parent.parent / "src").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text("utf-8"))):
                if (isinstance(node, (ast.BinOp, ast.AugAssign)) and isinstance(node.op, ast.BitOr)
                        and any(isinstance(side, ast.Dict)
                                for side in (getattr(node, "left", None), getattr(node, "right", None),
                                             getattr(node, "value", None)))):
                    found.append(f"{path.name}:{node.lineno}")
        self.assertEqual(found, [], "dict | dict needs Python 3.9: use {**a, **b}")


if __name__ == "__main__":
    unittest.main()
