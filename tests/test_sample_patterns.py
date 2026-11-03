"""Tests built from the shapes seen in a real library sample.

All names below are synthetic / anonymized; no real library data is stored here.
No network calls: enrichment HTTP is mocked.
"""
import sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    import main
import title_rules as tr
import match_guard as mg
import metadata_sources as ms
import parse_meta as pm


class TitleRules(unittest.TestCase):
    def p(self, raw):
        r = tr.parse(raw)
        return (r["artist"], r["title"]) if r else None

    def test_lyric_channel(self):
        self.assertEqual(self.p("张三 - 夜空『想把星星都摘給你，我的夜空只有你。』【動態歌詞Lyrics】"), ("张三", "夜空"))
        self.assertEqual(self.p("張三（Zhang San） - 夜空『歌詞』【動態歌詞⧸Pinyin Lyrics】♫"), ("張三(Zhang San)", "夜空"))  # alias kept (Jenny: keep)

    def test_lyric_channel_multi_artist_and_hyphen_name(self):
        r = tr.parse("王五⧸赵六 - 心事『一句歌詞』【動態歌詞Lyrics】")
        self.assertEqual((r["artist"], r["title"], r["featured"]), ("王五", "心事", ["赵六"]))
        self.assertEqual(self.p("Ka-9x - 會不會『一句歌詞』【動態歌詞Lyrics】"), ("Ka-9x", "會不會"))

    def test_cover_shapes(self):
        r = tr.parse("【小明】磁性翻唱 對的時間點-（Cover：某歌手）")
        self.assertEqual((r["artist"], r["title"], r["original_artist"]), ("小明", "對的時間點", "某歌手"))
        r = tr.parse("【小明】煙嗓翻唱【晴天】Cover：周杰倫 【Xiao Ming】Smoky Voice")
        self.assertEqual((r["artist"], r["title"], r["original_artist"]), ("小明", "晴天", "周杰倫"))

    def test_guillemet_and_official(self):
        self.assertEqual(self.p("某歌手《大鱼》Official Audio"), ("某歌手", "大鱼"))
        self.assertEqual(self.p("Some Singer - Let It Go (from Frozen) (Official Video)"), ("Some Singer", "Let It Go"))

    def test_abstains_on_ambiguous(self):
        for raw in [
            "中国美女翻唱童年回忆《某剧》主题曲某歌，听到这首歌你想起了什么？",  # 《》 is the drama
            "我和自己搭档！挑战一人双声线翻唱《手心的蔷薇》",                     # clickbait prefix
            "Pocahontas - Colors of the Wind HD",                                   # movie, not artist
            "Dig a Little Deeper - Princess and the Frog",                          # title first
            "Journey To Somewhere PART 1⧸ Vlog Review No. 367",
            "某歌手：我的巡演要开始了！",
            "Ultimate Disney Playlist - Song (Official Video)",
            "Wonderful U",
        ]:
            self.assertIsNone(tr.parse(raw), raw)

    def test_clean_raw(self):
        self.assertEqual(tr.clean_raw("Some Show： Episode Name [sHSgwpzqKQY]"), "Some Show: Episode Name")
        self.assertEqual(tr.clean_raw("A⧸B ＂Song＂ ｜ X"), 'A/B "Song" | X')


class Detection(unittest.TestCase):
    def test_youtube_categories_are_junk(self):
        for g in ["Travel & Events", "News & Politics", "Science & Technology", "Sports",
                  "Film & Animation", "People & Blogs", "Music", "Entertainment", "Education"]:
            self.assertIn(g.lower(), main.JUNK_GENRES)
        self.assertNotIn("mandopop", main.JUNK_GENRES)

    def test_uploader_as_artist(self):
        for a in ["DisneyMusicVEVO", "Holy007", "Someone - Topic", "Stories with Grandma", "Disney Kids"]:
            self.assertTrue(main._looks_like_uploader(a), a)
        for a in ["Idina Menzel", "张三", "A-Lin", "Phil Collins"]:
            self.assertFalse(main._looks_like_uploader(a), a)

    def test_tags_look_wrong(self):
        r = main._tags_look_wrong(Path("Mix - X/张三 - 夜空『歌詞』【動態歌詞Lyrics】.mp3"),
                                  {"title": "夜空『歌詞』", "artist": "张三", "album": "夜空"})
        self.assertIn("title_junk", r)
        # cover tagged with the original singer instead of the performer
        r = main._tags_look_wrong(Path("covers/【小明】翻唱【晴天】Cover：周杰倫.mp3"),
                                  {"title": "晴天", "artist": "周杰倫", "album": "晴天"})
        self.assertIn("artist_mismatch", r)
        r = main._tags_look_wrong(Path("Disney/Let It Go.mp3"),
                                  {"title": "Let It Go", "artist": "DisneyMusicVEVO", "album": "Frozen"})
        self.assertIn("artist_is_uploader", r)
        r = main._tags_look_wrong(Path("x/张三 - 夜空『歌詞』【動態歌詞Lyrics】.mp3"),
                                  {"title": "夜空", "artist": "张三", "album": "夜空"})
        self.assertEqual(r, [])


    def test_more_wrong_signals(self):
        r = main._tags_look_wrong(Path("Mix - 某人/Falling You.mp3"),
                                  {"title": "Falling into You", "artist": "Céline Dion", "album": "Falling into You"})
        self.assertIn("title_not_in_filename", r)
        r = main._tags_look_wrong(Path("某人合集/某人：我的巡演要开始了！.mp3"),
                                  {"title": "你是 (You Are)", "artist": "某人", "album": "你是"})
        self.assertIn("title_not_in_filename", r)
        r = main._tags_look_wrong(Path("covers/煙嗓翻唱【剛剛好】（cover：某歌手）.mp3"),
                                  {"title": "剛剛好", "artist": "某歌手", "album": "剛剛好"})
        self.assertIn("cover_tagged_as_original", r)
        # Traditional filename vs Simplified tag is not "wrong"
        r = main._tags_look_wrong(Path("x/對的時間點.mp3"), {"title": "对的时间点", "artist": "某人", "album": "对的时间点"})
        self.assertNotIn("title_not_in_filename", r)


