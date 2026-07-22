"""Library routes: vibes, tags, external lookup, forget, manual overrides
(track + segment), and similarity."""

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from flask import jsonify, request

from .. import lookup
from ..db import (
    _db_lock,
    cosine,
    db,
    track_embedding,
    vibe_centroid,
)
from ._shared import _artist_of, _dominant_style, bp


@bp.post("/forget/<h>")
def forget_route(h):
    """Delete a track's analysis by content hash: removes it from the cache, the
    map, and any vibe/tag membership. Does NOT touch the audio file -- dropping
    the track again will re-analyze it from scratch."""
    with _db_lock, closing(db()) as conn, conn as c:
        deleted = c.execute("DELETE FROM tracks WHERE hash=?", (h,)).rowcount
        c.execute("DELETE FROM track_tags WHERE hash=?", (h,))
        c.execute("DELETE FROM vibe_tracks WHERE hash=?", (h,))
    return jsonify({"ok": True, "deleted": deleted})


@bp.post("/override/<h>")
def override_route(h):
    """Manually set a track's genre. Persists into the cached analysis (so the
    map, list and audit reflect it and it survives a reload), and files the audio
    under ~/genre_training/<genre>/ when a server-side file is available."""
    import shutil

    data = request.get_json(silent=True) or {}
    genre = (data.get("genre") or "").strip()
    if not genre:
        return jsonify({"error": "genre required"}), 400
    with _db_lock, closing(db()) as conn, conn as c:
        row = c.execute("SELECT filepath, payload FROM tracks WHERE hash=?", (h,)).fetchone()
        if not row:
            return jsonify({"error": "track not in database"}), 404
        filepath, payload = row
        p = json.loads(payload)
        p["override"] = genre
        c.execute("UPDATE tracks SET payload=? WHERE hash=?", (json.dumps(p), h))
    # INVARIANT: the training-copy file I/O below runs AFTER the _db_lock block
    # closes -- `filepath` was read inside the lock, but shutil.copy2 must not be
    # moved back inside it (never hold the DB lock across disk I/O).
    trained = False
    if filepath:
        safe = "".join(ch if ch.isalnum() or ch in " _-" else "_" for ch in genre).strip()
        src = Path(filepath)
        if safe and src.is_file():
            dest_dir = Path.home() / "genre_training" / safe
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            if not dest.exists():
                shutil.copy2(src, dest)
            trained = True
    return jsonify({"ok": True, "genre": genre, "trained": trained})


def _remove_segment_clip(h, genre, start, end):
    """Best-effort delete of the training clip an override produced. Reconstructs
    the exact path _extract_segment wrote (same genre folder + <hash>_<s>-<e><ext>,
    ext from the track's source file). Returns True if a file was removed."""
    with _db_lock, closing(db()) as conn, conn as c:
        row = c.execute("SELECT filepath FROM tracks WHERE hash=?", (h,)).fetchone()
    filepath = row[0] if row else None
    safe = "".join(ch if ch.isalnum() or ch in " _-" else "_" for ch in genre).strip()
    if not safe:
        return False
    ext = (Path(filepath).suffix.lower() if filepath else "") or ".wav"
    dest = Path.home() / "genre_training" / safe / f"{h}_{int(round(start))}-{int(round(end))}{ext}"
    try:
        if dest.is_file():
            dest.unlink()
            return True
    except OSError:
        pass
    return False


