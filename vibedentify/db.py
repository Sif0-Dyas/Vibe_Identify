"""SQLite persistence: analysis cache, embeddings, and vibe centroids."""

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

DB_PATH = Path(os.environ.get("GENRE_DB", Path.home() / "genre_v2.db"))
_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# --- schema migrations -------------------------------------------------------
# The schema is versioned. A `schema_version` table holds the highest applied
# migration number; init_db() runs every migration whose number exceeds it, in
# order, inside init_db's transaction, then records the new version. A database
# with no `schema_version` row (a fresh file, or a legacy DB from before this
# machinery) counts as version 0 and is upgraded in place.
#
# To evolve the schema: APPEND a new (version, fn) entry to MIGRATIONS with the
# next integer and a function taking an open cursor. Never edit or renumber an
# already-shipped migration — a populated database has already run it; only add
# new ones. Keep migrations re-run-safe (CREATE TABLE IF NOT EXISTS, guarded
# ALTERs) so an interrupted upgrade recovers on the next start.


def _migration_1(c):
    """v1 — the full baseline schema (everything that predates versioning).

    Moved verbatim from the pre-migration init_db, including the weight-column
    patch for older `vibe_tracks`. On a legacy DB whose tables already exist the
    CREATE ... IF NOT EXISTS calls no-op and the guarded ALTER is skipped, so it
    converges to the exact same schema a fresh DB gets — no data touched."""
    c.execute("""CREATE TABLE IF NOT EXISTS tracks(
        hash TEXT PRIMARY KEY, filename TEXT, filepath TEXT, title TEXT,
        payload TEXT, embedding BLOB, created REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS vibes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)""")
    c.execute("""CREATE TABLE IF NOT EXISTS vibe_tracks(
        vibe_id INTEGER, hash TEXT, UNIQUE(vibe_id, hash))""")
    c.execute("""CREATE TABLE IF NOT EXISTS tags(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)""")
    c.execute("""CREATE TABLE IF NOT EXISTS track_tags(
        tag_id INTEGER, hash TEXT, UNIQUE(tag_id, hash))""")
    # labeling accelerator: confirmed genre labels (source records HOW a label
    # was made -- 'propagation' from the queue, room for 'override' etc.) and
    # per-genre rejects so a rejected track never resurfaces in that queue.
    c.execute("""CREATE TABLE IF NOT EXISTS training_labels(
        hash TEXT, genre TEXT, source TEXT, created REAL,
        UNIQUE(hash, genre))""")
    c.execute("""CREATE TABLE IF NOT EXISTS training_rejects(
        hash TEXT, genre TEXT, UNIQUE(hash, genre))""")
    # segment-level manual overrides: a time range of a track labelled a genre
    # (drag-selected on the waveform). Shipped with the payload on cache hits so
    # the waveform repaints the span; also drives ffmpeg clip extraction.
    c.execute("""CREATE TABLE IF NOT EXISTS segment_overrides(
        hash TEXT, start_s REAL, end_s REAL, genre TEXT, created REAL)""")
    # external metadata-lookup cache: one row per (track, source). External
    # hits are cached permanently so a repeat click never re-queries the API.
    c.execute("""CREATE TABLE IF NOT EXISTS lookup_cache(
        hash TEXT, source TEXT, response_json TEXT, fetched REAL,
        UNIQUE(hash, source))""")
    # high-resolution min/max/rms waveform (DAW-style rendering). Computed once
    # per track -- pre-filled at analysis time, or on demand from the audio file
    # -- and cached permanently so it's never recomputed.
    c.execute("""CREATE TABLE IF NOT EXISTS waveform_cache(
        hash TEXT PRIMARY KEY, data_json TEXT, created REAL)""")
    # weighted vibe membership (Rocchio relevance feedback).
    # Older DBs have vibe_tracks(vibe_id, hash) only; add the weight column.
    cols = {r[1] for r in c.execute("PRAGMA table_info(vibe_tracks)")}
    if "weight" not in cols:
        c.execute("ALTER TABLE vibe_tracks ADD COLUMN weight REAL DEFAULT 1.0")


