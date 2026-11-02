"""Synthetic fixtures for patterns found in the full-library review (made-up names)."""
import unittest
from pathlib import Path

import title_rules
import main


class NewRules(unittest.TestCase):
    def p(self, s):
        return title_rules.parse(s)

    def test_hd_lyric(self):
        r = self.p("【HD】张三 - 春风 [歌詞字幕][電視劇《某某》主題曲][完整高清音質] Zhang San - Spring")
        self.assertEqual((r["artist"], r["title"], r["rule"]), ("张三", "春风", "hd_lyric"))

    def test_quote_lyric(self):
        r = self.p("李四 & 王五 - 晚风【歌詞字幕 / 完整高清音質】♫「吹过山岗...」Li Si - Wind")
        self.assertEqual((r["artist"], r["title"], r["featured"]), ("李四", "晚风", ["王五"]))

    def test_original_singer_paren(self):
        r = self.p("赵六 - 某首歌(原唱:钱七)『这是一句很长的歌词啊』【動態歌詞MV】")
        self.assertEqual((r["title"], r["original_artist"]), ("某首歌", "钱七"))

    def test_source_block_and_quotes(self):
        self.assertEqual(self.p("孙八 – 雨天《電視劇【某劇】主題曲》 (Official Music Video)")["title"], "雨天")
        self.assertEqual(self.p('Foo Bar - "Night Drive" (Official Music Video)')["title"], "Night Drive")

    def test_guillemet_source_paren(self):
        r = self.p("周九 Zhou Jiu《啦啦啦(某品牌40週年主題曲)》Official Music Video")
        self.assertEqual((r["artist"], r["title"]), ("周九 Zhou Jiu", "啦啦啦"))

    def test_rejects_playlist_prefix_artist(self):
        self.assertIsNone(self.p("2030流行歌曲|吴十 - 某歌(Official Music Video)"))

    def test_guillemet_needs_official(self):
        self.assertIsNone(self.p("某歌手首张专辑 某名《TELL ME》"))


class Detection(unittest.TestCase):
    def wrong(self, path, **t):
        return main._tags_look_wrong(Path(path), {"title": t.get("title"), "artist": t.get("artist"), "album": t.get("album")})

    def test_artist_holds_artist_dash_title(self):
        self.assertIn("artist_junk", self.wrong("x/y/张三 - 夜空 - DJ版.mp3", artist="张三 - 夜空", title="DJ版", album="夜空"))

    def test_artist_with_bracket_junk(self):
        self.assertIn("artist_junk", self.wrong("x/y/【歌手2030·纯享】#李四《远方》.mp3", artist="歌手2030·纯享】#李四", title="远方", album="远方"))

    def test_title_is_show(self):
        self.assertIn("title_is_show", self.wrong("x/y/舞台纯享《乘风2030》《星河》王五.mp3", artist="王五", title="乘风2030", album="乘风2030"))

    def test_clean_artist_not_flagged(self):
        self.assertEqual([], self.wrong("x/y/A-Lin - 某歌.mp3", artist="A-Lin", title="某歌", album="某歌"))

    def test_channel_album_regex(self):
        self.assertTrue(main._CHANNEL_ALBUM_RE.search("Some Music - Videos"))
        self.assertFalse(main._CHANNEL_ALBUM_RE.search("Greatest Hits"))


if __name__ == "__main__":
    unittest.main()
