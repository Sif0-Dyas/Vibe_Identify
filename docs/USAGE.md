# Vibedentify — User Guide

A hands-on guide to analyzing and exploring your music library with Vibedentify
(codename *Genre v2*). For architecture and routes, see the
[README](../README.md); for the version history, the
[CHANGELOG](../CHANGELOG.md).

---

## Contents

1. [Launching the app](#1-launching-the-app)
2. [The two views](#2-the-two-views)
3. [How analysis works (the pipeline)](#3-how-analysis-works-the-pipeline)
4. [The four ways to analyze](#4-the-four-ways-to-analyze)
5. [Reading a result (List view)](#5-reading-a-result-list-view)
6. [The lens system](#6-the-lens-system)
7. [Managing tracks](#7-managing-tracks)
8. [Finding bad reads (Review reads)](#8-finding-bad-reads-review-reads)
9. [The Map](#9-the-map)
10. [Good to know](#10-good-to-know)

---

## 1. Launching the app

From the project directory:

```bash
python -m vibedentify          # real analysis (loads Essentia models)
```

Then open **http://localhost:5005** in your browser. The first analysis of a
session loads the models (a few seconds) and, on a fresh machine, downloads them
once to `~/essentia_models/`. Everything after that is fully local — your audio
never leaves the machine, and **your files are never modified** (analysis is
read-only; "training" saves are copies).

> Tip: `FAKE_ANALYZER=1 python -m vibedentify` serves instant fake results with
> no models — handy for poking at the UI.

---

## 2. The two views

The header has two tabs:

- **≣ List** — the analyzer. Drop tracks in, read their genre / BPM / key, and
  manage them.
- **✷ Map** — a 3-D constellation of everything you've analyzed, for exploring
  by sound. See [section 9](#9-the-map).

---

## 3. How analysis works (the pipeline)

Every track goes through the same pipeline. Understanding it makes the different
analyze methods (next section) and the lens system (section 6) make sense.

1. **Decode → embeddings.** The audio is decoded to 16 kHz mono and fed through
   Essentia's **Discogs-EffNet** model, producing a **1280-dimensional
   embedding** per short frame. This embedding is the track's "sonic
   fingerprint" — it's what powers the Map, vibes, similarity, and misread
   detection.
2. **Classify → 400 styles.** A Discogs classifier head turns each frame's
   embedding into scores across **400 Discogs styles** (Dubstep, Deep House,
   Tech Trance, …). So a track isn't one label — it's a *stream* of per-frame
   genre guesses.
3. **Top-k per frame.** Each frame is reduced to its top ~6 styles. This stream
   feeds the waveform coloring and the segmentation lens.
4. **Salience read.** To get one overall "what genre *is* this?" answer, frames
   are weighted by **energy × confidence × recurrence** — loud, confident,
   recurring sections count more than a brief ambiguous intro. This is the
   default **v2** identity read.
5. **BPM + key.** The audio is decoded again at 44.1 kHz and run through
   `RhythmExtractor2013` (**BPM**) and `KeyExtractor` (**key**, mapped to the
   **Camelot** wheel for harmonic mixing).
6. **Waveform.** A 240-point amplitude envelope is computed for the drawn
   waveform.
7. **Custom head (optional).** If you've trained a custom head
   (`~/essentia_models/custom_head.npz`), it scores the *same* embeddings for
   your own genres — no extra audio decoding. Without one, the "custom" scores
   are simply absent.

The result is cached in SQLite keyed by a **content hash** of the file, so the
same audio is never analyzed twice (see [section 10](#10-good-to-know)).

---

## 4. The four ways to analyze

There are two ways to get a track *into* the analyzer, and two per-track
"deeper" analyses you can run on demand.

### 4a. Drag & drop / Browse files — the everyday way

Drag any number of audio files anywhere in the List window, or click
**⊕ Browse files…**. Supported: MP3, FLAC, WAV, M4A, OGG, AIFF, and more.

**How it works:** each file is uploaded to `/analyze` and processed **one at a
time, in order** (~1.5 s per track on CPU). Each file appears as a *pending*
row immediately, then fills in when its analysis returns. It's **cache-first**:
if the exact audio was analyzed before (this session or a previous one), the
result comes back instantly.

Notes:
- **Serial** — good for a handful of tracks. For a big library, use *batch
  folder* (below), which is ~3× faster.
- **512 MB per file** cap (configurable via `MAX_UPLOAD_MB`). No limit on the
  *number* of files.
- Duplicate handling: re-dropping a file that's already in the list is skipped,
  and a track already in the database is skipped from the list entirely (you'll
  see a brief "skipped N …" note) — nothing is re-analyzed or duplicated.

### 4b. Batch folder — for whole libraries

Click **⊕ batch folder** and give it a folder *path on the server* (e.g. a WSL
path like `/mnt/c/Users/you/Music`). It scans that folder **recursively** and
analyzes every audio file it finds.

**How it works:** it hits `/batch`, which runs **3 analyses in parallel** and
streams results back as they complete (you'll see a running `done / total`
count). It's **cache-aware** — already-analyzed tracks return instantly, so
re-scanning a folder only spends time on new files. Because the files are on
disk, batch-scanned tracks also get a stored path (used by the audio preview).

Use this for bulk work; use drag & drop for one-offs and quick checks.

### 4c. Refine — fine-detail segmentation (per track)

On any analyzed row, click **fine**. This re-analyzes *that one track* at high
time resolution (~**0.5 s per frame** instead of the ~2 s coarse default),
returning a much denser genre stream.

**When to use it:** when a track shifts genre mid-song (a DnB tune with a
half-time breakdown, an intro that's a different vibe) and the coarse read
smears those transitions together. Refine shows the fine-grained switches. It's
on-demand because it costs ~4× the compute of a normal analysis.

### 4d. Compare engines — a second opinion (per track)

On any analyzed row, click **⚖ compare engines**. This runs a *second* model —
**MAEST**, a transformer — alongside the default EffNet CNN and shows both
reads plus their merge, with a **live blend slider** (EffNet ↔ MAEST).

**How it works:** both models output the identical 400 Discogs styles, so their
predictions can be averaged directly. `/compare` runs each model **once** and
returns both score sets; dragging the slider re-mixes them **instantly**
client-side (the models are the slow part; averaging is free). It's a great tie-
breaker when a genre read looks off — if both engines agree, trust it; if they
disagree, the track is genuinely ambiguous.

Caveats: MAEST is **~10× slower** than EffNet (~15 s/track on CPU), so it runs
only when you click, and its result is **never cached**. It also requires the
optional MAEST model to be installed.

---

## 5. Reading a result (List view)

Each analyzed row has three columns:

- **Track** — the title, with a **genre-colored waveform** underneath. The
  waveform is painted by the per-segment genre stream, so you can *see* where
  the track changes character. Hover it to magnify (fisheye); click to play/seek
  from that point.
- **BPM / Key** — tempo and musical key, shown in **Camelot** notation (e.g.
  `8A`) plus the raw key.
- **Genre** — the breakdown. By default this is the **v2 salience** read: the
  dominant genre(s) with their share, tagged with the broad **PulseRoots
  family** (◇). Below it you may see a **custom** row (if a custom head is
  loaded) and, for uncertain reads, a **⚠ "sounds like …"** hint (see
  [section 8](#8-finding-bad-reads-review-reads)).

An audio **preview player** is built in: play/scrub any analyzed track through
its waveform. Dropped files play from an in-memory copy; batch-scanned tracks
stream from the server. (A browser-dropped track that hasn't been folder-scanned
has no server-side file, so its preview may be unavailable.)

---

## 6. The lens system

The genre "read" isn't a single fixed answer — it's a *view* over the per-frame
stream, and you control the view with two **lenses** in the header. Lens changes
are instant (they re-interpret existing data; nothing is re-analyzed). Set the
global default in the header; any individual row can override it independently
(a `*` marks an overridden row).

### Identity lens — "what genre *is* this track?"

- **v2 · salience** *(default)* — weights sections by energy × confidence ×
  recurrence. The read you get for a track whose loud, recurring drops are
  Dubstep even if the intro is Ambient.
- **v1 · flat %** — plain share of the track by frame count. Simpler; treats a
  quiet intro the same as the main section.

### Segmentation lens — "what plays *when*?" (the waveform stream)

- **raw** — each frame's single top genre. Most detailed, but flickers.
- **hysteresis** *(default)* — holds the current genre until a challenger beats
  it by a margin for several frames. Kills the flicker; shows real sections.
- **sibling-merge** — pools near-synonym styles per frame before picking a
  winner (e.g. Deep House / Tech House / Bassline → House). The groups are
  **editable** via the **siblings ⚙** button.
- **family (PulseRoots)** — rolls each frame up to a broad family (House,
  Trance, Techno, Bass Music, …). Coarsest; taxonomy in
  `static/genre_families.json`.
- **hyst + sibling** — sibling-merge first, then hysteresis over the result.

Minor genres in the stream collapse into a single grey **"Other"** bucket, and
the same rule is applied to both the waveform and the breakdown so the two
always agree.

---

## 7. Managing tracks

Per-row actions (List view), plus library-wide tools in the footer:

- **✎ override** — set the genre yourself (e.g. your own *Riddim* / *Tearout*).
  An override does two things: (1) it **persists** into the track's cached
  analysis, so it becomes the track's dominant genre everywhere — the List, the
  Map, exports, and the misread audit — and **survives a reload**; re-dropping
  the same track shows your override, not the model's original read. (2) the
  track's audio is **copied** to `~/genre_training/<genre>/` to build a labelled
  dataset for training a custom head later (for a dropped file with no
  server-side path, the file you dropped is uploaded for that copy). The **same
  ✎ override** is available in the **Map popup** and behaves identically — both
  routes go through `POST /override/<hash>`, so an override set in either place
  sticks in the other. To revert, **omit** the track and re-scan it.
- **✕ omit** — delete a track's analysis entirely (a bogus read). Removes it
  from the cache, the Map, and any tag/vibe membership. **Your audio file is not
  touched** — re-scanning re-analyzes it fresh. Also available in the Map popup.
- **Tags** — attach manual labels ("high energy", "opener") to tracks.
- **◈ vibes** — user-defined similarity clusters over the embeddings. Add tracks
  to a vibe, and it builds a **playlist** of everything in your library that
  sounds similar (ranked by cosine similarity to the vibe's centre). Membership
  is **weighted** (per-track 👍/👎 and a slider), so you can steer a vibe toward
  or away from example tracks.
- **Clear list** — empties the on-screen list (does **not** delete analyses).
- **Export .txt** — dump the current list (genre, BPM, key, duration) to a text
  file.

---

## 8. Finding bad reads (Review reads)

The classifier is usually right, but occasionally confidently wrong (a bass
track read as "K-pop"). Vibedentify flags these automatically using the
embeddings: a read is **suspect** when it's *low-confidence* **and** its closest
sonic neighbours strongly agree on a *different* genre.

- On a flagged track's row you'll see a **⚠ "sounds like <X>"** hint — click it
  to prefill an override with the suggested genre.
- Click **⚠ review reads** in the footer to open a panel listing **every**
  flagged track (`reads as X → sounds like Y, N% agree`), each with a **map**
  button (jump to it on the Map) and an **omit** button. A quick way to sweep
  the whole library for misreads and clean them up.

It only ever **flags and suggests** — it never silently changes a genre.

---

## 9. The Map

Switch to the **✷ Map** tab for a 3-D constellation of your whole library.
Nearby dots sound alike; faint lines connect each track to its nearest sonic
neighbours; colour = genre family.

### Layouts

- **◎ regions** *(default)* — genres form clusters (the biggest family in the
  centre, the rest around it). Within a family, tracks are grouped by subgenre,
  so Dubstep and Drum n Bass sit apart. **Semantic zoom:** when you zoom in, the
  big family labels fade and **subgenre labels** fade in.
- **✦ galaxy** — no fixed regions; position is pure sonic similarity (a global
  projection of the embeddings).

### Navigating

Three independent camera moves — **orbit** (rotate the cloud), **pan** (slide
across it), and **zoom** (dolly in/out):

- **Mouse:**
  - **Left-drag** — orbit.
  - **Right-drag**, **middle-drag**, or **Shift + left-drag** — pan (fly across
    the scene without rotating).
  - **Scroll** — zoom *into wherever you point* (up to 60×).
- **Keyboard:**
  - **Arrows** — orbit · **Shift + arrows** — pan.
  - **`+` / `-`** — zoom.
  - **`space`** — play / pause the orbit · **`f`** — fit · **`esc`** — close the
    popup.
- **Click a track** to select it — the popup opens and **the orbit re-centres to
  spin around that track**; close the popup and it returns to the normal centre
  orbit.
- **Hover** a dot for a quick tooltip (title / style / BPM / key).
- **⏸ / ▶ button** and the **↻ slider** control the auto-orbit: the button
  pauses/resumes it (same as `space`), the slider sets its speed (drag to 0 to
  stop).

### Controls (top bar)

- **Search** — type a track/artist/genre; picking one flies the camera to it.
- **🎧 harmonic** — with a track selected, highlights **key + BPM compatible**
  tracks (Camelot-wheel neighbours / relative major-minor, BPM within ±6% or
  half/double-time) with a teal ring and dims the rest. Your "what can I mix
  next" view.
- **≈ edges** — show/hide the connection lines (a selected track's own web still
  shows).
- **Genre filter** — a dropdown (and clickable legend chips): show only one
  genre, or choose **⚠ likely misreads** to show just the flagged tracks.
- **⏸ / ▶ + ↻ slider** — pause/resume and set the auto-orbit speed (see
  *Navigating* above).
- **fit** — reset the view (and clear any filter).

### The popup

Selecting a track shows its family, metadata, "also reads as", **similar
artists** and **similar tracks** (from the embeddings), and a **🎲 "a match for
you"** block that surfaces a random one of its closest matches (with a re-roll).
Flagged tracks show the **⚠ misread note**. There's an **✎ override** button
(persists just like the List-view override — see §7) and an **omit** button.

### Deep links

`#map` opens the Map · `#map=<hash>` opens and selects a track · `#galaxy`
opens in galaxy layout · `#review` opens the review-reads panel.

---

## 10. Good to know

- **Nothing is analyzed twice.** Results are cached by *content hash*, so the
  same audio returns instantly regardless of filename or location — and a
  renamed/moved copy is recognised as the same track.
- **The list is a session view; the data is permanent.** Reloading the page (or
  restarting) clears the on-screen list, but every analysis stays in the
  database and on the Map. Re-drop files to repopulate the list (instant, from
  cache).
- **Limits:** 512 MB per file; no limit on track count. The on-screen list gets
  heavy past a few thousand rows (each keeps a waveform in memory), so for very
  large libraries lean on the Map and batch folder.
- **Keep it local.** The server binds to `127.0.0.1`. Its API reads/scans/copies
  server-side paths, so don't expose it beyond localhost.
- **Config:** all optional env vars are documented in
  [`.env.example`](../.env.example) (model dir, database path, upload cap, etc.).
