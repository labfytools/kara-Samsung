"""Optional, read-only catalogue adapter for an unpacked LRCLIB snapshot."""
from contextlib import closing
from functools import lru_cache
import os
from pathlib import Path
import sqlite3
from time import monotonic


@lru_cache(maxsize=4)
def readable(path, _identity):
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=.15)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("""SELECT t.name_lower,t.artist_name_lower,t.album_name_lower,t.duration,
                                 t.last_lyrics_id,l.synced_lyrics,l.has_word_sync,l.lyricsfile
                          FROM tracks t, lyrics l LIMIT 0""")
        return True
    except (OSError, sqlite3.Error, ValueError):
        return False


def configured_snapshot():
    location = os.environ.get("PAROLE_LRCLIB_SQLITE")
    if not location:
        return None
    try:
        path = Path(location).expanduser().resolve(strict=True)
        info = path.stat()
        if not path.is_file() or path.with_name(path.name + "-wal").exists():
            return None
        # Configuration can be atomically replaced while the scheduler runs.
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        return path if readable(path, identity) else None
    except (OSError, ValueError):
        return None


def candidates(snapshot, title, artist, album, duration, owner):
    # LRCLIB's migration 01 supplies title/artist/album/duration keys and
    # last_lyrics_id. Never scan the lyric text or open the live write database.
    if not album:
        return []
    path = Path(snapshot).expanduser().resolve(strict=True)
    if not path.is_file() or path.with_name(path.name + "-wal").exists():
        raise ValueError("Une sauvegarde LRCLIB SQLite autonome est requise")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=.15)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA query_only=ON")
        deadline = monotonic() + .35
        db.set_progress_handler(lambda: int(monotonic() >= deadline), 500)
        rows = db.execute(
            """SELECT t.id, l.id AS revision_id, t.name AS trackName, t.artist_name AS artistName,
                      t.album_name AS albumName, t.duration, l.instrumental, l.has_word_sync AS hasWordSync,
                      CASE WHEN length(CAST(l.plain_lyrics AS BLOB)) <= 32000 THEN l.plain_lyrics END AS plainLyrics,
                      CASE WHEN length(CAST(l.synced_lyrics AS BLOB)) <= 64000 THEN l.synced_lyrics END AS syncedLyrics,
                      CASE WHEN length(CAST(l.lyricsfile AS BLOB)) <= 256000 THEN l.lyricsfile END AS lyricsfile
               FROM tracks t JOIN lyrics l ON l.id=t.last_lyrics_id
               WHERE t.name_lower=? AND t.artist_name_lower IN (?,?) AND t.album_name_lower=?
                     AND t.duration BETWEEN ? AND ?
               ORDER BY abs(t.duration-?),t.id LIMIT 20""",
            (title.lower(), artist.lower(), (owner or artist).lower(), album.lower(),
             duration - 3, duration + 3, duration),
        ).fetchall()
    return [dict(row, instrumental=row["instrumental"] == 1, hasWordSync=row["hasWordSync"] == 1)
            for row in rows]
