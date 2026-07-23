/* Panel subsystems for the List UI — the sliding side panels and their queues:
   the sibling-group editor, per-row tags, the external-lookup results panel, the
   vibes panel (+ per-row match holders), and the label-propagation queue.

   LOAD ORDER: load AFTER app.js and player.js (see index.html). These panels call
   shared helpers from app.js (escapeHtml, styleInfo, colorFor, fmtTime, …) and
   read app.js row state (results, GLOBAL, SIBLING_MAP, SIBLING_GROUPS); the label
   queue drives the shared PLAYER from player.js. In turn app.js's row builder
   (finishRow) calls renderTags / renderLookup / renderVibeMatches defined here — a
   normal cross-file reference in the shared <script> scope, declared as globals in
   eslint.config.js. */

/* exported renderTags, renderLookup, renderVibeMatches */ // defined here, called from app.js's finishRow (shared scope)

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

/* Apply a tag by NAME idempotently: create it if new, then toggle it ON only if
   it isn't already on the track (so re-applying never removes it). */
async function applyTagByName(hash, name){
  const cr = await fetch('/tags', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
  if (!cr.ok) return false;
  const t = await cr.json();
  const mine = await fetch(`/tags/for/${hash}`).then(r => r.ok ? r.json() : []);
  if (!mine.some(x => x.id === t.id)){
    await fetch('/tags/toggle', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({tag_id: t.id, hash})});
  }
  await fetchTags(true);
  return true;
}

/* Render the external-lookup suggestions panel: source-attributed groups, each
   suggestion offering "tag" (apply as a tag) and "train" (record as a training
   label + copy the audio into that genre's folder). */
