"""Smoke test for the custom-genre training pipeline (training/train_head.py).

Runs in-process on tiny synthetic data -- no models, no audio on disk. There is
deliberately NO test for training/embed_extract.py: it runs real audio through
the Essentia EffNet embedder, which -- like analysis.py's real-analysis path --
can't run in CI (the suite runs in FAKE_ANALYZER mode with no models), so it
stays out of the suite by the same convention.
"""

import numpy as np
import pytest

from training.train_head import train


def test_train_head_learns_and_round_trips(tmp_path):
    # Two well-separated synthetic clusters -> the head should nail the (unseen
    # tracks) validation split. Each "track" is a few frames near its class center.
    rng = np.random.default_rng(0)
    dim = 1280
    c0 = np.zeros(dim, dtype=np.float32)
    c0[:64] = 3.0
    c1 = np.zeros(dim, dtype=np.float32)
    c1[:64] = -3.0
    centers = [c0, c1]

    def tracks(ci, n):
        return [
            ((centers[ci] + rng.normal(0, 0.5, (4, dim))).astype(np.float32), ci) for _ in range(n)
        ]

    train_tracks = tracks(0, 15) + tracks(1, 15)
    val_tracks = tracks(0, 5) + tracks(1, 5)
    X = np.vstack([f for f, _ in train_tracks])
    y = np.concatenate([np.full(len(f), ci, dtype=np.int64) for f, ci in train_tracks])
    labels = ["Riddim", "other"]

    head, acc = train(X, y, val_tracks, labels, hidden=32, epochs=40, verbose=False, seed=0)
    assert acc > 0.9

    # np.savez must round-trip through the EXACT load pattern the app uses in
    # vibedentify.analysis.get_custom_head: the six arrays + labels, allow_pickle=False.
    out = tmp_path / "custom_head.npz"
    np.savez(out, **head)
    d = np.load(out, allow_pickle=False)
    reloaded = {k: d[k] for k in ("W1", "b1", "W2", "b2", "mu", "sigma")}
    assert set(reloaded) == {"W1", "b1", "W2", "b2", "mu", "sigma"}
    assert all(reloaded[k].dtype == np.float32 for k in reloaded)
    assert [str(x) for x in d["labels"]] == labels
    assert float(d["val_acc"]) == pytest.approx(acc)