class Guard(unittest.TestCase):
    def test_rejects_different_song(self):
        self.assertFalse(mg.accept("Falling You", "某歌手", "Falling into You", "Céline Dion"))
        self.assertFalse(mg.accept("Falling You", "Céline Dion", "Falling into You", "Céline Dion"))

    def test_accepts_variants(self):
        self.assertTrue(mg.accept("晴天", "周杰伦", "晴天", "周杰倫"))           # trad vs simp
        self.assertTrue(mg.accept("Let It Go", "Idina Menzel", "Let It Go (From \"Frozen\")", "Idina Menzel"))
        self.assertTrue(mg.accept("Beauty and the Beast", "Ariana Grande", "Beauty and the Beast", "Ariana Grande, John Legend"))

    def test_lastfm_needs_title_match(self):
        ms.clear_cache()
        wrong = {"track": {"name": "Falling into You", "artist": {"name": "Céline Dion"}, "album": {}}}
        resp = mock.Mock(status_code=200); resp.json.return_value = wrong; resp.raise_for_status = lambda: None
        with mock.patch.object(ms.cfg, "LASTFM_API_KEY", "x"), mock.patch.object(ms, "_throttle", lambda s: None), \
             mock.patch.object(ms.requests, "get", return_value=resp):
            self.assertIsNone(ms.lastfm_lookup("Falling You", "Céline Dion"))
        ms.clear_cache()

    def test_supplement_skips_non_music(self):
        with mock.patch.object(ms, "lastfm_lookup") as sp:
            ms.supplement({"title": "x", "artist": "y", "is_music": False})
            sp.assert_not_called()


class LLMRow(unittest.TestCase):
    def test_non_music_clears(self):
        m = {"title": "你是", "artist": "某歌手"}
        pm._apply_row(m, {"title": "你是", "artist": "某歌手", "is_music": False, "confidence": "high", "notes": "talk"})
        self.assertFalse(m["is_music"]); self.assertEqual((m["title"], m["artist"]), ("", ""))

    def test_low_confidence_not_written(self):
        m = {}
        pm._apply_row(m, {"title": "Falling into You", "artist": "Céline Dion", "is_music": True, "confidence": "low"})
        self.assertEqual((m["title"], m["artist"]), ("", ""))

    def test_keeps_script_and_punctuation(self):
        m = {}
        pm._apply_row(m, {"title": "You're Welcome", "artist": "Fire EX.", "original_artist": "周傑倫",
                          "is_music": True, "confidence": "high"})
        self.assertEqual((m["title"], m["artist"], m["original_artist"]), ("You're Welcome", "Fire EX.", "周傑倫"))

    def test_prompt_mentions_key_rules(self):
        for k in ("is_music", "confidence", "original_artist", "Traditional", "VEVO", "『", "folder"):
            self.assertIn(k, pm.SYSTEM_PROMPT)


class NonMusicMarker(unittest.TestCase):
    def test_mark_and_skip(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "talk.mp3"; f.write_bytes(b"\x00" * 128)
            with mock.patch.object(main, "MUSIC_DIR", Path(d)):
                main._handle_non_music(f, {"llm_notes": "talk clip"})
            self.assertTrue(f.exists())
            self.assertTrue(main._is_marked_non_music(f))

    def test_rules_fill_meta(self):
        m = {"raw": "张三 - 夜空『歌詞』【動態歌詞Lyrics】"}
        self.assertTrue(main._apply_rules(m))
        self.assertEqual((m["artist"], m["title"], m["llm_authoritative"]), ("张三", "夜空", True))
        self.assertFalse(main._apply_rules({"raw": "Wonderful U"}))


if __name__ == "__main__":
    unittest.main()
