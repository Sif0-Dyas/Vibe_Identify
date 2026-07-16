const rowsEl  = document.getElementById('rows');
const emptyEl = document.getElementById('empty');
const drop    = document.getElementById('drop');
const picker  = document.getElementById('picker');
const exportB = document.getElementById('export');
const clearB  = document.getElementById('clear');
const countEl = document.getElementById('count');

let results = [];          // {title, filename, genreLabel, ok}
let queue = [];
let busy = false;

/* Okabe-Ito palette (color-blind safe) + shape + texture per genre.
   Focus genres are pre-registered with fixed identities; everything else
   gets the next most-distinct combination on first appearance.
   (Grey is intentionally NOT here -- it's reserved for the "Other" bucket.) */
const PALETTE = ["#009E73","#F0E442","#D55E00","#0072B2","#E69F00",
                 "#56B4E9","#CC79A7","#7FCDBB","#B79AE3","#C44E78"];
const SHAPES  = ["sq","ci","tr","di","hx"];
const TEXTURES = ["none",
  "repeating-linear-gradient(45deg, rgba(0,0,0,.35) 0 2px, transparent 2px 6px)",
  "repeating-linear-gradient(-45deg, rgba(0,0,0,.35) 0 2px, transparent 2px 6px)",
  "repeating-linear-gradient(0deg, rgba(0,0,0,.35) 0 2px, transparent 2px 6px)",
  "radial-gradient(rgba(0,0,0,.45) 1px, transparent 1.5px)"];
const OTHER_COLOR = "#5A6472";      // deliberate neutral slate = "Other"
const TOP_N_COARSE = 3;             // coarse view: name top-3 genres, rest -> Other
const REG = new Map([
  ["drum n bass", {color:"#E69F00", shape:"sq", tex:TEXTURES[0]}],
  ["trance",      {color:"#56B4E9", shape:"ci", tex:TEXTURES[1]}],
  ["dubstep",     {color:"#CC79A7", shape:"tr", tex:TEXTURES[2]}],
]);
let regCount = 0;
function styleInfo(style){
  const k = (style||'').toLowerCase();
  if (k === 'other') return {color:OTHER_COLOR, shape:'hx', tex:'none'};
  if (!REG.has(k)){
    const i = regCount++;
    REG.set(k, {color: PALETTE[i % PALETTE.length],
                shape: SHAPES[(i + 3) % SHAPES.length],
                tex:   TEXTURES[(i + 3) % TEXTURES.length]});
  }
  return REG.get(k);
}
function colorFor(style){ return styleInfo(style).color; }


/* recolor a genre everywhere (canvas + breakdown, all rows) for the session */
function recolorGenre(key, hex){
  const info = styleInfo(key);   // registers it if new
  info.color = hex;
  for (const r of results){
    if (!r.ok || !r.row) continue;
    if (r.row._renderGenre) r.row._renderGenre();
    if (r.row._redrawWave) r.row._redrawWave();
  }
}
// one shared <input type=color>, repositioned and reused per click
let _picker = null;
function openColorPicker(key, currentHex, anchorEl){
  if (!_picker){
    _picker = document.createElement('input');
    _picker.type = 'color';
    _picker.style.cssText = 'position:fixed;width:0;height:0;opacity:0;border:0;padding:0;pointer-events:none';
    document.body.appendChild(_picker);
  }
  _picker.value = /^#[0-9a-fA-F]{6}$/.test(currentHex) ? currentHex : '#888888';
  _picker.oninput = () => recolorGenre(key, _picker.value);
  const r = anchorEl.getBoundingClientRect();
  _picker.style.left = r.left + 'px';
  _picker.style.top = r.bottom + 'px';
  _picker.click();
}

/* In coarse view the top-N genres by track time stay named; the rest are
   collapsed into a single "Other" bucket -- used identically by the waveform and
   the side breakdown so the two always agree. */
/* In coarse view the top-N genres by track time stay named; the rest collapse
   to "Other". When expand=true (fine detail) every genre is named -- no Other.
   Used identically by the waveform and the side breakdown so they always agree. */
function mainGenreSet(segments, expand){
  if (!segments || !segments.length) return null;
  const counts = {}; for (const s of segments) counts[s] = (counts[s] || 0) + 1;
  if (expand) return new Set(Object.keys(counts));        // name everything
  const ranked = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  return new Set(ranked.slice(0, TOP_N_COARSE).map(e => e[0]));
}
function bandColor(g, mainSet){ return (mainSet && !mainSet.has(g)) ? OTHER_COLOR : colorFor(g); }
function bandLabel(g, mainSet){ return (mainSet && !mainSet.has(g)) ? 'Other' : g; }

/* ===================== ANALYZER LENSES ===========================
   Two independent switches, both recomputed from the per-frame top-k data:
     identity  : 'v2' (salience) | 'v1' (flat % of track)   -> side breakdown
     seg       : 'raw' | 'hysteresis' | 'sibling'           -> waveform stream
   GLOBAL holds the defaults; each row may override either. */
const GLOBAL = {identity: 'v2', seg: 'hysteresis'};

// near-synonym clusters that flicker; member -> canonical name. Editable.
const SIBLING_GROUPS = {
  "House":       ["House","Tribal House","Deep House","Tech House","Progressive House",
                  "Electro House","Garage House","Italo House","Euro House","Hard House",
                  "Ghetto House","Speed Garage"],
  "Trance":      ["Trance","Tech Trance","Hard Trance","Progressive Trance","Goa Trance",
                  "Psy-Trance","Hands Up"],
  "Techno":      ["Techno","Hard Techno","Minimal Techno","Dub Techno","Deep Techno","Schranz"],
  "Drum n Bass": ["Drum n Bass","Jungle","Halftime","Breakcore","Neurofunk"],
  "Hardcore":    ["Hardcore","Gabber","Speedcore","Happy Hardcore","Makina","Jumpstyle",
                  "Hardstyle","Terrorcore"],
  "Breaks":      ["Breaks","Progressive Breaks","Breakbeat"],
  "Dubstep":     ["Dubstep","Brostep"],
  "Ambient":     ["Ambient","Dark Ambient","Drone","Berlin-School","New Age"],
};
const SIBLING_MAP = (() => {
  const m = {};
  for (const canon in SIBLING_GROUPS) for (const mem of SIBLING_GROUPS[canon]) m[mem.toLowerCase()] = canon;
  return m;
})();

/* PulseRoots family roll-up: map each Discogs style to a broad family
   (mendiak.github.io/pulse.roots, MIT-licensed hierarchy). Resolution falls
   back through the editable sibling groups, then to the style itself, so
   coverage stays high even where PulseRoots has no direct entry. */
let STYLE_FAMILY = {};
fetch('/static/genre_families.json')
  .then(r => r.ok ? r.json() : null)
  .then(d => { if (d && d.style_family) STYLE_FAMILY = d.style_family; })
  .catch(() => {});
function familyOf(style){
  const k = (style || '').toLowerCase();
  if (STYLE_FAMILY[k]) return STYLE_FAMILY[k];
  const canon = SIBLING_MAP[k];                 // editable near-synonym group
  if (canon) return STYLE_FAMILY[canon.toLowerCase()] || canon;
  return style;
}
/* family-merge: pool each frame's scores by family, take the winner */
function segsFamily(frames){
  return frames.map(f => {
    const agg = {};
    for (const [s, p] of f){ const fam = familyOf(s); agg[fam] = (agg[fam] || 0) + p; }
    let best = null, bp = -1;
    for (const k in agg){ if (agg[k] > bp){ bp = agg[k]; best = k; } }
    return best;
  });
}

const HYST_MARGIN = 0.08;   // challenger must beat the held genre by this to switch
const HYST_HOLD   = 2;      // ...for this many consecutive frames

/* winners-only stream straight from frames */
function segsRaw(frames){ return frames.map(f => f[0][0]); }

/* hysteresis: stay on the current genre until a challenger decisively wins */
function segsHysteresis(frames, margin, hold){
  margin = margin ?? HYST_MARGIN; hold = hold ?? HYST_HOLD;
  const out = []; let cur = null, cand = null, count = 0;
  for (const f of frames){
    const top = f[0][0], topP = f[0][1];
    if (cur === null){ cur = top; out.push(cur); continue; }
    let curP = 0;                                   // current genre's score this frame
    for (const [s, p] of f){ if (s === cur){ curP = p; break; } }
    if (top === cur){ cand = null; count = 0; }
    else {
      if (top === cand) count++; else { cand = top; count = 1; }
      if (count >= hold && (topP - curP) >= margin){ cur = cand; cand = null; count = 0; }
    }
    out.push(cur);
  }
  return out;
}

/* sibling-merge: sum near-synonym scores per frame, then take the winner */
function segsSibling(frames){
  return frames.map(f => {
    const agg = {};
    for (const [s, p] of f){
      const canon = SIBLING_MAP[s.toLowerCase()] || s;
      agg[canon] = (agg[canon] || 0) + p;
    }
    let best = null, bp = -1;
    for (const k in agg){ if (agg[k] > bp){ bp = agg[k]; best = k; } }
    return best;
  });
}

function segsForMode(frames, mode){
  if (!frames || !frames.length) return null;
  if (mode === 'hysteresis') return segsHysteresis(frames);
  if (mode === 'sibling')    return segsSibling(frames);
  if (mode === 'family')     return segsFamily(frames);
  if (mode === 'hyst+sib'){
    // sibling first (score-pooling per frame), then hysteresis on the resulting stream
    const sibSegs = segsSibling(frames);
    // re-wrap as minimal frames for hysteresis: [[genre, 1.0]]
    const sibFrames = sibSegs.map(g => [[g, 1.0]]);
    return segsHysteresis(sibFrames);
  }
  return segsRaw(frames);
}
function refreshFooter(){
  const done = results.filter(r => r.ok).length;
  countEl.textContent = `${done} track${done===1?'':'s'}`;
  exportB.disabled = done === 0;
}

function addRow(file){
  emptyEl.style.display = 'none';
  const row = document.createElement('div');
  row.className = 'row pending';
  row.innerHTML = `
    <div>
      <div class="title">${escapeHtml(file.name.replace(/\.[^.]+$/,''))}</div>
      <div class="file">${escapeHtml(file.name)}</div>
    </div>
    <div class="musical"></div>
    <div>
      <span class="chip"><span class="dot"></span>analyzing&hellip;</span>
    </div>`;
  rowsEl.appendChild(row);
  row.scrollIntoView({block:'nearest'});
  return row;
}

/* Fisheye waveform. When focus (0..1) is set, the area under the cursor bulges
   larger and tapers toward normal at the edges -- like the macOS Dock. With
   focus null it draws flat. Returns nothing; purely visual.
   STRENGTH controls bulge amount; ZONE is how wide (in track-fraction) the lens
   reaches before it's back to ~1x. */
function drawWave(canvas, peaks, fallbackColor, segments, focus, mainSet){
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 300, h = canvas.clientHeight || 60;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  const mid = h / 2;
  const segN = segments ? segments.length : 0;
  const STRENGTH = 0.85, ZONE = 0.14;

  // map a screen fraction sx (0..1) -> source track fraction (0..1) through lens
  function lens(sx){
    if (focus == null) return sx;
    const d = sx - focus;
    const g = Math.exp(-(d * d) / (2 * ZONE * ZONE));   // 1 at cursor -> 0 far
    return sx - STRENGTH * d * g;                        // compress toward focus
  }
  // display label for a source frame, collapsing minor genres to "Other"
  const disp = i => bandLabel(segments[Math.max(0, Math.min(i, segN - 1))], mainSet);

  const cols = Math.max(Math.floor(w), 1);
  let lastLabel = null;
  for (let px = 0; px < cols; px++){
    const sx = px / (cols - 1);
    const f = Math.max(0, Math.min(1, lens(sx)));
    const pi = Math.min(Math.floor(f * peaks.length), peaks.length - 1);
    const amp = Math.max(peaks[Math.max(pi, 0)] * mid, 1.0);
    let color = fallbackColor, label = null;
    if (segN){
      const segIdx = Math.min(Math.floor(f * segN), segN - 1);
      const raw = segments[Math.max(segIdx, 0)];
      color = bandColor(raw, mainSet);
      label = bandLabel(raw, mainSet);
    }
    // boundary tick where the displayed genre changes (Other counts as one genre)
    if (label !== lastLabel && lastLabel !== null){
      ctx.globalAlpha = 1; ctx.fillStyle = 'rgba(255,255,255,.20)';
      ctx.fillRect(px, 0, 1, h);
    }
    lastLabel = label;
    ctx.globalAlpha = focus == null ? 0.9 : 0.94;
    ctx.fillStyle = color;
    ctx.fillRect(px, mid - amp, 1.05, amp * 2);
  }
  ctx.globalAlpha = 1;
}

