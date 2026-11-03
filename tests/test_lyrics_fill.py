"""Lyrics (LRCLIB, mocked) and fill-only re-run mode."""
import shutil, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import lyrics
import main as pipeline
from mutagen.mp3 import MP3
from mutagen.id3 import TIT2, TPE1, TALB, TCON, USLT, APIC


def make_mp3(path: Path, **frames):
    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413  # MPEG1 L3 128k 44.1k
    path.write_bytes(frame * 40)
    a = MP3(path); a.add_tags()
    for k, v in frames.items():
        a.tags.add(v)
    a.save()
    return path


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status
    def json(self):
        return self._d
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


HIT = {"trackName": "晴天", "artistName": "周杰伦", "plainLyrics": "故事的小黄花",
       "syncedLyrics": "[00:01.00]故事的小黄花", "instrumental": False}


class Base(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.d, True)
        lyrics._mem.clear()
        for p in (mock.patch.object(lyrics, "_db", return_value=None), mock.patch.object(lyrics.time, "sleep")):
            p.start(); self.addCleanup(p.stop)


class LyricsTests(Base):
    def test_get_then_write_tag_and_lrc(self):
        f = make_mp3(self.d / "a.mp3", TIT2=TIT2(encoding=3, text="晴天"), TPE1=TPE1(encoding=3, text="周杰伦"))
        with mock.patch.object(lyrics.requests, "get", return_value=Resp(HIT)) as g:
            self.assertEqual(lyrics.add_lyrics(f), "added")
            self.assertEqual(lyrics.add_lyrics(f), "has")
        self.assertEqual(g.call_args.args[0], lyrics.LRCLIB_GET)
        self.assertEqual(MP3(f).tags.getall("USLT")[0].text, "故事的小黄花")
        self.assertIn("[00:01.00]", f.with_suffix(".lrc").read_text(encoding="utf-8"))

    def test_falls_back_without_album_then_search(self):
        calls = []
        def get(url, params=None, **kw):
            calls.append((url, dict(params)))
            if url == lyrics.LRCLIB_SEARCH:
                return Resp([HIT])
            return Resp({}, 404)
        with mock.patch.object(lyrics.requests, "get", side_effect=get):
            r = lyrics.lookup("晴天", "周杰伦", "叶惠美", 269.4)
        self.assertEqual(r["plain"], "故事的小黄花")
        self.assertEqual(calls[0][1]["duration"], 269); self.assertIn("album_name", calls[0][1])
        self.assertNotIn("album_name", calls[1][1])
        self.assertEqual(calls[2][0], lyrics.LRCLIB_SEARCH)

    def test_instrumental_and_notfound_cached(self):
        with mock.patch.object(lyrics.requests, "get", return_value=Resp({"instrumental": True})):
            self.assertTrue(lyrics.lookup("x", "y")["instrumental"])
        with mock.patch.object(lyrics.requests, "get", return_value=Resp({}, 404)) as g:
            self.assertIsNone(lyrics.lookup("nope", "nobody"))
            self.assertIsNone(lyrics.lookup("nope", "nobody"))
        self.assertEqual(g.call_count, 2)  # 1 get + 1 search, second call cached

    def test_never_overwrites_existing_lyrics(self):
        f = make_mp3(self.d / "b.mp3", TIT2=TIT2(encoding=3, text="晴天"), TPE1=TPE1(encoding=3, text="周杰伦"),
                     USLT=USLT(encoding=3, lang="und", desc="", text="mine"))
        with mock.patch.object(lyrics.requests, "get", return_value=Resp(HIT)):
            lyrics.add_lyrics(f)
        self.assertEqual(MP3(f).tags.getall("USLT")[0].text, "mine")
        self.assertTrue(f.with_suffix(".lrc").exists())


class FillOnlyTests(Base):
    def test_fill_only_fills_empty_never_overwrites_or_moves(self):
        org = self.d / "organized"; album = org / "周杰伦" / "叶惠美"; album.mkdir(parents=True)
        f = make_mp3(album / "晴天.mp3", TIT2=TIT2(encoding=3, text="晴天"), TPE1=TPE1(encoding=3, text="周杰伦"),
                     TALB=TALB(encoding=3, text="叶惠美"),
                     APIC=APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=b"x" * 50))
        enriched = {"title": "晴天", "artist": "周杰伦", "album": "Other Album", "album_year": "2003", "genre": "Mandopop"}
        with mock.patch.object(pipeline, "ORGANIZED_DIR", org), \
             mock.patch.object(pipeline, "enrich", return_value=enriched), \
             mock.patch.object(pipeline, "organize_library") as orglib, \
             mock.patch.object(pipeline, "_retag_folder") as retag, \
             mock.patch.object(pipeline, "fetch_cover_url") as cover, \
             mock.patch.object(lyrics.requests, "get", return_value=Resp(HIT)):
            pipeline.process_folder(album)  # auto fill-only: inside ORGANIZED_DIR
        orglib.assert_not_called(); retag.assert_not_called(); cover.assert_not_called()
        t = MP3(f).tags
        self.assertEqual(str(t["TALB"].text[0]), "叶惠美")       # kept
        self.assertEqual(str(t["TDRC"].text[0]), "2003")         # filled
        self.assertEqual(str(t["TCON"].text[0]), "Mandopop")     # filled
        self.assertEqual(t.getall("APIC")[0].data, b"x" * 50)    # small art kept, not replaced
        self.assertEqual(t.getall("USLT")[0].text, "故事的小黄花")
        self.assertTrue(f.exists()); self.assertTrue(f.with_suffix(".lrc").exists())

    def test_normal_mode_outside_organized(self):
        org = self.d / "organized"; src = self.d / "inbox"; src.mkdir()
        with mock.patch.object(pipeline, "ORGANIZED_DIR", org), \
             mock.patch.object(pipeline, "_retag_folder") as retag, \
             mock.patch.object(pipeline, "fix_art_folder"), \
             mock.patch.object(pipeline, "organize_library") as orglib, \
             mock.patch.object(pipeline, "fill_missing_folder") as fill:
            pipeline.process_folder(src, lyrics=False)
        retag.assert_called_once(); orglib.assert_called_once(); fill.assert_not_called()