# Ordered, append-only list of (version, migration_fn).
MIGRATIONS = [
    (1, _migration_1),
]


def init_db():
    """Bring the database up to the latest schema version.

    Applies every migration numbered above the DB's recorded version, in order,
    within init_db's transaction, then records the new version. Idempotent: an
    already-current DB runs no migrations and just re-affirms its version."""
    with _db_lock, closing(db()) as conn, conn as c:
        c.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL)")
        row = c.execute("SELECT version FROM schema_version").fetchone()
        current = row[0] if row else 0
        for version, migrate in MIGRATIONS:
            if version > current:
                migrate(c)
                current = version
        if row is None:
            c.execute("INSERT INTO schema_version(version) VALUES(?)", (current,))
        else:
            c.execute("UPDATE schema_version SET version=?", (current,))


def file_hash(path) -> str:
    """Content hash: same song caches regardless of filename or location."""
    h = hashlib.sha1()  # nosec B324  # content cache key (dedupe by audio), not security
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_get(h: str):
    with _db_lock, closing(db()) as conn, conn as c:
        row = c.execute("SELECT payload FROM tracks WHERE hash=?", (h,)).fetchone()
    return json.loads(row[0]) if row else None


def cache_put(h: str, filename, filepath, title, payload: dict, emb):
    import numpy as np

    blob = np.asarray(emb, dtype=np.float32).tobytes() if emb is not None else None
    with _db_lock, closing(db()) as conn, conn as c:
        c.execute(
            "INSERT OR REPLACE INTO tracks VALUES(?,?,?,?,?,?,?)",
            (h, filename, filepath or "", title, json.dumps(payload), blob, time.time()),
        )


def waveform_cache_get(h: str):
    with _db_lock, closing(db()) as conn, conn as c:
        row = c.execute("SELECT data_json FROM waveform_cache WHERE hash=?", (h,)).fetchone()
    return json.loads(row[0]) if row else None


def waveform_cache_put(h: str, data: dict):
    with _db_lock, closing(db()) as conn, conn as c:
        c.execute(
            "INSERT OR REPLACE INTO waveform_cache(hash, data_json, created) VALUES(?,?,?)",
            (h, json.dumps(data), time.time()),
        )


def track_embedding(h: str):
    import numpy as np

    with _db_lock, closing(db()) as conn, conn as c:
        row = c.execute("SELECT embedding FROM tracks WHERE hash=?", (h,)).fetchone()
    if not row or row[0] is None:
        return None
    return np.frombuffer(row[0], dtype=np.float32)


def cosine(a, b):
    import numpy as np

    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def vibe_centroid(vibe_id: int):
    """Preference-weighted center of a vibe (Rocchio relevance feedback):

        center = Σ (weightᵢ · embeddingᵢ) / Σ |weightᵢ|

    Positive-weight (liked) tracks pull the center toward them; negative-weight
    (disliked) tracks push it away. Cosine ranking is scale-invariant, so the
    normalization just keeps magnitudes tame. With all weights = 1 this reduces
    to the old plain mean. Returns None if the vibe has no usable members."""
    import numpy as np

    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute(
            "SELECT t.embedding, v.weight FROM vibe_tracks v JOIN tracks t "
            "ON t.hash=v.hash WHERE v.vibe_id=? AND t.embedding IS NOT NULL",
            (vibe_id,),
        ).fetchall()
    acc, wsum = None, 0.0
    for blob, w in rows:
        if not blob:
            continue
        w = 1.0 if w is None else float(w)
        emb = np.frombuffer(blob, dtype=np.float32) * w
        acc = emb if acc is None else acc + emb
        wsum += abs(w)
    if acc is None or wsum == 0:
        return None
    return acc / wsum
