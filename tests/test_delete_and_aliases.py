"""Delete-dir moves, 伴奏/DJ detection, artist alias merge (synthetic files only)."""
import csv
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import artist_aliases
import deletion
import main
import title_rules

FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 413  # one silent MPEG-1 Layer III frame


def make_mp3(path: Path, title="", artist="", album="", payload=b""):
    from mutagen.id3 import ID3, TIT2, TPE1, TALB
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(FRAME * 20 + payload)
    t = ID3()
    for cls, v in ((TIT2, title), (TPE1, artist), (TALB, album)):
        if v:
            t.add(cls(encoding=3, text=[v]))
    t.save(path)
    return path


class VersionReason(unittest.TestCase):
    def test_instrumental(self):
        for s in ["张三 - 夜空 - 伴奏", "夜空 (伴奏版)", "Song (Karaoke Version)", "Aria (karaoke)"]:
            self.assertEqual(deletion.version_reason(s), "instrumental", s)

    def test_dj(self):
        for s in ["张三 - 夜空 - DJ版", "夜空（DJ某某版）", "李四 - 晚风 - DJ0.9降速版", "晚风 DJ抖音版"]:
            self.assertEqual(deletion.version_reason(s), "dj", s)

    def test_not_versions(self):
        for s in ["DJ Snake - Taki Taki", "张三 - 夜空『我弹琴为你伴奏』【動態歌詞MV】",
                  "歌名【ピアノ伴奏⧸うた入り】", "某歌手 - 夜空 (Live)", "Vietsub & karaoke 某歌", "纯音乐 钢琴曲"]:
            self.assertIsNone(deletion.version_reason(s), s)


