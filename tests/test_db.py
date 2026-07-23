"""Schema-migration tests for vibedentify.db.

Covers the versioned-migration machinery: a fresh database and a legacy
database (built here from the pre-versioning schema, WITHOUT the weight column
or a schema_version table — i.e. what the oldest real DBs contain) must both
upgrade to the same schema version with a structurally identical schema, and
existing rows must survive the upgrade untouched.
"""

import sqlite3

from vibedentify import db

# The schema an old build produced, written out independently of MIGRATIONS so
# this test genuinely catches a future divergence in migration 1 (e.g. someone
# inlining the weight column). vibe_tracks intentionally has NO weight column and
# there is NO schema_version table: the migration must add both in place.
LEGACY_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS tracks(hash TEXT PRIMARY KEY, filename TEXT, "
    "filepath TEXT, title TEXT, payload TEXT, embedding BLOB, created REAL)",
    "CREATE TABLE IF NOT EXISTS vibes(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)",
    "CREATE TABLE IF NOT EXISTS vibe_tracks(vibe_id INTEGER, hash TEXT, UNIQUE(vibe_id, hash))",
    "CREATE TABLE IF NOT EXISTS tags(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)",
    "CREATE TABLE IF NOT EXISTS track_tags(tag_id INTEGER, hash TEXT, UNIQUE(tag_id, hash))",
    "CREATE TABLE IF NOT EXISTS training_labels(hash TEXT, genre TEXT, source TEXT, "
    "created REAL, UNIQUE(hash, genre))",
    "CREATE TABLE IF NOT EXISTS training_rejects(hash TEXT, genre TEXT, UNIQUE(hash, genre))",
    "CREATE TABLE IF NOT EXISTS segment_overrides(hash TEXT, start_s REAL, end_s REAL, "
    "genre TEXT, created REAL)",
    "CREATE TABLE IF NOT EXISTS lookup_cache(hash TEXT, source TEXT, response_json TEXT, "
    "fetched REAL, UNIQUE(hash, source))",
    "CREATE TABLE IF NOT EXISTS waveform_cache(hash TEXT PRIMARY KEY, data_json TEXT, created REAL)",
]


def _norm(sql):
    """Compare stored CREATE text by non-whitespace structure only: SQLite keeps
    the original formatting, and moving the schema into a function re-indented it,
    so whitespace differences are incidental while any column/constraint change is
    still caught (every non-whitespace character is preserved)."""
    return "".join(sql.split()) if sql else sql


def _schema(path):
    """Normalized (type, name, sql) for every non-internal object in the DB."""
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    finally:
        con.close()
    return [(t, n, _norm(s)) for t, n, s in rows]


def _version(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT version FROM schema_version").fetchone()[0]
    finally:
        con.close()


def _make_legacy(path):
    """A populated pre-versioning database: old schema + a couple of rows."""
    con = sqlite3.connect(path)
    try:
        for stmt in LEGACY_SCHEMA:
            con.execute(stmt)
        con.execute(
            "INSERT INTO tracks(hash, filename, title) VALUES(?,?,?)",
            ("h1", "song.mp3", "Song"),
        )
        con.execute("INSERT INTO vibes(name) VALUES(?)", ("night",))
        con.execute("INSERT INTO vibe_tracks(vibe_id, hash) VALUES(?,?)", (1, "h1"))
        con.commit()
    finally:
        con.close()


def test_fresh_and_legacy_converge(tmp_path, monkeypatch):
    fresh = tmp_path / "fresh.db"
    legacy = tmp_path / "legacy.db"

    # Fresh DB: init from nothing.
    monkeypatch.setattr(db, "DB_PATH", fresh)
    db.init_db()

    # Legacy DB: build the old schema by hand, populate it, then migrate.
    _make_legacy(legacy)
    monkeypatch.setattr(db, "DB_PATH", legacy)
    db.init_db()

    # Both land on the same version...
    assert _version(fresh) == 1
    assert _version(legacy) == 1

    # ...with a structurally identical schema (incl. schema_version + the weight
    # column the migration added to the legacy vibe_tracks in place).
    assert _schema(fresh) == _schema(legacy)
    legacy_cols = {r[1] for r in sqlite3.connect(legacy).execute("PRAGMA table_info(vibe_tracks)")}
    assert "weight" in legacy_cols

    # ...and the legacy rows survived, with the new column defaulted.
    con = sqlite3.connect(legacy)
    try:
        assert con.execute("SELECT title FROM tracks WHERE hash='h1'").fetchone()[0] == "Song"
        assert con.execute("SELECT name FROM vibes").fetchone()[0] == "night"
        assert con.execute("SELECT weight FROM vibe_tracks WHERE hash='h1'").fetchone()[0] == 1.0
    finally:
        con.close()


def test_init_db_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "x.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    before = _schema(path)
    db.init_db()  # second run applies nothing
    assert _schema(path) == before
    assert _version(path) == 1
    # exactly one version row, not one appended per run
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    finally:
        con.close()