def _extract_segment(src, safe_genre, h, start, end):
    """Extract [start, end] of ``src`` into ~/genre_training/<safe_genre>/ with
    ffmpeg. Tries a stream-copy first (fast, lossless, container permitting) and
    falls back to a re-encode. Returns (dest_path | None, error | None); a missing
    ffmpeg is a soft failure (the override is still recorded, just not clipped)."""
    import shutil
    import subprocess  # nosec B404  # only used to run ffmpeg with a fixed arg list, never a shell

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None, "ffmpeg not found on PATH -- override recorded, clip not extracted"
    dest_dir = Path.home() / "genre_training" / safe_genre
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower() or ".wav"
    dest = dest_dir / f"{h}_{int(round(start))}-{int(round(end))}{ext}"
    # -ss before -i = fast input seek; -t = duration. Build both variants.
    base = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(src),
        "-t",
        f"{end - start:.3f}",
    ]

    def _run(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=300)  # nosec B603  # ffmpeg from shutil.which, args are a list (no shell), src is a DB-recorded path
            if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                return True, None
            return False, (r.stderr.decode(errors="replace")[-300:].strip() or "ffmpeg failed")
        except (subprocess.SubprocessError, OSError) as e:
            return False, str(e)

    ok, err = _run(base + ["-map", "0:a", "-c", "copy", str(dest)])  # (1) stream-copy
    if ok:
        return str(dest), None
    ok, err = _run(base + ["-vn", str(dest)])  # (2) re-encode fallback
    return (str(dest), None) if ok else (None, err)


@bp.post("/override_segment")
def override_segment_route():
    """Label a time range of a track a genre. Validates 0 <= start < end <=
    duration, records the span, and extracts that range into the genre's training
    folder. Needs a server-side source file: a browser-dropped track (no saved
    path) gets a clear message rather than a silent re-upload."""
    data = request.get_json(silent=True) or {}
    h = (data.get("hash") or "").strip()
    genre = (data.get("genre") or "").strip()
    if not h or not genre:
        return jsonify({"error": "hash and genre required"}), 400
    try:
        start = float(data.get("start"))
        end = float(data.get("end"))
    except (TypeError, ValueError):
        return jsonify({"error": "start and end must be numbers"}), 400

    with _db_lock, closing(db()) as conn, conn as c:
        row = c.execute("SELECT filepath, payload FROM tracks WHERE hash=?", (h,)).fetchone()
    if not row:
        return jsonify({"error": "track not in database"}), 404
    filepath, payload = row
    duration = (json.loads(payload) if payload else {}).get("duration")

    # validate the range: 0 <= start < end <= duration (small tolerance on the end)
    if not (start >= 0 and end > start):
        return jsonify({"error": "need 0 <= start < end"}), 400
    if duration and end > duration + 0.5:
        return jsonify(
            {"error": f"end {end:.1f}s is past the track duration ({duration:.1f}s)"}
        ), 400

    # extraction needs the real file -- dropped tracks have none; say so plainly.
    if not filepath:
        return jsonify(
            {
                "error": "section overrides need a server-side file. This track was "
                "dropped in the browser and has no saved path -- add it from a folder "
                "or batch scan first, then override sections of it."
            }
        ), 400
    src = Path(filepath)
    if not src.is_file():
        return jsonify({"error": "the source file for this track no longer exists on disk"}), 404

    with _db_lock, closing(db()) as conn, conn as c:
        cur = c.execute(
            "INSERT INTO segment_overrides(hash, start_s, end_s, genre, created) VALUES(?,?,?,?,?)",
            (h, start, end, genre, time.time()),
        )
        new_id = cur.lastrowid
    # ffmpeg extraction stays OUTSIDE the DB lock (subprocess + disk I/O)
    safe = "".join(ch if ch.isalnum() or ch in " _-" else "_" for ch in genre).strip()
    clip, err = _extract_segment(src, safe, h, start, end) if safe else (None, "invalid genre name")
    return jsonify(
        {
            "ok": True,
            "id": new_id,
            "hash": h,
            "genre": genre,
            "start": start,
            "end": end,
            "extracted": bool(clip),
            "extract_error": err,
        }
    )