class DeleteRunMoves(unittest.TestCase):
    def test_moves_keep_relpath_and_never_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root, ddir, logs = Path(d) / "lib", Path(d) / "_to_delete", Path(d) / "logs"
            a = make_mp3(root / "X" / "Y" / "s.mp3")
            (ddir / "X" / "Y").mkdir(parents=True)
            (ddir / "X" / "Y" / "s.mp3").write_bytes(b"older")
            run = deletion.DeleteRun(root, ddir, logs, enabled=True)
            self.assertTrue(run.consider(a, "dj"))
            self.assertFalse(a.exists())
            self.assertEqual((ddir / "X" / "Y" / "s.mp3").read_bytes(), b"older")
            self.assertTrue((ddir / "X" / "Y" / "s_1.mp3").exists())
            cand, moved = run.write_logs()
            self.assertEqual(next(csv.DictReader(cand.open()))["action"], "moved")
            self.assertIn("s_1.mp3", moved.read_text())

    def test_low_confidence_and_disabled_do_not_move(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "lib"
            a = make_mp3(root / "a.mp3")
            run = deletion.DeleteRun(root, Path(d) / "del", Path(d) / "logs", enabled=True)
            self.assertFalse(run.consider(a, "non-music: talk", "medium"))
            off = deletion.DeleteRun(root, Path(d) / "del", Path(d) / "logs", enabled=False)
            self.assertFalse(off.consider(a, "dj"))
            self.assertTrue(a.exists())
            self.assertEqual([c["action"] for c in run.candidates + off.candidates], ["review", "dry-run"])

    def test_list_skips_delete_dir(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            make_mp3(root / "keep" / "a.mp3")
            make_mp3(root / "_to_delete" / "b.mp3")
            with mock.patch("config.DELETE_DIR", root / "_to_delete"):
                names = [p.name for p in main._list_mp3s(root)]
            self.assertEqual(names, ["a.mp3"])

    def test_non_music_high_confidence_moved(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "lib"
            a = make_mp3(root / "talk.mp3")
            run = deletion.DeleteRun(root, Path(d) / "del", Path(d) / "logs", enabled=True)
            with mock.patch("config.NON_MUSIC_ACTION", "delete"), mock.patch.object(main, "_DELETE_RUN", run):
                main._handle_non_music(a, {"llm_notes": "talk clip", "confidence": "high"})
            self.assertFalse(a.exists())
            self.assertTrue((Path(d) / "del" / "talk.mp3").exists())


class Aliases(unittest.TestCase):
    def test_groups_and_canonical(self):
        g = artist_aliases.build(Counter({"Jay Chou": 160, "周杰倫 Jay Chou": 150, "周杰伦": 40,
                                          "张三, 李四": 3, "某某 - 夜空": 2, "Solo": 5}))
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["canonical"], "周杰伦 Jay Chou")  # bilingual form wins, converted to Simplified
        self.assertEqual(set(g[0]["variants"]), {"Jay Chou", "周杰倫 Jay Chou", "周杰伦"})

    def test_latin_name_shared_by_two_people_not_merged(self):
        g = artist_aliases.build(Counter({"王一 Eric": 5, "李二 Eric": 5, "Eric": 9}))
        self.assertTrue(all("Eric" not in x["variants"] for x in g))

    def test_merge_dry_run_then_apply(self):
        with tempfile.TemporaryDirectory() as d:
            org = Path(d) / "organized"
            a = make_mp3(org / "Jay Chou" / "晴天" / "晴天.mp3", "晴天", "Jay Chou", "晴天")
            dup_src = make_mp3(org / "周杰伦" / "稻香" / "稻香.mp3", "稻香", "周杰伦", "稻香", b"x")
            make_mp3(org / "周杰倫 Jay Chou" / "稻香" / "稻香.mp3", "稻香", "周杰倫 Jay Chou", "稻香", b"y")
            amap = Path(d) / "aliases.json"
            artist_aliases.save(amap, {"Jay Chou": "周杰倫 Jay Chou", "周杰伦": "周杰倫 Jay Chou"})
            with mock.patch("config.ARTIST_ALIASES_FILE", amap), mock.patch.object(main, "ORGANIZED_DIR", org), \
                    mock.patch.object(main, "LOG_DIR", Path(d) / "logs"):
                main.merge_artists_folder(org, apply=False)
                self.assertTrue(a.exists())
                main.merge_artists_folder(org, apply=True)
            dest = org / "周杰倫 Jay Chou"
            self.assertTrue((dest / "晴天" / "晴天.mp3").exists())
            self.assertTrue((dest / "稻香" / "稻香.mp3").exists())
            self.assertTrue((dest / "稻香" / "稻香_1.mp3").exists())  # collision renamed, not overwritten
            self.assertEqual(main._read_tags(dest / "晴天" / "晴天.mp3")["artist"], "周杰倫 Jay Chou")
            self.assertFalse(dup_src.exists())


class Conventions(unittest.TestCase):
    def test_alias_in_brackets_kept(self):
        r = title_rules.parse("某某(本名) - 夜空『这是一句很长的歌词啊』【動態歌詞MV】")
        self.assertEqual(r["artist"], "某某(本名)")

    def test_feat_paren_still_dropped(self):
        self.assertEqual(title_rules._split("张三 (feat. 李四)"), ["张三"])

    def test_original_artist_written_to_txxx(self):
        with tempfile.TemporaryDirectory() as d:
            f = make_mp3(Path(d) / "c.mp3")
            main._write_tags(f, "晴天", "小明", "晴天", original_artist="周杰倫")
            from mutagen.id3 import ID3
            t = ID3(f)
            self.assertEqual(str(t["TPE1"].text[0]), "小明")
            self.assertEqual(str(t["TXXX:ORIGINAL_ARTIST"].text[0]), "周杰倫")


if __name__ == "__main__":
    unittest.main()


class JennyRound2(unittest.TestCase):
    def test_livestream(self):
        for s in ["某歌手「晴天」Cover by 小明 直播歌切", "小明直播唱《夜空》", "【小明】【歌切】温柔的歌声", "Song (live stream)"]:
            self.assertEqual(deletion.version_reason(s), "livestream", s)
        for s in ["非歌回｜九张机 • 某某", "某某 - 夜空 (Live)", "【纯享】某某《远方》"]:
            self.assertNotEqual(deletion.version_reason(s), "livestream", s)

    def test_canonical_prefers_simplified(self):
        g = artist_aliases.build(Counter({"周杰倫 Jay Chou": 150, "周杰伦 Jay Chou": 2, "Jay Chou": 160}))
        self.assertEqual(g[0]["canonical"], "周杰伦 Jay Chou")

    def test_canonical_converted_when_no_simplified(self):
        import match_guard
        g = artist_aliases.build(Counter({"張三 Zhang San": 5, "Zhang San": 9}))
        self.assertEqual(g[0]["canonical"], match_guard._t2s("張三 Zhang San"))
