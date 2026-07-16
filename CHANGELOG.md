# Changelog — Genre v2

All notable changes to this project are recorded here.

Format follows [Keep a Changelog](https://keepachangelog.com/); versioning is
loosely [SemVer](https://semver.org/): `MAJOR.MINOR.PATCH`.

- **MAJOR** — a reanalysis-invalidating change (model, embedding, or cache-key
  change; anything that makes old cached results incomparable to new ones).
- **MINOR** — a new feature or route (a lens, a workflow, a UI panel) that
  leaves existing analysis valid.
- **PATCH** — a fix or refinement with no schema or feature surface change.

Keep the newest version on top. Under each version, group lines under
**Added / Changed / Fixed / Removed**. Date format is `YYYY-MM-DD`.

---

## [Unreleased]

_Work in progress lands here, then gets stamped with a version + date on release._

### Added
- **Map: fuller camera navigation.** The 3-D view now supports **pan** in
  addition to orbit + zoom — **right-drag / middle-drag / Shift + left-drag** to
  slide across the scene, or **Shift + arrow keys**. A **⏸ / ▶ button** next to
  the orbit-speed slider pauses/resumes the auto-spin (icon reflects the state,
  and stays in sync when you drag the slider to/from 0); **Space** toggles it
  too. Keyboard scheme: **W/S** zoom · **A/D** orbit · **arrow keys** pan · `+`/`-`
  also zoom · Space play/pause · `f` fit · `esc` close (mouse: left-drag orbit,
  right/middle/Shift-drag pan, scroll zoom). A collapsible **nav legend** in the
  map's top-left corner lists the controls (state remembered).
- **Genre Map (3-D constellation view).** New **Map** tab (header `List | Map`)
  renders the entire scanned library as a rotating 3-D point-cloud on black,
  inspired by pulse.roots. Two new read-only routes: `GET /map` returns every
  track (title, parsed artist, dominant style, bpm/key, and an 8-component PCA of
  its embedding) plus each track's top-2 cosine-nearest-neighbour edges;
  `GET /similar/<hash>?k=` returns the nearest tracks by embedding cosine.
  Rendered on a **Canvas 2-D with a hand-rolled perspective projection** (no
  libraries): the cloud auto-spins, drag to orbit, wheel to zoom; depth drives
  node size + brightness. Two layouts, toggleable in-view: **regions** (genre
  families as 3-D clusters — biggest family at the core, the rest on a Fibonacci
  sphere — with members offset by their 3 highest within-family-variance PCA
  axes) and **galaxy** (position = first 3 PCA axes, pure sonic similarity).
  Search or click a point → the camera flies to that track and its neighbour web
  lights up; the popup shows family, metadata, "also reads as", **similar
  artists**, **similar tracks**, and a **🎲 "a match for you"** block that pulls
  up a random one of the track's closest matches (with a re-roll). Colour-coded
  legend, `#map` / `#map=<hash>` / `#galaxy` deep links. No new dependencies.
- **Misread detection.** New `GET /audit` scans the library for likely-misread
  genres: a low-confidence read whose closest embedding-neighbours (cosine ≥ 0.80)
  strongly agree on a *different* family is flagged, and the neighbour-consensus
  genre is suggested (never auto-applied). Surfaced three ways — an amber ring +
  popup note on the **Map**, a **⚠ review reads** panel listing every flagged
  track (with map/omit actions and a `#review` deep link), and a clickable
  **⚠ sounds like &lt;X&gt;** hint on list rows that prefills the override.
  `/analyze` also attaches the flag to each new read (`vibedentify/insight.py`;
  thresholds conf < 0.55, agree ≥ 0.60, sim ≥ 0.80, k=8 → ~5% flag rate).
- **Omit / forget a track.** New `POST /forget/<hash>` deletes a track's analysis
  (cache + map + vibe/tag membership; the audio file is untouched). Exposed as
  **✕ omit** on each list row and **omit from library** in the map popup.
- `CHANGELOG.md` (this file), `README.md`, and a detailed
  [`docs/USAGE.md`](docs/USAGE.md) user guide.
- Packaging & metadata: `requirements.txt`, `requirements-dev.txt`,
  `pyproject.toml`, `.env.example`, and an MIT `LICENSE`.
- A `pytest` smoke-test suite (`tests/`) exercising the HTTP surface in FAKE
  mode against a throwaway DB.

### Changed
- **Split `static/app.js` (2.7k lines) into three `<script>`-loaded files.** The
  3-D Genre Map moved to `static/map.js` and the row audio player to
  `static/player.js`; `static/app.js` keeps the rest (rows, lenses, vibes, tags,
  compare, batch) and the shared helpers. No bundler or module conversion — plain
  ordered `<script>` tags (app.js first, then player.js, then map.js), with the
  load-order requirement documented atop each file. No behaviour change.
- **User guide rewritten for end users.** `docs/USAGE.md` (the in-app **Guide**
  tab) is now task- and decision-oriented — it explains *which* analyze method,
  map layout (regions / galaxy / tree), and lens to pick and *why*, and walks
  through every capability from a new user's point of view, rather than covering
  installation and internals (those live in the README). Paragraphs are single-
  line so the in-app markdown renderer fills them correctly (bold no longer
  breaks across a wrapped line).
- **Restructured the backend into a package.** The ~1.2 k-line `genre_gui.py`
  monolith is split into a `vibedentify/` package — `config.py`, `db.py`,
  `analysis.py`, a single-Blueprint `routes.py`, and a `create_app()` factory
  (`__init__.py`). New entry points: `python -m vibedentify` (dev server) and
  `wsgi:app` (production). `templates/` and `static/` moved inside the package.
  No route behaviour changed; the smoke suite verifies parity.
- **Map: subgenre semantic zoom.** The regions layout now sub-clusters each
  family by dominant style (Dubstep and Drum n Bass separate visibly inside Bass
  Music); family labels fade back and cyan subgenre labels fade in as you zoom
  (level-of-detail). The subgenre-label threshold scales with library size, and
  the genre labels are brighter/more legible.
- **Override now persists from the List too.** The List-view **✎ override** used
  to only file a training copy and relabel the on-screen chip — re-dropping the
  track showed the model's original read again. It now goes through the same
  `POST /override/<hash>` as the Map popup, writing `payload["override"]` into
  the cached analysis so the manual genre sticks everywhere (List, Map, exports,
  audit) and survives a reload. Dropped files (no server-side path) still upload
  the file so the `~/genre_training/<genre>/` copy is saved.
- **Map: label readability pass.** Family labels no longer pile on top of each
  other — they relax apart in 2-D (box separation) and connect back to their
  cluster with a colour-coded leader line. In **regions** the families sit
  further apart (wider Fibonacci sphere) with more room for subgenres, and the
  view auto-fits to the cloud on entry / reset. In **galaxy** the points stay in
  their natural similarity positions (untouched); since a tight ball makes each
  centroid's *direction* meaningless, the labels instead ring the cluster evenly
  by angle, each tied back by a leader line. Subgenre labels get more space and
  fade in sooner.
- **List: skip duplicate + already-analyzed drops.** Re-dropping a file already
  in the list is a no-op (dedup by name+size), and a track already in the DB
  (cache-hit) is skipped from the list entirely instead of adding a redundant
  row. A brief "skipped N …" note shows in the footer.

### Fixed
- **`/map` no longer builds the full pairwise cosine matrix.** It only keeps the
  top-2 neighbours per node, so the similarity search now runs in row-blocks and
  pulls each row's top-K (via `argpartition`) instead of allocating the whole
  N×N float32 matrix (~400 MB at 10k tracks) and argsorting it on every map load;
  `insight.audit()` got the same treatment. Output is byte-identical to before.
- Stop leaking raw exception strings to HTTP clients — log the real error and
  return a generic message.
- Removed a stray `<style>` tag from the top of `static/app.css`.
- Added a `MAX_CONTENT_LENGTH` upload cap; replaced `print()` diagnostics with
  the `logging` module.
- **Audio preview player.** New read-only `GET /audio/<hash>` route streams a
  previously-analyzed track by content hash with HTTP Range support (serves only
  files recorded in the DB — not an arbitrary-file endpoint). Per-row UI: a
  ▶ play/pause button, click-the-waveform-to-seek, a gold playhead, and a
  `mm:ss / mm:ss` readout. One shared `<audio>` (one track at a time); dropped
  files play client-side via blob URL, batch/cached tracks stream from `/audio`.
- **MAEST ensemble (compare engines).** New on-demand `POST /compare` route runs
  the MAEST transformer (`discogs-maest-30s-pw`, outputs Discogs-400 directly at
  `StatefulPartitionedCall:0`, input node `serving_default_melspectrogram`)
  alongside the EffNet CNN and returns both reads plus their element-wise merge
  (valid because both share the exact 400-label order). Per-row **⚖ compare
  engines** button shows EffNet / MAEST / Merged side by side, with a **live
  EffNet↔MAEST weight slider** and an agree/disagree note. `/compare` returns each
  engine's per-style scores once (union of both top-15); the slider re-mixes the
  merge instantly client-side — MAEST does **not** re-run on drag. MAEST is ~10×
  slower than EffNet on CPU (~15 s/track), so it runs only on the initial click
  and never touches the analysis cache. Requires the ~334 MB MAEST model
  (`discogs-maest-30s-pw-1.pb`) in `MODEL_DIR`; absent → feature is a no-op.
- **PulseRoots family roll-up.** New `family` segmentation lens and an always-on
  family tag (◇) on each track's headline genre. Maps the Discogs-400 styles to
  14 broad electronic families via `static/genre_families.json`, derived from the
  [PulseRoots](https://mendiak.github.io/pulse.roots/) hierarchy (MIT-licensed).
  Resolution chains PulseRoots → the editable sibling groups → the style itself
  (68% of electronic styles map directly; the sibling fallback covers most of the
  rest). No effect on recognition — it's a taxonomy/organization layer.

### Changed
- **Reworked the layout to a single full-window drop area.** Replaced the
  two-column split (left dropzone / right list) with one big drop-anywhere window
  that holds the analyzed list, plus a bottom action bar. Browse / batch folder /
  vibes moved to the bar's left; count / clear / export to its right; the header
  slimmed to logo + lens controls. Added a full-window "⊕ release to add" drag
  overlay with flicker-free dragleave (ignores child-element boundaries), sticky
  column headers, and a larger centered empty-state hero (clickable to browse).
  Also removed click-to-browse on the whole area (would fire on rows/buttons) and
  fixed a stray duplicate `<body>` tag.
- Reskinned the Winamp theme from near-black to a **silver metallic** chassis for
  readability (base was too dark): `--bg` `#17171C`→`#3E3E48`, `--panel`
  `#26262E`→`#55555F`, `--panel-2`→`#6A6A74`, near-white etched text
  (`--text`→`#F2F3F7`), lifted secondary text (`--dim` `#6B7690`→`#CDD2DE`, AA on
  the lighter panels), silver header/panel ridge stripes, brighter export button,
  and darker LCD-green readouts bumped for contrast (`#0A8F0A`→`#22C022`). The
  dark LCD wells (BPM/key/waveform) are kept dark on purpose — inset displays in a
  silver body, the authentic classic-Winamp look. Gold + green identity preserved.

### Fixed
- **Model-build race on the first `/batch` run.** The earlier fix locked the
  *inference* pass but not the lazy *build*: `get_engine()` (and `get_maest()`,
  `get_custom_head()`, `get_fine_embedder()`) were unguarded, so the first few
  batch workers could hit an unbuilt engine at once — racing on the model
  download (two `urlretrieve`s to the same path can truncate a `.pb`) and
  constructing duplicate TF instances. All four now build under a dedicated
  `_engine_lock` (double-checked, with atomic publish), so concurrent callers
  wait for the single build instead of racing it.
- **`/compare` froze batch workers for the whole decode.** `/compare` held the
  inference lock around the entire `compare_engines()` call, including the
  `MonoLoader` decode — so an on-demand compare during a batch blocked all
  workers for the full ~15 s. Decode and the one-time MAEST build now happen
  outside the lock (matching `analyze()`); only the two inference passes are
  serialized.
- **`/save_training` always reported `"new": false`** for server-side (batch)
  saves — it evaluated `dest.exists()` *after* the copy created the file. Now
  captured before the copy.
- **Sibling-editor removal handler stacked listeners.** The delegated chip-remove
  click handler was re-bound (with `{once:true}`) on every `renderSibEditor()`
  and every panel open, accumulating stale handlers. Bound once at panel setup;
  it survives re-renders because it lives on the container, not its contents.
- Guarded `finishRow` against an empty `styles` array (`styles[0]` → a
  placeholder) so a style-less analysis can't throw and break the row.
- **`/batch` race on the shared TF models.** `/batch` drove the shared, non-thread-safe
  `embedder`/`classifier` instances from a 3-worker pool with no lock, while Essentia
  releases the GIL during compute — so concurrent workers overlapped in the model,
  intermittently corrupting predictions or crashing (timing-dependent, so it only
  surfaced on some batch runs). Moved the lock *inside* `analyze()`/`refine_segments()`,
  held only around the embedder+classifier inference pass; decode, BPM, and key stay
  parallel across workers, and the redundant route-level locks on `/analyze`/`/refine`
  were removed (`analyze()` now self-locks).
- **Bind to localhost by default.** The server now binds `127.0.0.1` unless
  `GENRE_HOST=0.0.0.0` is set, so the trust-a-caller HTTP API (`/batch`,
  `/save_training`, `/audio`) isn't exposed to the LAN by accident.
- Per-row **+ tag** and **+ vibe** buttons did nothing: they used JavaScript
  `prompt()` dialogs, which browsers suppress after a page fires several (so the
  handler ran, the prompt returned null, and it silently bailed). Replaced both
  with inline inputs/menus — no dialogs. Also fixed a latent bug where refreshing
  vibe matches stacked duplicate holders.
- Restored the `templates/` + `static/` layout the code requires: moved
  `index.html` → `templates/`, `app.css` and `app.js` → `static/`. They had been
  flattened into the project root, which made the app un-runnable —
  `render_template("index.html")` 500'd with `TemplateNotFound` and `/static/*`
  404'd. Boot + serving now verified (GET `/`, `/static/app.js`, `/static/app.css`
  all 200 in both fake and real modes).

---

## [2.0.0] — 2026-07-02

Baseline snapshot of the fully-functional, verified state at the time versioning
began. Everything below already existed; it is recorded here as the starting
point, not as new work.

### Core analysis
- Local genre analysis via Essentia Discogs-EffNet embedder → Discogs-400
  classifier head (400 styles), CPU-only (`essentia-tensorflow`).
- Per-track pipeline: 16 kHz decode → 1280-dim embeddings → 400-style scores ×
  N frames → `frame_topk` (top-6/frame) → `salience_read` (energy × confidence ×
  recurrence). Separate 44.1 kHz decode → `RhythmExtractor2013` (BPM) +
  `KeyExtractor` (key/scale → Camelot) + 240-point waveform envelope.
- Optional custom head (`custom_head.npz`, trained externally) scores the same
  embeddings — no extra audio work.

### Lens system (client-side)
- Identity lens: `v2` (salience-weighted) / `v1` (flat % of track).
- Segmentation lens: `raw` / `hysteresis` / `sibling-merge` / `hyst+sib`.
- Global defaults with independent per-row overrides on either axis.
- Editable sibling groups (near-synonym clusters: House / Trance / Techno / …).
- Temporal smoothing + "Other"-bucket collapse shared by waveform and breakdown.

### UI
- Fisheye (Dock-style) waveform colored by genre segment, hover-to-magnify.
- Winamp-homage skin (beveled panels, LCD readouts, Silkscreen font).
- Fine-detail on-demand re-analysis (`/refine`, ~0.5 s resolution).
- Manual genre override (`✎ override`) that also saves a copy to
  `~/genre_training/<genre>/` for the custom-head training pipeline.
- Per-track color recoloring, details/tags panel, export to `.txt`.

### Persistence & workflows
- SQLite at `~/genre_v2.db` (env `GENRE_DB`): analysis cache keyed by content
  hash (re-drops return instantly, tagged "cached"), plus vibes and tags.
- Vibes: user-defined similarity clusters over the 1280-dim mean embeddings;
  auto match %, whole-DB cosine-similarity playlists (`/vibes` routes).
- Tags: manual toggleable chips per track (`/tags` routes).
- Batch folder mode (`/batch`): NDJSON stream, 3 parallel workers, cache-aware.

### Guarantees
- Never modifies the user's music files — analysis is read-only; training saves
  are copies. Fully local; no internet after the one-time model download.
