"""Essentia model plumbing (EffNet / MAEST / custom head) and the per-track
analysis pipeline: genre styles, BPM, key, and the waveform envelope.
"""

import json
import os
import threading
import urllib.request
from pathlib import Path

from .config import FAKE, MODEL_DIR, MODELS, log

_lock = threading.Lock()  # TF models are shared + not thread-safe; hold during inference only

_engine = {}  # lazily-built: labels, embedder, classifier
_engine_lock = threading.Lock()  # guards the one-time model build/download so
# /batch's first 3 workers don't race on it


def ensure_models():
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in MODELS.items():
        dest = MODEL_DIR / name
        if not (dest.exists() and dest.stat().st_size > 0):
            log.info("downloading %s ...", name)
            urllib.request.urlretrieve(url, dest)


def get_engine():
    """Build (once) and return labels + models. Guarded by _engine_lock so the
    first /batch run (3 workers hitting an unbuilt engine at once) can't race on
    the model download or construct duplicate TF instances."""
    if _engine:
        return _engine
    with _engine_lock:
        if _engine:  # another thread built it while we waited
            return _engine
        ensure_models()
        from essentia.standard import TensorflowPredict2D, TensorflowPredictEffnetDiscogs

        with open(MODEL_DIR / "genre_discogs400-discogs-effnet-1.json") as fh:
            labels = json.load(fh)["classes"]
        embedder = TensorflowPredictEffnetDiscogs(
            graphFilename=str(MODEL_DIR / "discogs-effnet-bs64-1.pb"), output="PartitionedCall:1"
        )
        classifier = TensorflowPredict2D(
            graphFilename=str(MODEL_DIR / "genre_discogs400-discogs-effnet-1.pb"),
            input="serving_default_model_Placeholder",
            output="PartitionedCall:0",
        )
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
        if "maest" in _engine:  # built while we waited on the lock
            return _engine["maest"]
        if not MAEST_PB.exists():
            _engine["maest"] = None
            return None
        from essentia.standard import TensorflowPredictMAEST

        _engine["maest"] = TensorflowPredictMAEST(
            graphFilename=str(MAEST_PB),
            input="serving_default_melspectrogram",  # this graph's actual input node
            output="StatefulPartitionedCall:0",
        )  # discogs-400 predictions, direct
        return _engine["maest"]


def maest_genre(audio16):
    """Track-level 400-dim genre probabilities from MAEST (mean over 30s patches),
    aligned to the same Discogs-400 label order as the EffNet head. None if the
    MAEST model isn't installed."""
    import numpy as np

    m = get_maest()
    if m is None:
        return None
    preds = np.asarray(m(audio16))  # shape (patches, 1, 1, 400)
    return preds.reshape(-1, preds.shape[-1]).mean(axis=0)


# --- optional custom head (trained with train_head.py) ----------------------
CUSTOM_HEAD_PATH = Path(os.environ.get("CUSTOM_HEAD", MODEL_DIR / "custom_head.npz"))
_custom = {"checked": False, "head": None}


def get_custom_head():
    """Load ~/essentia_models/custom_head.npz once, if it exists. Guarded so the
    concurrent /batch workers that call this (via custom_predict) load it once."""
    if _custom["checked"]:
        return _custom["head"]
    with _engine_lock:
        if _custom["checked"]:  # loaded while we waited on the lock
            return _custom["head"]
        if CUSTOM_HEAD_PATH.exists():
            try:
                import numpy as np

                d = np.load(CUSTOM_HEAD_PATH, allow_pickle=False)
                head = {k: d[k] for k in ("W1", "b1", "W2", "b2", "mu", "sigma")}
                head["labels"] = [str(x) for x in d["labels"]]
                acc = float(d["val_acc"]) if "val_acc" in d else None
                _custom["head"] = head  # publish only once fully built
                log.info(
                    "custom head loaded: %s%s",
                    head["labels"],
                    f"  (val acc {acc:.0%})" if acc else "",
                )
            except Exception:
                log.warning("could not load custom head", exc_info=True)
        _custom["checked"] = True  # set last: don't try again either way
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
    probs = (e / e.sum(axis=1, keepdims=True)).mean(axis=0)  # avg over frames
    order = np.argsort(probs)[::-1]
    return [{"style": head["labels"][int(i)], "score": round(float(probs[i]), 4)} for i in order]


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
                for k in (
                    "title",
                    "artist",
                    "album",
                    "albumartist",
                    "genre",
                    "date",
                    "tracknumber",
                    "discnumber",
                    "composer",
                    "bpm",
                ):
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
    ("C", "major"): "8B",
    ("G", "major"): "9B",
    ("D", "major"): "10B",
    ("A", "major"): "11B",
    ("E", "major"): "12B",
    ("B", "major"): "1B",
    ("F#", "major"): "2B",
    ("Gb", "major"): "2B",
    ("C#", "major"): "3B",
    ("Db", "major"): "3B",
    ("G#", "major"): "4B",
    ("Ab", "major"): "4B",
    ("D#", "major"): "5B",
    ("Eb", "major"): "5B",
    ("A#", "major"): "6B",
    ("Bb", "major"): "6B",
    ("F", "major"): "7B",
    ("A", "minor"): "8A",
    ("E", "minor"): "9A",
    ("B", "minor"): "10A",
    ("F#", "minor"): "11A",
    ("Gb", "minor"): "11A",
    ("C#", "minor"): "12A",
    ("Db", "minor"): "12A",
    ("G#", "minor"): "1A",
    ("Ab", "minor"): "1A",
    ("D#", "minor"): "2A",
    ("Eb", "minor"): "2A",
    ("A#", "minor"): "3A",
    ("Bb", "minor"): "3A",
    ("F", "minor"): "4A",
    ("C", "minor"): "5A",
    ("G", "minor"): "6A",
    ("D", "minor"): "7A",
}