@bp.post("/override_segment/delete")
def override_segment_delete():
    """Remove a segment override by id: drops the DB record AND deletes the training
    clip it produced (undoing the override should not leave the clip behind to keep
    training the head). The client gates this behind an explicit confirm."""
    data = request.get_json(silent=True) or {}
    oid = data.get("id")
    if oid is None:
        return jsonify({"error": "id required"}), 400
    try:
        oid = int(oid)
    except (TypeError, ValueError):
        return jsonify({"error": "id must be an integer"}), 400
    with _db_lock, closing(db()) as conn, conn as c:
        row = c.execute(
            "SELECT hash, start_s, end_s, genre FROM segment_overrides WHERE rowid=?", (oid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "override not found"}), 404
        c.execute("DELETE FROM segment_overrides WHERE rowid=?", (oid,))
    h, start, end, genre = row
    # disk I/O stays outside the DB lock
    clip_removed = _remove_segment_clip(h, genre, start, end)
    return jsonify({"ok": True, "deleted": 1, "clip_removed": clip_removed})


# ----------------------------------------------------------------------------
# Tags: manual designations ("high energy", "opener"...) attached to tracks
# ----------------------------------------------------------------------------
@bp.get("/tags")
def tags_list():
    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute(
            "SELECT t.id, t.name, COUNT(tt.hash) FROM tags t "
            "LEFT JOIN track_tags tt ON tt.tag_id = t.id "
            "GROUP BY t.id ORDER BY t.name"
        ).fetchall()
    return jsonify([{"id": r[0], "name": r[1], "count": r[2]} for r in rows])


@bp.post("/tags")
def tags_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    with _db_lock, closing(db()) as conn, conn as c:
        row = c.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
        if row:
            return jsonify({"id": row[0], "name": name})
        cur = c.execute("INSERT INTO tags(name) VALUES(?)", (name,))
        return jsonify({"id": cur.lastrowid, "name": name})


@bp.post("/tags/toggle")
def tags_toggle():
    """Add the tag to the track if absent, remove it if present."""
    data = request.get_json(silent=True) or {}
    tid, h = data.get("tag_id"), data.get("hash")
    if not tid or not h:
        return jsonify({"error": "tag_id and hash required"}), 400
    with _db_lock, closing(db()) as conn, conn as c:
        row = c.execute("SELECT 1 FROM track_tags WHERE tag_id=? AND hash=?", (tid, h)).fetchone()
        if row:
            c.execute("DELETE FROM track_tags WHERE tag_id=? AND hash=?", (tid, h))
            return jsonify({"tagged": False})
        c.execute("INSERT OR IGNORE INTO track_tags VALUES(?,?)", (tid, h))
        return jsonify({"tagged": True})


@bp.get("/lookup/<h>")
def lookup_route(h):
    """Look up external metadata (genres/styles/tags) for a track by artist/title
    across Discogs, MusicBrainz, and Last.fm. Only sources with a configured key are
    queried (MusicBrainz needs none); each source degrades independently (a timeout
    or failure is reported for that source, never fatal). Successful responses are
    cached PERMANENTLY per (hash, source) so a repeat click never re-queries."""
    with _db_lock, closing(db()) as conn, conn as c:
        row = c.execute("SELECT title, filename, payload FROM tracks WHERE hash=?", (h,)).fetchone()
        if not row:
            return jsonify({"error": "track not in database"}), 404
        cached = {
            r[0]: json.loads(r[1])
            for r in c.execute("SELECT source, response_json FROM lookup_cache WHERE hash=?", (h,))
        }
    title, filename, payload = row
    artist, track_title, remix = lookup.parse_track(
        json.loads(payload) if payload else {}, title, filename
    )

    fetchers = {
        "discogs": (lookup.fetch_discogs, lookup.parse_discogs),
        "musicbrainz": (lookup.fetch_musicbrainz, lookup.parse_musicbrainz),
        "lastfm": (lookup.fetch_lastfm, lookup.parse_lastfm),
    }
    conf = lookup.configured()
    results, errors = {}, {}
    for src, (fetch, parse) in fetchers.items():
        if src in cached:  # permanent cache -> never re-query
            results[src] = cached[src]
            continue
        if not conf.get(src):
            errors[src] = "not configured"
            continue
        if not track_title:
            errors[src] = "no title to search"
            continue
        raw, err = fetch(artist, track_title)  # network stays OUTSIDE the DB lock
        if err:
            errors[src] = err
            continue
        parsed = parse(raw)
        results[src] = parsed
        with _db_lock, closing(db()) as conn, conn as c:  # cache the hit permanently
            c.execute(
                "INSERT OR REPLACE INTO lookup_cache(hash, source, response_json, fetched) "
                "VALUES(?,?,?,?)",
                (h, src, json.dumps(parsed), time.time()),
            )
    return jsonify(
        {
            "query": {"artist": artist, "title": track_title, "remix": remix},
            "results": results,
            "errors": errors,
        }
    )


