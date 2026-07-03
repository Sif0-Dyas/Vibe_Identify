# Genre v2

A local music-genre analysis web app. Drop an audio file (or point it at a
folder) and it identifies the genre using Essentia's Discogs-EffNet model across
400 Discogs styles — plus BPM, musical key (Camelot), and a genre-colored
fisheye waveform. Everything runs locally; no internet is used after the
one-time model download, and **your music files are never modified** — analysis
is read-only and training saves are copies.

Built as a classic-Winamp homage (beveled panels, LCD readouts, Silkscreen
font).

---

## Stack

- **Backend:** Python 3.14 · Flask · `essentia-tensorflow` (CPU only) · NumPy ·
  Mutagen · SQLite
- **Frontend:** vanilla JS + Canvas (no frameworks)
- **Runs in:** WSL2 (venv at `~/genre`), served at `localhost:5005`, used from a
  Windows browser. Windows music lives under `/mnt/c/...`.

> GPU is a known dead end for this model — don't revisit. Analysis is CPU-bound
> by design.

---

## Files

```
Genre Identifier/
  genre_gui.py         ← Flask backend + all analysis logic (start here; top
                         docstring is an architecture summary)
  templates/index.html ← HTML shell (Flask renders this via render_template)
  static/app.js        ← all frontend logic: lens system, row rendering, canvas
  static/app.css       ← all styling (Winamp skin layered at the end)
  CHANGELOG.md         ← version history — update on every notable change
  README.md            ← this file
```

> Flask serves `index.html` from `templates/` and CSS/JS from `static/`. The app
> **will not run** if these files are flattened into the project root
> (`render_template` 500s with `TemplateNotFound`, `/static/*` 404s).

External, separate from this repo:
- `embed_extract.py` + `train_head.py` — the custom-head training pipeline.
- `~/genre_training/<genre>/` — where manual overrides copy tracks for training.
- `~/essentia_models/` — downloaded model graphs (+ optional `custom_head.npz`).
- `~/genre_v2.db` — SQLite cache/vibes/tags (override with env `GENRE_DB`).

---

## Run

```bash
python genre_gui.py                    # real Essentia analysis
FAKE_ANALYZER=1 python genre_gui.py    # instant fake results, no models (UI dev)
```

Then open <http://localhost:5005> in your Windows browser.

The server binds `127.0.0.1` (localhost) by default; set `GENRE_HOST=0.0.0.0` to
deliberately opt into LAN/Tailscale access. The HTTP API assumes a trusted caller
(routes read/scan/copy server-side paths), so only expose it beyond localhost
intentionally.

Environment variables: `MODEL_DIR` (model download dir), `GENRE_DB` (SQLite
path), `CUSTOM_HEAD` (path to `custom_head.npz`), `GENRE_HOST` (bind address,
default `127.0.0.1`), `FAKE_ANALYZER=1` (mock mode).

---

## Analysis pipeline (per track)

1. Decode to 16 kHz mono → EffNet embedder → 1280-dim embeddings.
2. Discogs classifier head → 400 style scores × N frames.
3. `frame_topk()` → top-6 styles per frame (feeds the lens system).
4. `salience_read()` → energy × confidence × recurrence weighting for a single
   overall identity read.
5. Decode to 44.1 kHz → `RhythmExtractor2013` (BPM) + `KeyExtractor` (key →
   Camelot).
6. `waveform_peaks()` → 240-point amplitude envelope for drawing.

The optional custom head (if `custom_head.npz` exists) scores the same
embeddings — no extra audio decode.

---

## The two-lens system

All lens logic is **client-side** in `app.js`. `GLOBAL` holds the defaults; any
row may override either axis independently.

**Identity lens** (side breakdown — "what genre *is* this track?"):
- `v2` — salience-weighted (energy × confidence × recurrence). Default.
- `v1` — flat % of track by frame count over the segmentation stream.

**Segmentation lens** (waveform stream — "what plays *when*?"):
- `raw` — per-frame argmax winners.
- `hysteresis` — hold the current genre until a challenger beats it by a margin
  for several consecutive frames (kills flicker). Default.
- `sibling` — pool near-synonym scores per frame, then take the winner. Groups
  (House / Trance / Techno / …) are **editable** in the sibling panel.
- `hyst+sib` — sibling-merge, then hysteresis over the result.

Downstream of the segmentation lens: `smoothSegments()` does temporal cleanup,
then `mainGenreSet()` / `bandColor()` / `bandLabel()` collapse the minor-genre
tail into a single grey **"Other"** bucket — applied identically to the waveform
and the breakdown so the two always agree.

---

## Features

- **Cache:** analysis is keyed by *content hash*, so the same song returns
  instantly regardless of filename/location (shown with a "cached" tag).
- **Fine detail:** re-analyze one track at ~0.5 s resolution on demand
  (`/refine`, 4× overlap at ~4× cost).
- **Vibes:** user-defined similarity clusters over the 1280-dim mean embeddings.
  New tracks auto-show match %; a vibe's "playlist" scans the whole DB by cosine
  similarity to the vibe's centroid.
- **Tags:** manual toggleable designations ("high energy", "opener") per track.
- **Manual override:** set the genre yourself; the track is copied to
  `~/genre_training/<genre>/` to feed the custom-head training pipeline.
- **Batch folder:** point at a WSL folder path; results stream as NDJSON with 3
  parallel workers, cache-aware.
- **Export:** dump the current list (genres, BPM, key, duration) to `.txt`.

---

## HTTP routes

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | UI shell |
| `/analyze` | POST | single file upload → full analysis JSON (cache-first) |
| `/refine` | POST | single file → dense segment stream (~0.5 s hop) |
| `/batch` | POST | `{path, workers}` → NDJSON stream of results |
| `/save_training` | POST | copy a track to `~/genre_training/<genre>/` |
| `/tags`, `/tags/toggle`, `/tags/for/<h>` | GET/POST | manual tags |
| `/vibes`, `/vibes/add`, `/vibes/match/<h>`, `/vibes/<id>/playlist` | GET/POST | similarity clusters |

---

## Guarantees

- **Read-only** on your music library. Training saves are copies; nothing is
  moved, renamed, or edited.
- **Fully local.** The only network access is the one-time model download to
  `~/essentia_models/`.

---

## Maintaining docs

When you change behavior, record it in [`CHANGELOG.md`](./CHANGELOG.md) under
`[Unreleased]`, then stamp a version + date on release. See that file's header
for the MAJOR/MINOR/PATCH convention (note: model/embedding/cache-key changes are
MAJOR because they invalidate cached results).
