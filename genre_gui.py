#!/usr/bin/env python3
"""
genre_gui.py — Genre v2 backend (Flask + Essentia analysis)
============================================================
Project layout:
    genre_gui.py          ← this file: all Python/Flask logic
    templates/index.html  ← HTML shell (references CSS + JS below)
    static/app.css        ← all styling
    static/app.js         ← all frontend logic (lenses, rows, canvas, vibes/tags)

Run:
    python genre_gui.py          (real Essentia analysis)
    FAKE_ANALYZER=1 python genre_gui.py   (instant fake results, no models)
    → open http://localhost:5005 in your Windows browser

Key architecture:
    /analyze    POST  single file upload → full analysis JSON
    /refine     POST  single file upload → dense segment stream (fine detail)
    /compare    POST  file/filepath → EffNet vs MAEST scores (on-demand ensemble)
    /batch      POST  {path, workers}    → NDJSON stream of results
    /save_training POST genre + file/filepath → copies to ~/genre_training/<genre>/
    /audio/<h>  GET   stream a DB-recorded track by hash (preview player)

Analysis pipeline (per track):
    1. Decode to 16 kHz mono  → EffNet embedder → 1280-dim embeddings
    2. Discogs classifier head → 400 style scores × N frames
    3. frame_topk()           → top-6 styles per frame (for lens system)
    4. salience_read()        → energy × confidence × recurrence weighting
    5. Decode to 44.1 kHz     → RhythmExtractor2013 (BPM) + KeyExtractor
    6. waveform_peaks()       → 240-point amplitude envelope

Frontend lens system (all client-side in app.js):
    Global:  GLOBAL.identity ('v1'=flat%, 'v2'=salience)
             GLOBAL.seg ('raw','hysteresis','sibling','hyst+sib')
    Per-row: each row can override the global lens independently
    SIBLING_GROUPS: editable near-synonym clusters (House family, Trance family…)
    Hysteresis: requires MARGIN lead + HOLD frames before genre switch

Dependencies:
    pip install flask mutagen essentia-tensorflow numpy
"""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import closing, contextmanager
from pathlib import Path

from flask import render_template, Flask, jsonify, request, send_file

# ----------------------------------------------------------------------------
# Model plumbing (same as genre_test.py)
# ----------------------------------------------------------------------------
MODEL_DIR = Path(os.environ.get("MODEL_DIR", Path.home() / "essentia_models"))
MODELS = {
    "discogs-effnet-bs64-1.pb":
        "https://essentia.upf.edu/models/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb",
    "genre_discogs400-discogs-effnet-1.pb":
        "https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.pb",
    "genre_discogs400-discogs-effnet-1.json":
        "https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json",
}
AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".mp4", ".aac", ".alac", ".ogg", ".oga", ".opus",
    ".wav", ".aif", ".aiff", ".aifc", ".wma", ".wv", ".ape", ".mpc", ".dsf",
}
FAKE = os.environ.get("FAKE_ANALYZER") == "1"

app = Flask(__name__)
_lock = threading.Lock()          # TF model instances are shared and not thread-safe; hold during inference only

# ----------------------------------------------------------------------------
# Persistence: analysis cache + vibes (SQLite, single file at ~/genre_v2.db)
# ----------------------------------------------------------------------------
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


init_db()


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
_engine = {}                      # lazily-built: labels, embedder, classifier
_engine_lock = threading.Lock()   # guards the one-time model build/download so
                                  # /batch's first 3 workers don't race on it


def ensure_models():
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in MODELS.items():
        dest = MODEL_DIR / name
        if not (dest.exists() and dest.stat().st_size > 0):
            print(f"  downloading {name} ...", flush=True)
            urllib.request.urlretrieve(url, dest)


def get_engine():
    """Build (once) and return labels + models. Guarded by _engine_lock so the
    first /batch run (3 workers hitting an unbuilt engine at once) can't race on
    the model download or construct duplicate TF instances."""
    if _engine:
        return _engine
    with _engine_lock:
        if _engine:                       # another thread built it while we waited
            return _engine
        ensure_models()
        from essentia.standard import TensorflowPredictEffnetDiscogs, TensorflowPredict2D
        with open(MODEL_DIR / "genre_discogs400-discogs-effnet-1.json") as fh:
            labels = json.load(fh)["classes"]
        embedder = TensorflowPredictEffnetDiscogs(
            graphFilename=str(MODEL_DIR / "discogs-effnet-bs64-1.pb"),
            output="PartitionedCall:1")
        classifier = TensorflowPredict2D(
            graphFilename=str(MODEL_DIR / "genre_discogs400-discogs-effnet-1.pb"),
            input="serving_default_model_Placeholder",
            output="PartitionedCall:0")
        # publish all three keys at once so the lock-free fast path above never
        # observes a half-built _engine
        _engine.update({"labels": labels, "embedder": embedder, "classifier": classifier})
        return _engine


