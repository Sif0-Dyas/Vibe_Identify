"""All HTTP routes for the app, grouped on a single Blueprint."""

import json
import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

from flask import Blueprint, Response, jsonify, render_template, request, send_file

from .analysis import (
    FINE_HOP_SECONDS,
    _lock,
    analyze,
    build_payload,
    get_engine,
    get_maest,
    maest_genre,
    read_tags,
    read_title,
    refine_segments,
)
from .config import AUDIO_EXTS, FAKE, log
from .db import (
    _db_lock,
    cache_get,
    cache_put,
    cosine,
    db,
    file_hash,
    track_embedding,
    vibe_centroid,
)

bp = Blueprint("main", __name__)


# ----------------------------------------------------------------------------
# Upload plumbing shared by /analyze, /refine, /compare: validate the audio
# upload, stage it to a temp file, and always clean up.
# ----------------------------------------------------------------------------
class UploadError(Exception):
    """Bad/missing upload -- carries the HTTP status the route should return."""

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def _check_upload(f, missing_msg="no file received"):
    """Validate a Werkzeug FileStorage; raise UploadError, else return its suffix."""
    if f is None or not f.filename:
        raise UploadError(missing_msg, 400)
    suffix = Path(f.filename).suffix.lower()
    if suffix not in AUDIO_EXTS:
        raise UploadError(f"unsupported file type: {suffix or 'none'}", 415)
    return suffix


@contextmanager
def saved_upload(f, missing_msg="no file received"):
    """Validate `f`, save it to a temp file, yield its Path, and unlink on exit."""
    suffix = _check_upload(f, missing_msg)
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        f.save(tmp.name)
        tmp.close()
        yield Path(tmp.name)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@bp.post("/analyze")
def analyze_route():
    f = request.files.get("file")
    try:
        with saved_upload(f) as p:
            # cache-first: identical audio content = identical hash = instant return
            h = file_hash(p)
            cached = cache_get(h)
            if cached:
                cached["hash"] = h
                cached["cached"] = True
                return jsonify(cached)

            title = read_title(p) or Path(f.filename).stem
            tags = read_tags(p)
            result = analyze(p)  # analyze() locks its own model inference
            emb = result.pop("emb_mean", None)
            payload = build_payload(f.filename, None, title, tags, result)
            cache_put(h, f.filename, None, title, payload, emb)
            payload["hash"] = h
            payload["cached"] = False
            return jsonify(payload)
    except UploadError as e:
        return jsonify({"error": str(e)}), e.status
    except Exception:
        log.exception("request failed")
        return jsonify({"error": "internal error"}), 500


@bp.post("/refine")
def refine_route():
    """Re-analyze one track at fine resolution; returns a denser segment list."""
    f = request.files.get("file")
    try:
        _check_upload(f)
        if FAKE:
            import hashlib
            import random

            rng = random.Random(hashlib.md5(("fine" + f.filename).encode()).hexdigest())
            pool = [
                "Drum n Bass",
                "Trance",
                "Dubstep",
                "Hard Techno",
                "Hardstyle",
                "House",
                "Techno",
                "Jungle",
                "Breakcore",
                "Psy-Trance",
            ]
            rng.shuffle(pool)
            seg_styles = [pool[0]] * 4 + pool[1:3]
            segments = []
            for _ in range(rng.randint(30, 60)):
                segments += [rng.choice(seg_styles)] * rng.randint(3, 12)
            frames = []
            for s in segments:
                others = rng.sample([p for p in pool if p != s], 3)
                top = round(rng.uniform(0.25, 0.6), 3)
                rest = sorted(
                    (round(rng.uniform(0.02, top - 0.02), 3) for _ in range(3)), reverse=True
                )
                frames.append([[s, top]] + [[others[j], rest[j]] for j in range(3)])
            return jsonify(
                {"segments": segments, "frames": frames, "hop_seconds": FINE_HOP_SECONDS}
            )

        with saved_upload(f) as p:
            segments, frames = refine_segments(p)  # locks its own inference
            return jsonify(
                {"segments": segments, "frames": frames, "hop_seconds": FINE_HOP_SECONDS}
            )
    except UploadError as e:
        return jsonify({"error": str(e)}), e.status
    except Exception:
        log.exception("request failed")
        return jsonify({"error": "internal error"}), 500


def _top_styles(vec, labels, k=6, thresh=0.02):
    """Top-k [{parent,style,score}] from a 400-dim genre vector."""
    import numpy as np

    order = np.argsort(vec)[::-1][:k]
    out = []
    for i in order:
        if float(vec[int(i)]) < thresh:
            break
        parent, child = labels[int(i)].split("---", 1)
        out.append({"parent": parent, "style": child, "score": round(float(vec[int(i)]), 4)})
    return out


