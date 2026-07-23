"""Analysis routes: analyze / refine / compare / batch, plus the audio and
waveform media endpoints and their upload helpers."""

import os
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

from flask import Response, jsonify, request, send_file

from .. import insight
from ..analysis import (
    FINE_HOP_SECONDS,
    _lock,
    analyze,
    build_payload,
    get_engine,
    get_maest,
    load_samples_for_waveform,
    maest_genre,
    read_tags,
    read_title,
    refine_segments,
    waveform_minmax,
)
from ..config import AUDIO_EXTS, FAKE, log
from ..db import (
    _db_lock,
    cache_get,
    cache_put,
    db,
    file_hash,
    waveform_cache_get,
    waveform_cache_put,
)
from ._shared import bp


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
                cached["segment_overrides"] = _segment_overrides(h)
                return jsonify(cached)

            title = read_title(p) or Path(f.filename).stem
            tags = read_tags(p)
            result = analyze(p)  # analyze() locks its own model inference
            emb = result.pop("emb_mean", None)
            wave = result.pop("wave", None)  # DAW-style min/max/rms -> its own cache
            payload = build_payload(f.filename, None, title, tags, result)
            nc = insight.check(emb, *insight.dominant(payload)) if emb is not None else None
            if nc:
                payload["neighbor_check"] = nc  # flag likely misreads
            cache_put(h, f.filename, None, title, payload, emb)
            if wave is not None:
                waveform_cache_put(h, wave)
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

            seed = hashlib.md5(("fine" + f.filename).encode()).hexdigest()  # nosec B324  # deterministic seed for FAKE-mode data, not security
            rng = random.Random(seed)  # nosec B311  # deterministic FAKE-mode PRNG, not security
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

        rng = random.Random(42)  # nosec B311  # seeds deterministic FAKE-mode data, not security
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


# ----------------------------------------------------------------------------
# Segment-level overrides: label a drag-selected time range of a track a genre.
# Records the span (repainted on the waveform, persisted across cache hits) and
# extracts just that range into ~/genre_training/<genre>/ with ffmpeg.
# ----------------------------------------------------------------------------
def _backfill_filepath(h, path):
    """Record a server-side path for a cached track that has none yet (drop-analyzed
    rows stored an empty path). Only fills a blank -- never overwrites an existing
    path. Enables audio preview, on-demand waveform, and section overrides."""
    with _db_lock, closing(db()) as conn, conn as c:
        c.execute(
            "UPDATE tracks SET filepath=? WHERE hash=? AND (filepath IS NULL OR filepath='')",
            (path, h),
        )


def _segment_overrides(h):
    """The persisted segment overrides for a track, oldest span first. Each carries
    its rowid as ``id`` so the client can target it for removal."""
    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute(
            "SELECT rowid, start_s, end_s, genre FROM segment_overrides WHERE hash=? "
            "ORDER BY start_s",
            (h,),
        ).fetchall()
    return [{"id": r[0], "start_s": r[1], "end_s": r[2], "genre": r[3]} for r in rows]


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


@bp.get("/waveform/<h>")
def waveform_route(h):
    """A DAW-style min/max/rms waveform for a track. Served from the permanent
    cache (pre-filled at analysis time); for older tracks with no cache yet, decode
    the source file once and cache it. A track with neither a cache nor a file
    (e.g. an old browser-dropped one) 404s, and the client keeps its envelope."""
    cached = waveform_cache_get(h)
    if cached:
        return jsonify(cached)
    with _db_lock, closing(db()) as conn, conn as c:
        row = c.execute("SELECT filepath FROM tracks WHERE hash=?", (h,)).fetchone()
    if not row:
        return jsonify({"error": "track not in database"}), 404
    filepath = row[0]
    if not filepath or not Path(filepath).is_file():
        return jsonify({"error": "no server-side audio to render (re-add the track)"}), 404
    try:
        samples = load_samples_for_waveform(Path(filepath))  # decode stays outside the DB lock
    except Exception:
        log.exception("waveform decode failed for %s", h)
        return jsonify({"error": "could not decode this track's audio"}), 500
    data = waveform_minmax(samples)
    waveform_cache_put(h, data)
    return jsonify(data)


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
                cached["segment_overrides"] = _segment_overrides(h)
                # backfill a server-side path for older drop-analyzed rows (which
                # stored none) so audio preview / DAW waveform / section overrides
                # light up for the whole library on a re-scan -- no re-analysis.
                _backfill_filepath(h, str(path))
                return cached
            title = read_title(path) or path.stem
            tags = read_tags(path)
            result = analyze(path)
            emb = result.pop("emb_mean", None)
            wave = result.pop("wave", None)
            payload = build_payload(path.name, str(path), title, tags, result)
            cache_put(h, path.name, str(path), title, payload, emb)
            if wave is not None:
                waveform_cache_put(h, wave)
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