# --- optional MAEST engine (2nd genre model, for ensembling) ----------------
# A transformer trained on the SAME Discogs-400 task, so its predictions align
# 1:1 with the EffNet head's label order -> the two can be averaged directly.
# ~10x slower than EffNet on CPU, so it's used on demand (the /compare route),
# never in the normal /analyze path.
MAEST_PB = MODEL_DIR / os.environ.get("MAEST_MODEL", "discogs-maest-30s-pw-1.pb")


def get_maest():
    """Lazily build the MAEST genre model. Returns None if the ~334 MB model
    file isn't present, so the ensemble feature stays optional."""
    if "maest" in _engine:
        return _engine["maest"]
    with _engine_lock:
        if "maest" in _engine:            # built while we waited on the lock
            return _engine["maest"]
        if not MAEST_PB.exists():
            _engine["maest"] = None
            return None
        from essentia.standard import TensorflowPredictMAEST
        _engine["maest"] = TensorflowPredictMAEST(
            graphFilename=str(MAEST_PB),
            input="serving_default_melspectrogram",     # this graph's actual input node
            output="StatefulPartitionedCall:0")         # discogs-400 predictions, direct
        return _engine["maest"]


def maest_genre(audio16):
    """Track-level 400-dim genre probabilities from MAEST (mean over 30s patches),
    aligned to the same Discogs-400 label order as the EffNet head. None if the
    MAEST model isn't installed."""
    import numpy as np
    m = get_maest()
    if m is None:
        return None
    preds = np.asarray(m(audio16))              # shape (patches, 1, 1, 400)
    return preds.reshape(-1, preds.shape[-1]).mean(axis=0)


# --- optional custom head (trained with train_head.py) ----------------------
CUSTOM_HEAD_PATH = Path(os.environ.get("CUSTOM_HEAD",
                                       MODEL_DIR / "custom_head.npz"))
_custom = {"checked": False, "head": None}


def get_custom_head():
    """Load ~/essentia_models/custom_head.npz once, if it exists. Guarded so the
    concurrent /batch workers that call this (via custom_predict) load it once."""
    if _custom["checked"]:
        return _custom["head"]
    with _engine_lock:
        if _custom["checked"]:            # loaded while we waited on the lock
            return _custom["head"]
        if CUSTOM_HEAD_PATH.exists():
            try:
                import numpy as np
                d = np.load(CUSTOM_HEAD_PATH, allow_pickle=False)
                head = {k: d[k] for k in ("W1", "b1", "W2", "b2", "mu", "sigma")}
                head["labels"] = [str(x) for x in d["labels"]]
                acc = float(d["val_acc"]) if "val_acc" in d else None
                _custom["head"] = head    # publish only once fully built
                print(f"custom head loaded: {head['labels']}"
                      + (f"  (val acc {acc:.0%})" if acc else ""))
            except Exception as exc:
                print(f"could not load custom head: {exc}")
        _custom["checked"] = True         # set last: don't try again either way
        return _custom["head"]


def custom_predict(embeddings):
    """Forward pass of the trained NumPy head; track-level probabilities."""
    import numpy as np
    head = get_custom_head()
    if head is None:
        return None
    X = (np.asarray(embeddings) - head["mu"]) / head["sigma"]
    h = np.maximum(X @ head["W1"] + head["b1"], 0.0)
    logits = h @ head["W2"] + head["b2"]
    e = np.exp(logits - logits.max(axis=1, keepdims=True))
    probs = (e / e.sum(axis=1, keepdims=True)).mean(axis=0)   # avg over frames
    order = np.argsort(probs)[::-1]
    return [{"style": head["labels"][int(i)], "score": round(float(probs[i]), 4)}
            for i in order]


def read_title(path: Path) -> str | None:
    """Title from the file's tags via mutagen, or None."""
    try:
        from mutagen import File as MFile
        mf = MFile(str(path), easy=True)
        if mf and mf.tags:
            vals = mf.tags.get("title")
            if vals:
                t = str(vals[0]).strip()
                if t:
                    return t
    except Exception:
        pass
    return None