function renderLookup(panel, row, hash, j){
  const SOURCES = [['discogs','Discogs'], ['musicbrainz','MusicBrainz'], ['lastfm','Last.fm']];
  const q = j.query || {};
  panel.innerHTML = `<div class="lk-q">searched as <b>${escapeHtml(q.artist || '?')}</b> — ` +
    `<b>${escapeHtml(q.title || '?')}</b>${q.remix ? ` <i>(${escapeHtml(q.remix)})</i>` : ''}</div>`;
  let any = false;
  for (const [key, label] of SOURCES){
    const items = (j.results && j.results[key]) || null;
    const err = j.errors && j.errors[key];
    const grp = document.createElement('div');
    grp.className = 'lk-src';
    const note = err ? `<span class="lk-err">${escapeHtml(err)}</span>`
                     : ((items && items.length) ? '' : '<span class="lk-err">no matches</span>');
    grp.innerHTML = `<div class="lk-srchd"><span>${label}</span>${note}</div>`;
    for (const it of (items || [])){
      any = true;
      const chip = document.createElement('div');
      chip.className = 'lk-item';
      chip.innerHTML = `<span class="lk-name">${escapeHtml(it.name)}</span>` +
        (it.count != null ? `<span class="lk-count">${escapeHtml(String(it.count))}</span>` : '') +
        `<button class="lk-act lk-tag" type="button" title="add as a tag on this track">tag</button>` +
        `<button class="lk-act lk-train" type="button" title="record as a training label + copy the audio into that genre's training folder">train</button>`;
      chip.querySelector('.lk-tag').addEventListener('click', async e => {
        const b = e.target; b.disabled = true;
        const ok = await applyTagByName(hash, it.name);
        b.textContent = ok ? 'tagged ✓' : 'failed';
        if (ok) renderTags(row, hash);
      });
      chip.querySelector('.lk-train').addEventListener('click', async e => {
        const b = e.target; b.disabled = true;
        try {
          const r = await fetch('/training/confirm', {method:'POST',
            headers:{'Content-Type':'application/json'}, body: JSON.stringify({hash, genre: it.name})});
          b.textContent = r.ok ? 'trained ✓' : 'failed';
        } catch(_){ b.textContent = 'failed'; }
      });
      grp.appendChild(chip);
    }
    panel.appendChild(grp);
  }
  if (!any && !Object.keys(j.errors || {}).length){
    panel.insertAdjacentHTML('beforeend', '<div class="lk-note">no suggestions found</div>');
  }
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


/* ---- label queue: rank unlabeled cached tracks by embedding similarity to a
   chosen genre, then confirm ✓ / reject ✕ each into the training set. Confirms
   are copied into ~/genre_training/<genre>/ (the custom-head pipeline's input)
   and recorded so the queue keeps sharpening; rejects never resurface. ---- */
(() => {
  const btn = document.getElementById('label-btn');
  const panel = document.getElementById('label-panel');
  const body = document.getElementById('label-body');
  const closeB = document.getElementById('label-close');
  if (!btn || !panel) return;
  closeB && closeB.addEventListener('click', () => panel.classList.remove('open'));

  let genreOpts = null;                 // cached datalist options (fetched once)
  let confirmed = 0, rejected = 0;      // running session counts, per loaded queue

  async function loadGenreOptions(){
    if (genreOpts) return genreOpts;
    try {
      const data = await fetch('/map').then(r => r.json());
      const set = new Set();
      for (const n of (data.nodes || [])) if (n.style) set.add(n.style);
      genreOpts = [...set].sort();
    } catch(_){ genreOpts = []; }
    return genreOpts;
  }

  function updateCount(genre){
    const el = body.querySelector('#lbl-count');
    if (el) el.textContent = `${genre} — ✓ ${confirmed} confirmed · ✕ ${rejected} rejected`;
  }

  // a lightweight preview button driving the SHARED player singleton (one track
  // at a time; taking over resets whichever row/candidate was playing before).
  function candPlayer(hash){
    const b = document.createElement('button');
    b.className = 'lbl-play'; b.type = 'button'; b.textContent = '▶'; b.title = 'preview';
    const isActive = () => PLAYER.ctl === ctl;
    const ctl = {
      tick(){},
      render(){ b.textContent = (isActive() && !PLAYER.audio.paused && !PLAYER.audio.ended) ? '❙❙' : '▶'; },
      stopVisual(){ b.textContent = '▶'; },
      error(){ b.textContent = '✕'; b.disabled = true; b.title = 'no server-side audio for this track'; },
    };
    b.addEventListener('click', async () => {
      if (isActive() && !PLAYER.audio.paused){ PLAYER.audio.pause(); return; }
      if (!isActive()){
        if (PLAYER.ctl) PLAYER.ctl.stopVisual();
        PLAYER.ctl = ctl;
        PLAYER.audio.src = '/audio/' + hash;
      }
      try { await PLAYER.audio.play(); } catch(_){ /* 'error' event drives the UI */ }
      ctl.render();
    });
    return b;
  }

  function candidateRow(genre, cnd){
    const d = document.createElement('div');
    d.className = 'lbl-row';
    const meta = [cnd.bpm ? `${cnd.bpm.toFixed(0)} BPM` : '', cnd.camelot || ''].filter(Boolean).join(' · ');
    d.innerHTML =
      `<span class="lbl-sim">${(cnd.sim * 100).toFixed(0)}%</span>` +
      `<div class="lbl-main"><b title="${escapeHtml(cnd.title)}">${escapeHtml(cnd.title)}</b>` +
        (meta ? `<span class="lbl-meta">${escapeHtml(meta)}</span>` : '') + `</div>` +
      `<div class="lbl-acts"></div>`;
    const acts = d.querySelector('.lbl-acts');
    acts.appendChild(candPlayer(cnd.hash));
    const yes = document.createElement('button');
    yes.className = 'lbl-yes'; yes.type = 'button'; yes.textContent = '✓'; yes.title = 'confirm into training data';
    const no = document.createElement('button');
    no.className = 'lbl-no'; no.type = 'button'; no.textContent = '✕'; no.title = 'reject (never resurface for this genre)';
    const post = async (url) => {
      yes.disabled = no.disabled = true;
      try {
        await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({hash: cnd.hash, genre})});
        return true;
      } catch(_){ yes.disabled = no.disabled = false; return false; }
    };
    yes.addEventListener('click', async () => {
      if (!await post('/training/confirm')) return;
      confirmed++; updateCount(genre); d.classList.add('done-yes'); setTimeout(() => d.remove(), 180);
    });
    no.addEventListener('click', async () => {
      if (!await post('/training/reject')) return;
      rejected++; updateCount(genre); d.classList.add('done-no'); setTimeout(() => d.remove(), 180);
    });
    acts.appendChild(yes); acts.appendChild(no);
    return d;
  }

  async function loadQueue(genre){
    if (!genre) return;
    confirmed = 0; rejected = 0; updateCount(genre);
    const queue = body.querySelector('#lbl-queue');
    queue.innerHTML = `<div class="flag-note">ranking your library…</div>`;
    let data;
    try { data = await fetch(`/training/candidates/${encodeURIComponent(genre)}?limit=25`).then(r => r.json()); }
    catch(_){ queue.innerHTML = `<div class="flag-note">failed to load candidates</div>`; return; }
    if (data.error){ queue.innerHTML = `<div class="flag-note">${escapeHtml(data.error)}</div>`; return; }
    if (data.message){ queue.innerHTML = `<div class="flag-note">${escapeHtml(data.message)}</div>`; return; }
    const cands = data.candidates || [];
    if (!cands.length){
      queue.innerHTML = `<div class="flag-note">No unlabeled candidates left for “${escapeHtml(genre)}”. 🎉</div>`;
      return;
    }
    queue.innerHTML = '';
    for (const cnd of cands) queue.appendChild(candidateRow(genre, cnd));
  }

  function shell(){
    body.innerHTML =
      `<div class="lbl-pick">` +
        `<input id="lbl-genre" type="text" list="lbl-genres" autocomplete="off" spellcheck="false" placeholder="genre to label (e.g. Riddim)">` +
        `<datalist id="lbl-genres"></datalist>` +
        `<button id="lbl-go" type="button">load queue</button>` +
      `</div>` +
      `<div class="lbl-count" id="lbl-count"></div>` +
      `<div class="lbl-queue" id="lbl-queue">` +
        `<p class="sib-note">Pick or type a genre, then confirm ✓ / reject ✕ each ranked
         candidate. The queue is ordered by how close each track sounds to the ones you've
         already labelled that genre (via ✎ override or earlier confirms). Confirms copy the
         audio into <code>~/genre_training/&lt;genre&gt;/</code> for the custom-head trainer.</p>` +
      `</div>`;
    const input = body.querySelector('#lbl-genre');
    loadGenreOptions().then(list => {
      const dl = body.querySelector('#lbl-genres');
      if (dl) dl.innerHTML = list.map(g => `<option value="${escapeHtml(g)}"></option>`).join('');
    });
    body.querySelector('#lbl-go').addEventListener('click', () => loadQueue(input.value.trim()));
    input.addEventListener('keydown', e => { if (e.key === 'Enter') loadQueue(input.value.trim()); });
    input.focus();
  }

  btn.addEventListener('click', () => { shell(); panel.classList.add('open'); });
})();
