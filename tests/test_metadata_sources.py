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


SP_TRACK = {"tracks": {"items": [{
    "name": "Blinding Lights",
    "artists": [{"name": "The Weeknd", "id": "a1"}],
    "album": {"name": "After Hours", "album_type": "album", "release_date": "2020-03-20",
              "images": [{"url": "small", "width": 64}, {"url": "big", "width": 640}]},
}]}}
SP_ARTIST = {"genres": ["canadian pop", "pop"]}
LF_TRACK = {"track": {"name": "Blinding Lights", "artist": {"name": "The Weeknd"},
                      "album": {"title": "After Hours", "image": [{"#text": "lf_s"}, {"#text": "lf_xl"}]},
                      "toptags": {"tag": [{"name": "seen live"}, {"name": "synthpop"}]}}}


def keys(sp=True, lf=True):
    return mock.patch.multiple(config,
        SPOTIFY_CLIENT_ID="id" if sp else "", SPOTIFY_CLIENT_SECRET="sec" if sp else "",
        LASTFM_API_KEY="lf" if lf else "")


class Base(unittest.TestCase):
    def setUp(self):
        ms.clear_cache()
        p = mock.patch.object(ms, "_throttle", lambda src: None)
        p.start(); self.addCleanup(p.stop)


class SpotifyTests(Base):
    def test_lookup_parses_and_caches(self):
        def get(url, **kw):
            return Resp(SP_ARTIST if "/artists/" in url else SP_TRACK)
        with keys(), mock.patch.object(ms.requests, "post", return_value=Resp({"access_token": "t", "expires_in": 3600})) as post, \
             mock.patch.object(ms.requests, "get", side_effect=get) as g:
            r = ms.spotify_lookup("Blinding Lights", "The Weeknd")
            ms.spotify_lookup("Blinding Lights", "The Weeknd")
        self.assertEqual(r["album"], "After Hours")
        self.assertEqual(r["year"], "2020")
        self.assertEqual(r["cover_url"], "big")
        self.assertEqual(r["genres"], ["canadian pop", "pop"])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(g.call_count, 2)  # second lookup served from cache
        self.assertIn("Basic ", post.call_args.kwargs["headers"]["Authorization"])

    def test_no_keys_no_http(self):
        with keys(sp=False), mock.patch.object(ms.requests, "post") as post, mock.patch.object(ms.requests, "get") as g:
            self.assertIsNone(ms.spotify_lookup("x", "y"))
        post.assert_not_called(); g.assert_not_called()

    def test_http_error_returns_none(self):
        with keys(), mock.patch.object(ms.requests, "post", return_value=Resp({}, 401)):
            self.assertIsNone(ms.spotify_lookup("x", "y"))

    def test_artist_mismatch_ignored(self):
        data = {"tracks": {"items": [{"name": "x", "artists": [{"name": "Someone Else"}], "album": {}}]}}
        with keys(), mock.patch.object(ms.requests, "post", return_value=Resp({"access_token": "t"})), \
             mock.patch.object(ms.requests, "get", return_value=Resp(data)):
            self.assertIsNone(ms.spotify_lookup("x", "The Weeknd"))


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
    def test_precedence_existing_wins_then_spotify_then_lastfm(self):
        meta = {"title": "Blinding Lights", "artist": "The Weeknd", "album": "From MusicBrainz"}
        sp = {"album": "SP album", "year": "2020", "genres": [], "cover_url": "sp_cover"}
        lf = {"album": "LF album", "year": None, "genres": ["synthpop"], "cover_url": "lf_cover"}
        with mock.patch.object(ms, "spotify_lookup", return_value=sp), mock.patch.object(ms, "lastfm_lookup", return_value=lf):
            ms.supplement(meta)
        self.assertEqual(meta["album"], "From MusicBrainz")
        self.assertEqual(meta["album_year"], "2020")
        self.assertEqual(meta["cover_url"], "sp_cover")
        self.assertEqual(meta["genre"], "Synthpop")

    def test_fallback_when_spotify_fails(self):
        meta = {"title": "t", "artist": "a"}
        with mock.patch.object(ms, "spotify_lookup", return_value=None), \
             mock.patch.object(ms, "lastfm_lookup", return_value={"album": "LF", "genres": ["rock"], "cover_url": None, "year": None}):
            ms.supplement(meta)
        self.assertEqual(meta["album"], "LF"); self.assertEqual(meta["genre"], "Rock")

    def test_all_sources_down(self):
        meta = {"title": "t", "artist": "a"}
        with keys(), mock.patch.object(ms.requests, "post", side_effect=OSError), mock.patch.object(ms.requests, "get", side_effect=OSError):
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
            SPOTIFY_CLIENT_ID = "cid"
            BASE_DIR = Path(r"%s")
        ''' % d))
        code = "import config, metadata_sources as m; print(config.LASTFM_API_KEY, config.SPOTIFY_CLIENT_ID, config.LOG_DIR, m.cfg.LASTFM_API_KEY)"
        env = dict(os.environ, PYTHONPATH=f"{d}{os.pathsep}{ROOT}")
        out = subprocess.run([sys.executable, "-c", code], env=env, cwd=d, capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(out[0], "from_local"); self.assertEqual(out[1], "cid")
        self.assertEqual(out[2], str(Path(d) / "logs")); self.assertEqual(out[3], "from_local")

    def test_config_py_has_no_real_keys(self):
        self.assertEqual(config.__dict__.get("SPOTIFY_CLIENT_SECRET") is not None, True)
        src = (ROOT / "config.py").read_text()
        for k in ("LASTFM_API_KEY", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"):
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