def read_tags(path: Path) -> dict:
    """Common tag fields + technical info for the details box."""
    tag, tech = {}, {}
    try:
        from mutagen import File as MFile
        mf = MFile(str(path), easy=True)
        if mf:
            if mf.tags:
                for k in ("title", "artist", "album", "albumartist", "genre",
                          "date", "tracknumber", "discnumber", "composer", "bpm"):
                    vals = mf.tags.get(k)
                    if vals and str(vals[0]).strip():
                        tag[k] = str(vals[0]).strip()
            info = getattr(mf, "info", None)
            if info is not None:
                br = getattr(info, "bitrate", 0)
                if br:
                    tech["bitrate"] = f"{round(br / 1000)} kbps"
                sr = getattr(info, "sample_rate", 0)
                if sr:
                    tech["sample rate"] = f"{sr} Hz"
                ch = getattr(info, "channels", 0)
                if ch:
                    tech["channels"] = str(ch)
                tech["format"] = type(mf).__name__
    except Exception:
        pass
    return {"tag": tag, "tech": tech}


CAMELOT = {  # (key, scale) -> Camelot wheel position; enharmonics included
    ("C", "major"): "8B",  ("G", "major"): "9B",  ("D", "major"): "10B",
    ("A", "major"): "11B", ("E", "major"): "12B", ("B", "major"): "1B",
    ("F#", "major"): "2B", ("Gb", "major"): "2B", ("C#", "major"): "3B",
    ("Db", "major"): "3B", ("G#", "major"): "4B", ("Ab", "major"): "4B",
    ("D#", "major"): "5B", ("Eb", "major"): "5B", ("A#", "major"): "6B",
    ("Bb", "major"): "6B", ("F", "major"): "7B",
    ("A", "minor"): "8A",  ("E", "minor"): "9A",  ("B", "minor"): "10A",
    ("F#", "minor"): "11A", ("Gb", "minor"): "11A", ("C#", "minor"): "12A",
    ("Db", "minor"): "12A", ("G#", "minor"): "1A", ("Ab", "minor"): "1A",
    ("D#", "minor"): "2A", ("Eb", "minor"): "2A", ("A#", "minor"): "3A",
    ("Bb", "minor"): "3A", ("F", "minor"): "4A",  ("C", "minor"): "5A",
    ("G", "minor"): "6A",  ("D", "minor"): "7A",
}

WAVE_BINS = 240


def waveform_peaks(audio, bins=WAVE_BINS):
    """Downsample |audio| into `bins` peak values in 0..1 for drawing."""
    import numpy as np
    a = np.abs(np.asarray(audio))
    if a.size == 0:
        return [0.0] * bins
    edges = np.linspace(0, a.size, bins + 1, dtype=int)
    peaks = np.array([a[edges[i]:edges[i + 1]].max() if edges[i + 1] > edges[i] else 0.0
                      for i in range(bins)])
    top = peaks.max()
    if top > 0:
        peaks = peaks / top
    return [round(float(v), 3) for v in peaks]


def frame_topk(preds, labels, k=6):
    """Per-frame top-k predictions as [style, score] pairs -- the data the
    hysteresis and sibling-merge lenses need (winner plus near-misses)."""
    import numpy as np
    preds = np.asarray(preds)
    if preds.ndim != 2 or preds.shape[0] == 0:
        return []
    kk = min(k, preds.shape[1])
    out = []
    for row in preds:
        idx = np.argpartition(row, -kk)[-kk:]
        idx = idx[np.argsort(row[idx])[::-1]]
        out.append([[labels[int(i)].split("---", 1)[1], round(float(row[i]), 3)] for i in idx])
    return out


def salience_read(preds, audio16, labels, topk=8):
    """
    Weighted overall-genre read that mimics how an experienced listener collapses
    a whole track to one identity. Each ~2s frame's vote is scaled by three signals:
      energy     -- loud, dense sections (drops, choruses) count most; silence ~0
      confidence -- frames where one style clearly wins count more than ambiguous ones
      recurrence -- genres that keep coming back outweigh one-off moments
    Returns [{style, score}] over the salient styles (scores sum to <=1; the
    remainder is the incidental tail, shown as "Other" in the UI).
    """
    import numpy as np
    preds = np.asarray(preds)
    n = preds.shape[0]
    if n == 0:
        return []
    a = np.asarray(audio16, dtype=np.float32)

    # per-frame RMS energy, aligned to the n genre frames, normalized to 0..1
    edges = np.linspace(0, len(a), n + 1, dtype=int)
    energy = np.array([
        float(np.sqrt(np.mean(a[edges[i]:edges[i + 1]] ** 2))) if edges[i + 1] > edges[i] else 0.0
        for i in range(n)])
    if energy.max() > 0:
        energy = energy / energy.max()

    # confidence = how peaked each frame's distribution is, relative to the track
    conf = preds.max(axis=1)
    conf_n = conf / conf.max() if conf.max() > 0 else conf

    # recurrence = how often each frame's winning genre wins across the whole track
    winners = preds.argmax(axis=1)
    counts = np.bincount(winners, minlength=preds.shape[1]).astype(np.float32)
    freq = counts / counts.sum()
    rec = freq[winners]
    rec_n = rec / rec.max() if rec.max() > 0 else rec

    # combine: energy can fully zero a silent intro; the other two modulate in [0.4,1]
    salience = energy * (0.4 + 0.6 * conf_n) * (0.4 + 0.6 * rec_n)

    tally = {}
    for i in range(n):
        g = labels[int(winners[i])].split("---", 1)[1]
        tally[g] = tally.get(g, 0.0) + float(salience[i])
    total = sum(tally.values()) or 1.0
    ranked = sorted(tally.items(), key=lambda kv: -kv[1])[:topk]
    return [{"style": g, "score": v / total} for g, v in ranked]