def compare_engines(path: Path, weight=0.5):
    """Run EffNet and MAEST on one track. Returns, for the union of each engine's
    top styles, BOTH per-style scores -- so the client can re-mix the merge at any
    weight live (the models are the slow part; averaging is instant). Both models
    share the identical 400-label order, so the merge is a plain weighted average."""
    import numpy as np
    from essentia.standard import MonoLoader

    eng = get_engine()
    labels = eng["labels"]
    # decode + one-time model builds stay OUTSIDE the inference lock (matching
    # analyze); only the shared, non-thread-safe TF inference is serialized, so a
    # /compare during a batch no longer freezes the workers for the whole decode.
    audio16 = MonoLoader(filename=str(path), sampleRate=16000, resampleQuality=4)()
    get_maest()  # warm MAEST (if installed) before taking the lock
    with _lock:
        eff = np.mean(eng["classifier"](eng["embedder"](audio16)), axis=0)
        mae = maest_genre(audio16)
    if mae is None:
        return {"maest_available": False, "effnet": _top_styles(eff, labels)}
    # union of each engine's top-K, wide enough that the merged top-5 for ANY
    # weight is contained in it; carry both scores per style
    K = 15
    idx = sorted(
        set(np.argsort(eff)[::-1][:K]) | set(np.argsort(mae)[::-1][:K]),
        key=lambda i: -max(float(eff[i]), float(mae[i])),
    )
    pairs = [
        {
            "parent": labels[i].split("---", 1)[0],
            "style": labels[i].split("---", 1)[1],
            "eff": round(float(eff[i]), 4),
            "mae": round(float(mae[i]), 4),
        }
        for i in idx
    ]
    return {"maest_available": True, "weight": weight, "pairs": pairs}


@bp.post("/compare")
def compare_route():
    """A/B the EffNet genre read against MAEST + their merge for one track.
    On demand only (MAEST is ~10x slower); does NOT touch the analysis cache.
    Accepts a file upload (dropped tracks) or a server-side filepath (batch)."""
    if FAKE:
        import random

        rng = random.Random(42)
        pool = ["Drum n Bass", "Dance-pop", "House", "Deep House", "Techno", "Trance", "Dubstep"]
        pairs = [
            {
                "parent": "Electronic",
                "style": s,
                "eff": round(rng.uniform(0.03, 0.45), 3),
                "mae": round(rng.uniform(0.03, 0.45), 3),
            }
            for s in pool
        ]
        return jsonify({"maest_available": True, "weight": 0.5, "pairs": pairs})

    filepath = (request.form.get("filepath") or "").strip()
    try:
        if filepath:
            p = Path(filepath)
            if not p.is_file():
                return jsonify({"error": f"file not found: {filepath}"}), 404
            return jsonify(compare_engines(p))  # locks only its own inference pass
        with saved_upload(request.files.get("file"), "no file or filepath provided") as p:
            return jsonify(compare_engines(p))  # locks only its own inference pass
    except UploadError as e:
        return jsonify({"error": str(e)}), e.status
    except Exception:
        log.exception("request failed")
        return jsonify({"error": "internal error"}), 500


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


# ----------------------------------------------------------------------------
# Audio preview: stream a previously-analyzed track for in-app playback
# ----------------------------------------------------------------------------
AUDIO_MIME = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".aifc": "audio/aiff",
    ".wma": "audio/x-ms-wma",
}


@bp.get("/audio/<h>")
def audio_route(h):
    """Stream a track's audio by content hash for the in-app preview player.
    Read-only; serves ONLY files already recorded in the analysis DB (so this is
    not an arbitrary-file endpoint). Supports HTTP Range so the browser can seek.
    Browser-dropped files have no server path -- those play client-side via a
    blob URL instead, so a 404 here is expected for them."""
    with _db_lock, closing(db()) as conn, conn as c:
        row = c.execute("SELECT filepath, filename FROM tracks WHERE hash=?", (h,)).fetchone()
    if not row or not row[0]:
        return jsonify({"error": "no server-side file for this track"}), 404
    p = Path(row[0])
    if not p.is_file():
        return jsonify({"error": "file no longer exists on disk"}), 404
    mime = AUDIO_MIME.get(p.suffix.lower(), "application/octet-stream")
    return send_file(
        str(p), mimetype=mime, conditional=True, as_attachment=False, download_name=row[1] or p.name
    )


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


# ---------------------------------------------------------------------------
# Genre Map -- interactive constellation of the entire scanned library.
# /map returns every track + nearest-neighbour edges; /similar/<h> powers the
# popup. Both lean on the same 1280-d embeddings that drive vibes.
# ---------------------------------------------------------------------------
def _artist_of(title, filename):
    """Best-effort artist: our titles follow 'Artist - Title'."""
    base = (title or "") or (Path(filename).stem if filename else "")
    return base.split(" - ", 1)[0].strip() if " - " in base else ""


def _dominant_style(payload):
    """Salience winner (v2 identity), falling back to the top flat style."""
    sal = payload.get("salience") or []
    if sal:
        return sal[0].get("style"), round(float(sal[0].get("score", 0)), 4)
    st = payload.get("styles") or []
    if st:
        return st[0].get("style"), round(float(st[0].get("score", 0)), 4)
    return None, 0.0


