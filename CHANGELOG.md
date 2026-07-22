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
- **Auto-loaded `.env`.** A project-root `.env` is now read at startup (dependency
  -free, in `config.py`) so the dev server picks up config/secrets without a manual
  `source`. Real environment variables always win; parsing is tolerant (blank
  lines, `#` comments, optional `export`, quoted values); a missing file is a
  silent no-op. Skipped under pytest. Discogs lookup accepts either a personal
  access token (`DISCOGS_TOKEN`) or a consumer key + secret (`DISCOGS_KEY` /
  `DISCOGS_SECRET`).
- **External metadata lookup (🔎 per row).** A lookup button on each analyzed row
  queries three public music APIs by artist/title (parsed from the tags / title /
  filename, remix descriptor included): **Discogs** (`DISCOGS_TOKEN`) release
  styles, **MusicBrainz** (no key; recording genres/tags, 1 req/s + descriptive
  User-Agent per their etiquette), and **Last.fm** (`LASTFM_KEY`) track top tags.
  `GET /lookup/<hash>` queries only sources whose keys are configured, degrades
  per-source (5s timeouts, failures reported not fatal), and caches every hit
  **permanently** in a new `lookup_cache` table so a repeat click never re-queries.
  Suggestions render source-attributed under the row, each with **tag** (apply as a
  track tag) and **train** (record as a training label + copy the audio). API
  endpoints only — no HTML scraping. Parsers live in `vibedentify/lookup.py`.
- **Segment-level overrides (shift-drag the waveform).** Shift-drag a time range
  on any track's waveform to label just that section a genre. A floating "override
  section as [genre]" menu appears; confirming (1) records the span in a new
  `segment_overrides` table, (2) extracts exactly that range from the source audio
  with ffmpeg — stream-copy where the container allows, re-encode fallback — into
  `~/genre_training/<genre>/<hash>_<start>-<end><ext>` (so partial-track labels
  feed the custom-head trainer), and (3) repaints that span on the waveform in the
  manual-override style with the genre in the hover tooltip, persisted across cache
  hits. New route `POST /override_segment {hash, start, end, genre}` validates
  `0 ≤ start < end ≤ duration`; browser-dropped tracks (no saved path) get a clear
  "needs a server-side file" message instead of a silent re-upload. The modifier
  (shift) keeps drag-select from fighting the hover magnifier / click-to-play.
  **Shift-click** an existing override span to **remove** it behind a confirmation
  wall (`POST /override_segment/delete {id}`) — deleting both the record and its
  extracted training clip so an undo doesn't leave the clip behind.
- **Labeling accelerator (`◎ label` panel).** A queue that turns building a
  custom-genre training set from hunt-and-peck into confirm/reject. Pick or type a
  genre and it ranks every unlabeled cached track by embedding similarity to that
  genre's centroid (mean embedding of tracks already labelled it — via `✎ override`
  or prior confirms), with an inline audio preview per candidate and a running
  confirmed/rejected count. Three routes: `GET /training/candidates/<genre>?limit=`
  (ranked hash/title/sim/bpm/camelot; a clear message when no examples exist yet),
  `POST /training/confirm` (records the label in the new `training_labels` table and
  copies the audio into `~/genre_training/<genre>/`, feeding the `training/`
  pipeline), and `POST /training/reject` (records in `training_rejects` so a track
  never resurfaces for that genre). Confirms/rejects both drop the track from future
  queues; a confirm also clears any prior reject.
- **Tests: `_second_style` runner-up helper.** Direct unit tests for the map's
  colour-blend helper (`vibedentify/routes.py`) — override short-circuit, exact
  weight `sc / (top_score + sc)` with its 0.5 ceiling, no-distinct-runner-up,
  salience→styles fallback, and the zero/None `top_score` (no division-by-zero).
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
- **Map: click a genre/subgenre to fly to & orbit its cluster; hierarchical
  legend; recolour genres.** Clicking a **genre** (its map label, or its name in
  the legend) flies the camera to that cluster and **orbits around the cluster's
  centre** (a pivot) instead of the origin, so it stays framed while the rest
  rotates. Clicking a **subgenre** (its map label, or its entry in the legend)
  does the same for that sub-cluster (centroid computed on demand, so it works in
  any layout). The genre legend is a **collapsible, scrollable panel** that groups
  each overarching genre with its subgenres beneath it (indented, in their shade
  colours) so the hierarchy is clear. Clicking a legend entry's **colour dot**
  opens a picker to recolour that whole genre (and its subgenre shades), persisted
  in `localStorage`. (Filtering to a single genre stays on the top-bar dropdown.)