def analyze(path: Path) -> dict:
    """Genre styles + BPM, key, duration, and a waveform envelope for one file."""
    if FAKE:
        import hashlib, random, math
        rng = random.Random(hashlib.md5(path.name.encode()).hexdigest())
        pool = ["Drum n Bass", "Trance", "Dubstep", "Hard Techno", "Hardstyle",
                "House", "Techno", "Jungle", "Breakcore", "Psy-Trance"]
        rng.shuffle(pool)
        scores = sorted((rng.uniform(0.04, 0.55) for _ in range(4)), reverse=True)
        key, scale = rng.choice(list(CAMELOT.keys()))
        wave = [round(abs(math.sin(i / 9) * rng.uniform(0.4, 1.0)), 3) for i in range(WAVE_BINS)]
        segments = []
        seg_styles = [pool[0]] * 3 + pool[1:3]          # mostly primary, some switches
        for _ in range(rng.randint(5, 9)):
            segments += [rng.choice(seg_styles)] * rng.randint(8, 30)
        # fake salience: weight the primary genre up, as energy-weighting would
        from collections import Counter as _C
        _c = _C(segments); _tot = sum(_c.values())
        _sal = sorted(((g, n / _tot) for g, n in _c.items()), key=lambda kv: -kv[1])
        _sal = [(_sal[0][0], min(0.92, _sal[0][1] + 0.15))] + _sal[1:]
        _s = sum(p for _, p in _sal)
        salience = [{"style": g, "score": round(p / _s, 4)} for g, p in _sal]
        # fake per-frame top-k: winner + near-misses (House/Tribal House flicker-like)
        frames = []
        for s in segments:
            others = rng.sample([p for p in pool if p != s], 3)
            top = round(rng.uniform(0.26, 0.55), 3)
            rest = sorted((round(rng.uniform(0.02, max(0.03, top - 0.02)), 3) for _ in range(3)), reverse=True)
            frames.append([[s, top]] + [[others[j], rest[j]] for j in range(3)])
        return {
            "styles": [{"parent": "Electronic", "style": s, "score": v}
                       for s, v in zip(pool, scores)],
            "segments": segments,
            "salience": salience,
            "frames": frames,
            "custom": [{"style": s, "score": round(v, 4)} for s, v in
                       zip(["Riddim", "Tearout", "Liquid DnB", "Other"],
                           sorted((rng.uniform(0.02, 0.7) for _ in range(4)),
                                  reverse=True))],
            "bpm": round(rng.uniform(120, 178), 1), "bpm_confidence": rng.uniform(0.5, 5.0),
            "key": key, "scale": scale, "camelot": CAMELOT[(key, scale)],
            "key_strength": rng.uniform(0.5, 0.95),
            "duration": rng.uniform(150, 420), "waveform": wave,
            "emb_mean": [rng.uniform(-1, 1) for _ in range(1280)],
        }

    import numpy as np
    from essentia.standard import MonoLoader, RhythmExtractor2013, KeyExtractor
    eng = get_engine()

    # --- genre (model wants 16 kHz) ---
    audio16 = MonoLoader(filename=str(path), sampleRate=16000, resampleQuality=4)()
    # embedder + classifier are shared, non-thread-safe TF instances -> serialize
    # this one inference pass; decode/BPM/key below stay parallel across workers.
    with _lock:
        embeddings = eng["embedder"](audio16)
        preds = eng["classifier"](embeddings)
    mean = np.mean(preds, axis=0)
    order = np.argsort(mean)[::-1]
    labels = eng["labels"]
    styles = []
    for i in order[:8]:
        parent, child = labels[i].split("---", 1)
        styles.append({"parent": parent, "style": child, "score": float(mean[i])})

    # per-frame winner -> which genre dominates each ~2s patch of the track
    frame_winners = np.argmax(preds, axis=1)
    segments = [labels[int(i)].split("---", 1)[1] for i in frame_winners]

    # salience-weighted overall identity (energy x confidence x recurrence)
    salience = salience_read(preds, audio16, labels)

    # per-frame top-k predictions (for hysteresis / sibling-merge lenses)
    frames = frame_topk(preds, labels)

    # custom head (if trained) scores the SAME embeddings -- no extra audio work
    custom = custom_predict(embeddings)

    # --- musical details (44.1 kHz for accuracy) ---
    audio44 = MonoLoader(filename=str(path), sampleRate=44100, resampleQuality=4)()
    duration = float(len(audio44)) / 44100.0

    bpm = bpm_conf = None
    try:
        bpm, _, conf, _, _ = RhythmExtractor2013(method="multifeature")(audio44)
        bpm, bpm_conf = float(bpm), float(conf)
    except Exception:
        pass

    key = scale = camelot = None
    key_strength = None
    try:
        k, s, strength = KeyExtractor()(audio44)
        key, scale, key_strength = str(k), str(s), float(strength)
        camelot = CAMELOT.get((key, scale))
    except Exception:
        pass

    return {
        "styles": styles,
        "segments": segments,
        "salience": salience,
        "frames": frames,
        "custom": custom,
        "bpm": round(bpm, 1) if bpm else None, "bpm_confidence": bpm_conf,
        "key": key, "scale": scale, "camelot": camelot, "key_strength": key_strength,
        "duration": duration, "waveform": waveform_peaks(audio44),
        "emb_mean": [float(x) for x in np.mean(embeddings, axis=0)],
    }


