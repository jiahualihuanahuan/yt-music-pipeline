import os, subprocess, sys, tempfile, textwrap, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config
import metadata_sources as ms


class Resp:
    def __init__(self, data, status=200, headers=None):
        self._d, self.status_code, self.headers = data, status, headers or {}
        self.ok = status < 400
    def json(self):
        return self._d
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


LF_TRACK = {"track": {"name": "Blinding Lights", "artist": {"name": "The Weeknd"},
                      "album": {"title": "After Hours", "image": [{"#text": "lf_s"}, {"#text": "lf_xl"}]},
                      "toptags": {"tag": [{"name": "seen live"}, {"name": "synthpop"}]}}}


def keys(lf=True):
    return mock.patch.multiple(config, LASTFM_API_KEY="lf" if lf else "")


class Base(unittest.TestCase):
    def setUp(self):
        ms.clear_cache()
        p = mock.patch.object(ms, "_throttle", lambda src: None)
        p.start(); self.addCleanup(p.stop)


class NoSpotifyTests(Base):
    def test_spotify_code_removed(self):
        self.assertFalse(hasattr(ms, "spotify_lookup"))
        for f in ("metadata_sources.py", "artwork.py", "musicbrainz.py", "main.py", "config.py"):
            self.assertNotIn("spotify", (ROOT / f).read_text().lower(), f)

    def test_caa_cover(self):
        with mock.patch.object(ms.requests, "head", return_value=Resp({}, 200)) as h:
            self.assertEqual(ms.caa_cover_url("rid"), "https://coverartarchive.org/release/rid/front-500")
        self.assertIn("coverartarchive.org", h.call_args.args[0])
        with mock.patch.object(ms.requests, "head", return_value=Resp({}, 404)):
            self.assertIsNone(ms.caa_cover_url("missing"))

    def test_musicbrainz_cover_lookup(self):
        data = {"recordings": [{"title": "Blinding Lights", "artist-credit": [{"name": "The Weeknd"}],
                                "releases": [{"id": "r1"}]}]}
        with mock.patch.object(ms.requests, "get", return_value=Resp(data)), \
             mock.patch.object(ms.requests, "head", return_value=Resp({}, 200)):
            self.assertEqual(ms.musicbrainz_cover_lookup("Blinding Lights", "The Weeknd"),
                             "https://coverartarchive.org/release/r1/front-500")


class LastfmTests(Base):
    def test_lookup(self):
        with keys(), mock.patch.object(ms.requests, "get", return_value=Resp(LF_TRACK)) as g:
            r = ms.lastfm_lookup("Blinding Lights", "The Weeknd")
        self.assertEqual(r["album"], "After Hours")
        self.assertEqual(r["genres"], ["synthpop"])  # "seen live" filtered
        self.assertEqual(r["cover_url"], "lf_xl")
        self.assertEqual(g.call_args.kwargs["params"]["method"], "track.getInfo")

    def test_no_key(self):
        with keys(lf=False), mock.patch.object(ms.requests, "get") as g:
            self.assertIsNone(ms.lastfm_lookup("x", "y"))
        g.assert_not_called()

    def test_error(self):
        with keys(), mock.patch.object(ms.requests, "get", side_effect=ConnectionError("down")):
            self.assertIsNone(ms.lastfm_lookup("x", "y"))


class SupplementTests(Base):
    def test_precedence_existing_wins_then_caa_then_lastfm(self):
        meta = {"title": "Blinding Lights", "artist": "The Weeknd", "album": "From MusicBrainz", "mb_release_id": "r1"}
        lf = {"album": "LF album", "year": None, "genres": ["synthpop"], "cover_url": "lf_cover"}
        with mock.patch.object(ms, "caa_cover_url", return_value="caa_cover"), \
             mock.patch.object(ms, "lastfm_lookup", return_value=lf):
            ms.supplement(meta)
        self.assertEqual(meta["album"], "From MusicBrainz")
        self.assertEqual(meta["cover_url"], "caa_cover")
        self.assertEqual(meta["genre"], "Synthpop")

    def test_lastfm_fallback(self):
        meta = {"title": "t", "artist": "a"}
        with mock.patch.object(ms, "lastfm_lookup", return_value={"album": "LF", "genres": ["rock"], "cover_url": "c", "year": None}):
            ms.supplement(meta)
        self.assertEqual(meta["album"], "LF"); self.assertEqual(meta["genre"], "Rock"); self.assertEqual(meta["cover_url"], "c")

    def test_all_sources_down(self):
        meta = {"title": "t", "artist": "a"}
        with keys(), mock.patch.object(ms.requests, "get", side_effect=OSError):
            ms.supplement(meta)
        self.assertNotIn("album", meta)


class MusicBrainzTests(Base):
    def test_urls_are_plain(self):
        import musicbrainz as mb
        for u in (mb.MB_API, mb.ITUNES_API, mb.DEEZER_API):
            self.assertTrue(u.startswith("https://") and "[" not in u, u)

    def test_mb_artist_uses_api(self):
        import musicbrainz as mb
        with mock.patch.object(mb.requests, "get", return_value=Resp({"artists": [{"name": "The Weeknd", "id": "x"}]})) as g, \
             mock.patch.object(mb.time, "sleep"):
            a = mb.mb_artist("The Weeknd")
        self.assertEqual(a["id"], "x")
        self.assertEqual(g.call_args.args[0], "https://musicbrainz.org/ws/2/artist")


class ConfigOverrideTest(unittest.TestCase):
    def test_config_local_overrides(self):
        d = tempfile.mkdtemp()
        Path(d, "config_local.py").write_text(textwrap.dedent('''
            from pathlib import Path
            LASTFM_API_KEY = "from_local"
            SPOTIFY_CLIENT_ID = "cid"  # stale key in an old config_local.py is tolerated
            BASE_DIR = Path(r"%s")
        ''' % d))
        code = "import config, metadata_sources as m; print(config.LASTFM_API_KEY, config.SPOTIFY_CLIENT_ID, config.LOG_DIR, m.cfg.LASTFM_API_KEY)"
        env = dict(os.environ, PYTHONPATH=f"{d}{os.pathsep}{ROOT}")
        out = subprocess.run([sys.executable, "-c", code], env=env, cwd=d, capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(out[0], "from_local"); self.assertEqual(out[1], "cid")
        self.assertEqual(out[2], str(Path(d) / "logs")); self.assertEqual(out[3], "from_local")

    def test_config_py_has_no_real_keys(self):
        src = (ROOT / "config.py").read_text()
        self.assertNotIn("SPOTIFY", src)
        for k in ("LASTFM_API_KEY",):
            self.assertIn(f'{k} = ""', src)


class RegexRemovedTest(unittest.TestCase):
    def test_parse_one_sends_everything_to_llm(self):
        import parse_meta
        with mock.patch.object(config, "USE_REGEX_PARSER", False):
            m = parse_meta.parse_one(Path("/nonexistent/Taylor Swift - Anti-Hero.mp3"))
        self.assertTrue(m["needs_llm"]); self.assertIsNone(m["artist"])
        self.assertEqual(m["raw"], "Taylor Swift - Anti-Hero")


if __name__ == "__main__":
    unittest.main()
