"""Local SQLite cache of MusicBrainz metadata for cross-checking parsed titles.

Tables: artists, albums, tracks. Lookups are case-insensitive exact match
on normalized names (lowercase, stripped, collapsed whitespace).
"""
import sqlite3
import re
from config import LOCAL_DB_PATH

_NORM_RE = re.compile(r"[\x5b\x5d(){}<>\u3010\u3011\u300a\u300b\u300c\u300d\u300e\u300f\u201c\u201d\u2018\u2019\"\'\s+]+")

def normalize(s):
    if not s:
        return ""
    s = s.lower().strip()
    s = _NORM_RE.sub(" ", s)
    return " ".join(s.split())


def connect():
    conn = sqlite3.connect(LOCAL_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn=None):
    own = conn is None
    if own:
        conn = connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS artists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_name TEXT,
            region TEXT,
            source TEXT DEFAULT 'musicbrainz',
            UNIQUE(name, region)
        );
        CREATE TABLE IF NOT EXISTS albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            artist_id INTEGER REFERENCES artists(id),
            year INTEGER,
            source TEXT DEFAULT 'musicbrainz',
            UNIQUE(title, artist_id)
        );
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
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
    if own:
        conn.close()


def upsert_artist(conn, name, sort_name=None, region=None, source="musicbrainz"):
    n = normalize(name)
    if not n:
        return None
    cur = conn.execute(
        "INSERT INTO artists(name, sort_name, region, source) VALUES(?,?,?,?) "
        "ON CONFLICT(name, region) DO UPDATE SET sort_name=excluded.sort_name "
        "RETURNING id",
        (n, normalize(sort_name) if sort_name else None, region, source),
    )
    return cur.fetchone()[0]


def upsert_album(conn, title, artist_id, year=None, source="musicbrainz"):
    n = normalize(title)
    if not n or artist_id is None:
        return None
    cur = conn.execute(
        "INSERT INTO albums(title, artist_id, year, source) VALUES(?,?,?,?) "
        "ON CONFLICT(title, artist_id) DO UPDATE SET year=excluded.year "
        "RETURNING id",
        (n, artist_id, year, source),
    )
    return cur.fetchone()[0]


def upsert_track(conn, title, album_id, artist_id, duration_ms=None, source="musicbrainz"):
    n = normalize(title)
    if not n:
        return None
    cur = conn.execute(
        "INSERT INTO tracks(title, album_id, artist_id, duration_ms, source) VALUES(?,?,?,?,?) "
        "ON CONFLICT(title, album_id, artist_id) DO UPDATE SET duration_ms=excluded.duration_ms "
        "RETURNING id",
        (n, album_id, artist_id, duration_ms, source),
    )
    return cur.fetchone()[0]


def lookup_track(conn, title, artist=None):
    n = normalize(title)
    if not n:
        return []
    if artist:
        na = normalize(artist)
        rows = conn.execute(
            "SELECT t.title, t.duration_ms, a.name AS artist, al.title AS album, al.year "
            "FROM tracks t JOIN artists a ON a.id=t.artist_id "
            "LEFT JOIN albums al ON al.id=t.album_id "
            "WHERE t.title=? AND a.name=?", (n, na)).fetchall()
        if rows:
            return rows
    return conn.execute(
        "SELECT t.title, t.duration_ms, a.name AS artist, al.title AS album, al.year "
        "FROM tracks t JOIN artists a ON a.id=t.artist_id "
        "LEFT JOIN albums al ON al.id=t.album_id WHERE t.title=?", (n,)).fetchall()


def lookup_album(conn, title, artist=None):
    n = normalize(title)
    if not n:
        return []
    if artist:
        na = normalize(artist)
        return conn.execute(
            "SELECT al.title, al.year, a.name AS artist "
            "FROM albums al JOIN artists a ON a.id=al.artist_id "
            "WHERE al.title=? AND a.name=?", (n, na)).fetchall()
    return conn.execute(
        "SELECT al.title, al.year, a.name AS artist "
        "FROM albums al JOIN artists a ON a.id=al.artist_id WHERE al.title=?", (n,)).fetchall()


def lookup_artist(conn, name):
    n = normalize(name)
    if not n:
        return None
    return conn.execute("SELECT * FROM artists WHERE name=?", (n,)).fetchone()


def stats(conn=None):
    own = conn is None
    if own:
        conn = connect()
    s = {"artists": conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0],
         "albums": conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0],
         "tracks": conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]}
    if own:
        conn.close()
    return s


if __name__ == "__main__":
    init_db()
    print("DB initialized at", LOCAL_DB_PATH)
    print(stats())