# default embedder hops 128 mel-frames (~2.0s); a 32-frame hop (~0.5s) gives 4x
# overlap and thus ~4x finer genre-boundary resolution -- at ~4x the inference cost.
FINE_HOP = 32
FINE_HOP_SECONDS = round(FINE_HOP * 256 / 16000, 2)   # 256-sample mel hop @ 16 kHz


def get_fine_embedder():
    eng = get_engine()
    if "embedder_fine" in eng:
        return eng["embedder_fine"]
    with _engine_lock:
        if "embedder_fine" not in eng:
            from essentia.standard import TensorflowPredictEffnetDiscogs
            eng["embedder_fine"] = TensorflowPredictEffnetDiscogs(
                graphFilename=str(MODEL_DIR / "discogs-effnet-bs64-1.pb"),
                output="PartitionedCall:1", patchHopSize=FINE_HOP)
    return eng["embedder_fine"]


def refine_segments(path: Path):
    """Re-run one track with overlapping patches -> (dense segments, dense frames)."""
    import numpy as np
    from essentia.standard import MonoLoader
    eng = get_engine()
    audio16 = MonoLoader(filename=str(path), sampleRate=16000, resampleQuality=4)()
    # shared, non-thread-safe TF instances -> serialize inference only
    with _lock:
        emb = get_fine_embedder()(audio16)
        preds = eng["classifier"](emb)
    winners = np.argmax(preds, axis=1)
    labels = eng["labels"]
    segments = [labels[int(i)].split("---", 1)[1] for i in winners]
    return segments, frame_topk(preds, labels)


def build_payload(filename, filepath, title, tags, result):
    """Shared response shape for /analyze and /batch. emb_mean is stored in the
    DB, not sent to the client (1280 floats the frontend doesn't need)."""
    styles = [s for s in result["styles"] if s["score"] >= 0.02][:5] or result["styles"][:1]
    return {
        "filename": filename, "filepath": filepath, "title": title, "tags": tags,
        "styles": [{"parent": s["parent"], "style": s["style"],
                    "score": round(s["score"], 4)} for s in styles],
        "salience": result.get("salience"),
        "frames": result.get("frames"),
        "bpm": result.get("bpm"), "bpm_confidence": result.get("bpm_confidence"),
        "key": result.get("key"), "scale": result.get("scale"),
        "camelot": result.get("camelot"), "key_strength": result.get("key_strength"),
        "duration": result.get("duration"),
        "waveform": result.get("waveform"),
        "segments": result.get("segments"),
        "custom": result.get("custom"),
    }


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