WAVE_BINS = 240


def waveform_peaks(audio, bins=WAVE_BINS):
    """Downsample |audio| into `bins` peak values in 0..1 for drawing."""
    import numpy as np

    a = np.abs(np.asarray(audio))
    if a.size == 0:
        return [0.0] * bins
    edges = np.linspace(0, a.size, bins + 1, dtype=int)
    peaks = np.array(
        [a[edges[i] : edges[i + 1]].max() if edges[i + 1] > edges[i] else 0.0 for i in range(bins)]
    )
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
    energy = np.array(
        [
            float(np.sqrt(np.mean(a[edges[i] : edges[i + 1]] ** 2)))
            if edges[i + 1] > edges[i]
            else 0.0
            for i in range(n)
        ]
    )
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
        import hashlib
        import math
        import random

        rng = random.Random(hashlib.md5(path.name.encode()).hexdigest())
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
        scores = sorted((rng.uniform(0.04, 0.55) for _ in range(4)), reverse=True)
        key, scale = rng.choice(list(CAMELOT.keys()))
        wave = [round(abs(math.sin(i / 9) * rng.uniform(0.4, 1.0)), 3) for i in range(WAVE_BINS)]
        segments = []
        seg_styles = [pool[0]] * 3 + pool[1:3]  # mostly primary, some switches
        for _ in range(rng.randint(5, 9)):
            segments += [rng.choice(seg_styles)] * rng.randint(8, 30)
        # fake salience: weight the primary genre up, as energy-weighting would
        from collections import Counter as _C

        _c = _C(segments)
        _tot = sum(_c.values())
        _sal = sorted(((g, n / _tot) for g, n in _c.items()), key=lambda kv: -kv[1])
        _sal = [(_sal[0][0], min(0.92, _sal[0][1] + 0.15))] + _sal[1:]
        _s = sum(p for _, p in _sal)
        salience = [{"style": g, "score": round(p / _s, 4)} for g, p in _sal]
        # fake per-frame top-k: winner + near-misses (House/Tribal House flicker-like)
        frames = []
        for s in segments:
            others = rng.sample([p for p in pool if p != s], 3)
            top = round(rng.uniform(0.26, 0.55), 3)
            rest = sorted(
                (round(rng.uniform(0.02, max(0.03, top - 0.02)), 3) for _ in range(3)), reverse=True
            )
            frames.append([[s, top]] + [[others[j], rest[j]] for j in range(3)])
        return {
            "styles": [
                {"parent": "Electronic", "style": s, "score": v} for s, v in zip(pool, scores)
            ],
            "segments": segments,
            "salience": salience,
            "frames": frames,
            "custom": [
                {"style": s, "score": round(v, 4)}
                for s, v in zip(
                    ["Riddim", "Tearout", "Liquid DnB", "Other"],
                    sorted((rng.uniform(0.02, 0.7) for _ in range(4)), reverse=True),
                )
            ],
            "bpm": round(rng.uniform(120, 178), 1),
            "bpm_confidence": rng.uniform(0.5, 5.0),
            "key": key,
            "scale": scale,
            "camelot": CAMELOT[(key, scale)],
            "key_strength": rng.uniform(0.5, 0.95),
            "duration": rng.uniform(150, 420),
            "waveform": wave,
            "emb_mean": [rng.uniform(-1, 1) for _ in range(1280)],
        }

    import numpy as np
    from essentia.standard import KeyExtractor, MonoLoader, RhythmExtractor2013

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
        "bpm": round(bpm, 1) if bpm else None,
        "bpm_confidence": bpm_conf,
        "key": key,
        "scale": scale,
        "camelot": camelot,
        "key_strength": key_strength,
        "duration": duration,
        "waveform": waveform_peaks(audio44),
        "emb_mean": [float(x) for x in np.mean(embeddings, axis=0)],
    }


# default embedder hops 128 mel-frames (~2.0s); a 32-frame hop (~0.5s) gives 4x
# overlap and thus ~4x finer genre-boundary resolution -- at ~4x the inference cost.
FINE_HOP = 32
FINE_HOP_SECONDS = round(FINE_HOP * 256 / 16000, 2)  # 256-sample mel hop @ 16 kHz


def get_fine_embedder():
    eng = get_engine()
    if "embedder_fine" in eng:
        return eng["embedder_fine"]
    with _engine_lock:
        if "embedder_fine" not in eng:
            from essentia.standard import TensorflowPredictEffnetDiscogs

            eng["embedder_fine"] = TensorflowPredictEffnetDiscogs(
                graphFilename=str(MODEL_DIR / "discogs-effnet-bs64-1.pb"),
                output="PartitionedCall:1",
                patchHopSize=FINE_HOP,
            )
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
        "filename": filename,
        "filepath": filepath,
        "title": title,
        "tags": tags,
        "styles": [
            {"parent": s["parent"], "style": s["style"], "score": round(s["score"], 4)}
            for s in styles
        ],
        "salience": result.get("salience"),
        "frames": result.get("frames"),
        "bpm": result.get("bpm"),
        "bpm_confidence": result.get("bpm_confidence"),
        "key": result.get("key"),
        "scale": result.get("scale"),
        "camelot": result.get("camelot"),
        "key_strength": result.get("key_strength"),
        "duration": result.get("duration"),
        "waveform": result.get("waveform"),
        "segments": result.get("segments"),
        "custom": result.get("custom"),
    }
