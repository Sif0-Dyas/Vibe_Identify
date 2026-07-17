/* Audio preview for List rows: one <audio>, one track at a time — play /
   pause, seek by clicking the waveform, and the moving playhead.

   LOAD ORDER: load AFTER app.js (attachPlayer uses fmtTime from app.js).
   Exposes the globals PLAYER, OBJ_URLS and attachPlayer(), which app.js's
   row builder and Clear handler use. See index.html. */

/* exported attachPlayer */ // defined here, called from app.js's finishRow (shared scope)

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

/* Wire the play button, playhead and waveform-seek for one List row. */
function attachPlayer(row, container, controls, c, file, data, dur){
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
}