- **Omit / forget a track.** New `POST /forget/<hash>` deletes a track's analysis
  (cache + map + vibe/tag membership; the audio file is untouched). Exposed as
  **✕ omit** on each list row and **omit from library** in the map popup.
- `CHANGELOG.md` (this file), `README.md`, and a detailed
  [`docs/USAGE.md`](docs/USAGE.md) user guide.
- Packaging & metadata: `requirements.txt`, `requirements-dev.txt`,
  `pyproject.toml`, `.env.example`, and an MIT `LICENSE`.
- A `pytest` smoke-test suite (`tests/`) exercising the HTTP surface in FAKE
  mode against a throwaway DB.
- **CI: bandit (Python SAST).** Runs `bandit -r vibedentify/` on every push; the
  handful of in-context false positives (fake-mode RNG seeds, the hardcoded-HTTPS
  model download, best-effort `try/except` around BPM/key) are triaged with
  inline `# nosec <id>` justifications, so any *new* finding fails the build.
- **CI: pip-audit (dependency CVEs).** Audits the pinned runtime deps against the
  advisory DB on every push. `essentia-tensorflow` is excluded (its pre-release
  wheel is unauditable and has no CVE history — documented in the workflow), the
  same blind spot as its CI-install omission.
- **CI: ESLint (frontend).** Flat-config `eslint.config.js` lints
  `vibedentify/static/*.js` for `no-undef` / `no-unused-vars` — catching the
  split's failure mode, a cross-file `ReferenceError` between app.js / player.js /
  map.js. The shared cross-file globals are declared in the config; the first run
  earned its keep by flagging dead helpers (`roundRect`, `disp`, `total`,
  `identityMode`), now removed, and a stale comment ESLint read as a `/* global */`
  directive.
- **CI: coverage floor (`pytest-cov`).** The pytest step now enforces
  `--cov=vibedentify --cov-fail-under=50`. Added FAKE-mode tests for the
  previously-untested routes to clear it — `/batch` (NDJSON stream + cache
  re-hit), `/compare`, `/map` with a populated DB (edges only reference real
  nodes), the vibe lifecycle (`/vibes/add` → `/weight` → `/members` → `/remove`),
  and `/vibes/match` + `/vibes/<id>/playlist` — taking coverage 42% → 55%.
- **Custom-genre training pipeline (`training/`) documented + smoke-tested.**
  `training/embed_extract.py` + `training/train_head.py` — which consume the
  `~/genre_training/<genre>/` folders the **✎ override** feature fills — are now
  first-class: a **Training custom genres** section in the README walks the loop
  (override → `embed_extract` → `train_head` → restart → the **custom** row) with
  the guidance the scripts print (~30+ tracks/genre, an `other/` negative class),
  `training/` is in the project-structure block, and the guide's override section
  now says what the saved audio is for. A pytest smoke test trains the head on
  tiny synthetic clusters (asserts val acc > 0.9) and round-trips it through the
  exact `get_custom_head` load pattern (six arrays + labels, `allow_pickle=False`).
  `.gitignore` gains `_cache/`, `manifest.json`, `*.npz` as insurance.

### Changed
- **Desktop shell: theme, quality gates, and honest config docs.** The `desktop/`
  launcher's inline splash + error pages are reskinned from the old generic dark
  look to the app's current **Neon-DJ** palette (near-black chassis, cyan LCD-well
  loading dots + code, gradient wordmark), derived from the active `:root` in
  `static/app.css` (a provenance comment in each page records which variables).
  `.pyw` is now in the ruff `include` list so `ruff check` / `ruff format` cover
  the shell (previously skipped as "no Python files"), and a `python
  desktop/genre_app.pyw --selftest` step runs in CI (pure-stdlib, no extra deps).
  `desktop/README.md` gains an **On a different machine** section spelling out that
  `GENRE_WSL_PYTHON` / `GENRE_WIN_PROJECT` are one machine's exact paths and must
  be set, and the boot-timeout error page now leads with a clear "project folder
  not found: `<path>`" message (naming the var to fix) when `WIN_PROJECT` is wrong.
