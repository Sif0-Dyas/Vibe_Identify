"""Misread detection.

A genre read is suspect when it is *low-confidence* AND its nearest sonic
neighbours (by embedding cosine) strongly agree on a DIFFERENT family. That is
exactly the fingerprint of a bad read -- e.g. a bass track the classifier
labelled "K-pop" at 29%, whose 8 closest neighbours are all Bass Music at ~0.9
similarity. We only ever *flag + suggest*; we never silently overwrite (in a
library dominated by one genre, auto-correcting would create an echo chamber).
"""

import json
from contextlib import closing

import numpy as np

from .config import log
from .db import _db_lock, db

# Thresholds (tuned on the reference library -> ~5% flag rate).
CONF_MAX = 0.55  # only second-guess a shaky top read
AGREE_MIN = 0.60  # neighbours must mostly agree on one family
SIM_MIN = 0.80  # ...and the nearest neighbour must be genuinely close
K = 8  # neighbours to consult

_FAM = None


def _families():
    global _FAM
    if _FAM is None:
        try:
            from pathlib import Path

            path = Path(__file__).with_name("static") / "genre_families.json"
            _FAM = json.loads(path.read_text()).get("style_family", {})
        except Exception:
            log.warning("could not load genre_families.json for misread detection", exc_info=True)
            _FAM = {}
    return _FAM


def family_of(style):
    return _families().get((style or "").lower(), style or "Other")


def dominant(payload):
    """(top_style, confidence) from a stored payload -- manual override, then salience."""
    if payload.get("override"):
        return payload["override"], 1.0
    sal = payload.get("salience") or []
    if sal:
        return sal[0].get("style"), float(sal[0].get("score", 0))
    st = payload.get("styles") or []
    if st:
        return st[0].get("style"), float(st[0].get("score", 0))
    return None, 0.0


def _score(top_style, top_conf, neighbours):
    """Given (sim, style) neighbours sorted desc, return a flag/suggestion dict."""
    own_fam = family_of(top_style)
    vote, styvote = {}, {}
    for sim, s in neighbours:
        if sim <= 0:
            continue
        f = family_of(s)
        vote[f] = vote.get(f, 0.0) + sim
        d = styvote.setdefault(f, {})
        d[s] = d.get(s, 0.0) + sim
    if not vote:
        return None
    tot = sum(vote.values())
    nb_fam, nb_w = max(vote.items(), key=lambda kv: kv[1])
    agree = nb_w / tot
    top_sim = neighbours[0][0]
    suggest = max(styvote[nb_fam].items(), key=lambda kv: kv[1])[0]
    flag = top_conf < CONF_MAX and nb_fam != own_fam and agree >= AGREE_MIN and top_sim >= SIM_MIN
    return {
        "flag": bool(flag),
        "confidence": round(float(top_conf), 3),
        "suggested_family": nb_fam,
        "suggested_style": suggest,
        "agreement": round(agree, 3),
        "top_sim": round(top_sim, 3),
    }


def check(emb, top_style, top_conf, exclude_hash=None):
    """Neighbour sanity-check for one (in-memory) embedding against the DB.
    Returns a flag/suggestion dict, or None if there isn't enough to judge."""
    if emb is None:
        return None
    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute(
            "SELECT hash, payload, embedding FROM tracks WHERE embedding IS NOT NULL"
        ).fetchall()
    q = np.asarray(emb, dtype=np.float32)
    q = q / (np.linalg.norm(q) + 1e-9)
    sims = []
    for h, payload, blob in rows:
        if exclude_hash and h == exclude_hash:
            continue
        e = np.frombuffer(blob, dtype=np.float32)
        e = e / (np.linalg.norm(e) + 1e-9)
        s, _ = dominant(json.loads(payload))
        sims.append((float(q @ e), s))
    if len(sims) < 3:  # cold start: not enough neighbours
        return None
    sims.sort(key=lambda x: -x[0])
    return _score(top_style, top_conf, sims[:K])


def audit():
    """Scan the whole library fresh; return the list of flagged (likely-misread)
    tracks, most-suspect (lowest confidence) first."""
    with _db_lock, closing(db()) as conn, conn as c:
        rows = c.execute(
            "SELECT hash, title, payload, embedding FROM tracks WHERE embedding IS NOT NULL"
        ).fetchall()
    if len(rows) < 4:
        return []
    hashes, titles, payloads, embs = [], [], [], []
    for h, title, payload, blob in rows:
        hashes.append(h)
        titles.append(title)
        payloads.append(json.loads(payload))
        embs.append(np.frombuffer(blob, dtype=np.float32))
    M = np.vstack(embs)
    M = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    # Compute the cosine matrix in row-blocks and pull each row's top-K
    # neighbours, instead of materialising the full n*n matrix (which is
    # ~400 MB at 10k tracks and is rebuilt on every /map load).
    n = len(hashes)
    kth = min(K, n) - 1
    BLOCK = 512
    out = []
    for i0 in range(0, n, BLOCK):
        block = M[i0 : i0 + BLOCK] @ M.T  # (rows, n)
        for r in range(block.shape[0]):
            i = i0 + r
            row = block[r]
            row[i] = -1.0  # exclude self (was np.fill_diagonal)
            # top-K via argpartition, ordered by descending sim so the flag
            # decisions match the previous argsort()[:K] behaviour exactly.
            nn = np.argpartition(-row, kth)[:K]
            nn = nn[np.argsort(-row[nn])]
            top_style, top_conf = dominant(payloads[i])
            neighbours = [(float(row[int(j)]), dominant(payloads[int(j)])[0]) for j in nn]
            res = _score(top_style, top_conf, neighbours)
            if res and res["flag"]:
                out.append(
                    {
                        "hash": hashes[i],
                        "title": titles[i],
                        "style": top_style,
                        "family": family_of(top_style),
                        **res,
                    }
                )
    out.sort(key=lambda x: x["confidence"])
    return out
