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


def init_db():
    with _db_lock, closing(db()) as conn, conn as c:
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
        # migration: weighted vibe membership (Rocchio relevance feedback).
        # Older DBs have vibe_tracks(vibe_id, hash) only; add the weight column.
        cols = {r[1] for r in c.execute("PRAGMA table_info(vibe_tracks)")}
        if "weight" not in cols:
            c.execute("ALTER TABLE vibe_tracks ADD COLUMN weight REAL DEFAULT 1.0")


def file_hash(path) -> str:
    """Content hash: same song caches regardless of filename or location."""
    h = hashlib.sha1()
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
        c.execute("INSERT OR REPLACE INTO tracks VALUES(?,?,?,?,?,?,?)",
                  (h, filename, filepath or "", title, json.dumps(payload),
                   blob, time.time()))


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
            (vibe_id,)).fetchall()
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