@bp.get("/tags/for/<h>")
def tags_for(h):
    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute(
            "SELECT t.id, t.name FROM track_tags tt JOIN tags t ON t.id=tt.tag_id "
            "WHERE tt.hash=? ORDER BY t.name",
            (h,),
        ).fetchall()
    return jsonify([{"id": r[0], "name": r[1]} for r in rows])


# ----------------------------------------------------------------------------
# Vibes: user-defined similarity clusters over cached track embeddings
# ----------------------------------------------------------------------------
@bp.get("/vibes")
def vibes_list():
    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute(
            "SELECT v.id, v.name, COUNT(t.hash) FROM vibes v "
            "LEFT JOIN vibe_tracks t ON t.vibe_id = v.id "
            "GROUP BY v.id ORDER BY v.name"
        ).fetchall()
    return jsonify([{"id": r[0], "name": r[1], "count": r[2]} for r in rows])


@bp.post("/vibes")
def vibes_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    try:
        with _db_lock, closing(db()) as conn, conn as c:
            cur = c.execute("INSERT INTO vibes(name) VALUES(?)", (name,))
            vid = cur.lastrowid
        return jsonify({"id": vid, "name": name})
    except sqlite3.IntegrityError:
        return jsonify({"error": "a vibe with that name already exists"}), 409


def _upsert_vibe_weight(vid, h, weight):
    """Insert or update a track's weight within a vibe (shared by add + weight)."""
    with _db_lock, closing(db()) as conn, conn as c:
        c.execute(
            "INSERT INTO vibe_tracks(vibe_id, hash, weight) VALUES(?,?,?) "
            "ON CONFLICT(vibe_id, hash) DO UPDATE SET weight=excluded.weight",
            (vid, h, weight),
        )


@bp.post("/vibes/add")
def vibes_add():
    data = request.get_json(silent=True) or {}
    vid, h = data.get("vibe_id"), data.get("hash")
    if not vid or not h:
        return jsonify({"error": "vibe_id and hash required"}), 400
    weight = max(-1.0, min(1.0, float(data.get("weight", 1.0))))
    _upsert_vibe_weight(vid, h, weight)
    return jsonify({"added": True, "weight": weight})


@bp.post("/vibes/weight")
def vibes_weight():
    """Set a track's weight within a vibe (Rocchio feedback). Positive pulls the
    vibe toward the track, negative pushes it away, 0 is a neutral member. This is
    what the per-song 👍/👎 and the slider editor both call. Upserts the link."""
    data = request.get_json(silent=True) or {}
    vid, h = data.get("vibe_id"), data.get("hash")
    if not vid or not h:
        return jsonify({"error": "vibe_id and hash required"}), 400
    try:
        weight = float(data.get("weight", 1.0))
    except (TypeError, ValueError):
        return jsonify({"error": "weight must be a number"}), 400
    weight = max(-1.0, min(1.0, weight))
    _upsert_vibe_weight(vid, h, weight)
    return jsonify({"vibe_id": vid, "hash": h, "weight": weight})


@bp.post("/vibes/remove")
def vibes_remove():
    """Remove a track from a vibe entirely (drop the membership link)."""
    data = request.get_json(silent=True) or {}
    vid, h = data.get("vibe_id"), data.get("hash")
    if not vid or not h:
        return jsonify({"error": "vibe_id and hash required"}), 400
    with _db_lock, closing(db()) as conn, conn as c:
        c.execute("DELETE FROM vibe_tracks WHERE vibe_id=? AND hash=?", (vid, h))
    return jsonify({"removed": True})


