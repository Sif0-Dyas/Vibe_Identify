/* Genre Map — the 3-D constellation view (scene, camera controls, tree
   view, popups, search, tab switching). Self-contained IIFE.

   LOAD ORDER: load AFTER app.js — uses shared helpers from app.js
   (escapeHtml, colorFor, styleInfo, familyOf, fmtTime) and calls
   window.vibeLoadGuide (defined in app.js). Exposes window.vibeMapGoto,
   which app.js's review panel calls. See index.html. */

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
  let treeHits = [], hoverGenre = null;    // tree node hit-boxes + hovered node
  let famLabelHits = [];                   // family-label hit-boxes -> click to fly
  let styleLabelHits = [];                 // subgenre-label hit-boxes -> click to fly
  let MAXR = 1;                            // cloud bounding radius (for auto-fit)
  let selHash = null;
  let simCache = [];                       // last popup's /similar result

  let W = 0, H = 0, DPR = 1;
  const rot = { x: -0.15, y: 0.5 };        // orbit angles
  const view = { zoom: 1, panx: 0, pany: 0 };
  const pivot = { x:0, y:0, z:0 };     // orbit centre (eased): see the frame loop
  let famPivot = null;                 // a clicked genre's centroid to orbit around
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
  // user-customisable per-family base hue (persisted); falls back to the hash hue
  let FAM_HUE = {};
  try { FAM_HUE = JSON.parse(localStorage.getItem('vibeFamHue') || '{}') || {}; } catch(_){ FAM_HUE = {}; }
  let legendCollapsed = false;
  try { legendCollapsed = localStorage.getItem('vibeLegend') === 'off'; } catch(_){ /* private mode */ }
  const famHue = fam => (FAM_HUE[fam] == null ? hueOf(fam) : FAM_HUE[fam]);
  const famCss = fam => `hsl(${famHue(fam)} 62% 62%)`;
  function hexToHue(hex){
    const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || '');
    if (!m) return null;
    const r = parseInt(m[1],16)/255, g = parseInt(m[2],16)/255, b = parseInt(m[3],16)/255;
    const mx = Math.max(r,g,b), mn = Math.min(r,g,b), d = mx-mn;
    if (d === 0) return 0;
    let h; if (mx===r) h = ((g-b)/d) % 6; else if (mx===g) h = (b-r)/d + 2; else h = (r-g)/d + 4;
    return ((h*60) % 360 + 360) % 360;
  }
  function hslHex(h, s, l){                          // for seeding the colour picker
    s/=100; l/=100;
    const k = n => (n + h/30) % 12, a = s*Math.min(l, 1-l);
    const f = n => l - a*Math.max(-1, Math.min(k(n)-3, Math.min(9-k(n), 1)));
    const to = x => Math.round(x*255).toString(16).padStart(2,'0');
    return `#${to(f(0))}${to(f(8))}${to(f(4))}`;
  }
  // deterministic 0..1 hash of a string
  function hash01(s){ let h = 0; for (let i=0;i<s.length;i++) h = (h*31 + s.charCodeAt(i)) | 0;
    return (((h % 4096) + 4096) % 4096) / 4096; }
  // A subgenre's colour = its family's hue nudged a little (so it still reads as
  // the same family) plus a small saturation/lightness wobble -- subgenres come
  // out distinct but kin, so a family cluster shows its internal groupings even
  // from afar. Returns {h, s, dl} (hue, saturation, lightness delta).
  function styleShade(fam, style){
    const base = famHue(fam);
    if (!style || style === fam) return { h: base, s: 64, dl: 0 };
    const r = hash01(style + '|' + fam);
    return {
      h: ((base + (r*2 - 1)*36) % 360 + 360) % 360,   // family hue +/- 36 degrees
      s: 48 + Math.floor(hash01('s' + style) * 38),   // 48..86 (wide, for shade contrast)
      dl: (hash01('l' + style)*2 - 1) * 15,           // +/- 15 lightness
    };
  }
  // blend shade b into a by t (0..1); hue interpolated along the short arc so a
  // mixed track lands *between* its two genres' colours ("colour matching").
  function mixShade(a, b, t){
    const d = ((b.h - a.h) % 360 + 540) % 360 - 180;
    return { h: ((a.h + d*t) % 360 + 360) % 360, s: a.s + (b.s-a.s)*t, dl: a.dl + (b.dl-a.dl)*t };
  }
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
      let sh = styleShade(n.fam, n.style || n.styles[0]);   // per-subgenre shade of the family
      if (n.mix && n.mix[1] > 0.02){                        // lean toward the 2nd genre by its %
        const f2 = familyOf(n.mix[0]) || n.fam;
        sh = mixShade(sh, styleShade(f2, n.mix[0]), Math.min(0.5, n.mix[1]));
      }
      n.hue = sh.h; n.sat = sh.s; n.dl = sh.dl;
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
      // natural positions == the first 3 PCA axes; spacing follows the tracks'
      // own connections, no artificial spreading.
      const p = [0,1,2].map(j => pctl(NODES.map(n=>Math.abs(n.e?n.e[j]:0)),0.96)||1);
      for (const n of NODES){
        const r = rng(n.hash);
        n.x3 = clamp((n.e?n.e[0]:0)/p[0], -1.3, 1.3) + (r()-0.5)*0.04;
        n.y3 = clamp((n.e?n.e[1]:0)/p[1], -1.3, 1.3) + (r()-0.5)*0.04;
        n.z3 = clamp((n.e?n.e[2]:0)/p[2], -1.3, 1.3) + (r()-0.5)*0.04;
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
        anchors[f] = { x:1.7*Math.cos(th)*Math.sin(phi),
                       y:1.7*Math.sin(th)*Math.sin(phi),
                       z:1.7*Math.cos(phi) };
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
        const famSpread = 0.06 + Math.sqrt(COUNTS[f]) * 0.013;
        const subR = famSpread * (styles.length>1 ? 1.0 : 0);   // more room for subgenres
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
        const famSpread = 0.06 + Math.sqrt(COUNTS[n.fam]) * 0.013;
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
    MAXR = 0.5;
    for (const n of NODES){ const d = Math.hypot(n.x3, n.y3, n.z3); if (d > MAXR) MAXR = d; }

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
    // subgenre breakdown per overarching genre (family)
    const subs = {};
    for (const n of NODES){ const st = n.style || n.fam;
      (subs[n.fam] = subs[n.fam] || {})[st] = (subs[n.fam][st] || 0) + 1; }
    const groups = FAMS.map(f => {
      const active = filterFam === f ? ' active' : '';
      const head = `<span class="leg leg-fam${active}" data-fam="${escapeHtml(f)}">`
        + `<span class="dot" title="click to recolour this genre" style="background:${famCss(f)}"></span>`
        + `<b>${escapeHtml(f)}</b>&nbsp;${COUNTS[f]}</span>`;
      // a lone subgenre identical to the family isn't really a "sub" -> skip it
      const list = Object.entries(subs[f] || {}).filter(([st]) => st !== f).sort((a,b) => b[1]-a[1]);
      if (!list.length) return `<div class="leg-group">${head}</div>`;
      const shown = list.slice(0, 10), more = list.length - shown.length;
      const subHtml = shown.map(([st, c]) => {
        const sh = styleShade(f, st);
        const col = `hsl(${sh.h} ${clamp(sh.s, 40, 88)}% ${clamp(58 + sh.dl, 44, 70)}%)`;
        return `<span class="leg-sub" data-fam="${escapeHtml(f)}" data-style="${escapeHtml(st)}"`
          + ` title="zoom to ${escapeHtml(st)}"><span class="sdot" style="background:${col}"></span>${escapeHtml(st)} ${c}</span>`;
      }).join('') + (more > 0 ? `<span class="leg-more">+${more} more</span>` : '');
      return `<div class="leg-group">${head}<div class="leg-subs">${subHtml}</div></div>`;
    }).join('');
    legendEl.innerHTML =
      `<button class="leg-toggle" type="button" title="show / hide the genre legend">&#9698; genres</button>`
      + `<div class="leg-body">${groups}</div>`;
    legendEl.classList.toggle('collapsed', legendCollapsed);
    legendEl.querySelector('.leg-toggle').onclick = () => {
      legendCollapsed = !legendCollapsed;
      legendEl.classList.toggle('collapsed', legendCollapsed);
      try{ localStorage.setItem('vibeLegend', legendCollapsed ? 'off' : 'on'); }catch(_){ /* private */ }
    };
    // click a family header -> zoom to that genre; its dot -> recolour it;
    // click a subgenre -> zoom to that sub-cluster
    legendEl.querySelectorAll('.leg-fam').forEach(el => {
      const f = el.getAttribute('data-fam');
      el.querySelector('.dot').onclick = ev => { ev.stopPropagation(); pickFamColor(f, ev.currentTarget); };
      el.onclick = () => focusFamily(f);
    });
    legendEl.querySelectorAll('.leg-sub').forEach(el =>
      el.onclick = () => focusStyle(el.getAttribute('data-fam'), el.getAttribute('data-style')));
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

  // recolour a whole genre family (and its subgenre shades) via a native colour
  // picker; the choice persists in localStorage and re-applies on load.
  let _famPicker = null;
  function pickFamColor(fam, anchor){
    if (!_famPicker){
      _famPicker = document.createElement('input');
      _famPicker.type = 'color';
      _famPicker.style.cssText = 'position:fixed;width:0;height:0;opacity:0;border:0;padding:0;pointer-events:none';
      document.body.appendChild(_famPicker);
    }
    _famPicker.value = hslHex(famHue(fam), 62, 62);   // seed with the current colour
    _famPicker.oninput = () => {
      const hue = hexToHue(_famPicker.value);
      if (hue == null) return;
      FAM_HUE[fam] = Math.round(hue);
      try{ localStorage.setItem('vibeFamHue', JSON.stringify(FAM_HUE)); }catch(_){ /* private mode */ }
      if (NODES.length) layout();                     // recompute node shades + legend
    };
    const r = anchor.getBoundingClientRect();
    _famPicker.style.left = r.left + 'px'; _famPicker.style.top = r.bottom + 'px';
    _famPicker.click();
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
    proj.clear(); treeHits = []; famLabelHits = []; styleLabelHits = [];
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
      treeHits.push({ node: nd, sx, sy, r });
      ctx.beginPath(); ctx.arc(sx, sy, r, 0, 6.2832);
      if (isFam){ ctx.fillStyle = famCss(nd.fam); }
      else { const sh = styleShade(nd.fam, nd.label);   // subgenre = shade of family
        ctx.fillStyle = `hsl(${sh.h} ${clamp(sh.s-4,42,80)}% ${clamp(56+sh.dl,44,66)}%)`; }
      ctx.fill();
      ctx.textAlign = isFam ? 'right' : 'left';   // fam labels left, sub labels right
      const lx = isFam ? sx - r - 6 : sx + r + 6;
      const hot = nd === hoverGenre;
      const label = hot ? `${nd.label} ${nd.count}` : nd.label;   // count only on hover
      ctx.font = isFam ? '800 15px Syne, sans-serif' : "500 11px 'JetBrains Mono', monospace";
      ctx.lineWidth = 3.5; ctx.strokeStyle = 'rgba(0,0,0,0.92)';
      ctx.strokeText(label, lx, sy);
      ctx.fillStyle = hot ? '#ffffff' : (isFam ? '#e9eef7' : '#aeb8ca');
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
    // orbit centre (eased): a clicked genre's centroid, else the selected track,
    // else the origin. So clicking a genre orbits AROUND that cluster.
    const sel = selHash ? byHash.get(selHash) : null;
    const tx = famPivot ? famPivot.x : (sel ? sel.x3 : 0);
    const ty = famPivot ? famPivot.y : (sel ? sel.y3 : 0);
    const tz = famPivot ? famPivot.z : (sel ? sel.z3 : 0);
    pivot.x += (tx - pivot.x) * 0.12;
    pivot.y += (ty - pivot.y) * 0.12;
    pivot.z += (tz - pivot.z) * 0.12;
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
      for (const ed of EDGES){
        const a = proj.get(ed.a), b = proj.get(ed.b);
        if (!a || !b) continue;
        const hot = selHash && (ed.a===selHash||ed.b===selHash);
        if (!edgesOn && !hot) continue;             // hidden: only the selection's web
        // brightness + thickness scale with how closely the two tracks match
        const s = clamp(((ed.sim ?? 0.75) - 0.6) / 0.4, 0, 1);
        let op, lw;
        if (selHash){ op = hot ? 0.9 : 0.05; lw = hot ? 1.8 : 1; }
        else { op = (0.14 + 0.34*s) * (0.55 + 0.45*Math.min(a.depth,b.depth)); lw = 0.8 + 1.4*s; }
        if (op < 0.02) continue;
        ctx.lineWidth = lw;
        ctx.strokeStyle = hot ? `rgba(86,180,233,${op})` : `rgba(150,172,208,${op})`;
        ctx.beginPath(); ctx.moveTo(a.sx,a.sy); ctx.lineTo(b.sx,b.sy); ctx.stroke();
      }
    }

    // Labels with semantic zoom (LOD): family names when zoomed out, subgenre
    // names fading in as you zoom in. Galaxy mode has no hierarchy -> no fade.
    ctx.textAlign='center'; ctx.textBaseline='middle';
    // subgenre detail ramps in with zoom (sooner than before), then is gated
    // per-family by how centred/near that cluster is -- see famFocus below.
    const zoomLod = (mapMode==='galaxy') ? 0 : clamp((view.zoom - 0.8) / (1.7 - 0.8), 0, 1);
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
    // Per-family "focus": high when that cluster is near the screen centre AND
    // you're zoomed in on it. A focused family fades its own big label and shows
    // its subgenre labels; families off to the side stay coarse (family label
    // only). So being deep in one cluster reveals ITS subgenres without lighting
    // up subgenres across the whole map -- and other clusters stay identifiable.
    const focusR = 0.40 * Math.min(W, H);
    const famFocus = {};
    for (const f of FAMS){
      const cp = projPt(CENTROIDS[f]);
      const prox = cp.persp > 0
        ? clamp(1 - Math.hypot(cp.sx - cxp, cp.sy - cyp) / focusR, 0, 1) : 0;
      famFocus[f] = zoomLod * prox;
    }

    // family labels: measure, then relax apart in 2D (box separation) so they
    // fan out around the cluster -- even a tight galaxy ball -- each tied back
    // to its true centroid by a colour-coded leader line.
    const fl = [];
    let cx0 = 0, cy0 = 0;
    for (const f of FAMS){
      if (filterFam && f !== filterFam) continue;
      const p = projPt(CENTROIDS[f]);
      if (p.persp <= 0) continue;
      const depth = clamp((p.z2+1.15)/2.3, 0, 1);
      const alpha = (0.52 + 0.32*depth) * (1 - 0.9*(famFocus[f]||0));   // fade when focused
      if (alpha < 0.04) continue;
      const fs = Math.min(21, 13 + CENTROIDS[f].n*0.3) * clamp(p.persp, 0.9, 1.25);
      ctx.font = `800 ${fs}px Syne, sans-serif`;
      fl.push({ f, ax:p.sx, ay:p.sy, lx:p.sx, ly:p.sy,
                hw:ctx.measureText(f.toUpperCase()).width/2 + 5, hh:fs*0.62, fs, alpha });
      cx0 += p.sx; cy0 += p.sy;
    }
    if (fl.length){
      cx0 /= fl.length; cy0 /= fl.length;
      let spread = 0;                                   // how clustered are the anchors?
      for (const l of fl) spread += Math.hypot(l.lx-cx0, l.ly-cy0);
      spread /= fl.length;
      if (spread < 90){
        // tight ball (galaxy): centroid *direction* is basically noise, so ring
        // the labels evenly by angle around the cluster, each tied back by a
        // leader line. The ring radius tracks the ball's ON-SCREEN size (+margin)
        // so labels always clear the cloud instead of overlapping it when the
        // ball is large (e.g. zoomed out at the fit view).
        let ballR = 0;
        { const ds = []; for (const [, pp] of proj) ds.push(Math.hypot(pp.sx-cx0, pp.sy-cy0));
          ballR = pctl(ds, 0.9); }
        const arr = fl.slice().sort((a,b) =>
          Math.atan2(a.ay-cy0, a.ax-cx0) - Math.atan2(b.ay-cy0, b.ax-cx0));
        const N = arr.length;
        const Rx = Math.max(180, ballR*1.18 + 62, N*8);   // wider: room for long names
        const Ry = Math.max(150, ballR*1.06 + 50);        // tall enough to clear the ball
        for (let i=0; i<N; i++){
          const ang = (i/N)*6.2832 - 1.5708;            // start at top, go clockwise
          arr[i].lx = cx0 + Math.cos(ang)*Rx;
          arr[i].ly = cy0 + Math.sin(ang)*Ry;
        }
      } else {
        // spread map (regions): nudge a hair so labels stay on their centroids.
        for (const l of fl){
          let dx = l.lx-cx0, dy = l.ly-cy0, d = Math.hypot(dx, dy);
          if (d < 1){ const a = (hueOf(l.f) % 360) * Math.PI/180; dx = Math.cos(a); dy = Math.sin(a); d = 1; }
          l.lx += dx/d * 3; l.ly += dy/d * 3;
        }
      }
    }
    for (let pass=0; pass<90; pass++){                  // 2D AABB min-penetration relax
      let moved = false;
      for (let i=0; i<fl.length; i++) for (let j=i+1; j<fl.length; j++){
        const a=fl[i], b=fl[j];
        const dx=b.lx-a.lx, dy=b.ly-a.ly;
        const ox=(a.hw+b.hw)-Math.abs(dx), oy=(a.hh+b.hh+2)-Math.abs(dy);
        if (ox>0 && oy>0){                              // boxes overlap -> push on shallow axis
          if (ox < oy){ const p=(ox/2)*(dx>=0?1:-1); a.lx-=p; b.lx+=p; }
          else        { const p=(oy/2)*(dy>=0?1:-1); a.ly-=p; b.ly+=p; }
          moved = true;
        }
      }
      if (!moved) break;
    }
    ctx.textAlign = 'center';
    for (const l of fl){
      if (Math.hypot(l.lx-l.ax, l.ly-l.ay) > 4){        // pulled away -> leader line
        ctx.lineWidth = 1;
        ctx.strokeStyle = `hsla(${famHue(l.f)} 62% 62% / ${clamp(0.65*l.alpha+0.2, 0, 0.75)})`;
        ctx.beginPath(); ctx.moveTo(l.ax, l.ay); ctx.lineTo(l.lx, l.ly); ctx.stroke();
      }
      drawLabel(l.f.toUpperCase(), {sx:l.lx, sy:l.ly},
        `800 ${l.fs}px Syne, sans-serif`, `rgba(233,238,247,${l.alpha})`);
      famLabelHits.push({ f:l.f, cx:l.lx, cy:l.ly, hw:l.hw, hh:l.hh });   // click -> fly here
    }
    // subgenre labels -- shown only inside the focused cluster(s), coloured as a
    // shade of the family so they read as "part of" it. Drawn far -> near.
    {
      const subs = [];
      for (const key in STYLE_CENTROIDS){
        if (filterFam && !key.startsWith(filterFam + '||')) continue;
        const fam = key.slice(0, key.indexOf('||'));
        const focus = famFocus[fam] || 0;
        if (focus < 0.02) continue;
        const c = STYLE_CENTROIDS[key], p = projPt(c);
        if (p.persp <= 0) continue;
        const depth = clamp((p.z2+1.15)/2.3, 0, 1);
        const a = (0.62 + 0.38*depth) * clamp(focus*1.5, 0, 1);
        if (a < 0.03) continue;
        const sh = styleShade(fam, c.style);
        const fs = Math.min(22, 11 + c.n*0.4) * clamp(p.persp, 0.85, 1.3);
        ctx.font = `700 ${fs}px 'JetBrains Mono', monospace`;
        subs.push({ p, depth, fam, style: c.style, text: c.style.toUpperCase(), fs,
          hw: ctx.measureText(c.style.toUpperCase()).width/2 + 4, hh: fs*0.6,
          // bright, high-lightness tint of the family hue so it reads over the
          // similarly-hued nodes (the dark halo in drawLabel does the rest).
          col: `hsla(${sh.h} ${clamp(sh.s+20, 55, 96)}% ${clamp(82 + sh.dl*0.5, 74, 90)}% / ${a})` });
      }
      subs.sort((x,y) => x.depth - y.depth);
      for (const s of subs){
        drawLabel(s.text, s.p, `700 ${s.fs}px 'JetBrains Mono', monospace`, s.col);
        styleLabelHits.push({ fam:s.fam, style:s.style, cx:s.p.sx, cy:s.p.sy, hw:s.hw, hh:s.hh });
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
      ctx.fillStyle = `hsl(${n.hue} ${n.sat||64}% ${clamp(light + (n.dl||0), 16, 84)}%)`;
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

  /* ---- interaction: orbit / pan / zoom / click --------------------- */
  // left-drag orbits; right / middle / Shift+left-drag pans (translate); wheel
  // zooms toward the cursor. Panning lets you fly through the 3-D scene.
  let dragging=false, panning=false, moved=false, lx=0, ly=0;
  canvas.addEventListener('pointerdown', e => {
    dragging=true; moved=false; lx=e.clientX; ly=e.clientY;
    panning = (e.button===1 || e.button===2 || e.shiftKey);
    canvas.classList.add(panning ? 'panning' : 'grabbing');
    canvas.setPointerCapture(e.pointerId); anim=null;
  });
  canvas.addEventListener('contextmenu', e => e.preventDefault());   // right-drag = pan
  canvas.addEventListener('pointermove', e => {
    if (dragging){
      const dx=e.clientX-lx, dy=e.clientY-ly; lx=e.clientX; ly=e.clientY;
      if (Math.abs(dx)+Math.abs(dy) > 2) moved=true;
      if (mapMode === 'tree' || panning){ view.panx += dx; view.pany += dy; }
      else { rot.y += dx*0.006; rot.x = clamp(rot.x + dy*0.006, -1.3, 1.3); }
      return;
    }
    const rr = canvas.getBoundingClientRect();
    if (mapMode === 'tree'){                 // hover a genre node -> reveal its count
      const mx = e.clientX-rr.left, my = e.clientY-rr.top;
      let hit = null;
      for (const h of treeHits){ if (Math.hypot(mx-h.sx, my-h.sy) <= h.r+6){ hit = h.node; break; } }
      hoverGenre = hit;
      canvas.style.cursor = hit ? 'pointer' : '';
      if (tipEl && !tipEl.hidden) tipEl.hidden = true;
      return;
    }
    // hover: a node -> tooltip; a clickable genre/subgenre label -> pointer cursor
    if (!tipEl) return;
    const r = canvas.getBoundingClientRect();
    const mx = e.clientX-r.left, my = e.clientY-r.top;
    const onLabel = g => Math.abs(mx-g.cx) <= g.hw && Math.abs(my-g.cy) <= g.hh+4;
    const overLabel = styleLabelHits.some(onLabel) || famLabelHits.some(onLabel);
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
    } else {
      if (!tipEl.hidden) tipEl.hidden = true;
      canvas.style.cursor = overLabel ? 'pointer' : '';
    }
  });
  canvas.addEventListener('pointerleave', () => { if (tipEl) tipEl.hidden = true; hoverGenre = null; });
  const endDrag = e => { dragging=false; canvas.classList.remove('grabbing','panning');
    try{ canvas.releasePointerCapture(e.pointerId); }catch(_){} };
  canvas.addEventListener('pointerup', e => {
    const wasPanning = panning;
    endDrag(e);
    if (!moved && !wasPanning){                     // treat as click -> hit test
      const r = canvas.getBoundingClientRect();
      const mx = e.clientX-r.left, my = e.clientY-r.top;
      for (const g of styleLabelHits){              // a subgenre label -> fly to its sub-cluster
        if (Math.abs(mx-g.cx) <= g.hw && Math.abs(my-g.cy) <= g.hh+4){ focusStyle(g.fam, g.style); return; }
      }
      for (const g of famLabelHits){                // a genre label -> fly to its cluster
        if (Math.abs(mx-g.cx) <= g.hw && Math.abs(my-g.cy) <= g.hh+4){ focusFamily(g.f); return; }
      }
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
    selHash = hash; famPivot = null;   // orbit around this track now, not a genre
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
      <div class="pop-title">${escapeHtml(n.artist ? stripArtist(n.title, n.artist) : n.title)}</div>
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
        <button class="pop-override" title="set the genre yourself (persists + saved for training)">✎ override</button>
        <button class="pop-omit" title="delete this track's analysis (audio file untouched)">✕ omit</button>
      </div>
      <div class="pop-ovr" hidden>
        <input class="pop-ovr-in" type="text" placeholder="genre…" autocomplete="off" spellcheck="false">
        <button class="pop-ovr-save">save</button>
        <button class="pop-ovr-cancel" title="cancel">✕</button>
      </div>`;
    popEl.hidden = false;
    popEl.querySelector('.pop-x').onclick = closePopup;
    popEl.querySelector('.pop-omit').onclick = () => omitTrack(n);
    const ovrRow = popEl.querySelector('.pop-ovr'), omitRow = popEl.querySelector('.pop-omit-row');
    const ovrIn = popEl.querySelector('.pop-ovr-in');
    const closeOvr = () => { ovrRow.hidden = true; omitRow.hidden = false; };
    popEl.querySelector('.pop-override').onclick = () => {
      omitRow.hidden = true; ovrRow.hidden = false;
      ovrIn.value = n.suggest || n.style || '';   // prefill with the flag's suggestion
      ovrIn.focus(); ovrIn.select();
    };
    popEl.querySelector('.pop-ovr-cancel').onclick = closeOvr;
    popEl.querySelector('.pop-ovr-save').onclick = () => overrideTrack(n, ovrIn.value);
    ovrIn.addEventListener('keydown', e => {
      if (e.key === 'Enter') overrideTrack(n, ovrIn.value);
      else if (e.key === 'Escape') closeOvr();
    });
    try{
      simCache = await fetch(`/similar/${n.hash}?k=12`).then(r=>r.ok?r.json():[]);
    }catch(_){ simCache = []; }
    renderPick();
    renderSimilar(simCache);
  }

  // override: persist a manual genre; the track moves to its new cluster
  async function overrideTrack(n, genre){
    genre = (genre || '').trim(); if (!genre) return;
    try{
      await fetch(`/override/${n.hash}`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ genre }) });
    }catch(_){ /* still relabel locally */ }
    n.style = genre;
    n.fam = familyOf(genre) || 'Other';
    n.mix = null;                          // manual override -> pure genre, no blend
    const sh = styleShade(n.fam, genre);
    n.hue = sh.h; n.sat = sh.s; n.dl = sh.dl;
    n.flag = false; n.suggest = null;
    closePopup();
    if (NODES.length) layout();          // re-cluster with the new genre
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
    popEl.hidden = true; selHash = null; famPivot = null;
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
    if (mapMode === 'tree') fitTree();
    else view.zoom = clamp(0.95 / (MAXR || 1), 0.25, 1.6);   // fit the cloud
  }
  function focusFamily(fam){          // fly to a genre's centroid + orbit around it
    const c = CENTROIDS[fam]; if (!c) return;
    closePopup();                     // drop any track selection...
    famPivot = c;                     // ...then orbit around this cluster's centre
    const ry = Math.atan2(-c.x, c.z), rx = Math.atan2(c.y, Math.hypot(c.x, c.z));
    anim = { f:{rx:rot.x,ry:rot.y,z:view.zoom,px:view.panx,py:view.pany},
             t:{rx, ry, z:2.0, px:0, py:0}, t0:performance.now(), dur:640 };
  }
  function focusStyle(fam, style){    // fly to & orbit ONE subgenre's sub-cluster
    let x=0, y=0, z=0, c=0;           // centroid computed on demand -> works in any layout
    for (const n of NODES){
      if (n.fam === fam && (n.style || n.fam) === style){ x+=n.x3; y+=n.y3; z+=n.z3; c++; }
    }
    if (!c) return;
    const cen = { x:x/c, y:y/c, z:z/c };
    closePopup();
    famPivot = cen;                   // orbit around the subgenre sub-cluster
    const ry = Math.atan2(-cen.x, cen.z), rx = Math.atan2(cen.y, Math.hypot(cen.x, cen.z));
    anim = { f:{rx:rot.x,ry:rot.y,z:view.zoom,px:view.panx,py:view.pany},
             t:{rx, ry, z:2.8, px:0, py:0}, t0:performance.now(), dur:640 };   // tighter zoom
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
  const playBtn = document.getElementById('map-play');  // pause / play the orbit
  let lastSpin = 0.0022;
  const syncSpin = () => { if (spinEl) spinEl.value = Math.round(spinSpeed / 0.006 * 100); };
  function reflectPlay(){                                // button icon <- current state
    if (!playBtn) return;
    const on = spinSpeed > 0;
    playBtn.classList.toggle('on', on);
    playBtn.innerHTML = on ? '&#9208;' : '&#9205;';     // ⏸ pause when spinning · ▶ play when stopped
    playBtn.title = (on ? 'pause' : 'play') + ' the orbit (Space)';
  }
  const applySpin = () => { if (spinEl){ spinSpeed = (spinEl.value / 100) * 0.006; reflectPlay(); } };
  function toggleSpin(){
    if (spinSpeed > 0){ lastSpin = spinSpeed; spinSpeed = 0; } else { spinSpeed = lastSpin || 0.0022; }
    syncSpin(); reflectPlay();
  }
  spinEl && spinEl.addEventListener('input', applySpin);
  // blur after click so a following Space isn't caught by the focused button
  // (native activation) *and* the document handler -> a double-toggle no-op.
  playBtn && playBtn.addEventListener('click', () => { toggleSpin(); playBtn.blur(); });
  applySpin();

  // navigation key legend: collapsible, state remembered
  const keysEl = document.getElementById('map-keys');
  const keysHead = document.getElementById('map-keys-head');
  if (keysEl && keysHead){
    try{ if (localStorage.getItem('vibeNavKeys') === 'off') keysEl.classList.add('collapsed'); }catch(_){}
    keysHead.addEventListener('click', () => {
      const off = keysEl.classList.toggle('collapsed');
      try{ localStorage.setItem('vibeNavKeys', off ? 'off' : 'on'); }catch(_){}
    });
  }

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

  // keyboard nav: W/S zoom · A/D orbit · arrows pan · +/- zoom · Space play/pause
  //               · f fit · Esc close
  document.addEventListener('keydown', e => {
    if (!document.body.classList.contains('view-map')) return;
    const tag = e.target.tagName || '';
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
    const PAN = 45, ROT = 0.12;
    const k = e.key.toLowerCase();
    switch (k){
      case '+': case '=': case 'w': view.zoom = clamp(view.zoom*1.15, 0.3, 60); anim=null; break;
      case '-': case '_': case 's': view.zoom = clamp(view.zoom/1.15, 0.3, 60); anim=null; break;
      case 'a': rot.y -= ROT; break;                 // orbit left
      case 'd': rot.y += ROT; break;                 // orbit right
      case 'arrowleft':  view.panx += PAN; break;    // arrows pan the view
      case 'arrowright': view.panx -= PAN; break;
      case 'arrowup':    view.pany += PAN; break;
      case 'arrowdown':  view.pany -= PAN; break;
      case ' ': toggleSpin(); break;
      case 'f': applyFilter(null); break;
      case 'escape': closePopup(); break;
      default: return;
    }
    if (k.startsWith('arrow') || k === ' ') e.preventDefault();
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
        resize();
        if (mapMode === 'tree') fitTree(); else view.zoom = clamp(0.95/(MAXR||1), 0.25, 1.6);
        startLoop();
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
