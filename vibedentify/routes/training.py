"""Training routes: save a training clip, and the labeling-accelerator queue
(candidates / confirm / reject)."""

import json
import time
from contextlib import closing
from pathlib import Path

from flask import jsonify, request

from ..db import (
    _db_lock,
    cosine,
    db,
)
from ._shared import bp


@bp.post("/save_training")
def save_training_route():
    """Save a track as training data for the custom genre head.
    Accepts either a file upload (dropped tracks) or a server-side filepath (batch).
    Creates ~/genre_training/<genre>/<filename> if needed."""
    import shutil

    genre_raw = request.form.get("genre", "").strip()
    if not genre_raw:
        return jsonify({"error": "genre required"}), 400

    # sanitize the genre name for use as a folder name
    safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in genre_raw).strip()
    if not safe:
        return jsonify({"error": "invalid genre name"}), 400

    dest_dir = Path.home() / "genre_training" / safe
    dest_dir.mkdir(parents=True, exist_ok=True)

    # server-side path (batch mode) -- just copy directly
    filepath = request.form.get("filepath", "").strip()
    if filepath:
        src = Path(filepath)
        if not src.is_file():
            return jsonify({"error": f"file not found: {filepath}"}), 404
        dest = dest_dir / src.name
        existed = dest.exists()  # capture BEFORE the copy creates it
        if not existed:
            shutil.copy2(src, dest)
        return jsonify({"saved": str(dest), "genre": safe, "new": not existed})

    # browser upload (dropped tracks)
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "no file or filepath provided"}), 400
    dest = dest_dir / Path(f.filename).name
    if not dest.exists():
        f.save(str(dest))
    return jsonify({"saved": str(dest), "genre": safe})


# ---------------------------------------------------------------------------
# Labeling accelerator -- rank unlabeled cached tracks by embedding similarity
# to a genre's centroid, so hand-labelling a training set is confirm/reject
# instead of hunt-and-peck. Feeds the same ~/genre_training/<genre>/ folders the
# custom-head pipeline (training/) consumes.
# ---------------------------------------------------------------------------
def _copy_into_training(filepath, genre):
    """Copy a server-side audio file into ~/genre_training/<genre>/ -- the same
    destination /save_training and /override file into. Returns True if the track
    was filed (an already-present copy counts), False when there's no usable
    server-side source (e.g. a browser-dropped track with no filepath). Callers
    MUST invoke this OUTSIDE the DB lock (it does disk I/O)."""
    import shutil

    if not filepath:
        return False
    safe = "".join(ch if ch.isalnum() or ch in " _-" else "_" for ch in genre).strip()
    src = Path(filepath)
    if not safe or not src.is_file():
        return False
    dest_dir = Path.home() / "genre_training" / safe
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if not dest.exists():
        shutil.copy2(src, dest)
    return True


@bp.get("/training/candidates/<genre>")
def training_candidates(genre):
    """Cached tracks ranked by embedding similarity to a genre's centroid, for
    hand-labelling. The centroid is the mean embedding of tracks already labelled
    this genre -- from a manual override in the cached payload OR a prior confirm
    in training_labels. Excludes tracks already labelled this genre or previously
    rejected for it. Returns hash/title/sim/bpm/camelot, most-similar first. An
    empty centroid (nothing labelled yet) returns a clear message, not an error."""
    import numpy as np

    genre = (genre or "").strip()
    if not genre:
        return jsonify({"error": "genre required"}), 400
    try:
        limit = int(request.args.get("limit", 25))
    except (TypeError, ValueError):
        limit = 25
    limit = max(1, min(200, limit))

    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute("SELECT hash, title, filename, payload, embedding FROM tracks").fetchall()
        labeled = {
            r[0] for r in c.execute("SELECT hash FROM training_labels WHERE genre=?", (genre,))
        }
        rejected = {
            r[0] for r in c.execute("SELECT hash FROM training_rejects WHERE genre=?", (genre,))
        }

    # parse once; fold in override-labelled tracks as part of the labelled set
    parsed = {}
    for h, title, filename, payload, blob in rows:
        p = json.loads(payload) if payload else {}
        parsed[h] = (title, filename, p, blob)
        if p.get("override") == genre:
            labeled.add(h)

    # centroid = mean embedding over the labelled tracks that actually have one
    embs = [
        np.frombuffer(parsed[h][3], dtype=np.float32)
        for h in labeled
        if parsed.get(h) and parsed[h][3] is not None
    ]
    if not embs:
        return jsonify(
            {
                "genre": genre,
                "labeled": len(labeled),
                "candidates": [],
                "message": (
                    f"No labelled examples of “{genre}” yet — nothing to rank against. "
                    f"Override a few tracks to “{genre}” (each seeds the centroid), then "
                    f"this queue ranks the rest of your library by similarity."
                ),
            }
        )
    centroid = np.mean(np.vstack(embs), axis=0)

    out = []
    for h, (title, filename, p, blob) in parsed.items():
        if blob is None or h in labeled or h in rejected:
            continue
        emb = np.frombuffer(blob, dtype=np.float32)
        out.append(
            {
                "hash": h,
                "title": title or (Path(filename).stem if filename else h[:8]),
                "sim": round(cosine(emb, centroid), 4),
                "bpm": p.get("bpm"),
                "camelot": p.get("camelot"),
            }
        )
    out.sort(key=lambda x: -x["sim"])
    return jsonify({"genre": genre, "labeled": len(labeled), "candidates": out[:limit]})


@bp.post("/training/confirm")
def training_confirm():
    """Accept a candidate into the training set for <genre>: record it in
    training_labels (source 'propagation') so it's counted toward the centroid and
    never re-offered, and copy its audio into ~/genre_training/<genre>/. A confirm
    also clears any prior reject of the same (hash, genre)."""
    data = request.get_json(silent=True) or {}
    h = (data.get("hash") or "").strip()
    genre = (data.get("genre") or "").strip()
    if not h or not genre:
        return jsonify({"error": "hash and genre required"}), 400
    with _db_lock, closing(db()) as conn, conn as c:
        row = c.execute("SELECT filepath FROM tracks WHERE hash=?", (h,)).fetchone()
        if not row:
            return jsonify({"error": "track not in database"}), 404
        filepath = row[0]
        c.execute(
            "INSERT OR IGNORE INTO training_labels(hash, genre, source, created) VALUES(?,?,?,?)",
            (h, genre, "propagation", time.time()),
        )
        c.execute("DELETE FROM training_rejects WHERE hash=? AND genre=?", (h, genre))
    # disk I/O stays OUTSIDE the lock (never hold the DB lock across a copy)
    trained = _copy_into_training(filepath, genre)
    return jsonify({"ok": True, "hash": h, "genre": genre, "trained": trained})


@bp.post("/training/reject")
def training_reject():
    """Reject a candidate for <genre> so it never resurfaces in that queue.
    Does not touch the track's analysis or any other genre's queue."""
    data = request.get_json(silent=True) or {}
    h = (data.get("hash") or "").strip()
    genre = (data.get("genre") or "").strip()
    if not h or not genre:
        return jsonify({"error": "hash and genre required"}), 400
    with _db_lock, closing(db()) as conn, conn as c:
        c.execute("INSERT OR IGNORE INTO training_rejects(hash, genre) VALUES(?,?)", (h, genre))
    return jsonify({"ok": True, "hash": h, "genre": genre})