@bp.get("/vibes/<int:vid>/members")
def vibes_members(vid):
    """Member tracks of a vibe with their current weights, for the weight editor.
    Ordered strongest-pull first."""
    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute(
            "SELECT vt.hash, vt.weight, t.title, t.filename FROM vibe_tracks vt "
            "LEFT JOIN tracks t ON t.hash=vt.hash WHERE vt.vibe_id=? "
            "ORDER BY vt.weight DESC",
            (vid,),
        ).fetchall()
    return jsonify(
        [
            {
                "hash": r[0],
                "weight": 1.0 if r[1] is None else round(float(r[1]), 3),
                "title": r[2],
                "filename": r[3],
            }
            for r in rows
        ]
    )


@bp.get("/vibes/match/<h>")
def vibes_match(h):
    """Similarity of one track against every vibe's centroid."""
    emb = track_embedding(h)
    if emb is None:
        return jsonify({"error": "track not in database"}), 404
    with _db_lock, closing(db()) as conn, conn as c:
        vibes = c.execute("SELECT id, name FROM vibes").fetchall()
    out = []
    for vid, name in vibes:
        cen = vibe_centroid(vid)
        if cen is None:
            continue
        out.append({"id": vid, "name": name, "sim": round(cosine(emb, cen), 4)})
    out.sort(key=lambda x: -x["sim"])
    return jsonify(out)


@bp.get("/vibes/<int:vid>/playlist")
def vibes_playlist(vid):
    """All cached tracks ranked by similarity to this vibe's centroid."""
    cen = vibe_centroid(vid)
    if cen is None:
        return jsonify({"error": "vibe has no member tracks yet"}), 404
    threshold = float(request.args.get("threshold", 0.60))
    import numpy as np

    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute(
            "SELECT hash, title, filename, payload, embedding FROM tracks "
            "WHERE embedding IS NOT NULL"
        ).fetchall()
        members = {
            r[0]
            for r in c.execute("SELECT hash FROM vibe_tracks WHERE vibe_id=?", (vid,)).fetchall()
        }
    out = []
    for h, title, filename, payload, blob in rows:
        emb = np.frombuffer(blob, dtype=np.float32)
        sim = cosine(emb, cen)
        if sim < threshold:
            continue
        p = json.loads(payload)
        out.append(
            {
                "hash": h,
                "title": title,
                "filename": filename,
                "sim": round(sim, 4),
                "member": h in members,
                "bpm": p.get("bpm"),
                "camelot": p.get("camelot"),
                "duration": p.get("duration"),
            }
        )
    out.sort(key=lambda x: -x["sim"])
    return jsonify(out)


@bp.get("/similar/<h>")
def similar_route(h):
    """Top-k nearest tracks to <h> by embedding cosine (for the map popup)."""
    import numpy as np

    k = max(1, min(int(request.args.get("k", 8)), 40))
    target = track_embedding(h)
    if target is None:
        return jsonify({"error": "track not in database"}), 404
    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute(
            "SELECT hash, title, filename, filepath, payload, embedding FROM tracks "
            "WHERE embedding IS NOT NULL AND hash != ?",
            (h,),
        ).fetchall()
    out = []
    for hh, title, filename, filepath, payload, blob in rows:
        emb = np.frombuffer(blob, dtype=np.float32)
        p = json.loads(payload)
        style, _ = _dominant_style(p)
        out.append(
            {
                "hash": hh,
                "title": title or (Path(filename).stem if filename else hh[:8]),
                "artist": _artist_of(p, title, filename),
                "style": style,
                "bpm": p.get("bpm"),
                "camelot": p.get("camelot"),
                "sim": round(cosine(target, emb), 4),
                "a": 1 if (filepath and str(filepath).strip()) else 0,
            }
        )
    out.sort(key=lambda x: -x["sim"])
    return jsonify(out[:k])
