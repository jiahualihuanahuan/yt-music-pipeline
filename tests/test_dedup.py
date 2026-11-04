"""Near-duplicate detection, organize integration and the --dedup scan (synthetic files only)."""
import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dedup
import main
from deletion import DeleteRun

F128 = b"\xff\xfb\x90\x64" + b"\x00" * 413   # MPEG-1 L3 128 kbps 44.1 kHz, ~26 ms
F192 = b"\xff\xfb\xb0\x64" + b"\x00" * 622   # MPEG-1 L3 192 kbps 44.1 kHz, ~26 ms


def make(path: Path, title="", artist="", album="", frames=200, frame=F128, extra=None):
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TCON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(frame * frames)
    t = ID3()
    for cls, v in ((TIT2, title), (TPE1, artist), (TALB, album), (TCON, extra)):
        if v:
            t.add(cls(encoding=3, text=[v]))
    t.save(path)
    return path


class Logic(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def info(self, name, **kw):
        return dedup.audio_info(make(self.d / name, **kw))

    def test_norm(self):
        self.assertEqual(dedup.norm("  Hello, World! "), dedup.norm("hello world"))
        self.assertEqual(dedup.norm("ＡＢＣ"), "abc")

    def test_same_song_similar_duration(self):
        a = self.info("a.mp3", title="Song", artist="Band", frames=200)
        b = self.info("b.mp3", title="song!", artist="BAND", frames=260)  # ~1.6 s longer
        self.assertTrue(dedup.is_near_duplicate(a, b))

    def test_duration_mismatch_not_dup(self):
        a = self.info("a.mp3", title="Song", artist="Band", frames=200)
        b = self.info("b.mp3", title="Song", artist="Band", frames=400)  # ~5 s longer
        self.assertFalse(dedup.is_near_duplicate(a, b))

    def test_missing_tags_not_dup(self):
        a = self.info("a.mp3", title="Song", artist="", frames=200)
        b = self.info("b.mp3", title="Song", artist="", frames=200)
        self.assertFalse(dedup.is_near_duplicate(a, b))

    def test_different_title_not_dup(self):
        a = self.info("a.mp3", title="Song", artist="Band")
        b = self.info("b.mp3", title="Song 2", artist="Band")
        self.assertFalse(dedup.is_near_duplicate(a, b))

    def test_better_prefers_bitrate_then_tags(self):
        lo = self.info("lo.mp3", title="S", artist="A", frame=F128, extra="Pop")
        hi = self.info("hi.mp3", title="S", artist="A", frame=F192)
        self.assertIs(dedup.better(lo, hi), hi)
        plain = self.info("p.mp3", title="S", artist="A")
        rich = self.info("r.mp3", title="S", artist="A", album="Album", extra="Pop")
        self.assertIs(dedup.better(plain, rich), rich)

    def test_group(self):
        infos = [self.info("1.mp3", title="S", artist="A", frames=200),
                 self.info("2.mp3", title="S", artist="A", frames=210),
                 self.info("3.mp3", title="S", artist="A", frames=600),
                 self.info("4.mp3", title="T", artist="A", frames=200)]
        groups = dedup.group_duplicates(infos)
        self.assertEqual(len(groups), 1)
        self.assertEqual({p["path"].name for p in groups[0]}, {"1.mp3", "2.mp3"})


class Scan(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.lib = self.root / "organized"
        self.trash = self.root / "_to_delete"
        self.logs = self.root / "logs"
        d = self.lib / "Band" / "Album"
        self.canon = make(d / "Song.mp3", "Song", "Band", "Album", frame=F128)
        self.copy = make(d / "Song_123456.mp3", "Song", "Band", "Album", frame=F192, frames=210)
        self.other = make(d / "Other.mp3", "Other", "Band", "Album")

    def test_dry_run_moves_nothing(self):
        run = DeleteRun(self.lib, self.trash, self.logs, enabled=False)
        groups, report = dedup.scan_folder(self.lib, run, self.logs, apply=False)
        self.assertEqual(len(groups), 1)
        self.assertTrue(self.canon.exists() and self.copy.exists())
        self.assertFalse(self.trash.exists())
        rows = list(csv.DictReader(report.open(encoding="utf-8")))
        self.assertEqual(rows[0]["action"], "dry-run")

    def test_apply_quarantines_loser_and_renames_winner(self):
        run = DeleteRun(self.lib, self.trash, self.logs, enabled=True)
        dedup.scan_folder(self.lib, run, self.logs, apply=True)
        # 192 kbps copy wins, takes canonical name; 128 kbps original is quarantined, not deleted
        self.assertTrue(self.canon.exists())
        self.assertFalse(self.copy.exists())
        self.assertEqual(dedup.audio_info(self.canon)["bitrate"], 192000)
        self.assertTrue((self.trash / "Band" / "Album" / "Song.mp3").exists())
        self.assertTrue(self.other.exists())


class Organize(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.org = self.root / "organized"
        self.src = self.root / "inbox"
        self.trash = self.root / "_to_delete"
        self.dest = make(self.org / "Band" / "Album" / "Song.mp3", "Song", "Band", "Album", frame=F128)
        self.p = [mock.patch.object(main, "ORGANIZED_DIR", self.org),
                  mock.patch.object(main, "LOG_DIR", self.root / "logs"),
                  mock.patch("config.DELETE_DIR", self.trash, create=True),
                  mock.patch("config.DELETE_ENABLED", True, create=True),
                  mock.patch("config.ARTIST_ALIASES_FILE", self.root / "aliases.json", create=True)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_worse_incoming_quarantined(self):
        inc = make(self.src / "Song.mp3", "Song", "Band", "Album", frame=F128, frames=190)
        main.organize_library(self.src)
        self.assertFalse(inc.exists())
        self.assertEqual(len(list(self.org.rglob("*.mp3"))), 1)
        self.assertEqual(len(list(self.trash.rglob("*.mp3"))), 1)

    def test_better_incoming_replaces(self):
        make(self.src / "Song.mp3", "Song", "Band", "Album", frame=F192)
        main.organize_library(self.src)
        files = list(self.org.rglob("*.mp3"))
        self.assertEqual([f.name for f in files], ["Song.mp3"])
        self.assertEqual(dedup.audio_info(files[0])["bitrate"], 192000)
        self.assertEqual(len(list(self.trash.rglob("*.mp3"))), 1)

    def test_different_song_same_filename_gets_suffix(self):
        make(self.src / "Song.mp3", "Song", "Band", "Album", frames=600)  # same tags, very different length
        main.organize_library(self.src)
        names = sorted(f.name for f in self.org.rglob("*.mp3"))
        self.assertEqual(len(names), 2)
        self.assertTrue(any(n.startswith("Song_") for n in names))
        self.assertFalse(self.trash.exists())


if __name__ == "__main__":
    unittest.main()
