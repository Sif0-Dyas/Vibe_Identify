#!/usr/bin/env python3
"""Extract EffNet embeddings from a labeled training-audio tree.

Walks ``<data_dir>/<genre>/*`` (the folders the app's override feature fills),
runs each audio file through the same Discogs-EffNet embedder the app uses,
and caches one ``.npy`` of frame embeddings (n_frames x 1280) per file under
``<data_dir>/_cache/``. Writes ``<data_dir>/manifest.json`` mapping each genre
to its cached files -- the input ``train_head.py`` consumes.

Embeddings are keyed by audio content hash, so re-runs only embed new files.

Usage:
    python training/embed_extract.py                # ~/genre_training
    python training/embed_extract.py /path/to/tree

Requires the app's runtime deps (essentia-tensorflow); run inside the venv.
"""

import argparse
import json
import sys
from pathlib import Path

# Allow running from a repo checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vibedentify.config import AUDIO_EXTS, log  # noqa: E402
from vibedentify.db import file_hash  # noqa: E402


def iter_labeled_files(data_dir: Path):
    """Yield (genre, path) for every audio file under <data_dir>/<genre>/."""
    for genre_dir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        if genre_dir.name.startswith(("_", ".")):
            continue  # _cache and friends
        for f in sorted(genre_dir.rglob("*")):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                yield genre_dir.name, f


def embed_file(path: Path):
    """Frame embeddings (n_frames x 1280) via the app's shared engine."""
    import numpy as np
    from essentia.standard import MonoLoader

    from vibedentify.analysis import get_engine

    audio16 = MonoLoader(filename=str(path), sampleRate=16000, resampleQuality=4)()
    embs = get_engine()["embedder"](audio16)
    return np.asarray(embs, dtype=np.float32)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "data_dir",
        nargs="?",
        default=str(Path.home() / "genre_training"),
        help="root of <genre>/ folders (default: ~/genre_training)",
    )
    args = ap.parse_args()

    data_dir = Path(args.data_dir).expanduser()
    if not data_dir.is_dir():
        print(f"not a directory: {data_dir}", file=sys.stderr)
        return 1

    cache_dir = data_dir / "_cache"
    cache_dir.mkdir(exist_ok=True)

    manifest: dict[str, list[dict]] = {}
    done = skipped = 0
    for genre, f in iter_labeled_files(data_dir):
        h = file_hash(f)
        npy = cache_dir / f"{h}.npy"
        if npy.exists():
            skipped += 1
        else:
            log.info("embedding %s/%s ...", genre, f.name)
            import numpy as np

            embs = embed_file(f)
            np.save(npy, embs)
            done += 1
        manifest.setdefault(genre, []).append({"file": str(f), "hash": h, "cache": npy.name})

    out = data_dir / "manifest.json"
    out.write_text(json.dumps(manifest, indent=2))
    counts = {g: len(v) for g, v in manifest.items()}
    print(f"manifest -> {out}")
    print(f"embedded {done} new, reused {skipped} cached")
    print("tracks per genre:", json.dumps(counts, indent=2))
    low = [g for g, n in counts.items() if n < 30 and g.lower() != "other"]
    if low:
        print(
            f"note: fewer than 30 tracks for {', '.join(low)} -- "
            "the head trains, but more examples = better generalization"
        )
    if "other" not in {g.lower() for g in counts}:
        print(
            "note: no other/ folder found -- consider adding a negative class "
            "of tracks that are NONE of your custom genres"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
