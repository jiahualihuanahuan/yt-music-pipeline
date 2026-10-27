"""Local SQLite cache of MusicBrainz metadata for cross-checking parsed titles.

Stores both normalized keys (for fast case-insensitive matching) and
canonical display names (so tags preserve proper capitalization).
"""
import sqlite3
import re
from config import LOCAL_DB_PATH

_NORM_RE = re.compile(r"[\x5b\x5d(){}<>\u3010\u3011\u300a\u300b\u300c\u300d\u300e\u300f\u201c\u201d\u2018\u2019\"\'\s+]+")

_warned = False


def normalize(s):
    if not s:
        return ""
    s = s.lower().strip()
    s = _NORM_RE.sub(" ", s)
    return " ".join(s.split())


def connect():
    """Open the local MusicBrainz cache. Falls back to in-memory DB if the path is unusable."""
    global _warned
    try:
        LOCAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(LOCAL_DB_PATH)
    except (OSError, sqlite3.OperationalError) as e:
        if not _warned:
            print(f"WARNING: cannot open local DB at {LOCAL_DB_PATH}: {e}")
            print("         Using an in-memory cache for this run. Create config_local.py with a writable BASE_DIR for persistence.")
            _warned = True
        conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA foreign_keys=ON")
    # Ensure schema exists even on the in-memory fallback
    init_db(conn)
    return conn


def _ensure_column(conn, table: str, col: str, col_type: str = "TEXT"):
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


def init_db(conn=None):
    own = conn is None
    if own:
        conn = connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS artists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            display_name TEXT,
            sort_name TEXT,
            region TEXT,
            source TEXT DEFAULT 'musicbrainz',
            UNIQUE(name, region)
        );
        CREATE TABLE IF NOT EXISTS albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            display_title TEXT,
            artist_id INTEGER REFERENCES artists(id),
            year INTEGER,
            source TEXT DEFAULT 'musicbrainz',
            UNIQUE(title, artist_id)
        );
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            display_title TEXT,
            album_id INTEGER REFERENCES albums(id),
            artist_id INTEGER REFERENCES artists(id),
            duration_ms INTEGER,
            source TEXT DEFAULT 'musicbrainz',
            UNIQUE(title, album_id, artist_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks(title);
        CREATE INDEX IF NOT EXISTS idx_albums_title ON albums(title);
        CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name);
    """)
    # Seamless migration for existing DBs created before display_* columns
    _ensure_column(conn, "artists", "display_name")
    _ensure_column(conn, "albums", "display_title")
    _ensure_column(conn, "tracks", "display_title")
    conn.commit()
    if own:
        conn.close()


def upsert_artist(conn, name, sort_name=None, region=None, source="musicbrainz"):
    n = normalize(name)
    if not n:
        return None
    disp = name.strip()
    cur = conn.execute(
        "INSERT INTO artists(name, display_name, sort_name, region, source) VALUES(?,?,?,?,?) "
        "ON CONFLICT(name, region) DO UPDATE SET "
        "display_name=COALESCE(excluded.display_name, artists.display_name), "
        "sort_name=COALESCE(excluded.sort_name, artists.sort_name) "
        "RETURNING id",
        (n, disp, normalize(sort_name) if sort_name else None, region, source),
    )
    return cur.fetchone()[0]


def upsert_album(conn, title, artist_id, year=None, source="musicbrainz"):
    n = normalize(title)
    if not n or artist_id is None:
        return None
    disp = title.strip()
    cur = conn.execute(
        "INSERT INTO albums(title, display_title, artist_id, year, source) VALUES(?,?,?,?,?) "
        "ON CONFLICT(title, artist_id) DO UPDATE SET "
        "display_title=COALESCE(excluded.display_title, albums.display_title), "
        "year=COALESCE(excluded.year, albums.year) "
        "RETURNING id",
        (n, disp, artist_id, year, source),
    )
    return cur.fetchone()[0]


def upsert_track(conn, title, album_id, artist_id, duration_ms=None, source="musicbrainz"):
    n = normalize(title)
    if not n:
        return None
    disp = title.strip()
    cur = conn.execute(
        "INSERT INTO tracks(title, display_title, album_id, artist_id, duration_ms, source) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(title, album_id, artist_id) DO UPDATE SET "
        "display_title=COALESCE(excluded.display_title, tracks.display_title), "
        "duration_ms=COALESCE(excluded.duration_ms, tracks.duration_ms) "
        "RETURNING id",
        (n, disp, album_id, artist_id, duration_ms, source),
    )
    return cur.fetchone()[0]


def lookup_track(conn, title, artist=None):
    n = normalize(title)
    if not n:
        return []
    if artist:
        na = normalize(artist)
        rows = conn.execute(
            "SELECT COALESCE(t.display_title, t.title) AS title, t.duration_ms, "
            "COALESCE(a.display_name, a.name) AS artist, a.region AS region, "
            "COALESCE(al.display_title, al.title) AS album, al.year "
            "FROM tracks t JOIN artists a ON a.id=t.artist_id "
            "LEFT JOIN albums al ON al.id=t.album_id "
            "WHERE t.title=? AND a.name=? "
            "ORDER BY al.year IS NULL, al.year ASC",
            (n, na),
        ).fetchall()
        if rows:
            return rows
    return conn.execute(
        "SELECT COALESCE(t.display_title, t.title) AS title, t.duration_ms, "
        "COALESCE(a.display_name, a.name) AS artist, a.region AS region, "
        "COALESCE(al.display_title, al.title) AS album, al.year "
        "FROM tracks t JOIN artists a ON a.id=t.artist_id "
        "LEFT JOIN albums al ON al.id=t.album_id "
        "WHERE t.title=? "
        "ORDER BY al.year IS NULL, al.year ASC",
        (n,),
    ).fetchall()


def lookup_album(conn, title, artist=None):
    n = normalize(title)
    if not n:
        return []
    if artist:
        na = normalize(artist)
        return conn.execute(
            "SELECT COALESCE(al.display_title, al.title) AS title, al.year, "
            "COALESCE(a.display_name, a.name) AS artist "
            "FROM albums al JOIN artists a ON a.id=al.artist_id "
            "WHERE al.title=? AND a.name=?",
            (n, na),
        ).fetchall()
    return conn.execute(
        "SELECT COALESCE(al.display_title, al.title) AS title, al.year, "
        "COALESCE(a.display_name, a.name) AS artist "
        "FROM albums al JOIN artists a ON a.id=al.artist_id WHERE al.title=?",
        (n,),
    ).fetchall()


def lookup_artist(conn, name):
    n = normalize(name)
    if not n:
        return None
    return conn.execute("SELECT * FROM artists WHERE name=?", (n,)).fetchone()


def stats(conn=None):
    own = conn is None
    if own:
        conn = connect()
    s = {
        "artists": conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0],
        "albums": conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0],
        "tracks": conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0],
    }
    if own:
        conn.close()
    return s


if __name__ == "__main__":
    init_db()
    print("DB initialized at", LOCAL_DB_PATH)
    print(stats())