@app.post("/analyze")
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
            result = analyze(p)          # analyze() locks its own model inference
            emb = result.pop("emb_mean", None)
            payload = build_payload(f.filename, None, title, tags, result)
            cache_put(h, f.filename, None, title, payload, emb)
            payload["hash"] = h
            payload["cached"] = False
            return jsonify(payload)
    except UploadError as e:
        return jsonify({"error": str(e)}), e.status
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/refine")
def refine_route():
    """Re-analyze one track at fine resolution; returns a denser segment list."""
    f = request.files.get("file")
    try:
        _check_upload(f)
        if FAKE:
            import hashlib, random
            rng = random.Random(hashlib.md5(("fine" + f.filename).encode()).hexdigest())
            pool = ["Drum n Bass", "Trance", "Dubstep", "Hard Techno", "Hardstyle",
                    "House", "Techno", "Jungle", "Breakcore", "Psy-Trance"]
            rng.shuffle(pool)
            seg_styles = [pool[0]] * 4 + pool[1:3]
            segments = []
            for _ in range(rng.randint(30, 60)):
                segments += [rng.choice(seg_styles)] * rng.randint(3, 12)
            frames = []
            for s in segments:
                others = rng.sample([p for p in pool if p != s], 3)
                top = round(rng.uniform(0.25, 0.6), 3)
                rest = sorted((round(rng.uniform(0.02, top - 0.02), 3) for _ in range(3)), reverse=True)
                frames.append([[s, top]] + [[others[j], rest[j]] for j in range(3)])
            return jsonify({"segments": segments, "frames": frames, "hop_seconds": FINE_HOP_SECONDS})

        with saved_upload(f) as p:
            segments, frames = refine_segments(p)   # locks its own inference
            return jsonify({"segments": segments, "frames": frames, "hop_seconds": FINE_HOP_SECONDS})
    except UploadError as e:
        return jsonify({"error": str(e)}), e.status
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _top_styles(vec, labels, k=6, thresh=0.02):
    """Top-k [{parent,style,score}] from a 400-dim genre vector."""
    import numpy as np
    order = np.argsort(vec)[::-1][:k]
    out = []
    for i in order:
        if float(vec[int(i)]) < thresh:
            break
        parent, child = labels[int(i)].split("---", 1)
        out.append({"parent": parent, "style": child,
                    "score": round(float(vec[int(i)]), 4)})
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
    get_maest()                     # warm MAEST (if installed) before taking the lock
    with _lock:
        eff = np.mean(eng["classifier"](eng["embedder"](audio16)), axis=0)
        mae = maest_genre(audio16)
    if mae is None:
        return {"maest_available": False, "effnet": _top_styles(eff, labels)}
    # union of each engine's top-K, wide enough that the merged top-5 for ANY
    # weight is contained in it; carry both scores per style
    K = 15
    idx = sorted(set(np.argsort(eff)[::-1][:K]) | set(np.argsort(mae)[::-1][:K]),
                 key=lambda i: -max(float(eff[i]), float(mae[i])))
    pairs = [{"parent": labels[i].split("---", 1)[0],
              "style":  labels[i].split("---", 1)[1],
              "eff": round(float(eff[i]), 4), "mae": round(float(mae[i]), 4)}
             for i in idx]
    return {"maest_available": True, "weight": weight, "pairs": pairs}


@app.post("/compare")
def compare_route():
    """A/B the EffNet genre read against MAEST + their merge for one track.
    On demand only (MAEST is ~10x slower); does NOT touch the analysis cache.
    Accepts a file upload (dropped tracks) or a server-side filepath (batch)."""
    if FAKE:
        import random
        rng = random.Random(42)
        pool = ["Drum n Bass", "Dance-pop", "House", "Deep House", "Techno", "Trance", "Dubstep"]
        pairs = [{"parent": "Electronic", "style": s,
                  "eff": round(rng.uniform(0.03, 0.45), 3),
                  "mae": round(rng.uniform(0.03, 0.45), 3)} for s in pool]
        return jsonify({"maest_available": True, "weight": 0.5, "pairs": pairs})

    filepath = (request.form.get("filepath") or "").strip()
    try:
        if filepath:
            p = Path(filepath)
            if not p.is_file():
                return jsonify({"error": f"file not found: {filepath}"}), 404
            return jsonify(compare_engines(p))   # locks only its own inference pass
        with saved_upload(request.files.get("file"),
                          "no file or filepath provided") as p:
            return jsonify(compare_engines(p))   # locks only its own inference pass
    except UploadError as e:
        return jsonify({"error": str(e)}), e.status
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/save_training")
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
        existed = dest.exists()          # capture BEFORE the copy creates it
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


# ----------------------------------------------------------------------------
# Audio preview: stream a previously-analyzed track for in-app playback
# ----------------------------------------------------------------------------
AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".flac": "audio/flac", ".wav": "audio/wav",
    ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac",
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".aif": "audio/aiff", ".aiff": "audio/aiff", ".aifc": "audio/aiff",
    ".wma": "audio/x-ms-wma",
}


