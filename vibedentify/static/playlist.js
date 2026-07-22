/* Playlist builder + queue. Tracks are added from the Map (popup / similar rows),
   managed in a slide-out panel, persisted to localStorage, played through the
   shared Now Playing bar (prev/next), and exportable as .m3u.

   LOAD ORDER: after nowbar.js (uses window.playHash / nowShowQueueControls);
   before map.js, which calls the globals exposed here:
     window.playlistAdd(track)   window.playlistHas(hash)
   plus the queue hooks nowbar.js looks for: __nowNext / __nowPrev / nowClearQueue */

(function () {
  const KEY = 'vibePlaylist';
  const PL = { tracks: load(), qi: -1 };   // qi = index currently playing (-1 = none)

  function load() { try { return JSON.parse(localStorage.getItem(KEY)) || []; } catch (_) { return []; } }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(PL.tracks)); } catch (_) {} }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  const panel = document.getElementById('pl-panel');
  const body = document.getElementById('pl-body');
  if (!panel || !body) return;

  function counts() {
    const n = PL.tracks.length;
    document.querySelectorAll('.pl-badge, #pl-count').forEach(e => { e.textContent = n; });
    document.querySelectorAll('.pl-badge').forEach(e => e.classList.toggle('has', n > 0));
    if (window.nowShowQueueControls) window.nowShowQueueControls(PL.tracks.length > 1 && PL.qi >= 0);
  }

  function render() {
    if (!PL.tracks.length) {
      body.innerHTML = '<div class="pl-empty">No tracks yet.<br>Open the <b>Map</b>, click a track, and hit <b>＋ playlist</b>.</div>';
    } else {
      body.innerHTML = PL.tracks.map((t, i) => `
        <div class="pl-item${i === PL.qi ? ' playing' : ''}" data-i="${i}">
          <span class="pl-dot" style="background:${t.color || 'var(--accent-a)'}"></span>
          <div class="pl-main">
            <div class="pl-title">${esc(t.title || 'Track')}</div>
            ${t.artist ? `<div class="pl-artist">${esc(t.artist)}</div>` : ''}
          </div>
          <button class="pl-act pl-play" title="${t.a ? 'play' : 'no file — re-scan folder'}"${t.a ? '' : ' disabled'}>▶</button>
          <button class="pl-act pl-up" title="move up"${i === 0 ? ' disabled' : ''}>▲</button>
          <button class="pl-act pl-down" title="move down"${i === PL.tracks.length - 1 ? ' disabled' : ''}>▼</button>
          <button class="pl-act pl-rm" title="remove">✕</button>
        </div>`).join('');
      body.querySelectorAll('.pl-item').forEach(row => {
        const i = +row.dataset.i;
        row.querySelector('.pl-play').onclick = () => playAt(i);
        row.querySelector('.pl-up').onclick = () => move(i, -1);
        row.querySelector('.pl-down').onclick = () => move(i, 1);
        row.querySelector('.pl-rm').onclick = () => removeAt(i);
      });
    }
    counts();
  }

  function playAt(i) {
    if (i < 0 || i >= PL.tracks.length) return;
    const t = PL.tracks[i];
    if (!t.a) return;               // no server-side file
    PL.qi = i;
    if (window.playHash) window.playHash(t.hash, { title: t.title, artist: t.artist, color: t.color });
    render();
  }
  // advance/retreat over PLAYABLE tracks (skip files-missing entries)
  function step(dir) {
    if (PL.qi < 0) return;
    for (let i = PL.qi + dir; i >= 0 && i < PL.tracks.length; i += dir) {
      if (PL.tracks[i].a) { playAt(i); return; }
    }
  }
  window.__nowNext = () => step(1);
  window.__nowPrev = () => step(-1);
  window.nowClearQueue = () => { PL.qi = -1; render(); };

  function move(i, d) {
    const j = i + d;
    if (j < 0 || j >= PL.tracks.length) return;
    const [x] = PL.tracks.splice(i, 1);
    PL.tracks.splice(j, 0, x);
    if (PL.qi === i) PL.qi = j; else if (PL.qi === j) PL.qi = i;
    save(); render();
  }
  function removeAt(i) {
    PL.tracks.splice(i, 1);
    if (PL.qi === i) PL.qi = -1; else if (i < PL.qi) PL.qi--;
    save(); render();
  }

  // ---- public API (called from map.js) ----
  window.playlistAdd = function (t) {
    if (!t || !t.hash) return false;
    if (PL.tracks.some(x => x.hash === t.hash)) return false;   // dedupe
    PL.tracks.push({ hash: t.hash, title: t.title, artist: t.artist, color: t.color, a: t.a ? 1 : 0 });
    save(); render();
    // brief pulse on the toggle buttons so the add is visible
    document.querySelectorAll('.pl-badge').forEach(e => {
      e.classList.remove('pulse'); void e.offsetWidth; e.classList.add('pulse');
    });
    return true;
  };
  window.playlistHas = h => PL.tracks.some(x => x.hash === h);

  // ---- panel open/close + actions ----
  const open = () => { panel.classList.add('open'); render(); };
  const close = () => panel.classList.remove('open');
  document.querySelectorAll('.pl-open').forEach(b => b.addEventListener('click', open));
  const closeBtn = document.getElementById('pl-close');
  if (closeBtn) closeBtn.addEventListener('click', close);

  const playAllBtn = document.getElementById('pl-playall');
  if (playAllBtn) playAllBtn.addEventListener('click', () => {
    const first = PL.tracks.findIndex(t => t.a);
    if (first >= 0) playAt(first);
  });
  const clearBtn = document.getElementById('pl-clear');
  if (clearBtn) clearBtn.addEventListener('click', () => {
    if (PL.tracks.length && !window.confirm('Clear the whole playlist?')) return;
    PL.tracks = []; PL.qi = -1; save(); render();
  });
  const exportBtn = document.getElementById('pl-export');
  if (exportBtn) exportBtn.addEventListener('click', exportM3U);

  function exportM3U() {
    if (!PL.tracks.length) return;
    const origin = location.origin;
    const lines = ['#EXTM3U'];
    for (const t of PL.tracks) {
      const label = (t.artist ? t.artist + ' - ' : '') + (t.title || 'Track');
      lines.push('#EXTINF:-1,' + label);
      lines.push(origin + '/audio/' + t.hash);   // streams from the running app
    }
    const blob = new Blob([lines.join('\n') + '\n'], { type: 'audio/x-mpegurl' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'vibedentify-playlist.m3u';
    a.click();
    URL.revokeObjectURL(a.href);
  }

  render();   // initial (restores a saved playlist)
  if (location.hash === '#playlist') open();   // deep link to the panel
})();