def _map_node(h, title, filename, payload):
    p = payload if isinstance(payload, dict) else json.loads(payload)
    style, score = _dominant_style(p)
    return {
        "hash": h,
        "title": title or (Path(filename).stem if filename else h[:8]),
        "artist": _artist_of(title, filename),
        "style": style,
        "score": score,
        "styles": [s.get("style") for s in (p.get("styles") or [])[:3]],
        "bpm": p.get("bpm"),
        "key": p.get("key"),
        "scale": p.get("scale"),
        "camelot": p.get("camelot"),
        "duration": p.get("duration"),
    }


@bp.get("/map")
def map_route():
    import numpy as np

    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute("SELECT hash, title, filename, payload, embedding FROM tracks").fetchall()
    nodes, embs, emb_idx = [], [], []
    for h, title, filename, payload, blob in rows:
        nodes.append(_map_node(h, title, filename, payload))
        if blob is not None:
            embs.append(np.frombuffer(blob, dtype=np.float32))
            emb_idx.append(len(nodes) - 1)
    edges = []
    if len(embs) >= 2:
        M = np.vstack(embs).astype(np.float32)
        # (1) similarity edges from cosine
        norm = np.linalg.norm(M, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        Mn = M / norm
        sims = Mn @ Mn.T
        np.fill_diagonal(sims, -1.0)
        K = 2  # nearest neighbours per node
        seen = set()
        for a in range(len(emb_idx)):
            for b in np.argsort(-sims[a])[:K]:
                s = float(sims[a, b])
                if s <= 0:
                    continue
                key = (min(a, b), max(a, b))
                if key in seen:
                    continue
                seen.add(key)
                edges.append(
                    {
                        "a": nodes[emb_idx[a]]["hash"],
                        "b": nodes[emb_idx[int(b)]]["hash"],
                        "sim": round(s, 3),
                    }
                )
        # (2) PCA of the embeddings -> a few sonic coordinates per track. The
        #     client picks, per genre region, the 2 components that best spread
        #     THAT region's members, so a big single-genre cluster (e.g. all
        #     dubstep) still fans out by how the tracks actually sound.
        Mc = M - M.mean(axis=0, keepdims=True)
        try:
            _, _, Vt = np.linalg.svd(Mc, full_matrices=False)
            ncomp = int(min(8, Vt.shape[0]))
            proj = Mc @ Vt[:ncomp].T  # (m, ncomp)
            std = proj.std(axis=0, keepdims=True)
            std[std == 0] = 1.0
            proj = proj / std  # standardise each component
            for k, i in enumerate(emb_idx):
                nodes[i]["e"] = [round(float(v), 4) for v in proj[k]]
        except np.linalg.LinAlgError:
            pass
    return jsonify({"nodes": nodes, "edges": edges})


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
            "SELECT hash, title, filename, payload, embedding FROM tracks "
            "WHERE embedding IS NOT NULL AND hash != ?",
            (h,),
        ).fetchall()
    out = []
    for hh, title, filename, payload, blob in rows:
        emb = np.frombuffer(blob, dtype=np.float32)
        p = json.loads(payload)
        style, _ = _dominant_style(p)
        out.append(
            {
                "hash": hh,
                "title": title or (Path(filename).stem if filename else hh[:8]),
                "artist": _artist_of(title, filename),
                "style": style,
                "bpm": p.get("bpm"),
                "camelot": p.get("camelot"),
                "sim": round(cosine(target, emb), 4),
            }
        )
    out.sort(key=lambda x: -x["sim"])
    return jsonify(out[:k])


@bp.get("/")
def index():
    return render_template("index.html")


@bp.post("/batch")
def batch_route():
    """Scan a server-side folder path and analyze all audio files in parallel.
    The client passes a WSL path like /mnt/c/Users/you/Music.
    Returns a stream of newline-delimited JSON results (NDJSON)."""
    import concurrent.futures
    import json as _json

    data = request.get_json(silent=True) or {}
    folder = Path(data.get("path", "")).expanduser()
    workers = int(data.get("workers", 3))  # 3 parallel analyses, safe on most CPUs

    if not folder.is_dir():
        return jsonify({"error": f"not a directory: {folder}"}), 400

    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    if not files:
        return jsonify({"error": "no audio files found"}), 404

    def analyze_one(path: Path):
        try:
            h = file_hash(path)
            cached = cache_get(h)
            if cached:
                cached = dict(cached)
                cached.update({"ok": True, "hash": h, "cached": True, "filepath": str(path)})
                return cached
            title = read_title(path) or path.stem
            tags = read_tags(path)
            result = analyze(path)
            emb = result.pop("emb_mean", None)
            payload = build_payload(path.name, str(path), title, tags, result)
            cache_put(h, path.name, str(path), title, payload, emb)
            payload.update({"ok": True, "hash": h, "cached": False})
            return payload
        except Exception:
            log.exception("batch analysis failed for %s", path.name)
            return {
                "ok": False,
                "filename": path.name,
                "filepath": str(path),
                "error": "analysis failed",
            }

    def generate():
        yield _json.dumps({"total": len(files)}) + "\n"
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(analyze_one, f): f for f in files}
            done = 0
            for fut in concurrent.futures.as_completed(futs):
                done += 1
                result = fut.result()
                result["progress"] = done
                yield _json.dumps(result) + "\n"

    return Response(generate(), mimetype="application/x-ndjson")