@app.get("/audio/<h>")
def audio_route(h):
    """Stream a track's audio by content hash for the in-app preview player.
    Read-only; serves ONLY files already recorded in the analysis DB (so this is
    not an arbitrary-file endpoint). Supports HTTP Range so the browser can seek.
    Browser-dropped files have no server path -- those play client-side via a
    blob URL instead, so a 404 here is expected for them."""
    with _db_lock, closing(db()) as conn, conn as c:
        row = c.execute("SELECT filepath, filename FROM tracks WHERE hash=?",
                        (h,)).fetchone()
    if not row or not row[0]:
        return jsonify({"error": "no server-side file for this track"}), 404
    p = Path(row[0])
    if not p.is_file():
        return jsonify({"error": "file no longer exists on disk"}), 404
    mime = AUDIO_MIME.get(p.suffix.lower(), "application/octet-stream")
    return send_file(str(p), mimetype=mime, conditional=True,
                     as_attachment=False, download_name=row[1] or p.name)


# ----------------------------------------------------------------------------
# Tags: manual designations ("high energy", "opener"...) attached to tracks
# ----------------------------------------------------------------------------
@app.get("/tags")
def tags_list():
    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute(
            "SELECT t.id, t.name, COUNT(tt.hash) FROM tags t "
            "LEFT JOIN track_tags tt ON tt.tag_id = t.id "
            "GROUP BY t.id ORDER BY t.name").fetchall()
    return jsonify([{"id": r[0], "name": r[1], "count": r[2]} for r in rows])


@app.post("/tags")
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


@app.post("/tags/toggle")
def tags_toggle():
    """Add the tag to the track if absent, remove it if present."""
    data = request.get_json(silent=True) or {}
    tid, h = data.get("tag_id"), data.get("hash")
    if not tid or not h:
        return jsonify({"error": "tag_id and hash required"}), 400
    with _db_lock, closing(db()) as conn, conn as c:
        row = c.execute("SELECT 1 FROM track_tags WHERE tag_id=? AND hash=?",
                        (tid, h)).fetchone()
        if row:
            c.execute("DELETE FROM track_tags WHERE tag_id=? AND hash=?", (tid, h))
            return jsonify({"tagged": False})
        c.execute("INSERT OR IGNORE INTO track_tags VALUES(?,?)", (tid, h))
        return jsonify({"tagged": True})


@app.get("/tags/for/<h>")
def tags_for(h):
    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute(
            "SELECT t.id, t.name FROM track_tags tt JOIN tags t ON t.id=tt.tag_id "
            "WHERE tt.hash=? ORDER BY t.name", (h,)).fetchall()
    return jsonify([{"id": r[0], "name": r[1]} for r in rows])


# ----------------------------------------------------------------------------
# Vibes: user-defined similarity clusters over cached track embeddings
# ----------------------------------------------------------------------------
@app.get("/vibes")
def vibes_list():
    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute(
            "SELECT v.id, v.name, COUNT(t.hash) FROM vibes v "
            "LEFT JOIN vibe_tracks t ON t.vibe_id = v.id "
            "GROUP BY v.id ORDER BY v.name").fetchall()
    return jsonify([{"id": r[0], "name": r[1], "count": r[2]} for r in rows])


@app.post("/vibes")
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
        c.execute("INSERT INTO vibe_tracks(vibe_id, hash, weight) VALUES(?,?,?) "
                  "ON CONFLICT(vibe_id, hash) DO UPDATE SET weight=excluded.weight",
                  (vid, h, weight))


@app.post("/vibes/add")
def vibes_add():
    data = request.get_json(silent=True) or {}
    vid, h = data.get("vibe_id"), data.get("hash")
    if not vid or not h:
        return jsonify({"error": "vibe_id and hash required"}), 400
    weight = max(-1.0, min(1.0, float(data.get("weight", 1.0))))
    _upsert_vibe_weight(vid, h, weight)
    return jsonify({"added": True, "weight": weight})


@app.post("/vibes/weight")
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


@app.post("/vibes/remove")
def vibes_remove():
    """Remove a track from a vibe entirely (drop the membership link)."""
    data = request.get_json(silent=True) or {}
    vid, h = data.get("vibe_id"), data.get("hash")
    if not vid or not h:
        return jsonify({"error": "vibe_id and hash required"}), 400
    with _db_lock, closing(db()) as conn, conn as c:
        c.execute("DELETE FROM vibe_tracks WHERE vibe_id=? AND hash=?", (vid, h))
    return jsonify({"removed": True})


@app.get("/vibes/<int:vid>/members")
def vibes_members(vid):
    """Member tracks of a vibe with their current weights, for the weight editor.
    Ordered strongest-pull first."""
    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute(
            "SELECT vt.hash, vt.weight, t.title, t.filename FROM vibe_tracks vt "
            "LEFT JOIN tracks t ON t.hash=vt.hash WHERE vt.vibe_id=? "
            "ORDER BY vt.weight DESC", (vid,)).fetchall()
    return jsonify([{"hash": r[0],
                     "weight": 1.0 if r[1] is None else round(float(r[1]), 3),
                     "title": r[2], "filename": r[3]} for r in rows])


