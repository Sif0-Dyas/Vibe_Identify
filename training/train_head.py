#!/usr/bin/env python3
"""Train the custom-genre classifier head from cached embeddings.

Consumes the ``manifest.json`` + ``_cache/*.npy`` written by
``embed_extract.py`` and trains a small from-scratch NumPy MLP
(1280 -> hidden ReLU -> K softmax) on the frame embeddings, with a
*track-level* train/validation split (all frames of one track stay on one
side, so validation measures generalization to unseen tracks, not unseen
frames of seen tracks).

Saves ``~/essentia_models/custom_head.npz`` in exactly the schema the app's
``vibedentify.analysis.get_custom_head`` loads: W1, b1, W2, b2, mu, sigma,
labels, val_acc. Restart the app after training and the "custom" row appears.

The from-scratch implementation (manual backprop, Adam) is deliberate -- it
keeps the training dependency-free beyond NumPy and doubles as a readable
reference for how the head works.

Usage:
    python training/train_head.py                 # ~/genre_training
    python training/train_head.py DIR --hidden 256 --epochs 200
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

# Allow running from a repo checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
def load_dataset(data_dir: Path, seed: int, val_frac: float = 0.2):
    """Frame matrices + track-level split.

    Returns (Xtr, ytr, val_tracks, labels) where val_tracks is a list of
    (frames_matrix, class_index) per held-out track -- validation is scored
    the way the app predicts: mean softmax over a track's frames, argmax.
    """
    manifest = json.loads((data_dir / "manifest.json").read_text())
    cache = data_dir / "_cache"
    labels = sorted(manifest)
    rng = np.random.default_rng(seed)

    Xtr_parts, ytr_parts, val_tracks = [], [], []
    for ci, genre in enumerate(labels):
        entries = manifest[genre]
        idx = rng.permutation(len(entries))
        n_val = max(1, int(round(len(entries) * val_frac))) if len(entries) > 1 else 0
        val_ids = set(idx[:n_val].tolist())
        for i, e in enumerate(entries):
            embs = np.load(cache / e["cache"]).astype(np.float32)
            if embs.ndim != 2 or embs.shape[0] == 0:
                continue
            if i in val_ids:
                val_tracks.append((embs, ci))
            else:
                Xtr_parts.append(embs)
                ytr_parts.append(np.full(len(embs), ci, dtype=np.int64))
    if not Xtr_parts:
        raise SystemExit("no training data -- run embed_extract.py first")
    return np.vstack(Xtr_parts), np.concatenate(ytr_parts), val_tracks, labels


# --------------------------------------------------------------------------
# model: 1280 -> H (ReLU) -> K (softmax), trained with Adam
# --------------------------------------------------------------------------
def train(
    X: np.ndarray,
    y: np.ndarray,
    val_tracks: list,
    labels: list[str],
    hidden: int = 256,
    epochs: int = 200,
    lr: float = 1e-3,
    batch: int = 256,
    patience: int = 20,
    seed: int = 0,
    verbose: bool = True,
):
    """Returns the trained head dict (app schema) and best val accuracy."""
    rng = np.random.default_rng(seed)
    K = len(labels)

    # standardize on TRAIN stats; sigma floored so dead dims don't divide by ~0
    mu = X.mean(axis=0)
    sigma = np.maximum(X.std(axis=0), 1e-6)
    Xn = (X - mu) / sigma

    # He init for the ReLU layer, zeros elsewhere
    W1 = rng.normal(0.0, np.sqrt(2.0 / Xn.shape[1]), (Xn.shape[1], hidden)).astype(np.float32)
    b1 = np.zeros(hidden, dtype=np.float32)
    W2 = rng.normal(0.0, np.sqrt(2.0 / hidden), (hidden, K)).astype(np.float32)
    b2 = np.zeros(K, dtype=np.float32)

    # Adam state
    params = [W1, b1, W2, b2]
    m = [np.zeros_like(p) for p in params]
    v = [np.zeros_like(p) for p in params]
    b1_, b2_, eps, t = 0.9, 0.999, 1e-8, 0

    def track_acc():
        """Validation accuracy the way the app predicts (per-track mean softmax)."""
        if not val_tracks:
            return float("nan")
        hit = 0
        for embs, ci in val_tracks:
            Xv = (embs - mu) / sigma
            h = np.maximum(Xv @ W1 + b1, 0.0)
            logits = h @ W2 + b2
            e = np.exp(logits - logits.max(axis=1, keepdims=True))
            probs = (e / e.sum(axis=1, keepdims=True)).mean(axis=0)
            hit += int(np.argmax(probs) == ci)
        return hit / len(val_tracks)

    best = {"acc": -1.0, "params": None, "epoch": 0}
    n = len(Xn)
    for epoch in range(1, epochs + 1):
        order = rng.permutation(n)
        for s in range(0, n, batch):
            idx = order[s : s + batch]
            xb, yb = Xn[idx], y[idx]

            # forward
            h_pre = xb @ W1 + b1
            h = np.maximum(h_pre, 0.0)
            logits = h @ W2 + b2
            e = np.exp(logits - logits.max(axis=1, keepdims=True))
            p = e / e.sum(axis=1, keepdims=True)

            # backward (softmax cross-entropy)
            g = p
            g[np.arange(len(yb)), yb] -= 1.0
            g /= len(yb)
            dW2 = h.T @ g
            db2 = g.sum(axis=0)
            dh = (g @ W2.T) * (h_pre > 0)
            dW1 = xb.T @ dh
            db1 = dh.sum(axis=0)

            # Adam step
            t += 1
            for p_, g_, i in ((W1, dW1, 0), (b1, db1, 1), (W2, dW2, 2), (b2, db2, 3)):
                m[i] = b1_ * m[i] + (1 - b1_) * g_
                v[i] = b2_ * v[i] + (1 - b2_) * g_ * g_
                p_ -= lr * (m[i] / (1 - b1_**t)) / (np.sqrt(v[i] / (1 - b2_**t)) + eps)

        acc = track_acc()
        if verbose and (epoch % 10 == 0 or epoch == 1):
            print(f"epoch {epoch:4d}  val track acc {acc:.1%}")
        if acc > best["acc"]:
            best = {
                "acc": acc,
                "params": [p.copy() for p in (W1, b1, W2, b2)],
                "epoch": epoch,
            }
        elif epoch - best["epoch"] >= patience:
            if verbose:
                print(f"early stop at epoch {epoch} (best {best['acc']:.1%} @ {best['epoch']})")
            break

    W1, b1, W2, b2 = best["params"] if best["params"] else (W1, b1, W2, b2)
    head = {
        "W1": W1,
        "b1": b1,
        "W2": W2,
        "b2": b2,
        "mu": mu.astype(np.float32),
        "sigma": sigma.astype(np.float32),
        "labels": np.array(labels),
        "val_acc": np.float32(best["acc"]),
    }
    return head, best["acc"]


def confusion(head: dict, val_tracks: list, labels: list[str]):
    """Print a per-track confusion matrix for the validation split."""
    K = len(labels)
    cm = np.zeros((K, K), dtype=int)
    for embs, ci in val_tracks:
        Xv = (embs - head["mu"]) / head["sigma"]
        h = np.maximum(Xv @ head["W1"] + head["b1"], 0.0)
        logits = h @ head["W2"] + head["b2"]
        e = np.exp(logits - logits.max(axis=1, keepdims=True))
        probs = (e / e.sum(axis=1, keepdims=True)).mean(axis=0)
        cm[ci, int(np.argmax(probs))] += 1
    w = max(len(x) for x in labels)
    print("\nconfusion (rows = truth, cols = predicted):")
    print(" " * (w + 2) + "  ".join(f"{x[:6]:>6}" for x in labels))
    for i, name in enumerate(labels):
        print(f"{name:>{w}} |" + "  ".join(f"{n:>6d}" for n in cm[i]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "data_dir",
        nargs="?",
        default=str(Path.home() / "genre_training"),
        help="root containing manifest.json + _cache/ (default: ~/genre_training)",
    )
    ap.add_argument("--hidden", type=int, default=256)
    ap.add_argument("--epochs", type=int, default=200)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--out",
        default=None,
        help="output .npz (default: the path the app loads, via vibedentify.config)",
    )
    args = ap.parse_args()

    data_dir = Path(args.data_dir).expanduser()
    X, y, val_tracks, labels = load_dataset(data_dir, seed=args.seed)
    print(f"{len(labels)} classes: {labels}")
    print(f"train frames: {len(X)}   val tracks: {len(val_tracks)}")

    head, acc = train(
        X,
        y,
        val_tracks,
        labels,
        hidden=args.hidden,
        epochs=args.epochs,
        lr=args.lr,
        seed=args.seed,
    )
    if val_tracks:
        confusion(head, val_tracks, labels)

    if args.out:
        out = Path(args.out).expanduser()
    else:
        from vibedentify.analysis import CUSTOM_HEAD_PATH

        out = CUSTOM_HEAD_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez(out, **head)
    print(f"\nsaved -> {out}   (val track acc {acc:.1%})")
    print("restart the app; the custom row appears on the next analysis")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
