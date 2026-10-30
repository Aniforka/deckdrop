"""Unreal Engine games: the game executable sits in Game/Binaries/Win64, not at the root.

    python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deckdrop import detect  # noqa: E402


class UnrealTest(unittest.TestCase):
    def setUp(self):
        detect._EXES.clear()
        self.game = Path(tempfile.mkdtemp()) / "Dragon Quest XI S"
        win64 = self.game / "Game" / "Binaries" / "Win64"
        paks = self.game / "Game" / "Content" / "Paks"
        win64.mkdir(parents=True)
        paks.mkdir(parents=True)
        self.exe = win64 / "DRAGON QUEST XI S-Win64-Shipping.exe"
        self.exe.write_bytes(b"MZ")
        (paks / "pak0.pak").write_bytes(b"x" * 1000)
        (self.game / "Launcher.exe").write_bytes(b"MZ")
        (self.game / "CrashReportClient.exe").write_bytes(b"MZ")
        self.rel = "Game/Binaries/Win64/DRAGON QUEST XI S-Win64-Shipping.exe"

    def test_deep_executable_is_found(self):
        exes = detect.find_exes(self.game)
        self.assertIn(self.rel, exes)
        self.assertIn("Launcher.exe", exes)

    def test_the_shipping_executable_is_recommended(self):
        self.assertEqual(detect.recommend(detect.find_exes(self.game)), self.rel)

    def test_crash_reporter_is_not_offered(self):
        self.assertFalse(any("CrashReportClient" in e for e in detect.find_exes(self.game)))

    def test_names_lose_the_unreal_suffix(self):
        self.assertEqual(detect.clean_title("DRAGON QUEST XI S-Win64-Shipping"), "DRAGON QUEST XI S")
        self.assertEqual(detect.clean_title("Game-Win64-Shipping"), "Game")
        self.assertEqual(detect.pretty_name(self.rel, self.game), "DRAGON QUEST XI S")
        self.assertTrue(detect.is_generic_stem("Game-Win64-Shipping"))
        self.assertIn("DRAGON QUEST XI S", detect.name_candidates(self.game, [self.rel]))

    def test_deep_linux_build_is_found(self):
        detect._EXES.clear()
        linux = self.game / "Binaries" / "Linux"
        linux.mkdir(parents=True)
        (linux / "Game.sh").write_bytes(b"#!/bin/sh\n")
        self.assertIn("Binaries/Linux/Game.sh", detect.find_exes(self.game))


if __name__ == "__main__":
    unittest.main()
