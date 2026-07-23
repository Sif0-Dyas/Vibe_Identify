# Genre v2

[![CI](https://github.com/Sif0-Dyas/Vibe_Identify/actions/workflows/ci.yml/badge.svg)](https://github.com/Sif0-Dyas/Vibe_Identify/actions/workflows/ci.yml)

> Codename **Genre v2** (the DB, changelog, and this repo); the UI is branded
> **Vibedentify**.

A local music-genre analysis web app. Drop an audio file (or point it at a
folder) and it identifies the genre using Essentia's Discogs-EffNet model across
400 Discogs styles — plus BPM, musical key (Camelot), and a genre-colored
fisheye waveform. A second **Map** tab plots your whole scanned library as a
rotating 3-D constellation you can search and explore by sonic similarity.
Everything runs locally; no internet is used after the
one-time model download, and **your music files are never modified** — analysis
is read-only and training saves are copies.

Built as a classic-Winamp homage (beveled panels, LCD readouts, Silkscreen
font).

> **New here?** The [**User Guide**](docs/USAGE.md) walks through the four ways
> to analyze, how the pipeline works, the lens system, and the Map in detail.

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
  vibedentify/               ← the application package
    __init__.py              ← create_app() app factory
    __main__.py              ← `python -m vibedentify` dev-server entry point
    config.py                ← env-derived configuration & constants
    db.py                    ← SQLite: analysis cache, embeddings, vibes, tags
    analysis.py              ← Essentia model plumbing + the analysis pipeline
    routes.py                ← all HTTP routes (one Flask Blueprint)
    templates/index.html     ← HTML shell (rendered via render_template)
    static/app.js            ← frontend: lens system, row rendering, 3D map
    static/app.css           ← all styling (Winamp skin layered at the end)
    static/genre_families.json
  wsgi.py                    ← WSGI entry point for production servers
  tests/                     ← pytest smoke tests (run in FAKE mode)
  training/                  ← custom-genre head pipeline (see "Training custom genres")
    embed_extract.py         ← cache EffNet embeddings from ~/genre_training/<genre>/
    train_head.py            ← train the NumPy MLP → ~/essentia_models/custom_head.npz
  requirements.txt · requirements-dev.txt · pyproject.toml
  .env.example               ← documented environment variables
  CHANGELOG.md · README.md · LICENSE
```

> `create_app()` uses `Flask(__name__)`, so Flask serves `index.html` from
> `vibedentify/templates/` and CSS/JS from `vibedentify/static/`. Keep those two
> folders **inside the package** — moving them out 500s `render_template`
> (`TemplateNotFound`) and 404s `/static/*`.

External, separate from this repo:
- `~/genre_training/<genre>/` — where manual overrides copy tracks for training
  (consumed by the scripts in `training/`).
- `~/essentia_models/` — downloaded model graphs (+ optional `custom_head.npz`).
- `~/genre_v2.db` — SQLite cache/vibes/tags (override with env `GENRE_DB`).

---

## Run

```bash
pip install -r requirements.txt         # first time only
python -m vibedentify                    # real Essentia analysis
FAKE_ANALYZER=1 python -m vibedentify    # instant fake results, no models (UI dev)
```

Then open <http://localhost:5005> in your Windows browser.

Run the tests and linter (fast, no models needed):

```bash
pip install -r requirements-dev.txt
pytest            # smoke tests (FAKE mode + throwaway DB)
ruff check .      # lint
ruff format .     # auto-format
```

CI (GitHub Actions, `.github/workflows/ci.yml`) runs `ruff` + `pytest` on every
push and pull request.

For production, serve the WSGI app instead of the dev server:
`gunicorn wsgi:app` (or `waitress-serve wsgi:app`).

The server binds `127.0.0.1` (localhost) by default; set `GENRE_HOST=0.0.0.0` to
deliberately opt into LAN/Tailscale access. The HTTP API assumes a trusted caller
(routes read/scan/copy server-side paths), so only expose it beyond localhost
intentionally.

**Process model — single process, single user, home-anchored.** These are
deliberate design choices for a personal, local tool; they are stated here so a
future deployment change knows what it would be renegotiating. The app runs as a
**single process**: concurrency is controlled by module-level locks (`_lock` and
`_engine_lock` in `analysis.py`, `_db_lock` in `db.py`), which are process-local,
so running under a **multi-worker** `gunicorn`/`waitress` would race the shared,
non-thread-safe TF models and the SQLite handle — run a single worker, or move
model/DB ownership out of process, before scaling out. It assumes a **single
user**: there is no authentication or per-user isolation (the localhost
trust-model note above covers network exposure). And it stores everything under
**`Path.home()`** — the SQLite DB (`~/genre_v2.db`), the model cache
(`~/essentia_models`), and training copies (`~/genre_training/`) — so state is
anchored to one machine account rather than a shared or multi-tenant location.

A `.env` file in the project root is **loaded automatically at startup** (no
dependency; real environment variables always take precedence). Copy
[`.env.example`](./.env.example) to `.env` to set the values below without
exporting them. `.env` is git-ignored; `.env.example` is tracked.

Environment variables (all optional; see [`.env.example`](./.env.example)):
`MODEL_DIR` (model download dir), `GENRE_DB` (SQLite path), `CUSTOM_HEAD` (path
to `custom_head.npz`), `MAEST_MODEL` (MAEST graph filename), `GENRE_HOST` (bind
address, default `127.0.0.1`), `MAX_UPLOAD_MB` (upload cap, default 512),
`FAKE_ANALYZER=1` (mock mode), and — for the 🔎 per-row metadata lookup —
`DISCOGS_TOKEN` ([Discogs token](https://www.discogs.com/settings/developers))
and `LASTFM_KEY` ([Last.fm key](https://www.last.fm/api/account/create));
MusicBrainz needs no key.

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

## Training custom genres

You can train a small **custom head** on genres you define yourself (e.g. your
own *Riddim* / *Tearout*). It closes the loop on the override feature — labelling
tracks in the app produces the training set:

1. **Label in the app.** Use **✎ override** on tracks; each override copies the
   audio into `~/genre_training/<genre>/`, so those folders accumulate a labelled
   dataset as you go.
2. **Extract embeddings.** `python training/embed_extract.py` walks the
   `<genre>/` folders, runs each track through the same EffNet embedder the app
   uses, and caches frame embeddings + a `manifest.json` (re-runs only embed new
   files). Needs the runtime deps (`essentia-tensorflow`).
3. **Train the head.** `python training/train_head.py` trains a small NumPy MLP
   (1280 → hidden → K softmax) with a *track-level* train/val split and saves
   `~/essentia_models/custom_head.npz` in the exact schema the app loads
   (`vibedentify.analysis.get_custom_head`). NumPy-only.
4. **Restart the app.** The **custom** row then appears under each analysis,
   scoring the same embeddings against your genres — no extra audio decode.

Both scripts default to `~/genre_training` and take an optional path argument.
Follow the guidance they print: aim for **~30+ tracks per genre**, and include an
`other/` folder of tracks that are *none* of your custom genres — that negative
class keeps the head from labelling everything as one of your genres.

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
- `family` — roll each frame up to a broad PulseRoots family (House / Trance / …)
  and take the winner. Coarser than sibling-merge; taxonomy lives in
  `static/genre_families.json`.
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
  similarity to the vibe's centroid. Membership is **Rocchio-weighted** (−1..+1
  per track, driven by the per-song 👍/👎 and the slider editor).
- **Genre map:** a **Map** tab renders the whole library as a rotating 3-D
  point-cloud (Canvas, no libraries). Two layouts — *regions* (genre-family
  clusters) and *galaxy* (pure embedding similarity) — with depth-driven size /
  brightness, search-to-fly, and a popup showing similar artists/tracks plus a
  🎲 random close match. Positions come from a PCA of the embeddings (`/map`),
  nearest-neighbour lines and the popup from cosine similarity (`/similar`).
  Zoom in and the family labels give way to per-subgenre sub-clusters (semantic
  zoom / level-of-detail).
- **Misread detection:** a low-confidence read whose closest sonic neighbours
  strongly disagree is flagged, with the neighbour-consensus genre suggested
  (never auto-applied). Surfaced as an amber ring + popup note on the map, a
  **⚠ review reads** audit panel (`#review`), and a clickable hint on list rows.
  `GET /audit` scans the whole library (`vibedentify/insight.py`).
- **Audio preview:** play/scrub any analyzed track through its waveform (one
  shared player; dropped files via blob URL, cached/batch tracks via `/audio`).
- **Compare engines:** on-demand A/B of the EffNet CNN against the MAEST
  transformer with a live blend slider (needs the optional MAEST model).
- **Family roll-up:** every track's headline genre is tagged with its broad
  PulseRoots family (◇); also selectable as the `family` segmentation lens.
- **Tags:** manual toggleable designations ("high energy", "opener") per track.
- **Manual override / omit:** relabel a track yourself (it's copied to
  `~/genre_training/<genre>/` to feed the custom-head training pipeline), or
  **omit** a bad read entirely — `POST /forget/<hash>` deletes it from the cache
  and map (the audio file is untouched). Both are on each list row and in the
  map popup.
- **Batch folder:** point at a WSL folder path; results stream as NDJSON with 3
  parallel workers, cache-aware.
- **Export:** dump the current list (genres, BPM, key, duration) to `.txt`.

---

## HTTP routes

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | UI shell |
| `/analyze` | POST | single file upload → full analysis JSON (cache-first) |
| `/map` | GET | every cached track (+ PCA coords) + nearest-neighbour edges, for the 3-D map |
| `/similar/<h>` | GET | tracks nearest to `<h>` by embedding cosine (map popup) |
| `/audit` | GET | likely-misread reads (low confidence + neighbours disagree) + suggestions |
| `/forget/<h>` | POST | delete a track's analysis (cache + map + vibe/tag membership) |
| `/refine` | POST | single file → dense segment stream (~0.5 s hop) |
| `/compare` | POST | file **or** `{filepath}` → EffNet vs MAEST per-style scores for live re-mixing (on-demand; never cached) |
| `/batch` | POST | `{path, workers}` → NDJSON stream of results |
| `/save_training` | POST | copy a track to `~/genre_training/<genre>/` |
| `/audio/<h>` | GET | stream a DB-recorded track by content hash (HTTP Range) for the preview player |
| `/tags`, `/tags/toggle`, `/tags/for/<h>` | GET/POST | manual tags |
| `/vibes`, `/vibes/add`, `/vibes/weight`, `/vibes/remove`, `/vibes/<id>/members`, `/vibes/match/<h>`, `/vibes/<id>/playlist` | GET/POST | similarity clusters (Rocchio-weighted membership) |

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