- **Map: subgenre colours + spatial, per-cluster label detail.** Each node is now
  a distinct *shade* of its family's colour keyed to its subgenre (family hue
  ±36° plus a wide saturation/lightness wobble), and it **leans toward its 2nd
  genre's colour** by that genre's relative weight (`GET /map` now returns a
  `mix` field), so a big single-genre cluster (e.g. Bass Music) visibly shows its
  internal groupings even from afar, and tracks between two genres get a blended,
  matching colour. Label level-of-detail is now **per-family and
  spatial**: as you zoom toward a cluster, *its* family label fades out and its
  subgenre labels fade in (as bright shades of the family), while clusters off to
  the side keep their family label and stay coarse — so being deep in one cluster
  reveals its subgenres without lighting up the whole map, and the other clusters
  stay identifiable. Subgenres also surface at a lower zoom than before, and the
  tree view's subgenre nodes get the same family-shade colours. In **galaxy**, the
  ring of family labels now sizes itself to the ball's on-screen radius, so the
  labels clear the cloud instead of overlapping it when zoomed out.
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
  breaks across a wrapped line). Includes an at-a-glance ASCII diagram
  contrasting the three map layouts (regions / galaxy / tree).
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

### Added
- **DAW-style waveforms (min/max + RMS).** Tracks now render like audio software:
  a translucent peak outline (min→max from a center line, so transients spike) with
  a solid RMS "loudness" core, keeping the genre coloring. `GET /waveform/<hash>`
  serves a high-res (1600-bin) min/max/rms envelope, cached permanently in a new
  `waveform_cache` table — pre-filled at analysis time (free; the audio is already
  decoded) for every new track, or decoded once on demand from the source file for
  older tracks that have one. The row renders its stored envelope instantly, then
  upgrades to the detailed waveform when it arrives; a track with no server audio
  keeps the (interpolated) envelope. No re-analysis required to benefit.
  **Batch re-scan now backfills a server-side path** for older drop-analyzed rows
  that stored none (cache hit, no re-analysis), so pointing *batch folder* at your
  music lights up the DAW waveform — plus audio preview and section overrides — for
  a whole library that was originally drag-dropped.

### Fixed
- **Desktop shell `win_to_wsl` is now portable (selftest runs on Linux/CI).** It
  called `os.path.abspath`, which on POSIX treats `C:\...` as relative and prepends
  the CWD, so `--selftest` failed everywhere but Windows and could never run in CI.
  Switched to `ntpath.abspath` (identical on Windows, correct elsewhere); the two
  environment-dependent "derived path" checks were rewritten to assert the
  derivation *rule* instead of a host-specific value, so the full selftest passes
  on the Linux runner.
- **Sharper waveforms.** The amplitude envelope was drawn with nearest-neighbour
  sampling over 240 bins, so it looked blocky/pixelated on wide or hi-DPI canvases.
  It now **interpolates between samples** (smooth for every track, including
  already-cached ones), and the analyzer captures **720 bins** instead of 240 for
  genuinely finer detail on newly-analyzed tracks (re-analyze to upgrade an old
  one; the higher count doesn't invalidate any cached genre analysis).
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
- **Weighted vibe membership (Rocchio relevance feedback).** A vibe's centroid is
  now a *preference-weighted* mean instead of a plain mean: each member track
  carries a −1..+1 weight, set by a per-track slider and by per-song 👍/👎 on the
  vibe matches. Positive weights pull the centroid toward a track and negative
  weights push it away, so you can steer a vibe by example; with every weight = 1
  it reduces exactly to the old plain-mean behaviour. Adds `POST /vibes/weight`,
  `POST /vibes/remove`, and `GET /vibes/<id>/members`, and migrates existing DBs
  by adding a `weight REAL DEFAULT 1.0` column to `vibe_tracks`.

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