@app.get("/vibes/match/<h>")
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


@app.get("/vibes/<int:vid>/playlist")
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
            "WHERE embedding IS NOT NULL").fetchall()
        members = {r[0] for r in c.execute(
            "SELECT hash FROM vibe_tracks WHERE vibe_id=?", (vid,)).fetchall()}
    out = []
    for h, title, filename, payload, blob in rows:
        emb = np.frombuffer(blob, dtype=np.float32)
        sim = cosine(emb, cen)
        if sim < threshold:
            continue
        p = json.loads(payload)
        out.append({"hash": h, "title": title, "filename": filename,
                    "sim": round(sim, 4), "member": h in members,
                    "bpm": p.get("bpm"), "camelot": p.get("camelot"),
                    "duration": p.get("duration")})
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


@app.get("/map")
def map_route():
    import numpy as np
    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute(
            "SELECT hash, title, filename, payload, embedding FROM tracks").fetchall()
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
        K = 2                                    # nearest neighbours per node
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
                edges.append({"a": nodes[emb_idx[a]]["hash"],
                              "b": nodes[emb_idx[int(b)]]["hash"],
                              "sim": round(s, 3)})
        # (2) PCA of the embeddings -> a few sonic coordinates per track. The
        #     client picks, per genre region, the 2 components that best spread
        #     THAT region's members, so a big single-genre cluster (e.g. all
        #     dubstep) still fans out by how the tracks actually sound.
        Mc = M - M.mean(axis=0, keepdims=True)
        try:
            _, _, Vt = np.linalg.svd(Mc, full_matrices=False)
            ncomp = int(min(8, Vt.shape[0]))
            proj = Mc @ Vt[:ncomp].T             # (m, ncomp)
            std = proj.std(axis=0, keepdims=True)
            std[std == 0] = 1.0
            proj = proj / std                    # standardise each component
            for k, i in enumerate(emb_idx):
                nodes[i]["e"] = [round(float(v), 4) for v in proj[k]]
        except np.linalg.LinAlgError:
            pass
    return jsonify({"nodes": nodes, "edges": edges})


@app.get("/similar/<h>")
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
            "WHERE embedding IS NOT NULL AND hash != ?", (h,)).fetchall()
    out = []
    for hh, title, filename, payload, blob in rows:
        emb = np.frombuffer(blob, dtype=np.float32)
        p = json.loads(payload)
        style, _ = _dominant_style(p)
        out.append({"hash": hh,
                    "title": title or (Path(filename).stem if filename else hh[:8]),
                    "artist": _artist_of(title, filename),
                    "style": style,
                    "bpm": p.get("bpm"), "camelot": p.get("camelot"),
                    "sim": round(cosine(target, emb), 4)})
    out.sort(key=lambda x: -x["sim"])
    return jsonify(out[:k])


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/batch")
def batch_route():
    """Scan a server-side folder path and analyze all audio files in parallel.
    The client passes a WSL path like /mnt/c/Users/you/Music.
    Returns a stream of newline-delimited JSON results (NDJSON)."""
    import concurrent.futures, json as _json
    data = request.get_json(silent=True) or {}
    folder = Path(data.get("path", "")).expanduser()
    workers = int(data.get("workers", 3))   # 3 parallel analyses, safe on most CPUs

    if not folder.is_dir():
        return jsonify({"error": f"not a directory: {folder}"}), 400

    files = sorted(p for p in folder.rglob("*")
                   if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    if not files:
        return jsonify({"error": "no audio files found"}), 404

    def analyze_one(path: Path):
        try:
            h = file_hash(path)
            cached = cache_get(h)
            if cached:
                cached = dict(cached)
                cached.update({"ok": True, "hash": h, "cached": True,
                               "filepath": str(path)})
                return cached
            title = read_title(path) or path.stem
            tags  = read_tags(path)
            result = analyze(path)
            emb = result.pop("emb_mean", None)
            payload = build_payload(path.name, str(path), title, tags, result)
            cache_put(h, path.name, str(path), title, payload, emb)
            payload.update({"ok": True, "hash": h, "cached": False})
            return payload
        except Exception as exc:
            return {"ok": False, "filename": path.name, "filepath": str(path),
                    "error": str(exc)}

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

    return app.response_class(generate(), mimetype="application/x-ndjson")


# ----------------------------------------------------------------------------

if __name__ == "__main__":
    if FAKE:
        print("FAKE_ANALYZER=1 -- serving fake results (GUI test mode, no Essentia).")
    print("Genre v2 running -> http://localhost:5005")
    app.run(host=os.environ.get("GENRE_HOST", "127.0.0.1"), port=5005, debug=False, threaded=True)