function roundRect(ctx, x, y, w, h, r){
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function fmtDur(sec){
  if (sec == null) return '';
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return `${m}:${String(s).padStart(2,'0')}`;
}

/* genre detected at fractional position f (0..1) along the track */
function genreAt(segments, f){
  if (!segments || !segments.length) return null;
  const i = Math.min(Math.floor(f * segments.length), segments.length - 1);
  return segments[Math.max(i, 0)];
}

/* Temporal smoothing of the per-frame genre stream.
   secs = strength in seconds; hop = seconds per frame (2.0 coarse / ~0.5 fine).
   Two passes: (1) median/majority filter over a window, (2) absorb any run
   shorter than the threshold into its larger neighbor. Pure display cleanup;
   the raw array is never mutated (we return a new one). */
function smoothSegments(segments, secs, hop){
  if (!segments || segments.length < 3 || secs <= 0) return segments ? segments.slice() : segments;
  const win = Math.max(1, Math.round(secs / (hop || 2.0)));   // frames in window
  const n = segments.length;

  // pass 1: majority vote in a +/- win neighborhood
  let out = new Array(n);
  for (let i = 0; i < n; i++){
    const counts = {};
    for (let j = Math.max(0, i - win); j <= Math.min(n - 1, i + win); j++){
      counts[segments[j]] = (counts[segments[j]] || 0) + 1;
    }
    let best = segments[i], bn = -1;
    for (const k in counts){ if (counts[k] > bn){ bn = counts[k]; best = k; } }
    out[i] = best;
  }

  // pass 2: merge runs shorter than the window into a neighbor
  const minRun = win;
  let changed = true, guard = 0;
  while (changed && guard++ < 20){
    changed = false;
    let i = 0;
    while (i < n){
      let j = i;
      while (j < n && out[j] === out[i]) j++;
      const len = j - i;
      if (len < minRun && (i > 0 || j < n)){
        const left = i > 0 ? out[i - 1] : null;
        const right = j < n ? out[j] : null;
        const repl = left !== null ? left : right;   // prefer left neighbor
        if (repl !== null && repl !== out[i]){
          for (let k = i; k < j; k++) out[k] = repl;
          changed = true;
        }
      }
      i = j;
    }
  }
  return out;
}

/* Percentages from the smoothed TIMELINE: what fraction of the track each
   genre actually occupies. Returns [{style, score}] sorted desc, score in 0..1. */
function timelinePercents(segments){
  if (!segments || !segments.length) return [];
  const counts = {};
  for (const s of segments) counts[s] = (counts[s] || 0) + 1;
  const n = segments.length;
  return Object.entries(counts)
    .map(([style, c]) => ({style, score: c / n}))
    .sort((a, b) => b.score - a.score);
}

function fmtTime(sec){
  if (sec == null || isNaN(sec)) return '0:00';
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2,'0')}`;
}

/* ---- shared audio preview player: one track plays at a time. Each row
   registers a controller (PLAYER.ctl) that the audio events drive; starting a
   new row hands the single <audio> over and resets the previous row's UI. ---- */
const PLAYER = { audio: new Audio(), ctl: null };
PLAYER.audio.preload = 'metadata';
const OBJ_URLS = [];   // blob URLs for dropped files, revoked on Clear
PLAYER.audio.addEventListener('timeupdate', () => { if (PLAYER.ctl) PLAYER.ctl.tick(); });
PLAYER.audio.addEventListener('play',       () => { if (PLAYER.ctl) PLAYER.ctl.render(); });
PLAYER.audio.addEventListener('pause',      () => { if (PLAYER.ctl) PLAYER.ctl.render(); });
PLAYER.audio.addEventListener('ended',      () => { if (PLAYER.ctl) PLAYER.ctl.render(); });
PLAYER.audio.addEventListener('error',      () => { if (PLAYER.ctl) PLAYER.ctl.error(); });

function finishRow(row, data, file){
  row.classList.remove('pending');
  const styles = data.styles || [];
  const primary = styles[0] || {style: '?', score: 0};   // guard: model returned no styles
  const total = styles.reduce((a,s) => a + s.score, 0) || 1;
  const pcol = colorFor(primary.style);
  row.querySelector('.title').textContent = data.title;

  /* waveform under the title, painted by per-segment genre, with magnifier */
  let renderGenreCell = () => {};   // assigned below; called on smoothing change
  if (data.waveform && data.waveform.length){
    const waveState = {
      peaks: data.waveform,
      frames: data.frames || null,        // per-frame top-k (for lenses)
      winners: data.segments,             // raw argmax winners (coarse)
      raw: data.segments,                 // seg-lens output (starts = winners)
      segments: data.segments,            // smoothed view of raw
      fine: false,
      hop: 2.0,                           // seconds per frame (coarse default)
      smooth: 1.0,                        // medium default (~1s)
    };
    const container = document.createElement('div');
    container.className = 'wavecontainer';
    const c = document.createElement('canvas');
    c.className = 'wave';
    c.title = 'click to play from here · hover to magnify';
    const tip = document.createElement('div');
    tip.className = 'wavetip';
    container.appendChild(c);
    container.appendChild(tip);

    const controls = document.createElement('div');
    controls.className = 'wavehint';
    controls.innerHTML = `<span class="restag" title="genre-boundary resolution">~2s</span>` +
      `<button class="finebtn" type="button" title="re-analyze this track at ~0.5s resolution (slower)">\u2295 fine detail</button>` +
      `<span class="smoothnote" title="single-frame genre flickers are smoothed out (~1s)">smoothed ~1s</span>`;
    const resTag = controls.querySelector('.restag');
    const fineBtn = controls.querySelector('.finebtn');

    row.children[0].appendChild(container);
    row.children[0].appendChild(controls);

    // per-row lens override controls (inherit global until changed)
    const rowlens = document.createElement('div');
    rowlens.className = 'rowlens';
    rowlens.innerHTML =
      `<span>lens:</span>` +
      `<select class="rl-id" title="identity (this track)">` +
        `<option value="">identity: global</option>` +
        `<option value="v2">v2 · salience</option><option value="v1">v1 · flat %</option></select>` +
      `<select class="rl-seg" title="segmentation (this track)">` +
        `<option value="">segment: global</option>` +
        `<option value="raw">raw</option><option value="hysteresis">hysteresis</option>` +
        `<option value="sibling">sibling-merge</option><option value="family">family</option></select>` +
      `<span class="ovr"></span>`;
    const rlId = rowlens.querySelector('.rl-id');
    const rlSeg = rowlens.querySelector('.rl-seg');
    const ovr = rowlens.querySelector('.ovr');
    row.children[0].appendChild(rowlens);

    function syncOvrTag(){
      const tags = [];
      if (row._idOverride) tags.push('id*');
      if (row._segOverride) tags.push('seg*');
      ovr.textContent = tags.length ? tags.join(' ') : '';
    }
    rlId.addEventListener('change', () => { row._idOverride = rlId.value || null; syncOvrTag(); row._applyModes(); });
    rlSeg.addEventListener('change', () => { row._segOverride = rlSeg.value || null; syncOvrTag(); row._applyModes(); });

    const dur = data.duration || 0;

    // effective mode = per-row override (if set) else the global default
    function segMode(){ return row._segOverride || GLOBAL.seg; }
    function identityMode(){ return row._idOverride || GLOBAL.identity; }

    function applySmoothing(){
      // 1) segmentation lens turns per-frame top-k into the genre stream
      const lensed = waveState.frames ? segsForMode(waveState.frames, segMode()) : null;
      waveState.raw = lensed || waveState.winners;
      // 2) temporal smoothing
      waveState.segments = waveState.smooth > 0
        ? smoothSegments(waveState.raw, waveState.smooth, waveState.hop)
        : waveState.raw.slice();
      waveState.mainSet = mainGenreSet(waveState.segments, waveState.fine);
    }
    function redraw(focus){
      drawWave(c, waveState.peaks, pcol, waveState.segments, focus ?? null, waveState.mainSet);
    }
    applySmoothing();
    requestAnimationFrame(() => redraw(null));

    function showAt(clientX){
      const rect = c.getBoundingClientRect();
      let f = (clientX - rect.left) / rect.width;
      f = Math.max(0, Math.min(1, f));
      redraw(f);
      const g = genreAt(waveState.segments, f);
      const isOther = g && waveState.mainSet && !waveState.mainSet.has(g);
      const swatch = g ? bandColor(g, waveState.mainSet) : null;
      const text = g ? (isOther ? `${g} \u00b7 other` : g) : '';
      tip.style.left = (f * rect.width) + 'px';
      tip.style.display = 'block';
      tip.innerHTML = `${fmtTime(f * dur)}` +
        (g ? `<span class="sw" style="background:${swatch}"></span>${escapeHtml(text)}` : '');
    }
    c.addEventListener('pointermove', e => showAt(e.clientX));
    c.addEventListener('pointerdown', e => { c.setPointerCapture(e.pointerId); showAt(e.clientX); });
    c.addEventListener('pointerleave', () => { tip.style.display = 'none'; redraw(null); });

    /* fine-detail: re-analyze just this track at ~0.5s resolution on demand */
    fineBtn.addEventListener('click', async () => {
      if (waveState.fine || !file) return;
      fineBtn.disabled = true;
      fineBtn.textContent = 'refining\u2026';
      try{
        const fd = new FormData();
        fd.append('file', file);
        const resp = await fetch('/refine', {method:'POST', body:fd});
        const j = await resp.json();
        if (!resp.ok) throw new Error(j.error || resp.statusText);
        waveState.winners = j.segments;
        waveState.frames = j.frames || null;
        waveState.fine = true;
        waveState.hop = j.hop_seconds ?? 0.5;
        resTag.textContent = `~${waveState.hop}s`;
        applySmoothing();
        redraw(null);
        renderGenreCell();
        fineBtn.textContent = 'fine \u2713';
        fineBtn.classList.add('done');
      }catch(err){
        fineBtn.textContent = 'retry';
        fineBtn.disabled = false;
        fineBtn.title = 'refine failed: ' + err.message;
      }
    });

    /* ---- audio preview: play/scrub this track through its waveform ---- */
    const playhead = document.createElement('div');
    playhead.className = 'playhead';
    container.appendChild(playhead);

    const playBtn = document.createElement('button');
    playBtn.className = 'playbtn'; playBtn.type = 'button';
    playBtn.textContent = '▶ play';
    playBtn.title = 'play / pause (or click the waveform to play from a point)';
    const playTime = document.createElement('span');
    playTime.className = 'playtime';
    controls.insertBefore(playTime, controls.firstChild);
    controls.insertBefore(playBtn, controls.firstChild);

    let objURL = null;
    function playSrc(){
      if (file){                       // dropped/browsed file -> client-side blob
        if (!objURL){ objURL = URL.createObjectURL(file); OBJ_URLS.push(objURL); }
        return objURL;
      }
      if (data.hash && data.filepath) return '/audio/' + data.hash;   // server file
      return null;
    }
    const isActive = () => PLAYER.ctl === ctl;
    function render(){
      const playing = isActive() && !PLAYER.audio.paused && !PLAYER.audio.ended;
      playBtn.textContent = playing ? '❙❙ pause' : '▶ play';
      playBtn.classList.toggle('playing', playing);
      playhead.style.display = (isActive() && (playing || PLAYER.audio.currentTime > 0)) ? 'block' : 'none';
      if (!isActive()) playTime.textContent = '';
    }
    function tick(){
      if (!isActive()) return;
      const d = PLAYER.audio.duration || dur || 0;
      const cur = PLAYER.audio.currentTime || 0;
      playhead.style.left = (d ? cur / d * 100 : 0) + '%';
      playTime.textContent = `${fmtTime(cur)} / ${fmtTime(d)}`;
    }
    function stopVisual(){
      playBtn.textContent = '▶ play'; playBtn.classList.remove('playing');
      playhead.style.display = 'none'; playTime.textContent = '';
    }
    function onError(){
      if (!isActive()) return;
      playBtn.textContent = '✕ can’t play'; playBtn.disabled = true;
      playBtn.title = 'this audio format can’t be played by the browser';
    }
    const ctl = { tick, render, stopVisual, error: onError };
    row._playCtl = ctl;

    async function startPlay(seekFrac){
      const src = playSrc();
      if (!src){ playBtn.textContent = '✕ no source'; playBtn.disabled = true; return; }
      if (!isActive()){                       // take over the shared player
        if (PLAYER.ctl) PLAYER.ctl.stopVisual();
        PLAYER.ctl = ctl;
        PLAYER.audio.src = src;
      }
      if (seekFrac != null){
        const f = Math.max(0, Math.min(1, seekFrac));
        const setT = () => { PLAYER.audio.currentTime = f * (PLAYER.audio.duration || dur || 0); };
        if (PLAYER.audio.readyState >= 1) setT();
        else PLAYER.audio.addEventListener('loadedmetadata', setT, {once:true});
      }
      try { await PLAYER.audio.play(); } catch(e){ /* error event drives the UI */ }
      render();
    }
    function togglePlay(){
      if (isActive() && !PLAYER.audio.paused) PLAYER.audio.pause();
      else startPlay(isActive() ? null : 0);
    }
    playBtn.addEventListener('click', togglePlay);
    c.addEventListener('pointerdown', e => {
      const rect = c.getBoundingClientRect();
      startPlay((e.clientX - rect.left) / rect.width);
    });

    // expose for the genre cell renderer defined below
    row._waveState = waveState;
    row._redrawWave = () => redraw(null);
    row._applyModes = () => { applySmoothing(); redraw(null); renderGenreCell(); };
  }

  /* BPM / key cell */
  const lowConf = data.bpm_confidence != null && data.bpm_confidence < 1.5;
  const alt = data.bpm != null ? (data.bpm < 100 ? data.bpm * 2 : data.bpm / 2) : null;
  let bpmText = data.bpm != null ? data.bpm.toFixed(1) : '---';
  let bpmHtml;
  if (data.bpm == null){
    bpmHtml = `<div class="keyrow">no BPM</div>`;
  } else if (lowConf){
    bpmText = `${data.bpm.toFixed(1)}/${alt.toFixed(1)}`;
    bpmHtml = `<div><span class="bpm">${data.bpm.toFixed(1)}</span>` +
      `<span class="altbpm"> / ${alt.toFixed(1)}</span><span class="unit">BPM</span>` +
      `<span class="unit warn" title="low beat-tracking confidence \u2014 the second number is the half/double-time alternate">?</span></div>`;
  } else {
    bpmHtml = `<div><span class="bpm">${data.bpm.toFixed(1)}</span><span class="unit">BPM</span></div>`;
  }
  const keyHtml = data.key
    ? `<div class="keyrow">` +
      (data.camelot ? `<span class="camelot">${escapeHtml(data.camelot)}</span>` : '') +
      `${escapeHtml(data.key)} ${escapeHtml((data.scale||'').slice(0,3))}</div>`
    : `<div class="keyrow">no key</div>`;
  row.children[1].innerHTML = bpmHtml + keyHtml +
    `<div class="dur">${fmtDur(data.duration)}</div>`;

  /* genre cell -- driven by the smoothed timeline when segments exist, else
     by the model's averaged confidences. Re-runs when the smoothing changes. */
  renderGenreCell = () => {
    const ws = row._waveState;
    const useTimeline = ws && ws.segments && ws.segments.length;
    let shown, srcLabel;
    const idMode = (row._idOverride || GLOBAL.identity);
    if (idMode === 'v2' && data.salience && data.salience.length){
      const named = data.salience.filter(s => s.score >= 0.03);
      const namedSum = named.reduce((a, s) => a + s.score, 0);
      const otherSum = Math.max(0, 1 - namedSum);
      shown = named.map(s => ({style:s.style, score:s.score, other:false}));
      if (otherSum > 0.005) shown.push({style:'Other', score:otherSum, other:true});
      srcLabel = 'v2 · genre identity (energy-weighted)';
    } else if (useTimeline){
      // v1: flat % of track by frame count, over the lens-processed stream
      const all = timelinePercents(ws.segments);
      const named = all.filter(s => s.score >= 0.03);
      const otherSum = all.filter(s => s.score < 0.03).reduce((a, s) => a + s.score, 0);
      shown = named.map(s => ({style:s.style, score:s.score, other:false}));
      if (otherSum > 0.005) shown.push({style:'Other', score:otherSum, other:true});
      srcLabel = 'v1 · % of track (flat)';
    } else {
      shown = styles.slice(0, 5).filter(s => s.score >= 0.02)
                    .map(s => ({style:s.style, score:s.score, other:false}));
      if (!shown.length) shown = styles.slice(0, 1).map(s => ({style:s.style, score:s.score}));
      srcLabel = 'model confidence';
    }
    const tot = shown.reduce((a, s) => a + s.score, 0) || 1;
    const pct = v => (v * 100 < 0.5 && v > 0) ? '<1' : (v * 100).toFixed(0);
    // headline = top non-Other genre (never lead with "Other")
    const head = shown.find(s => !s.other) || shown[0];
    const hcol = head.other ? OTHER_COLOR : colorFor(head.style);
    const hshape = head.other ? 'hx' : styleInfo(head.style).shape;

    const segHtml = shown.map(s => {
      const col = s.other ? OTHER_COLOR : colorFor(s.style);
      const inf = s.other ? {tex:'none'} : styleInfo(s.style);
      const tex = inf.tex !== 'none' ? `background-image:${inf.tex};background-size:5px 5px;` : '';
      return `<i style="width:${(s.score/tot*100).toFixed(1)}%;background-color:${col};${tex}"
          title="${escapeHtml(s.style)} ${(s.score*100).toFixed(0)}%"></i>`;
    }).join('');
    const itemsHtml = shown.map((s, i) => {
      const col = s.other ? OTHER_COLOR : colorFor(s.style);
      const shp = s.other ? 'hx' : styleInfo(s.style).shape;
      const click = s.other ? ''
        : ` class="sw ${shp} swc" data-genre="${escapeHtml(s.style)}" data-hex="${col}" title="click to recolor"`;
      const sw = s.other ? `<span class="sw ${shp}" style="background:${col}"></span>`
                         : `<span${click} style="background:${col}"></span>`;
      // top-3 non-Other styles get descending emphasis (#1 largest → #3 smallest)
      const rank = (!s.other && i < 3) ? ` rank-${i + 1}` : '';
      return `<span class="bd-item${rank}">${sw}<b>${escapeHtml(s.style)}</b> ${pct(s.score)}%</span>`;
    }).join('');

    let customHtml = '';
    if (data.custom && data.custom.length){
      const cs = data.custom.filter(s => s.score >= 0.05).slice(0,4);
      const citems = (cs.length ? cs : data.custom.slice(0,1)).map(s => {
        const inf = styleInfo(s.style);
        return `<span><span class="sw ${inf.shape}" style="background:${inf.color}"></span>` +
          `<b>${escapeHtml(s.style)}</b> ${(s.score*100).toFixed(0)}%</span>`;
      }).join('');
      customHtml = `<div class="customrow"><span class="ctag">custom</span>` +
                   `<span class="breakdown" style="display:inline-flex">${citems}</span></div>`;
    }

    const headFam = head.other ? null : familyOf(head.style);
    const famHtml = (headFam && headFam.toLowerCase() !== head.style.toLowerCase())
      ? `<span class="famtag" title="PulseRoots family roll-up">\u25c7 ${escapeHtml(headFam)}</span>` : '';
    row.children[2].innerHTML =
      `<div class="genre-src">${srcLabel}</div>` +
      `<span class="chip" style="--c:${hcol}"
        title="${escapeHtml(head.style)} \u2014 ${srcLabel}">
        <span class="dot ${hshape}" style="background:${hcol}"></span>${escapeHtml(head.style)}</span>${famHtml}
      <div class="blend">${segHtml}</div>
      <div class="breakdown">${itemsHtml}</div>${customHtml}`;

    row._genreList = shown;
  };
  renderGenreCell();
  row._renderGenre = renderGenreCell;

  // click a genre swatch to recolor it everywhere (delegated; survives re-renders)
  if (!row.children[2]._recolorBound){
    row.children[2]._recolorBound = true;
    row.children[2].addEventListener('click', e => {
      const sw = e.target.closest('.swc');
      if (!sw) return;
      openColorPicker(sw.dataset.genre.toLowerCase(), sw.dataset.hex, sw);
    });
  }

  /* ---- manual genre override ---- */
  const overrideBtn = document.createElement('button');
  overrideBtn.className = 'override-btn';
  overrideBtn.textContent = '✎ override';
  overrideBtn.title = 'set the genre yourself + save as training data';
  row.children[2].appendChild(overrideBtn);

  /* ---- omit: delete this analysis entirely (e.g. a bogus read) ---- */
  const omitBtn = document.createElement('button');
  omitBtn.className = 'override-btn omit-btn';
  omitBtn.textContent = '✕ omit';
  omitBtn.title = 'delete this analysis — remove the track from your library (audio untouched)';
  row.children[2].appendChild(omitBtn);
  omitBtn.addEventListener('click', async () => {
    if (!window.confirm(`Remove "${data.title}" from your library?\n\n`
      + `Deletes its analysis (genre, BPM, key) and takes it off the map. `
      + `The audio file is untouched.`)) return;
    const res = getResult();
    if (res && res.hash){ try{ await fetch(`/forget/${res.hash}`, {method:'POST'}); }catch(_){} }
    results = results.filter(r => r.row !== row);
    row.remove();
    if (!rowsEl.querySelector('.row')) emptyEl.style.display = '';
    refreshFooter();
  });

  const editor = document.createElement('div');
  editor.className = 'override-editor';
  editor.style.display = 'none';
  const oInput = document.createElement('input');
  oInput.type = 'text'; oInput.placeholder = 'e.g. Riddim, Tearout, Colour Bass…';
  const oSave = document.createElement('button');
  oSave.className = 'ovr-save'; oSave.textContent = 'save + train';
  const oCancel = document.createElement('button');
  oCancel.className = 'ovr-cancel'; oCancel.textContent = 'cancel';
  editor.append(oInput, oSave, oCancel);
  row.children[2].appendChild(editor);

  const trainBadge = document.createElement('div');
  trainBadge.className = 'train-badge';
  row.children[2].appendChild(trainBadge);

  overrideBtn.addEventListener('click', () => {
    editor.style.display = 'flex';
    overrideBtn.style.display = 'none';
    oInput.value = '';
    oInput.focus();
  });
  oCancel.addEventListener('click', () => {
    editor.style.display = 'none';
    overrideBtn.style.display = '';
  });
  oInput.addEventListener('keydown', e => { if (e.key === 'Enter') oSave.click(); });

  // likely-misread hint: click to override with the neighbour-suggested genre
  const nc = data.neighbor_check;
  if (nc && nc.flag){
    const warn = document.createElement('div');
    warn.className = 'row-flag';
    warn.innerHTML = `⚠ low-confidence — sounds like <b>${escapeHtml(nc.suggested_style)}</b>`;
    warn.title = 'nearest tracks disagree with this read · click to override to the suggestion';
    warn.addEventListener('click', () => {
      overrideBtn.click();                 // opens + clears the editor
      oInput.value = nc.suggested_style;
      oInput.focus();
    });
    row.children[2].appendChild(warn);
  }

  // find this track's result entry so we can use the stored file/filepath
  function getResult(){ return results.find(r => r.row === row); }

  oSave.addEventListener('click', async () => {
    const genre = oInput.value.trim();
    if (!genre) return;

    // 1. update the displayed chip to show the override
    const overrideEl = document.createElement('div');
    const hcol = colorFor(genre);
    const hinfo = styleInfo(genre);
    overrideEl.innerHTML =
      `<div class="genre-src">manual override</div>` +
      `<span class="chip overridden" style="--c:${hcol}" title="manually set">` +
      `<span class="dot ${hinfo.shape}" style="background:${hcol}"></span>${escapeHtml(genre)}</span>`;
    row.children[2].innerHTML = '';
    row.children[2].appendChild(overrideEl);
    row.children[2].appendChild(trainBadge);
    // re-attach recolor delegation
    row.children[2]._recolorBound = false;

    // update results so export uses the override label
    const res = getResult();
    if (res) res.overrideGenre = genre;

    // 2. save to ~/genre_training/<genre>/
    oSave.disabled = true; oSave.textContent = 'saving…';
    try {
      const fd = new FormData();
      fd.append('genre', genre);
      const res2 = getResult();
      if (res2 && res2.filepath){
        // batch mode: send server-side path
        fd.append('filepath', res2.filepath);
        const r = await fetch('/save_training', {method:'POST', body:fd});
        const j = await r.json();
        if (!r.ok) throw new Error(j.error);
        trainBadge.textContent = `✓ saved to ~/genre_training/${escapeHtml(genre)}/`;
      } else if (file){
        // dropped file: re-upload the original file object
        fd.append('file', file);
        const r = await fetch('/save_training', {method:'POST', body:fd});
        const j = await r.json();
        if (!r.ok) throw new Error(j.error);
        trainBadge.textContent = `✓ saved to ~/genre_training/${escapeHtml(genre)}/`;
      } else {
        trainBadge.textContent = '⚠ no file to save (batch without filepath)';
      }
      trainBadge.classList.add('show');
    } catch(err){
      trainBadge.textContent = `⚠ save failed: ${err.message}`;
      trainBadge.classList.add('show');
    }
  });

  /* ---- compare engines: EffNet vs MAEST vs merged (on-demand) ---- */
  const cmpBtn = document.createElement('button');
  cmpBtn.className = 'compare-btn';
  cmpBtn.textContent = '⚖ compare engines';
  cmpBtn.title = 'run MAEST (transformer) alongside EffNet and merge them — slower (~15s), on demand';
  const cmpBox = document.createElement('div');
  cmpBox.className = 'compare-box';
  cmpBox.style.display = 'none';
  row.children[2].appendChild(cmpBtn);
  row.children[2].appendChild(cmpBox);

  cmpBtn.addEventListener('click', async () => {
    if (cmpBox.style.display !== 'none' && cmpBox.dataset.done){   // toggle closed
      cmpBox.style.display = 'none'; cmpBox.dataset.done = ''; return;
    }
    cmpBtn.disabled = true; cmpBtn.textContent = 'running MAEST…';
    cmpBox.style.display = 'block';
    cmpBox.innerHTML = '<div class="cmp-wait">running MAEST transformer (~15s on CPU)…</div>';
    try {
      const fd = new FormData();
      const res = getResult();
      if (res && res.filepath) fd.append('filepath', res.filepath);
      else if (file) fd.append('file', file);
      else { cmpBox.innerHTML = '<div class="cmp-wait">no file available to compare</div>'; return; }
      const r = await fetch('/compare', {method:'POST', body:fd});
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.statusText);
      renderCompareInto(cmpBox, j);
      cmpBox.dataset.done = '1';
    } catch(err){
      cmpBox.innerHTML = `<div class="cmp-wait">compare failed: ${escapeHtml(err.message)}</div>`;
    } finally {
      cmpBtn.disabled = false; cmpBtn.textContent = '⚖ compare engines';
    }
  });

  /* details panel + toggle button */
  const t = (data.tags && data.tags.tag) || {};
  const tech = (data.tags && data.tags.tech) || {};
  const order = ["title","artist","album","albumartist","genre","date","tracknumber","discnumber","composer","bpm"];
  const nice = {albumartist:"album artist", tracknumber:"track", discnumber:"disc", bpm:"bpm (tag)"};
  let dhtml = '';
  const tagRows = order.filter(k => t[k])
    .map(k => `<div>${escapeHtml(nice[k]||k)}: <b>${escapeHtml(t[k])}</b></div>`).join('');
  dhtml += `<div class="sect">File tags</div>` +
           (tagRows || `<div>no tags in this file</div>`);
  dhtml += `<div class="sect">Technical</div>`;
  dhtml += `<div>file: <b>${escapeHtml(data.filename)}</b></div>`;
  for (const k of ["format","bitrate","sample rate","channels"]){
    if (tech[k]) dhtml += `<div>${escapeHtml(k)}: <b>${escapeHtml(tech[k])}</b></div>`;
  }
  const det = document.createElement('div');
  det.className = 'details';
  det.innerHTML = dhtml;
  const btn = document.createElement('button');
  btn.className = 'info'; btn.type = 'button';
  btn.textContent = 'i'; btn.title = 'show file tags & details';
  btn.addEventListener('click', () => det.classList.toggle('open'));
  row.appendChild(btn);
  row.appendChild(det);

  results.push({title:data.title, filename:data.filename, filepath:data.filepath||null,
                hash:data.hash||null,
                styles:styles, row:row, custom:data.custom||null,
                bpm:data.bpm, bpmText:bpmText, camelot:data.camelot,
                key:data.key, scale:data.scale,
                duration:data.duration, ok:true});

  /* cached badge: this analysis came from the DB, no compute happened */
  if (data.cached){
    const t = row.querySelector('.file');
    if (t){
      const tag = document.createElement('span');
      tag.className = 'cached-tag'; tag.textContent = '· cached';
      tag.title = 'previously analyzed -- served instantly from the database';
      t.appendChild(tag);
    }
  }

  /* vibe matches for this track */
  if (data.hash) renderVibeMatches(row, data.hash);

  /* manual tags for this track */
  if (data.hash) renderTags(row, data.hash);
  refreshFooter();
}

function failRow(row, msg){
  row.classList.remove('pending');
  row.classList.add('error');
  row.children[2].innerHTML = `<span class="chip">failed: ${escapeHtml(msg)}</span>`;
  refreshFooter();
}

async function pump(){
  if (busy) return;
  busy = true;
  let known = 0;
  while (queue.length){
    const {file, row} = queue.shift();
    const fd = new FormData();
    fd.append('file', file);
    try{
      const resp = await fetch('/analyze', {method:'POST', body:fd});
      const data = await resp.json();
      if (resp.ok && data.cached){
        // already analyzed (this session or a previous one) -> keep it out of
        // the list entirely. (TODO: make this behaviour configurable later.)
        row.remove();
        known++;
        continue;
      }
      if (!resp.ok) failRow(row, data.error || resp.statusText);
      else finishRow(row, data, file);
    }catch(e){
      failRow(row, 'server unreachable');
    }
  }
  busy = false;
  if (!rowsEl.querySelector('.row')) emptyEl.style.display = '';   // list went empty
  if (known){
    const bs = document.getElementById('batch-status');
    if (bs){
      bs.textContent = `skipped ${known} already analyzed`;
      setTimeout(() => { if (bs.textContent.startsWith('skipped')) bs.textContent = ''; }, 3000);
    }
  }
  refreshFooter();
}

/* dedupe the drop/browse list by name+size, so re-dropping a file already in
   the list is a no-op (the server would cache-hit it anyway; this just avoids a
   redundant row). Cleared by "Clear list". */
const listKeys = new Set();
const fileKey = f => `${f.name}::${f.size}`;

function enqueue(files){
  let skipped = 0;
  for (const f of files){
    const key = fileKey(f);
    if (listKeys.has(key)){ skipped++; continue; }   // already in the list
    listKeys.add(key);
    queue.push({file:f, row:addRow(f)});
  }
  if (skipped){
    const bs = document.getElementById('batch-status');
    if (bs){
      bs.textContent = `skipped ${skipped} duplicate${skipped > 1 ? 's' : ''}`;
      setTimeout(() => { if (bs.textContent.startsWith('skipped')) bs.textContent = ''; }, 2600);
    }
  }
  pump();
}

/* drag & drop -- the whole main window is the drop target */
['dragenter','dragover'].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.add('over');
}));
drop.addEventListener('dragleave', e => {
  // only clear when the cursor actually leaves the window, not its children
  if (!drop.contains(e.relatedTarget)) drop.classList.remove('over');
});
drop.addEventListener('drop', e => {
  e.preventDefault(); drop.classList.remove('over');
  const files = [...e.dataTransfer.files];
  if (files.length) enqueue(files);
});
picker.addEventListener('change', () => {
  if (picker.files.length) enqueue([...picker.files]);
  picker.value = '';
});
// click the empty hero to browse (the Browse button in the footer also opens it)
if (emptyEl) emptyEl.addEventListener('click', () => picker.click());

/* export */
exportB.addEventListener('click', () => {
  const ok = results.filter(r => r.ok);
  const stamp = new Date().toISOString().slice(0,16).replace('T',' ');
  const lines = [
    `Genre v2 export \u2014 ${stamp}`,
    `${ok.length} track${ok.length===1?'':'s'}`,
    ''.padEnd(46,'-'),
    ...ok.map(r => {
      const glist = (r.row && r.row._genreList) ? r.row._genreList : r.styles;
      const blend = r.overrideGenre
        ? `${r.overrideGenre} [manual]`
        : glist.map(s => `${s.style} ${(s.score*100).toFixed(0)}%`).join(' | ');
      const custom = (r.custom && r.custom.length)
        ? '  \u2014  Custom: ' + r.custom.filter(s => s.score >= 0.05).slice(0,4)
            .map(s => `${s.style} ${(s.score*100).toFixed(0)}%`).join(' | ')
        : '';
      const bpm = r.bpm != null ? `${r.bpmText} BPM` : '--- BPM';
      const key = r.camelot ? `${r.camelot} (${r.key} ${(r.scale||'').slice(0,3)})`
                            : (r.key ? `${r.key} ${(r.scale||'').slice(0,3)}` : '---');
      return `${r.title}  \u2014  ${bpm}  \u2014  ${key}  \u2014  ${fmtDur(r.duration)}  \u2014  ${blend}${custom}`;
    }),
    ''
  ];
  const blob = new Blob([lines.join('\n')], {type:'text/plain'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `genre_list_${stamp.replace(/[: ]/g,'-')}.txt`;
  a.click();
  URL.revokeObjectURL(a.href);
});

clearB.addEventListener('click', () => {
  PLAYER.audio.pause();
  PLAYER.audio.removeAttribute('src');
  PLAYER.audio.load();
  if (PLAYER.ctl){ PLAYER.ctl.stopVisual(); PLAYER.ctl = null; }
  while (OBJ_URLS.length) URL.revokeObjectURL(OBJ_URLS.pop());
  results = []; queue = [];
  listKeys.clear();
  rowsEl.querySelectorAll('.row').forEach(r => r.remove());
  emptyEl.style.display = '';
  refreshFooter();
});

/* global lens defaults -- re-apply to every row that hasn't overridden that axis */
/* ---- EQ bars: build the header signature element ---- */
(function(){
  const el = document.getElementById('eq-bars');
  if (!el) return;
  const COLORS = ['#E69F00','#56B4E9','#CC79A7','#009E73','#9B6FD4','#D55E00','#0072B2'];
  for (let i = 0; i < 48; i++){
    const s = document.createElement('span');
    const lo = Math.round(8 + Math.random()*20);
    const hi = Math.round(35 + Math.random()*60);
    const dur = (0.5 + Math.random()*1.2).toFixed(2);
    const del = (Math.random()*1.0).toFixed(2);
    s.style.cssText = `--lo:${lo}%;--hi:${hi}%;--d:${dur}s;--dl:${del}s;` +
      `background:${COLORS[i % COLORS.length]}`;
    el.appendChild(s);
  }
})();

/* ---- Batch folder analysis ---- */
const batchBtn = document.getElementById('batch-btn');
const batchStatus = document.getElementById('batch-status');
let batchRunning = false;

batchBtn.addEventListener('click', () => {
  if (batchRunning){ return; }
  const path = prompt(
    'Enter the WSL path to your music folder:\n(e.g. /mnt/c/Users/you/Music)',
    '/mnt/c/Users/'
  );
  if (!path || !path.trim()) return;
  runBatch(path.trim());
});

async function runBatch(folderPath){
  batchRunning = true;
  batchBtn.classList.add('active');
  batchBtn.textContent = '⏸ running…';
  batchStatus.textContent = 'scanning…';

  try {
    const resp = await fetch('/batch', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: folderPath, workers: 3})
    });
    if (!resp.ok){
      const j = await resp.json().catch(()=>({}));
      alert('Batch error: ' + (j.error || resp.statusText));
      return;
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '', total = 0, done = 0;
    while (true){
      const {value, done: eof} = await reader.read();
      if (eof) break;
      buf += dec.decode(value, {stream:true});
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines){
        if (!line.trim()) continue;
        try {
          const d = JSON.parse(line);
          if (d.total){ total = d.total; batchStatus.textContent = `0 / ${total}`; continue; }
          done = d.progress || done + 1;
          batchStatus.textContent = `${done} / ${total}`;
          if (d.ok){
            // inject into the queue as if the user dropped the file
            // (we have full data, so finishRow directly with no /analyze round-trip)
            const fakeFile = {name: d.filename};
            const row = addRow(fakeFile);
            finishRow(row, d, null);
          } else {
            const row = addRow({name: d.filename});
            failRow(row, d.error || 'failed');
          }
        } catch(e){ /* bad JSON line, skip */ }
      }
    }
    batchStatus.textContent = `✓ ${done} tracks`;
  } catch(err){
    batchStatus.textContent = 'error';
    alert('Batch failed: ' + err.message);
  } finally {
    batchRunning = false;
    batchBtn.classList.remove('active');
    batchBtn.textContent = '⊕ batch folder';
  }
}

const gId = document.getElementById('g-identity');
const gSeg = document.getElementById('g-seg');
gId.value = GLOBAL.identity; gSeg.value = GLOBAL.seg;
gId.addEventListener('change', () => {
  GLOBAL.identity = gId.value;
  for (const r of results){ if (r.ok && r.row && r.row._applyModes && !r.row._idOverride) r.row._applyModes(); }
});
gSeg.addEventListener('change', () => {
  GLOBAL.seg = gSeg.value;
  for (const r of results){ if (r.ok && r.row && r.row._applyModes && !r.row._segOverride) r.row._applyModes(); }
});

/* ---- Sibling editor ---- */
const sibPanel = document.getElementById('sib-panel');
const sibBody  = document.getElementById('sib-body');
document.getElementById('sib-btn').addEventListener('click', () => {
  renderSibEditor(); sibPanel.classList.add('open');
});
document.getElementById('sib-close').addEventListener('click', () => sibPanel.classList.remove('open'));

// chip removals via delegation -- bound ONCE. renderSibEditor() rebuilds sibBody's
// contents but not sibBody itself, so this delegated handler survives re-renders
// (binding it inside renderSibEditor stacked a new listener on every render).
sibBody.addEventListener('click', e => {
  const btn = e.target.closest('.sib-chip button');
  if (!btn) return;
  const canon = btn.dataset.canon, mem = btn.dataset.mem;
  if (!SIBLING_GROUPS[canon]) return;
  SIBLING_GROUPS[canon] = SIBLING_GROUPS[canon].filter(m => m !== mem);
  rebuildSibMap(); renderSibEditor();
});

function rebuildSibMap(){
  // rebuild SIBLING_MAP from SIBLING_GROUPS after any edit
  for (const k in SIBLING_MAP) delete SIBLING_MAP[k];
  for (const canon in SIBLING_GROUPS)
    for (const mem of SIBLING_GROUPS[canon]) SIBLING_MAP[mem.toLowerCase()] = canon;
  // re-apply to all rows using sibling or hyst+sib
  for (const r of results){
    if (r.ok && r.row && r.row._applyModes){
      const seg = r.row._segOverride || GLOBAL.seg;
      if (seg === 'sibling' || seg === 'hyst+sib') r.row._applyModes();
    }
  }
}

function renderSibEditor(){
  sibBody.innerHTML = '';
  for (const canon in SIBLING_GROUPS){
    const group = document.createElement('div');
    group.className = 'sib-group';
    const dot = styleInfo(canon);
    group.innerHTML = `<div class="sib-canon">` +
      `<span class="dot" style="background:${dot.color}"></span>${escapeHtml(canon)}</div>`;
    const chips = document.createElement('div');
    chips.className = 'sib-members';
    for (const mem of SIBLING_GROUPS[canon]){
      const chip = document.createElement('span');
      chip.className = 'sib-chip';
      chip.innerHTML = `${escapeHtml(mem)}` +
        (mem !== canon ? `<button data-canon="${escapeHtml(canon)}" data-mem="${escapeHtml(mem)}" title="remove">✕</button>` : '');
      chips.appendChild(chip);
    }
    // add button
    const addBtn = document.createElement('button');
    addBtn.className = 'sib-add'; addBtn.textContent = '+ add';
    addBtn.addEventListener('click', () => {
      const val = prompt(`Add genre to "${canon}" group:`);
      if (!val || !val.trim()) return;
      const mem = val.trim();
      if (!SIBLING_GROUPS[canon].includes(mem)) SIBLING_GROUPS[canon].push(mem);
      rebuildSibMap(); renderSibEditor();
    });
    chips.appendChild(addBtn);
    group.appendChild(chips);
    sibBody.appendChild(group);
  }
  sibBody.insertAdjacentHTML('beforeend',
    `<p class="sib-note">Click ✕ to remove a genre from a group.<br>
    A genre can only be in one group. The canonical name (bold) is what the waveform shows.<br>
    Changes apply immediately to all tracks using sibling or hyst+sibling mode.</p>`);
  // (chip-removal handler is bound once near the panel setup, not here)
}



/* =========================================================
   TAGS -- manual designations ("high energy", "opener", ...)
   ========================================================= */
let _allTags = null;   // session cache; invalidated on create
async function fetchTags(force){
  if (_allTags && !force) return _allTags;
  const r = await fetch('/tags');
  _allTags = r.ok ? await r.json() : [];
  return _allTags;
}

async function renderTags(row, hash){
  try {
    const [all, mine] = await Promise.all([
      fetchTags(), fetch(`/tags/for/${hash}`).then(r => r.ok ? r.json() : [])
    ]);
    const mineIds = new Set(mine.map(t => t.id));

    let holder = row.querySelector('.tagchips');
    if (!holder){
      holder = document.createElement('div');
      holder.className = 'tagchips';
      row.children[2].appendChild(holder);
    }
    holder.innerHTML = '';

    for (const t of all){
      const chip = document.createElement('button');
      chip.className = 'tagchip' + (mineIds.has(t.id) ? ' on' : '');
      chip.textContent = t.name;
      chip.title = mineIds.has(t.id) ? 'click to remove' : 'click to add';
      chip.addEventListener('click', async () => {
        const r = await fetch('/tags/toggle', {method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({tag_id: t.id, hash})});
        if (r.ok){
          const j = await r.json();
          chip.classList.toggle('on', j.tagged);
          chip.title = j.tagged ? 'click to remove' : 'click to add';
        }
      });
      holder.appendChild(chip);
    }

    const add = document.createElement('button');
    add.className = 'tagchip newtag';
    add.textContent = '+ tag';
    add.title = 'create a new designation';
    add.addEventListener('click', () => {
      // inline input (browsers suppress prompt() after a few dialogs)
      const inp = document.createElement('input');
      inp.className = 'taginput';
      inp.placeholder = 'tag name…';
      inp.title = 'Enter to add · Esc to cancel';
      add.replaceWith(inp);
      inp.focus();
      let done = false;
      const cancel = () => { if (done) return; done = true; inp.replaceWith(add); };
      const commit = async () => {
        if (done) return;
        const name = inp.value.trim();
        if (!name){ cancel(); return; }
        done = true;
        const r = await fetch('/tags', {method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({name})});
        if (r.ok){
          const t = await r.json();
          await fetch('/tags/toggle', {method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({tag_id: t.id, hash})});
          await fetchTags(true);         // refresh the session tag list
        }
        renderTags(row, hash);           // re-render this row's chips
      };
      inp.addEventListener('keydown', e => {
        if (e.key === 'Enter'){ e.preventDefault(); commit(); }
        else if (e.key === 'Escape'){ e.preventDefault(); cancel(); }
      });
      inp.addEventListener('blur', () => { inp.value.trim() ? commit() : cancel(); });
    });
    holder.appendChild(add);
  } catch(e){ /* tags are best-effort; never break the row */ }
}

/* =========================================================
   VIBES
   ========================================================= */
const vibePanel = document.getElementById('vibe-panel');
const vibeBody  = document.getElementById('vibe-body');
document.getElementById('vibe-btn').addEventListener('click', () => {
  renderVibePanel(); vibePanel.classList.add('open');
});
document.getElementById('vibe-close').addEventListener('click',
  () => vibePanel.classList.remove('open'));

async function fetchVibes(){
  const r = await fetch('/vibes');
  return r.ok ? r.json() : [];
}

async function renderVibePanel(){
  const vibes = await fetchVibes();
  vibeBody.innerHTML = '';

  // create form
  const create = document.createElement('div');
  create.className = 'vibe-create';
  create.innerHTML = `<input type="text" placeholder="new vibe name (e.g. Sunset Warmup)">` +
                     `<button>create</button>`;
  const inp = create.querySelector('input');
  create.querySelector('button').addEventListener('click', async () => {
    const name = inp.value.trim();
    if (!name) return;
    const r = await fetch('/vibes', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name})});
    if (r.ok) renderVibePanel();
    else { const j = await r.json(); alert(j.error || 'failed'); }
  });
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') create.querySelector('button').click();
  });
  vibeBody.appendChild(create);

  if (!vibes.length){
    vibeBody.insertAdjacentHTML('beforeend',
      `<p class="sib-note">No vibes yet. Create one, then add tracks to it with the
       "+ vibe" button on any analyzed track. Once a vibe has a few members,
       new tracks show their match % automatically, and "playlist" scans your whole
       analysis database for everything that fits.</p>`);
    return;
  }

  for (const v of vibes){
    const rowEl = document.createElement('div');
    rowEl.className = 'vibe-row';
    rowEl.innerHTML =
      `<span class="vname">${escapeHtml(v.name)}</span>` +
      `<span class="vcount">${v.count} track${v.count===1?'':'s'}</span>` +
      `<button class="vw-btn">weights</button>` +
      `<button class="pl-btn">playlist</button>`;

    // weight editor: a -1..+1 slider per member track (Rocchio feedback)
    const vwWrap = document.createElement('div');
    vwWrap.className = 'vibe-weights';
    vwWrap.style.display = 'none';
    rowEl.querySelector('.vw-btn').addEventListener('click', async () => {
      if (vwWrap.style.display !== 'none'){ vwWrap.style.display = 'none'; return; }
      vwWrap.style.display = '';
      vwWrap.innerHTML = '<div class="vw-track">loading members…</div>';
      const r = await fetch(`/vibes/${v.id}/members`);
      const members = r.ok ? await r.json() : [];
      if (!members.length){
        vwWrap.innerHTML = '<div class="vw-track">no tracks yet — add some with “+ vibe” on a track.</div>';
        return;
      }
      vwWrap.innerHTML = '';
      for (const t of members){
        const d = document.createElement('div');
        d.className = 'vw-track';
        const name = t.title || t.filename || t.hash.slice(0, 10);
        d.innerHTML =
          `<b title="${escapeHtml(name)}">${escapeHtml(name)}</b>` +
          `<input type="range" min="-1" max="1" step="0.1" value="${t.weight}">` +
          `<span class="vw-val"></span>` +
          `<button class="vw-rm" title="remove from vibe">✕</button>`;
        const slider = d.querySelector('input');
        const val = d.querySelector('.vw-val');
        const paint = () => {
          const w = parseFloat(slider.value);
          val.textContent = (w > 0 ? '+' : '') + w.toFixed(1);
          val.className = 'vw-val ' + (w > 0 ? 'pos' : w < 0 ? 'neg' : '');
        };
        paint();
        slider.addEventListener('input', paint);
        slider.addEventListener('change', () => fetch('/vibes/weight', {method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({vibe_id: v.id, hash: t.hash, weight: parseFloat(slider.value)})}));
        d.querySelector('.vw-rm').addEventListener('click', async () => {
          await fetch('/vibes/remove', {method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({vibe_id: v.id, hash: t.hash})});
          d.remove();
        });
        vwWrap.appendChild(d);
      }
    });

    const plWrap = document.createElement('div');
    plWrap.className = 'vibe-playlist';
    plWrap.style.display = 'none';
    rowEl.querySelector('.pl-btn').addEventListener('click', async () => {
      if (plWrap.style.display === 'none'){
        plWrap.style.display = '';
        plWrap.innerHTML = '<div class="pl-track">scanning database…</div>';
        const r = await fetch(`/vibes/${v.id}/playlist?threshold=0.60`);
        if (!r.ok){ const j = await r.json(); plWrap.innerHTML =
          `<div class="pl-track">${escapeHtml(j.error||'failed')}</div>`; return; }
        const tracks = await r.json();
        if (!tracks.length){ plWrap.innerHTML =
          '<div class="pl-track">no matches above 60% in the database yet</div>'; return; }
        plWrap.innerHTML = '';
        for (const t of tracks){
          const d = document.createElement('div');
          d.className = 'pl-track';
          d.innerHTML =
            `<span class="pl-sim">${(t.sim*100).toFixed(0)}%</span>` +
            `<b>${escapeHtml(t.title || t.filename)}</b>` +
            (t.bpm ? `<span>${t.bpm.toFixed(0)} BPM</span>` : '') +
            (t.camelot ? `<span>${escapeHtml(t.camelot)}</span>` : '') +
            (t.member ? `<span class="pl-member">in vibe</span>` : '');
          plWrap.appendChild(d);
        }
        // export button
        const exp = document.createElement('button');
        exp.className = 'pl-btn'; exp.textContent = 'export playlist .txt';
        exp.style.marginTop = '6px';
        exp.addEventListener('click', () => {
          const lines = [`Vibe: ${v.name}`, ''.padEnd(40,'-'),
            ...tracks.map(t => `${(t.sim*100).toFixed(0)}%  ${t.title || t.filename}` +
              (t.bpm ? `  ${t.bpm.toFixed(0)} BPM` : '') +
              (t.camelot ? `  ${t.camelot}` : ''))];
          const blob = new Blob([lines.join('\n')], {type:'text/plain'});
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = `vibe_${v.name.replace(/\W+/g,'_')}.txt`;
          a.click(); URL.revokeObjectURL(a.href);
        });
        plWrap.appendChild(exp);
      } else {
        plWrap.style.display = 'none';
      }
    });
    vibeBody.appendChild(rowEl);
    vibeBody.appendChild(vwWrap);
    vibeBody.appendChild(plWrap);
  }
  vibeBody.insertAdjacentHTML('beforeend',
    `<p class="sib-note">Playlist scans every track ever analyzed (cached in the
     database) and ranks by similarity to the vibe's sonic center.
     Similarity is timbral/rhythmic character from the ML embeddings --
     a strong candidate list, but your ear stays the final filter.</p>`);
}

/* per-row: show which vibes this track matches + add-to-vibe button */
async function renderVibeMatches(row, hash){
  try {
    const prev = row.children[2].querySelector('.vibematches');
    if (prev) prev.remove();            // avoid stacking holders on refresh
    const r = await fetch(`/vibes/match/${hash}`);
    const holder = document.createElement('div');
    holder.className = 'vibematches';
    async function feedback(vid, weight){
      // per-song 👍/👎: sets THIS track's weight inside that vibe (Rocchio),
      // then re-ranks -- 👍 pulls the vibe toward the song, 👎 pushes it away.
      await fetch('/vibes/weight', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({vibe_id: vid, hash, weight})});
      renderVibeMatches(row, hash);
    }
    if (r.ok){
      const matches = (await r.json()).filter(m => m.sim >= 0.55).slice(0, 3);
      if (matches.length){
        const lead = document.createElement('span');
        lead.className = 'vm-lead'; lead.textContent = 'vibes:';
        holder.appendChild(lead);
        matches.forEach((m, i) => {
          const vm = document.createElement('span');
          vm.className = `vibematch rank-${i + 1}`;
          vm.innerHTML = `<b>${escapeHtml(m.name)}</b> ${(m.sim*100).toFixed(0)}%`;
          const up = document.createElement('button');
          up.className = 'vm-thumb'; up.textContent = '👍';
          up.title = `more like this — strengthen "${m.name}"`;
          up.addEventListener('click', () => feedback(m.id, 1.0));
          const down = document.createElement('button');
          down.className = 'vm-thumb'; down.textContent = '👎';
          down.title = `not this — push "${m.name}" away`;
          down.addEventListener('click', () => feedback(m.id, -0.8));
          vm.append(' ', up, down);
          holder.appendChild(vm);
        });
      }
    }
    const addBtn = document.createElement('button');
    addBtn.className = 'addvibe-btn';
    addBtn.textContent = '+ vibe';
    addBtn.title = 'add this track to a vibe';
    async function addToVibe(vid){
      await fetch('/vibes/add', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({vibe_id: vid, hash})});
      renderVibeMatches(row, hash);      // refresh match % (rebuilds the holder)
    }
    addBtn.addEventListener('click', async () => {
      // inline menu (browsers suppress prompt() after a few dialogs)
      const existing = holder.querySelector('.vibe-menu');
      if (existing){ existing.remove(); return; }   // toggle off
      const vibes = await fetchVibes();
      const menu = document.createElement('div');
      menu.className = 'vibe-menu';
      for (const v of vibes){
        const b = document.createElement('button');
        b.className = 'vibe-menu-item';
        b.textContent = v.name;
        b.title = 'add this track to ' + v.name;
        b.addEventListener('click', () => addToVibe(v.id));
        menu.appendChild(b);
      }
      const nv = document.createElement('input');
      nv.className = 'vibe-menu-new';
      nv.placeholder = vibes.length ? '＋ new vibe…' : 'name your first vibe…';
      nv.title = 'Enter to create + add · Esc to close';
      nv.addEventListener('keydown', async e => {
        if (e.key === 'Escape'){ menu.remove(); return; }
        if (e.key !== 'Enter') return;
        const name = nv.value.trim();
        if (!name) return;
        const cr = await fetch('/vibes', {method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({name})});
        if (!cr.ok){ const j = await cr.json().catch(() => ({})); nv.title = j.error || 'failed'; nv.classList.add('err'); return; }
        addToVibe((await cr.json()).id);   // rebuilds holder, menu goes away
      });
      menu.appendChild(nv);
      holder.appendChild(menu);
      nv.focus();
    });
    holder.appendChild(addBtn);
    row._vibeholder = holder;
    row.children[2].appendChild(holder);
  } catch(e){ /* vibe UI is best-effort; never break the row */ }
}

/* render one engine's ranked genre list for the compare panel */
function cmpList(arr){
  if (!arr || !arr.length) return '<span class="cmp-none">—</span>';
  return arr.slice(0, 5).map(s => {
    const col = colorFor(s.style);
    return `<span class="cmp-item"><span class="sw ${styleInfo(s.style).shape}" style="background:${col}"></span>` +
      `<b>${escapeHtml(s.style)}</b> ${(s.score*100).toFixed(0)}%</span>`;
  }).join('');
}
/* top-5 [{style,score}] from the pairs list, scored by fn(pair) */
function topScored(pairs, fn, k){
  return pairs.map(p => ({style:p.style, score:fn(p)}))
              .sort((a,b) => b.score - a.score).slice(0, k || 5);
}
/* build the compare panel into `box`, with a live EffNet↔MAEST weight slider.
   Re-mixing the merge is instant client-side math — MAEST does NOT re-run. */
function renderCompareInto(box, j){
  if (!j.maest_available){
    box.innerHTML = `<div class="cmp-note">MAEST model not installed — showing EffNet only.</div>` +
      `<div class="cmp-col"><div class="cmp-h">EffNet</div>${cmpList(j.effnet)}</div>`;
    return;
  }
  const pairs = j.pairs || [];
  let w = j.weight ?? 0.5;                                    // EffNet share (0..1)
  box.innerHTML =
    `<div class="cmp-grid">` +
      `<div class="cmp-col"><div class="cmp-h">EffNet <span>CNN</span></div>${cmpList(topScored(pairs, p=>p.eff))}</div>` +
      `<div class="cmp-col"><div class="cmp-h">MAEST <span>transformer</span></div>${cmpList(topScored(pairs, p=>p.mae))}</div>` +
      `<div class="cmp-col merged"><div class="cmp-h">Merged <span class="cmp-w"></span></div><div class="cmp-mergedlist"></div></div>` +
    `</div>` +
    `<div class="cmp-slider"><span>EffNet</span>` +
      `<input type="range" class="cmp-range" min="0" max="100" step="5">` +
      `<span>MAEST</span></div>` +
    `<div class="cmp-note"></div>`;
  const range = box.querySelector('.cmp-range');
  const wLab  = box.querySelector('.cmp-w');
  const mList = box.querySelector('.cmp-mergedlist');
  const note  = box.querySelector('.cmp-note');
  const effTop = topScored(pairs, p=>p.eff, 1)[0];
  const maeTop = topScored(pairs, p=>p.mae, 1)[0];
  const agree = effTop && maeTop && effTop.style === maeTop.style;
  function paint(){
    mList.innerHTML = cmpList(topScored(pairs, p => w*p.eff + (1-w)*p.mae, 6));
    wLab.textContent = `${Math.round(w*100)}/${Math.round((1-w)*100)}`;
    note.textContent = agree
      ? `✓ both engines agree on the top genre (${effTop.style})`
      : `⚠ engines disagree — EffNet: ${effTop.style} · MAEST: ${maeTop.style}. The slider blends them.`;
  }
  range.value = Math.round(w*100);
  range.addEventListener('input', () => { w = range.value/100; paint(); });
  paint();
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

/* ===================================================================
   Genre Map -- 3D constellation of the whole scanned library.
   Tracks float in a rotating 3D point-cloud on black; depth drives
   size + brightness. Two layouts (regions / galaxy). Selecting a point
   opens its popup and pulls up a random one of its closest matches.
   Canvas 2D with a hand-rolled perspective projection (no libraries).
   =================================================================== */
(() => {
  const stage    = document.getElementById('map-stage');
  const canvas   = document.getElementById('map-canvas');
  const legendEl = document.getElementById('map-legend');
  const popEl    = document.getElementById('map-pop');
  const searchEl = document.getElementById('map-search');
  const suggestEl= document.getElementById('map-suggest');
  const countMap = document.getElementById('map-count');
  const resetB   = document.getElementById('map-reset');
  const modeEl   = document.getElementById('map-mode');
  const tabsEl   = document.getElementById('tabs');
  if (!tabsEl || !canvas) return;
  const ctx = canvas.getContext('2d');

  let NODES = [], EDGES = [], FAMS = [], COUNTS = {}, CENTROIDS = {}, STYLE_CENTROIDS = {};
  const byHash = new Map();
  let mapMode = 'regions';                 // 'regions' | 'galaxy' | 'tree'
  let TREE = null;                         // {nodes, links, rows} for tree mode
  let selHash = null;
  let simCache = [];                       // last popup's /similar result

  let W = 0, H = 0, DPR = 1;
  const rot = { x: -0.15, y: 0.5 };        // orbit angles
  const view = { zoom: 1, panx: 0, pany: 0 };
  const pivot = { x:0, y:0, z:0 };     // orbit centre: eases toward the selected track
  let spinSpeed = 0.0022, running = false, rafId = null, filterFam = null;
  let edgesOn = true, harmonic = false, flaggedOnly = false;
  const tipEl = document.getElementById('map-tip');
  let anim = null;                         // camera tween
  const proj = new Map();                  // hash -> {sx,sy,z,r} for this frame
  const CAM = 2.7;                         // camera distance (world units)

  /* -- deterministic RNG so a track's spot is stable across reloads -- */
  function rng(seed){
    let h = 1779033703 ^ seed.length;
    for (let i=0;i<seed.length;i++){
      h = Math.imul(h ^ seed.charCodeAt(i), 3432918353); h = (h<<13)|(h>>>19);
    }
    let a = h >>> 0;
    return () => {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a>>>15), 1 | a);
      t = (t + Math.imul(t ^ (t>>>7), 61 | t)) ^ t;
      return ((t ^ (t>>>14)) >>> 0) / 4294967296;
    };
  }
  function hueOf(fam){
    let h = 0; for (let i=0;i<fam.length;i++) h = (h*31 + fam.charCodeAt(i)) | 0;
    return ((h % 360) + 360) % 360;
  }
  const famCss = fam => `hsl(${hueOf(fam)} 62% 62%)`;
  const durfmt = s => { if (s==null) return '--'; s=Math.round(s);
    return Math.floor(s/60)+':'+String(s%60).padStart(2,'0'); };
  const keyfmt = n => n.camelot
    ? `${n.camelot} ${n.key||''} ${(n.scale||'').slice(0,3)}`.trim()
    : (n.key ? `${n.key} ${(n.scale||'').slice(0,3)}`.trim() : '--');

  /* ---- harmonic mixing (Camelot wheel + BPM tolerance) ------------- */
  const camelot = c => { const m = /^(\d{1,2})([AB])$/.exec((c||'').trim().toUpperCase());
    return m ? { n:+m[1], l:m[2] } : null; };
  function keyCompatible(a, b){
    const A = camelot(a), B = camelot(b);
    if (!A || !B) return false;
    if (A.n === B.n) return true;                       // same, or relative maj/min
    const d = Math.abs(A.n - B.n);
    return A.l === B.l && (d === 1 || d === 11);        // ±1 around the 12-hour wheel
  }
  function bpmCompatible(a, b){
    if (a == null || b == null) return true;            // unknown -> don't exclude
    const r = Math.max(a,b) / Math.min(a,b);
    const near = x => Math.abs(x - 1) <= 0.06;          // ±6%
    return near(r) || near(r/2) || near(r*2);           // same, or half / double-time
  }
  const clamp = (v,a,b) => Math.max(a, Math.min(b, v));
  const pctl = (arr,p) => { if(!arr.length) return 1;
    const s=arr.slice().sort((x,y)=>x-y);
    return s[Math.min(s.length-1, Math.floor(p*(s.length-1)))] || 1; };

  /* ---- build 3-D positions for the current mode -------------------- */
  function layout(){
    byHash.clear();
    for (const n of NODES){
      n.fam = familyOf(n.style || n.styles[0] || 'Other') || 'Other';
      n.hue = hueOf(n.fam);
      byHash.set(n.hash, n);
    }
    COUNTS = {};
    for (const n of NODES) COUNTS[n.fam] = (COUNTS[n.fam]||0)+1;
    FAMS = Object.keys(COUNTS).sort((a,b)=>COUNTS[b]-COUNTS[a]);
    const NC = (NODES.find(n=>n.e)||{}).e?.length || 0;

    if (mapMode === 'tree'){
      buildTree();
      buildLegend();
      countMap.textContent = `${NODES.length} tracks · ${FAMS.length} genres · tree`;
      return;
    }

    if (mapMode === 'galaxy'){
      // position == first 3 PCA axes -> a pure 3-D sonic galaxy
      const p = [0,1,2].map(j => pctl(NODES.map(n=>Math.abs(n.e?n.e[j]:0)),0.96)||1);
      for (const n of NODES){
        const r = rng(n.hash);
        n.x3 = clamp((n.e?n.e[0]:0)/p[0],-1.2,1.2) + (r()-0.5)*0.04;
        n.y3 = clamp((n.e?n.e[1]:0)/p[1],-1.2,1.2) + (r()-0.5)*0.04;
        n.z3 = clamp((n.e?n.e[2]:0)/p[2],-1.2,1.2) + (r()-0.5)*0.04;
        n.ph = r()*6.28;
      }
    } else {
      // regions: biggest family at the core, the rest on a Fibonacci sphere;
      // members offset in 3-D by their 3 highest within-family-variance axes.
      const anchors = {};
      const big = FAMS[0];
      anchors[big] = { x:0, y:0, z:0 };
      const rest = FAMS.slice(1);
      rest.forEach((f,i) => {
        const k = i + 0.5;
        const phi = Math.acos(1 - 2*k/rest.length);
        const th  = Math.PI * (1 + Math.sqrt(5)) * k;
        anchors[f] = { x:0.82*Math.cos(th)*Math.sin(phi),
                       y:0.82*Math.sin(th)*Math.sin(phi),
                       z:0.82*Math.cos(phi) };
      });
      // sub-clusters: within each family, group tracks by dominant style
      // (subgenre) and give each style its own sub-anchor on a small sphere
      // around the family anchor -- so Dubstep and Drum n Bass separate visibly
      // inside Bass Music instead of blending together.
      const styleAnchors = {}, styleCount = {};
      for (const f of FAMS){
        const cnt = {};
        for (const n of NODES) if (n.fam===f){ const s=n.style||f; cnt[s]=(cnt[s]||0)+1; }
        const styles = Object.keys(cnt).sort((a,b)=>cnt[b]-cnt[a]);
        const famSpread = 0.07 + Math.sqrt(COUNTS[f]) * 0.019;
        const subR = famSpread * (styles.length>1 ? 0.85 : 0);
        styles.forEach((s,i) => {
          styleCount[`${f}||${s}`] = cnt[s];
          const a = anchors[f];
          if (styles.length===1){ styleAnchors[`${f}||${s}`] = {x:a.x,y:a.y,z:a.z}; return; }
          const k = i + 0.5;
          const phi = Math.acos(1 - 2*k/styles.length);
          const th  = Math.PI * (1 + Math.sqrt(5)) * k;
          styleAnchors[`${f}||${s}`] = {
            x: a.x + subR*Math.cos(th)*Math.sin(phi),
            y: a.y + subR*Math.sin(th)*Math.sin(phi),
            z: a.z + subR*Math.cos(phi),
          };
        });
      }
      // per-family: pick 3 highest-variance components + their p90 spreads
      const fa = {};
      for (const f of FAMS){
        const mem = NODES.filter(n=>n.fam===f && n.e);
        const mean = new Array(NC).fill(0), varc = new Array(NC).fill(0);
        for (const n of mem) for (let j=0;j<NC;j++) mean[j]+=n.e[j];
        for (let j=0;j<NC;j++) mean[j]/=(mem.length||1);
        for (const n of mem) for (let j=0;j<NC;j++) varc[j]+=(n.e[j]-mean[j])**2;
        const ord = varc.map((v,j)=>[v,j]).sort((a,b)=>b[0]-a[0]);
        const ax = ord.map(o=>o[1]).slice(0,3);
        while (ax.length<3) ax.push(ax[0]||0);
        const pc = ax.map(j => pctl(mem.map(n=>Math.abs(n.e[j]-mean[j])),0.90));
        fa[f] = { ax, mean, pc };
      }
      for (const n of NODES){
        const s = n.style || n.fam;
        const a = styleAnchors[`${n.fam}||${s}`] || anchors[n.fam];
        const f = fa[n.fam];
        const famSpread = 0.07 + Math.sqrt(COUNTS[n.fam]) * 0.019;
        // tight sub-cluster so subgenres stay distinct
        const spread = Math.min(famSpread*0.42, 0.03 + Math.sqrt(styleCount[`${n.fam}||${s}`]||1)*0.011);
        const r = rng(n.hash);
        const loc = j => n.e && f ? clamp((n.e[f.ax[j]]-f.mean[f.ax[j]])/f.pc[j], -1.15, 1.15)
                                  : (r()-0.5);
        n.x3 = a.x + loc(0)*spread + (r()-0.5)*0.012;
        n.y3 = a.y + loc(1)*spread + (r()-0.5)*0.012;
        n.z3 = a.z + loc(2)*spread + (r()-0.5)*0.012;
        n.ph = r()*6.28;
      }
    }
    // family label anchors = member centroid (3-D)
    CENTROIDS = {};
    const acc = {}; for (const f of FAMS) acc[f] = {x:0,y:0,z:0,c:0};
    for (const n of NODES){ const a=acc[n.fam]; a.x+=n.x3; a.y+=n.y3; a.z+=n.z3; a.c++; }
    for (const f of FAMS){ const a=acc[f];
      CENTROIDS[f] = { x:a.x/a.c, y:a.y/a.c, z:a.z/a.c, n:COUNTS[f] }; }

    // subgenre label anchors (regions only): centroid of each style sub-cluster
    // with >=4 members -- these fade in as you zoom in (semantic zoom / LOD).
    STYLE_CENTROIDS = {};
    if (mapMode !== 'galaxy'){
      // label threshold scales with library size: a subgenre earns a label once
      // it has ~1/90th of the library (min 2, max 12) -- so a small library
      // surfaces subgenres eagerly, a large one stays uncluttered.
      const labelMin = Math.max(2, Math.min(12, Math.round(NODES.length / 90)));
      const sacc = {};
      for (const n of NODES){
        const key = `${n.fam}||${n.style || n.fam}`;
        if (!sacc[key]) sacc[key] = { x:0, y:0, z:0, c:0, style:n.style || n.fam };
        const a = sacc[key]; a.x+=n.x3; a.y+=n.y3; a.z+=n.z3; a.c++;
      }
      for (const key in sacc){ const a = sacc[key];
        if (a.c >= labelMin) STYLE_CENTROIDS[key] = { x:a.x/a.c, y:a.y/a.c, z:a.z/a.c, n:a.c, style:a.style }; }
    }

    buildLegend();
    countMap.textContent = `${NODES.length} tracks · ${FAMS.length} genres · ${mapMode}`;
  }

  function buildLegend(){
    legendEl.innerHTML = FAMS.map(f =>
      `<span class="leg" data-fam="${escapeHtml(f)}"><span class="dot" style="background:${famCss(f)}"></span>${escapeHtml(f)} ${COUNTS[f]}</span>`
    ).join('');
    // click a legend chip to filter to that genre (click again to clear)
    legendEl.querySelectorAll('.leg').forEach(el =>
      el.onclick = () => { const f = el.getAttribute('data-fam'); applyFilter(filterFam === f ? null : f); });
    // (re)populate the filter dropdown, preserving the current choice
    const fe = document.getElementById('map-filter');
    if (fe){
      const cur = fe.value;
      fe.innerHTML = `<option value="">all genres</option>`
        + `<option value="__flagged__">⚠ likely misreads</option>`
        + FAMS.map(f => `<option value="${escapeHtml(f)}">${escapeHtml(f)} · ${COUNTS[f]}</option>`).join('');
      fe.value = (cur === '__flagged__' || FAMS.includes(cur)) ? cur : '';
    }
  }

  /* ---- organic left-to-right genre tree (root -> families -> subgenres) ---
     A taxonomy of the library (à la pulse.roots / ishkur). Branch LENGTH grows
     with track count -> big branches reach farther before fanning out, so the
     ends aren't a straight column. */
  function buildTree(){
    const groups = {};
    for (const n of NODES){
      const fam = n.fam, sub = n.style || fam;
      (groups[fam] ||= { subs:{}, count:0 });
      groups[fam].subs[sub] = (groups[fam].subs[sub]||0) + 1;
      groups[fam].count++;
    }
    const famNames = Object.keys(groups).sort((a,b)=>groups[b].count-groups[a].count);
    const maxFam = Math.max(1, ...famNames.map(f=>groups[f].count));
    let maxSub = 1;
    for (const f of famNames) for (const s in groups[f].subs) maxSub = Math.max(maxSub, groups[f].subs[s]);
    const famLen = c => 0.7 + 1.9 * Math.sqrt(c / maxFam);   // root -> family reach
    const subLen = c => 0.5 + 1.8 * Math.sqrt(c / maxSub);   // family -> subgenre reach

    const nodes = [], links = [];
    let row = 0, maxX = 0;
    for (const fam of famNames){
      const g = groups[fam];
      const subNames = Object.keys(g.subs).sort((a,b)=>g.subs[b]-g.subs[a]);
      const famX = famLen(g.count);
      const subInfo = [];
      for (const sub of subNames){
        const y = row++, sx = famX + subLen(g.subs[sub]);
        maxX = Math.max(maxX, sx);
        subInfo.push({ sub, y, sx, count:g.subs[sub] });
      }
      const fy = subInfo.reduce((a,b)=>a+b.y,0) / subInfo.length;
      nodes.push({ kind:'fam', label:fam, fam, x:famX, y:fy, count:g.count });
      links.push([0, 0, famX, fy, fam]);                 // root -> family (root y = centre)
      for (const si of subInfo){
        nodes.push({ kind:'sub', label:si.sub, fam, x:si.sx, y:si.y, count:si.count });
        links.push([famX, fy, si.sx, si.y, fam]);        // family -> subgenre
      }
      row += 0.9;                                        // gap between families
    }
    const off = row / 2;                                 // centre vertically
    for (const nd of nodes) nd.y -= off;
    for (const l of links){ l[1] -= (l[0]===0 ? 0 : off); l[3] -= off; }
    nodes.push({ kind:'root', fam:null, x:0, y:0, count:NODES.length });
    TREE = { nodes, links, rows: row, maxX, rowPx: 0 };
  }
  function fitTree(){
    if (!TREE) return;
    const rows = TREE.rows;
    TREE.rowPx = Math.max(15, (H - 130) / Math.max(1, rows));   // readable row height
    view.zoom = 1; view.panx = 0;
    view.pany = 96 - H/2 + (rows/2) * TREE.rowPx;               // start at the top
  }
  function renderTree(){
    ctx.clearRect(0,0,W,H); ctx.fillStyle='#000'; ctx.fillRect(0,0,W,H);
    if (!TREE) return;
    if (!TREE.rowPx) fitTree();
    const cx = W/2 + view.panx, cy = H/2 + view.pany;
    const spanX = TREE.maxX || 1;
    const SPX = (W * 0.66) / spanX * view.zoom;
    const SX = wx => cx + (wx - spanX/2) * SPX;
    const SY = wy => cy + wy * TREE.rowPx * view.zoom;
    const show = fam => (!filterFam || !fam || fam === filterFam);
    // links (curved, coloured by family)
    ctx.lineWidth = 1.3;
    for (const l of TREE.links){
      if (!show(l[4])) continue;
      const x1=SX(l[0]), y1=SY(l[1]), x2=SX(l[2]), y2=SY(l[3]), mx=(x1+x2)/2;
      ctx.strokeStyle = `hsla(${hueOf(l[4])} 48% 55% / 0.4)`;
      ctx.beginPath(); ctx.moveTo(x1,y1); ctx.bezierCurveTo(mx,y1,mx,y2,x2,y2); ctx.stroke();
    }
    // nodes + labels
    proj.clear();
    ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
    for (const nd of TREE.nodes){
      if (!show(nd.fam)) continue;
      const sx = SX(nd.x), sy = SY(nd.y);
      if (nd.kind === 'root'){
        ctx.beginPath(); ctx.arc(sx, sy, 5, 0, 6.2832);
        ctx.fillStyle = 'rgba(200,210,225,0.85)'; ctx.fill();
        continue;
      }
      const isFam = nd.kind === 'fam';
      const r = isFam ? 6 + Math.min(12, Math.sqrt(nd.count)) : 3.5 + Math.min(8, Math.sqrt(nd.count)*0.9);
      ctx.beginPath(); ctx.arc(sx, sy, r, 0, 6.2832);
      ctx.fillStyle = isFam ? famCss(nd.fam) : `hsl(${hueOf(nd.fam)} 48% 56%)`; ctx.fill();
      ctx.textAlign = isFam ? 'right' : 'left';   // fam labels left, sub labels right
      const lx = isFam ? sx - r - 6 : sx + r + 6;
      const label = `${nd.label} ${nd.count}`;
      ctx.font = isFam ? '800 15px Syne, sans-serif' : "500 11px 'JetBrains Mono', monospace";
      ctx.lineWidth = 3.5; ctx.strokeStyle = 'rgba(0,0,0,0.92)';
      ctx.strokeText(label, lx, sy);
      ctx.fillStyle = isFam ? '#e9eef7' : '#aeb8ca';
      ctx.fillText(label, lx, sy);
    }
  }

  /* ---- canvas sizing ----------------------------------------------- */
  function resize(){
    DPR = Math.min(2, window.devicePixelRatio || 1);
    W = stage.clientWidth; H = stage.clientHeight;
    canvas.width = Math.round(W*DPR); canvas.height = Math.round(H*DPR);
    canvas.style.width = W+'px'; canvas.style.height = H+'px';
    ctx.setTransform(DPR,0,0,DPR,0,0);
  }

  /* ---- render one frame -------------------------------------------- */
  function frame(now){
    if (!running) return;
    const t = now/1000;
    if (anim){
      const p = Math.min(1,(now-anim.t0)/anim.dur), e = p<.5?4*p*p*p:1-Math.pow(-2*p+2,3)/2;
      rot.x = anim.f.rx + (anim.t.rx-anim.f.rx)*e;
      rot.y = anim.f.ry + (anim.t.ry-anim.f.ry)*e;
      view.zoom = anim.f.z + (anim.t.z-anim.f.z)*e;
      view.panx = anim.f.px + (anim.t.px-anim.f.px)*e;
      view.pany = anim.f.py + (anim.t.py-anim.f.py)*e;
      if (p>=1) anim = null;
    }
    if (mapMode === 'tree'){ renderTree(); rafId = requestAnimationFrame(frame); return; }
    if (spinSpeed > 0 && !anim && !dragging) rot.y += spinSpeed;   // idle orbit
    // orbit pivots around the selected track (eased); origin when nothing selected
    const sel = selHash ? byHash.get(selHash) : null;
    pivot.x += ((sel?sel.x3:0) - pivot.x) * 0.12;
    pivot.y += ((sel?sel.y3:0) - pivot.y) * 0.12;
    pivot.z += ((sel?sel.z3:0) - pivot.z) * 0.12;
    const cy=Math.cos(rot.y), sy=Math.sin(rot.y), cx=Math.cos(rot.x), sx=Math.sin(rot.x);
    const DISP = Math.min(W,H)*0.40*view.zoom;
    const cxp = W/2 + view.panx, cyp = H/2 + view.pany;

    // project every node
    proj.clear();
    const order = [];
    for (const n of NODES){
      if (filterFam && n.fam !== filterFam) continue;   // genre filter
      if (flaggedOnly && !n.flag) continue;             // "likely misreads" filter
      const ax = n.x3-pivot.x, ay = n.y3-pivot.y, az = n.z3-pivot.z;
      const x =  ax*cy + az*sy;
      const z = -ax*sy + az*cy;
      const y2 = ay*cx - z*sx;
      const z2 = ay*sx + z*cx;              // depth: bigger = nearer
      const dist = CAM - z2;
      if (dist < 0.15) continue;            // clipped behind the camera
      const persp = CAM / dist;
      const sxp = cxp + x*persp*DISP;
      const syp = cyp + y2*persp*DISP;
      const depth = clamp((z2+1.15)/2.3, 0, 1);
      const r = clamp(4.2*persp*Math.sqrt(view.zoom), 1.2, 46);
      proj.set(n.hash, { sx:sxp, sy:syp, z:z2, r, depth, node:n });
      order.push(n.hash);
    }
    order.sort((a,b)=> proj.get(a).z - proj.get(b).z);   // far -> near

    ctx.clearRect(0,0,W,H);
    ctx.fillStyle = '#000'; ctx.fillRect(0,0,W,H);

    // edges (unless hidden; selection's own web always shows)
    if (edgesOn || selHash){
      ctx.lineWidth = 1;
      for (const ed of EDGES){
        const a = proj.get(ed.a), b = proj.get(ed.b);
        if (!a || !b) continue;
        const hot = selHash && (ed.a===selHash||ed.b===selHash);
        if (!edgesOn && !hot) continue;             // hidden: only the selection's web
        let op;
        if (selHash) op = hot ? 0.85 : 0.04;
        else op = 0.05 + 0.11*Math.min(a.depth,b.depth);
        if (op < 0.02) continue;
        ctx.strokeStyle = hot ? `rgba(86,180,233,${op})` : `rgba(120,135,165,${op})`;
        ctx.beginPath(); ctx.moveTo(a.sx,a.sy); ctx.lineTo(b.sx,b.sy); ctx.stroke();
      }
    }

    // Labels with semantic zoom (LOD): family names when zoomed out, subgenre
    // names fading in as you zoom in. Galaxy mode has no hierarchy -> no fade.
    ctx.textAlign='center'; ctx.textBaseline='middle';
    const lod = (mapMode==='galaxy') ? 0 : clamp((view.zoom - 1.5) / (3.0 - 1.5), 0, 1);
    const projPt = c => {
      const ax = c.x-pivot.x, ay = c.y-pivot.y, az = c.z-pivot.z;
      const x = ax*cy + az*sy, z = -ax*sy + az*cy;
      const y2 = ay*cx - z*sx, z2 = ay*sx + z*cx;
      const persp = CAM/(CAM - z2);
      return { sx: cxp + x*persp*DISP, sy: cyp + y2*persp*DISP, z2, persp };
    };
    // labels get a dark halo (stroke) so they stay legible over dense clusters
    ctx.lineJoin = 'round';
    const drawLabel = (text, p, fs, fill) => {
      ctx.font = fs;
      ctx.lineWidth = 3.5;
      ctx.strokeStyle = 'rgba(0,0,0,0.9)';
      ctx.strokeText(text, p.sx, p.sy);
      ctx.fillStyle = fill;
      ctx.fillText(text, p.sx, p.sy);
    };
    // family labels -- fade back (but never fully vanish) as we zoom in
    for (const f of FAMS){
      if (filterFam && f !== filterFam) continue;
      const p = projPt(CENTROIDS[f]);
      if (p.persp <= 0) continue;
      const depth = clamp((p.z2+1.15)/2.3, 0, 1);
      const a = (0.34 + 0.22*depth) * (1 - 0.66*lod);
      if (a < 0.03) continue;
      drawLabel(f.toUpperCase(), p,
        `800 ${Math.min(46, 15 + CENTROIDS[f].n*1.1) * p.persp}px Syne, sans-serif`,
        `rgba(230,236,246,${a})`);
    }
    // subgenre labels -- fade in with zoom (tinted + mono, smaller)
    if (lod > 0.01){
      for (const key in STYLE_CENTROIDS){
        if (filterFam && !key.startsWith(filterFam + '||')) continue;
        const c = STYLE_CENTROIDS[key], p = projPt(c);
        if (p.persp <= 0) continue;
        const depth = clamp((p.z2+1.15)/2.3, 0, 1);
        const a = (0.42 + 0.28*depth) * lod;
        drawLabel(c.style.toUpperCase(), p,
          `700 ${Math.min(24, 9 + c.n*0.45) * p.persp}px 'JetBrains Mono', monospace`,
          `rgba(150,225,255,${a})`);
      }
    }

    // nodes far -> near
    const selNode = selHash ? byHash.get(selHash) : null;
    const harmonicOn = harmonic && selNode;
    for (const h of order){
      const p = proj.get(h), n = p.node;
      const tw = 0.9 + 0.1*Math.sin(t*1.6 + n.ph);
      const light = (26 + 44*p.depth) * tw;
      let compatible = false, dim = 1;
      if (harmonicOn && h !== selHash){         // harmonic mixing: mute non-matches
        compatible = keyCompatible(selNode.camelot, n.camelot) && bpmCompatible(selNode.bpm, n.bpm);
        dim = compatible ? 1 : 0.1;
      }
      ctx.globalAlpha = (0.45 + 0.55*p.depth) * dim;
      ctx.beginPath(); ctx.arc(p.sx, p.sy, p.r, 0, 6.2832);
      ctx.fillStyle = `hsl(${n.hue} 64% ${clamp(light,18,82)}%)`;
      ctx.fill();
      if (compatible){                          // key + BPM compatible -> teal ring
        ctx.globalAlpha = 0.5 + 0.5*p.depth;
        ctx.beginPath(); ctx.arc(p.sx, p.sy, p.r+2.5, 0, 6.2832);
        ctx.lineWidth = 1.6; ctx.strokeStyle = 'rgba(80,224,180,0.95)'; ctx.stroke();
      }
      if (n.flag){                              // likely misread -> amber ring
        ctx.globalAlpha = 0.5 + 0.5*p.depth;
        ctx.beginPath(); ctx.arc(p.sx, p.sy, p.r+2.5, 0, 6.2832);
        ctx.lineWidth = 1.5; ctx.strokeStyle = 'rgba(255,176,59,0.95)'; ctx.stroke();
      }
      if (h === selHash){
        ctx.globalAlpha = 1;
        ctx.beginPath(); ctx.arc(p.sx, p.sy, p.r+3.5, 0, 6.2832);
        ctx.lineWidth = 2; ctx.strokeStyle = '#fff'; ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;
    rafId = requestAnimationFrame(frame);
  }
  function startLoop(){ if(!running){ running=true; rafId=requestAnimationFrame(frame); } }
  function stopLoop(){ running=false; if(rafId) cancelAnimationFrame(rafId); rafId=null; }

  /* ---- interaction: orbit / zoom / click --------------------------- */
  let dragging=false, moved=false, lx=0, ly=0;
  canvas.addEventListener('pointerdown', e => {
    dragging=true; moved=false; lx=e.clientX; ly=e.clientY;
    canvas.classList.add('grabbing'); canvas.setPointerCapture(e.pointerId); anim=null;
  });
  canvas.addEventListener('pointermove', e => {
    if (dragging){
      const dx=e.clientX-lx, dy=e.clientY-ly; lx=e.clientX; ly=e.clientY;
      if (Math.abs(dx)+Math.abs(dy) > 2) moved=true;
      if (mapMode === 'tree'){ view.panx += dx; view.pany += dy; }
      else { rot.y += dx*0.006; rot.x = clamp(rot.x + dy*0.006, -1.3, 1.3); }
      return;
    }
    // hover tooltip: nearest node under the cursor
    if (!tipEl) return;
    const r = canvas.getBoundingClientRect();
    const mx = e.clientX-r.left, my = e.clientY-r.top;
    let best=null, bz=-Infinity;
    for (const [, p] of proj){
      if (Math.hypot(mx-p.sx, my-p.sy) <= p.r+4 && p.z>bz){ bz=p.z; best=p; }
    }
    if (best){
      const n = best.node;
      tipEl.innerHTML = `<b>${escapeHtml(n.title)}</b><span>${escapeHtml(n.style||'?')} · `
        + `${n.bpm?Math.round(n.bpm):'--'} bpm · ${escapeHtml(n.camelot||'--')}</span>`;
      tipEl.style.left = Math.min(mx+14, W-220)+'px';
      tipEl.style.top  = (my+14)+'px';
      tipEl.hidden = false;
      canvas.style.cursor = 'pointer';
    } else if (!tipEl.hidden){ tipEl.hidden = true; canvas.style.cursor = ''; }
  });
  canvas.addEventListener('pointerleave', () => { if (tipEl) tipEl.hidden = true; });
  const endDrag = e => { dragging=false; canvas.classList.remove('grabbing');
    try{ canvas.releasePointerCapture(e.pointerId); }catch(_){} };
  canvas.addEventListener('pointerup', e => {
    endDrag(e);
    if (!moved){                                   // treat as click -> hit test
      const r = canvas.getBoundingClientRect();
      const mx = e.clientX-r.left, my = e.clientY-r.top;
      let best=null, bz=-Infinity;
      for (const [h,p] of proj){
        if (Math.hypot(mx-p.sx, my-p.sy) <= p.r+5 && p.z>bz){ bz=p.z; best=h; }
      }
      if (best) selectNode(best); else closePopup();
    }
  });
  canvas.addEventListener('pointercancel', endDrag);
  canvas.addEventListener('wheel', e => {
    e.preventDefault(); anim=null;
    const r = canvas.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const nz = clamp(view.zoom * (e.deltaY<0 ? 1.14 : 1/1.14), 0.3, 60);
    const f = nz / view.zoom;                          // actual factor after clamp
    // keep the point under the cursor fixed -> zoom into wherever you're looking
    view.panx = mx - (mx - (W/2 + view.panx)) * f - W/2;
    view.pany = my - (my - (H/2 + view.pany)) * f - H/2;
    view.zoom = nz;
  }, { passive:false });

  /* ---- select + camera fly ----------------------------------------- */
  function selectNode(hash){
    const n = byHash.get(hash); if (!n) return;
    selHash = hash;
    if (mapMode !== 'tree'){
      // the pivot eases to this track (frame loop), so it becomes the orbit
      // centre. keep the rotation, just zoom in a bit and recentre the view.
      anim = { f:{rx:rot.x,ry:rot.y,z:view.zoom,px:view.panx,py:view.pany},
               t:{rx:rot.x, ry:rot.y, z:Math.max(1.9,view.zoom), px:0, py:0},
               t0:performance.now(), dur:600 };
    }
    openPopup(n);
  }

  async function openPopup(n){
    const meta = [
      n.style ? `<span><b>${escapeHtml(n.style)}</b> ${(n.score*100).toFixed(0)}%</span>` : '',
      n.bpm!=null ? `<span><b>${Math.round(n.bpm)}</b> bpm</span>` : '',
      `<span>${escapeHtml(keyfmt(n))}</span>`,
      `<span>${durfmt(n.duration)}</span>`,
    ].join('');
    const other = (n.styles||[]).filter(s=>s && s!==n.style);
    popEl.innerHTML = `
      <button class="pop-x" title="close">close ✕</button>
      <span class="pop-fam" style="background:${famCss(n.fam)}">${escapeHtml(n.fam)}</span>
      <div class="pop-title">${escapeHtml(n.title)}</div>
      ${n.artist ? `<div class="pop-artist">${escapeHtml(n.artist)}</div>` : ''}
      <div class="pop-meta">${meta}</div>
      ${n.flag ? `<div class="pop-flag">⚠ low-confidence read — its closest neighbours sound like
        <b>${escapeHtml(n.suggest || '?')}</b></div>` : ''}
      <div id="pop-pick"><div class="pop-bar" style="font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--dim)">finding a match…</div></div>
      ${other.length ? `<div class="pop-h">also reads as</div>
        <div class="pop-artists">${other.map(s=>`<span class="chip">${escapeHtml(s)}</span>`).join('')}</div>` : ''}
      <div class="pop-h">similar artists</div>
      <div class="pop-artists" id="pop-artists"><span class="pop-bar">…</span></div>
      <div class="pop-h">similar tracks</div>
      <div class="pop-sim" id="pop-sim"><span class="pop-bar">…</span></div>
      <div class="pop-omit-row">
        <button class="pop-omit" title="delete this track's analysis (audio file untouched)">✕ omit from library</button>
      </div>`;
    popEl.hidden = false;
    popEl.querySelector('.pop-x').onclick = closePopup;
    popEl.querySelector('.pop-omit').onclick = () => omitTrack(n);
    try{
      simCache = await fetch(`/similar/${n.hash}?k=12`).then(r=>r.ok?r.json():[]);
    }catch(_){ simCache = []; }
    renderPick();
    renderSimilar(simCache);
  }

  // omit/forget: delete this track's analysis from the DB and drop it off the map
  async function omitTrack(n){
    if (!window.confirm(
      `Remove "${n.title}" from your library?\n\n` +
      `This deletes its analysis (genre, BPM, key) and takes it off the map. ` +
      `Your audio file is NOT touched — re-scanning it will analyze it fresh.`)) return;
    try{ await fetch(`/forget/${n.hash}`, {method:'POST'}); }catch(_){ /* still drop it locally */ }
    NODES = NODES.filter(x => x.hash !== n.hash);
    EDGES = EDGES.filter(e => e.a !== n.hash && e.b !== n.hash);
    closePopup();
    if (NODES.length) layout();   // recompute clusters/labels/legend without it
  }

  // "pull up a random song among the ones that match it the most"
  function renderPick(){
    const box = document.getElementById('pop-pick'); if (!box) return;
    const top = simCache.slice(0, 6);
    if (!top.length){ box.innerHTML = ''; return; }
    const pick = top[Math.floor((performance.now()*13 % 997)/997 * top.length) % top.length];
    const nm = pick.artist ? `${pick.artist} – ${stripArtist(pick.title,pick.artist)}` : pick.title;
    box.innerHTML = `
      <div class="pop-pick" title="jump to this match">
        <div class="pk-top">🎲 a match for you
          <button class="pk-roll" title="another">⟳</button></div>
        <div class="pk-name">${escapeHtml(nm)}</div>
        <div class="pk-sub"><span>${escapeHtml(pick.style||'')}</span>
          <span class="pct">${(pick.sim*100).toFixed(0)}% match</span></div>
      </div>`;
    box.querySelector('.pop-pick').onclick = ev => {
      if (ev.target.closest('.pk-roll')) return;
      if (byHash.has(pick.hash)) selectNode(pick.hash);
    };
    box.querySelector('.pk-roll').onclick = ev => { ev.stopPropagation(); renderPick(); };
  }

  function renderSimilar(sim){
    const simEl = document.getElementById('pop-sim');
    const artEl = document.getElementById('pop-artists');
    if (!simEl) return;
    if (!sim.length){ simEl.innerHTML='<span class="pop-bar">no other tracks yet</span>';
                      if(artEl) artEl.innerHTML='<span class="pop-bar">--</span>'; return; }
    simEl.innerHTML = sim.slice(0,8).map(s => {
      const fam = familyOf(s.style||'') || 'Other';
      const nm = s.artist ? `${s.artist} – ${stripArtist(s.title,s.artist)}` : s.title;
      return `<div class="sim-row" data-h="${s.hash}">
        <span class="dot" style="background:${famCss(fam)}"></span>
        <span class="nm" title="${escapeHtml(s.title)}">${escapeHtml(nm)}</span>
        <span class="pct">${(s.sim*100).toFixed(0)}%</span></div>`;
    }).join('');
    simEl.querySelectorAll('.sim-row').forEach(row =>
      row.onclick = () => { const h=row.getAttribute('data-h'); if (byHash.has(h)) selectNode(h); });
    const seen=new Set(), artists=[];
    for (const s of sim){ const a=(s.artist||'').trim();
      if (a && !seen.has(a.toLowerCase())){ seen.add(a.toLowerCase()); artists.push(a); }
      if (artists.length>=6) break; }
    if (artEl) artEl.innerHTML = artists.length
      ? artists.map(a=>`<span class="chip">${escapeHtml(a)}</span>`).join('')
      : '<span class="pop-bar">--</span>';
  }
  const stripArtist = (title, artist) =>
    (artist && title.toLowerCase().startsWith(artist.toLowerCase()+' - '))
      ? title.slice(artist.length+3) : title;

  function closePopup(){
    popEl.hidden = true; selHash = null;
  }

  /* ---- search ------------------------------------------------------ */
  let sugItems=[], sugIdx=-1;
  function runSearch(q){
    q = q.trim().toLowerCase();
    if (!q){ suggestEl.hidden = true; return; }
    const hits = NODES.filter(n =>
      (`${n.title} ${n.artist} ${n.fam} ${n.style||''}`).toLowerCase().includes(q)
    ).slice(0,8);
    sugItems = hits; sugIdx = -1;
    if (!hits.length){ suggestEl.innerHTML=`<div class="sug">no match</div>`; suggestEl.hidden=false; return; }
    suggestEl.innerHTML = hits.map((n,i) =>
      `<div class="sug" data-i="${i}">
         <span class="dot" style="background:${famCss(n.fam)}"></span>
         <span>${escapeHtml(n.title)}</span>
         <span class="st">${escapeHtml(n.fam)}</span></div>`).join('');
    suggestEl.hidden = false;
    suggestEl.querySelectorAll('.sug').forEach(d =>
      d.onclick = () => choose(+d.getAttribute('data-i')));
  }
  function choose(i){ const n=sugItems[i]; if(!n) return;
    suggestEl.hidden=true; searchEl.value=n.title; selectNode(n.hash); }
  searchEl && searchEl.addEventListener('input', e => runSearch(e.target.value));
  searchEl && searchEl.addEventListener('keydown', e => {
    if (suggestEl.hidden) return;
    if (e.key==='ArrowDown'||e.key==='ArrowUp'){ e.preventDefault();
      sugIdx=(sugIdx+(e.key==='ArrowDown'?1:-1)+sugItems.length)%sugItems.length;
      suggestEl.querySelectorAll('.sug').forEach((d,i)=>d.classList.toggle('active',i===sugIdx));
    } else if (e.key==='Enter'){ e.preventDefault(); choose(sugIdx>=0?sugIdx:0); }
    else if (e.key==='Escape'){ suggestEl.hidden=true; }
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('.map-search-wrap')) suggestEl.hidden = true;
  });

  function resetView(){
    closePopup();
    view.panx=0; view.pany=0; rot.x=-0.15; anim=null;
    if (mapMode === 'tree') fitTree(); else view.zoom = 1;
  }
  function focusFamily(fam){          // fly to a genre's centroid
    const c = CENTROIDS[fam]; if (!c) return;
    const ry = Math.atan2(-c.x, c.z), rx = Math.atan2(c.y, Math.hypot(c.x, c.z));
    anim = { f:{rx:rot.x,ry:rot.y,z:view.zoom,px:view.panx,py:view.pany},
             t:{rx, ry, z:2.0, px:0, py:0}, t0:performance.now(), dur:640 };
  }
  function applyFilter(fam){          // null = show all genres
    flaggedOnly = false;
    filterFam = fam || null;
    const fe = document.getElementById('map-filter');
    if (fe) fe.value = filterFam || '';
    closePopup();
    if (filterFam) focusFamily(filterFam); else resetView();
  }
  resetB && resetB.addEventListener('click', () => applyFilter(null));

  const spinEl = document.getElementById('map-spin');   // orbit-speed slider
  const applySpin = () => { if (spinEl) spinSpeed = (spinEl.value / 100) * 0.006; };
  const syncSpin = () => { if (spinEl) spinEl.value = Math.round(spinSpeed / 0.006 * 100); };
  spinEl && spinEl.addEventListener('input', applySpin);
  applySpin();

  const filterEl = document.getElementById('map-filter');
  filterEl && filterEl.addEventListener('change', () => {
    if (filterEl.value === '__flagged__'){ filterFam = null; flaggedOnly = true; closePopup(); resetView(); }
    else applyFilter(filterEl.value);
  });

  // toggles: connection lines + harmonic-mix highlighting
  const edgesBtn = document.getElementById('map-edges');
  edgesBtn && edgesBtn.addEventListener('click', () => {
    edgesOn = !edgesOn; edgesBtn.classList.toggle('on', edgesOn);
  });
  const harmonicBtn = document.getElementById('map-harmonic');
  harmonicBtn && harmonicBtn.addEventListener('click', () => {
    harmonic = !harmonic; harmonicBtn.classList.toggle('on', harmonic);
  });

  // keyboard: +/- zoom, arrows rotate, space toggles spin, f fit, esc close
  let lastSpin = 0.0022;
  document.addEventListener('keydown', e => {
    if (!document.body.classList.contains('view-map')) return;
    const tag = e.target.tagName || '';
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
    const arrows = ['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'];
    switch (e.key){
      case '+': case '=': view.zoom = clamp(view.zoom*1.15, 0.3, 60); anim=null; break;
      case '-': case '_': view.zoom = clamp(view.zoom/1.15, 0.3, 60); anim=null; break;
      case 'ArrowLeft':  rot.y -= 0.12; break;
      case 'ArrowRight': rot.y += 0.12; break;
      case 'ArrowUp':    rot.x = clamp(rot.x-0.12, -1.3, 1.3); break;
      case 'ArrowDown':  rot.x = clamp(rot.x+0.12, -1.3, 1.3); break;
      case ' ':
        if (spinSpeed > 0){ lastSpin = spinSpeed; spinSpeed = 0; } else { spinSpeed = lastSpin || 0.0022; }
        syncSpin(); break;
      case 'f': case 'F': applyFilter(null); break;
      case 'Escape': closePopup(); break;
      default: return;
    }
    if (arrows.includes(e.key) || e.key === ' ') e.preventDefault();
  });
  modeEl && modeEl.addEventListener('click', e => {
    const b = e.target.closest('.mm'); if (!b || b.dataset.mode===mapMode) return;
    mapMode = b.dataset.mode;
    modeEl.querySelectorAll('.mm').forEach(m => m.classList.toggle('active', m===b));
    closePopup(); suggestEl.hidden = true;
    if (NODES.length){ layout(); resetView(); }
  });

  /* ---- load + tab wiring ------------------------------------------- */
  async function ensureBuilt(){
    try{
      const data = await fetch('/map').then(r=>r.json());
      NODES = data.nodes || []; EDGES = data.edges || [];
      if (!NODES.length){ countMap.textContent='0 tracks -- scan some music first'; return; }
      resize(); layout();
    }catch(err){ countMap.textContent='failed to load map'; console.error('map load failed', err); }
  }
  function switchTo(viewName){
    tabsEl.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.view===viewName));
    document.body.classList.toggle('view-guide', viewName === 'guide');
    if (viewName === 'guide' && window.vibeLoadGuide) window.vibeLoadGuide();
    showMap(viewName === 'map');
    const hash = viewName==='map' ? '#map' : (viewName==='guide' ? '#guide' : '#');
    try{ history.replaceState(null,'', hash); }catch(_){}
  }
  function showMap(on){
    const deepHash = location.hash;
    document.body.classList.toggle('view-map', on);
    document.getElementById('map-view').hidden = !on;
    if (on){
      ensureBuilt().then(() => {
        resize(); if (mapMode === 'tree') fitTree(); startLoop();
        const m = /^#map=(.+)$/.exec(deepHash);
        if (m && byHash.has(m[1])) selectNode(m[1]);
      });
    } else { stopLoop(); }
  }
  tabsEl.addEventListener('click', e => {
    const b = e.target.closest('.tab'); if (!b) return; switchTo(b.dataset.view);
  });
  if (location.hash === '#galaxy'){
    mapMode = 'galaxy';
    modeEl && modeEl.querySelectorAll('.mm').forEach(m => m.classList.toggle('active', m.dataset.mode==='galaxy'));
  }
  if (location.hash === '#map' || location.hash.startsWith('#map=') || location.hash === '#galaxy')
    switchTo('map');
  else if (location.hash === '#guide') switchTo('guide');

  let rz;
  window.addEventListener('resize', () => {
    if (!document.body.classList.contains('view-map') || !NODES.length) return;
    clearTimeout(rz); rz = setTimeout(resize, 150);
  });

  // let other UI (the review-reads panel) jump to a track on the map
  window.vibeMapGoto = (hash) => {
    try{ history.replaceState(null, '', '#map=' + hash); }catch(_){}
    switchTo('map');
  };
})();

/* ---- review reads: audit the library for likely-misread genres ---- */
(() => {
  const btn = document.getElementById('flag-btn');
  const panel = document.getElementById('flag-panel');
  const body = document.getElementById('flag-body');
  const closeB = document.getElementById('flag-close');
  if (!btn || !panel) return;
  closeB && closeB.addEventListener('click', () => panel.classList.remove('open'));

  btn.addEventListener('click', async () => {
    panel.classList.add('open');
    body.innerHTML = `<div class="flag-note">scanning your library…</div>`;
    let list;
    try{ list = await fetch('/audit').then(r => r.json()); }
    catch(_){ body.innerHTML = `<div class="flag-note">audit failed</div>`; return; }
    if (!list.length){ body.innerHTML = `<div class="flag-note">✓ no likely misreads found.</div>`; return; }
    body.innerHTML =
      `<div class="flag-note">${list.length} low-confidence reads whose closest sonic neighbours
        point elsewhere. These are only hints — review and omit the wrong ones.</div>` +
      list.map(f => `
        <div class="flag-row" data-h="${escapeHtml(f.hash)}">
          <div class="flag-main">
            <div class="flag-title">${escapeHtml(f.title)}</div>
            <div class="flag-sub">reads as <b>${escapeHtml(f.style||'?')}</b> ${(f.confidence*100).toFixed(0)}%
              · sounds like <b class="flag-suggest">${escapeHtml(f.suggested_style)}</b>
              (${(f.agreement*100).toFixed(0)}% agree)</div>
          </div>
          <div class="flag-acts">
            <button class="flag-go" title="show on the map">map</button>
            <button class="flag-omit" title="delete this track's analysis">omit</button>
          </div>
        </div>`).join('');
    body.querySelectorAll('.flag-row').forEach(row => {
      const h = row.getAttribute('data-h');
      row.querySelector('.flag-go').onclick = () => {
        panel.classList.remove('open');
        if (window.vibeMapGoto) window.vibeMapGoto(h);
      };
      row.querySelector('.flag-omit').onclick = async () => {
        try{ await fetch(`/forget/${h}`, {method:'POST'}); }catch(_){}
        row.remove();
      };
    });
  });

  if (location.hash === '#review') btn.click();   // deep link: open the audit
})();

/* ===================================================================
   Guide tab: fetch docs/USAGE.md (via /guide) and render it in-app with a
   small dependency-free Markdown converter (headings, lists w/ nesting,
   code, blockquotes, hr, bold, inline code, links).
   =================================================================== */
(() => {
  const body = document.getElementById('guide-body');
  if (!body) return;
  let loaded = false;

  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const inline = s => esc(s)
    .replace(/`([^`]+)`/g, (m,c)=>`<code>${c}</code>`)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m,t,u)=>{
      const ext = /^https?:/.test(u);
      return `<a href="${esc(u)}"${ext?' target="_blank" rel="noopener"':''}>${t}</a>`;
    });
  const slug = s => s.toLowerCase().replace(/[^\w\s-]/g,'').trim().replace(/\s+/g,'-');

  function mdToHtml(md){
    const lines = md.replace(/\r/g,'').split('\n');
    const out = [];
    const stack = [];                 // open lists: {type, indent}
    let inCode = false, code = [], quote = [];
    const closeLists = (toIndent) => {
      while (stack.length && stack[stack.length-1].indent >= toIndent)
        out.push(stack.pop().type === 'ol' ? '</ol>' : '</ul>');
    };
    const closeAll = () => closeLists(-1);
    const flushQuote = () => { if (quote.length){ out.push(`<blockquote>${quote.map(inline).join('<br>')}</blockquote>`); quote=[]; } };
    for (const raw of lines){
      if (/^\s*```/.test(raw)){
        if (inCode){ out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`); code=[]; inCode=false; }
        else { flushQuote(); closeAll(); inCode=true; }
        continue;
      }
      if (inCode){ code.push(raw); continue; }
      const line = raw.replace(/\s+$/,'');
      let m;
      if ((m = /^>\s?(.*)$/.exec(line))){ closeAll(); quote.push(m[1]); continue; }
      flushQuote();
      if (!line.trim()) continue;                                   // blank
      if ((m = /^(#{1,4})\s+(.*)$/.exec(line))){ closeAll(); const n=m[1].length; out.push(`<h${n} id="${slug(m[2])}">${inline(m[2])}</h${n}>`); continue; }
      if (/^-{3,}$/.test(line.trim())){ closeAll(); out.push('<hr>'); continue; }
      const li = /^(\s*)([-*]|\d+\.)\s+(.*)$/.exec(line);
      if (li){
        const indent = li[1].length, type = /\d/.test(li[2]) ? 'ol' : 'ul';
        closeLists(indent + 1);
        const top = stack[stack.length-1];
        if (!top || top.indent < indent){ out.push(type==='ol'?'<ol>':'<ul>'); stack.push({type, indent}); }
        out.push(`<li>${inline(li[3])}</li>`);
        continue;
      }
      closeAll(); out.push(`<p>${inline(line.trim())}</p>`);
    }
    flushQuote(); closeAll();
    if (inCode) out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`);
    return out.join('\n');
  }

  window.vibeLoadGuide = async () => {
    if (loaded) return;
    loaded = true;
    try{
      const md = await fetch('/guide').then(r => r.text());
      body.innerHTML = mdToHtml(md);
    }catch(_){
      body.innerHTML = '<p>Could not load the guide.</p>';
      loaded = false;
    }
  };

  // deep link (#guide) switches the view before this module defines the loader,
  // so kick off the load here too.
  if (location.hash === '#guide') window.vibeLoadGuide();
})();
