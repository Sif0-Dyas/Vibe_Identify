"""Vibedentify desktop shell — a native Windows window around the WSL web app.

This is an ADDITIVE launcher. It does not modify any existing Vibedentify code:
it opens the exact same Flask UI (served from WSL on :5005) inside a chromeless
native window (Edge WebView2), boots the WSL backend for you if it isn't already
running, and injects a small JS shim at runtime that adds two things a plain
browser can't do:

  * a native Windows *folder* picker that translates  C:\\Users\\...\\Music
    ->  /mnt/c/Users/.../Music  and feeds it to the app's existing runBatch(),
  * an always-visible "add files" button that reuses the app's existing native
    file picker (#picker) + enqueue() upload path.

Run:            pythonw genre_app.pyw        (or double-click "Launch Vibedentify.vbs")
Self-test:      python  genre_app.pyw --selftest   (no window; checks the pure logic)

Config below (WSL_PYTHON / WIN_PROJECT / PORT) is the only thing you may need to
edit for a different machine or WSL user.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

# --------------------------------------------------------------------------- #
# Config — the only machine-specific knobs.
# --------------------------------------------------------------------------- #
PORT = int(os.environ.get("GENRE_PORT", "5005"))
BASE_URL = f"http://127.0.0.1:{PORT}"

# Windows-side location of the project (used only to derive the WSL path below,
# so there is a single source of truth for "where the app lives").
WIN_PROJECT = os.environ.get(
    "GENRE_WIN_PROJECT", r"C:\Users\mrand\Documents\CODING\Genre Identifier"
)

# The venv Python inside WSL that has Essentia installed (see project memory).
WSL_PYTHON = os.environ.get("GENRE_WSL_PYTHON", "/home/euphy/genre/bin/python")

# WSL distro to run in. Empty => the default distro (`wsl.exe` with no -d).
WSL_DISTRO = os.environ.get("GENRE_WSL_DISTRO", "").strip()

# FAKE_ANALYZER=1 in the backend => instant fake results, no model load. Handy
# for trying the shell without waiting on Essentia. Off by default.
FAKE = os.environ.get("GENRE_DESKTOP_FAKE", "") == "1"

# How long to wait for the backend to answer after we start it (real Essentia
# imports + first model touch can be slow on a cold start).
BOOT_TIMEOUT_S = int(os.environ.get("GENRE_BOOT_TIMEOUT", "150"))

CREATE_NO_WINDOW = 0x08000000  # keep the WSL console from flashing up


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested by --selftest, no GUI / no WSL needed).
# --------------------------------------------------------------------------- #
def win_to_wsl(path: str) -> str | None:
    """Translate a Windows path to its WSL /mnt/<drive>/... equivalent.

    Returns None for paths with no drive letter (e.g. bare UNC \\\\server\\share),
    which WSL mounts differently and we don't try to guess.
    """
    if not path:
        return None
    p = os.path.abspath(path)
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if not m:
        return None
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
    rest = rest.rstrip("/")
    return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"


WSL_PROJECT = win_to_wsl(WIN_PROJECT) or WIN_PROJECT


def backend_up() -> bool:
    """True if something is answering on the app's port."""
    try:
        with urllib.request.urlopen(BASE_URL + "/", timeout=2) as r:
            return r.status < 500
    except Exception:
        return False


def _wsl_cmd() -> list[str]:
    """The argv (list form => the space in the folder name is safe) that starts
    the Flask backend inside WSL."""
    cmd = ["wsl.exe"]
    if WSL_DISTRO:
        cmd += ["-d", WSL_DISTRO]
    cmd += ["--cd", WSL_PROJECT, "--"]
    if FAKE:
        cmd += ["env", "FAKE_ANALYZER=1"]
    cmd += [WSL_PYTHON, "-m", "vibedentify"]
    return cmd


def start_backend() -> subprocess.Popen | None:
    """Launch `python -m vibedentify` inside WSL, detached and window-less.

    We deliberately do NOT kill this on exit: if you already had the server
    running (your normal browser workflow) we reuse it, and leaving it up means
    the browser workflow keeps working after you close the desktop window.
    """
    try:
        return subprocess.Popen(
            _wsl_cmd(),
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None  # wsl.exe not on PATH


# --------------------------------------------------------------------------- #
# The JS shim injected into the live page (only inside this shell). It wires the
# native folder picker to the app's existing runBatch(), and adds an "add files"
# button that reuses the app's own #picker. Nothing here touches app.js on disk.
# --------------------------------------------------------------------------- #
INJECT_JS = r"""
(function () {
  if (window.__vibeDesk) return;            // idempotent across reloads
  if (!window.pywebview || !window.pywebview.api) return;
  window.__vibeDesk = true;
  document.body.classList.add('desktop-shell');

  // 1) Intercept the "batch folder" button BEFORE its own prompt() handler runs.
  //    A capture-phase listener on document fires ahead of the target's own
  //    click listener, so stopImmediatePropagation() cleanly preempts it.
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('#batch-btn') : null;
    if (!btn) return;
    e.stopImmediatePropagation();
    e.preventDefault();
    Promise.resolve(window.pywebview.api.pick_folder()).then(function (wslPath) {
      if (wslPath && typeof window.runBatch === 'function') {
        window.runBatch(wslPath);
      }
    });
  }, true);

  // 2) Add an always-visible "add files" button next to the batch button that
  //    reuses the app's existing native file picker (#picker) + upload path.
  var batchBtn = document.getElementById('batch-btn');
  var picker = document.getElementById('picker');
  if (batchBtn && picker && !document.getElementById('desk-addfiles')) {
    var b = document.createElement('button');
    b.type = 'button';
    b.id = 'desk-addfiles';
    b.textContent = '♪ add files';
    b.title = 'Pick audio files (native Windows dialog)';
    batchBtn.insertAdjacentElement('afterend', b);
    b.addEventListener('click', function () { picker.click(); });
  }
})();
"""

# Splash + error pages shown while the backend is (not) coming up. Kept inline so
# the shell is a single self-contained file.
LOADING_HTML = """
<!doctype html><meta charset="utf-8">
<title>Vibedentify</title>
<style>
  html,body{height:100%;margin:0;background:#0b0d12;color:#e6e9ef;
    font:15px/1.5 'Segoe UI',system-ui,sans-serif;display:grid;place-items:center}
  .box{text-align:center}
  .dot{width:9px;height:9px;border-radius:50%;background:#5ac8fa;display:inline-block;
    margin:0 3px;animation:p 1s infinite ease-in-out}
  .dot:nth-child(2){animation-delay:.15s}.dot:nth-child(3){animation-delay:.3s}
  @keyframes p{0%,80%,100%{opacity:.25;transform:translateY(0)}40%{opacity:1;transform:translateY(-5px)}}
  h1{font-weight:600;letter-spacing:.5px;margin:0 0 6px}
  p{color:#9aa3b2;margin:.3em 0}
  code{color:#c9d3e6;background:#161a22;padding:1px 6px;border-radius:5px}
</style>
<div class="box">
  <h1>Vibedentify</h1>
  <p id="msg">Starting the analysis engine&hellip;</p>
  <div><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
</div>
"""


def error_html(detail: str) -> str:
    return f"""
<!doctype html><meta charset="utf-8"><title>Vibedentify — can't start</title>
<style>
  html,body{{height:100%;margin:0;background:#0b0d12;color:#e6e9ef;
    font:15px/1.6 'Segoe UI',system-ui,sans-serif;display:grid;place-items:center}}
  .box{{max-width:640px;padding:0 28px}}
  h1{{color:#ff6b6b;font-weight:600}}
  code{{color:#c9d3e6;background:#161a22;padding:2px 7px;border-radius:6px;
    display:inline-block;margin:2px 0}}
  li{{margin:.4em 0}} a{{color:#5ac8fa}}
</style>
<div class="box">
  <h1>Couldn't reach the analysis engine</h1>
  <p>The desktop window is fine, but the Vibedentify backend on
     <code>{BASE_URL}</code> didn't come up within {BOOT_TIMEOUT_S}s.</p>
  <p>{detail}</p>
  <p>Start it by hand to see the error, then relaunch this app:</p>
  <ul>
    <li><code>wsl</code></li>
    <li><code>cd "{WSL_PROJECT}"</code></li>
    <li><code>{WSL_PYTHON} -m vibedentify</code></li>
  </ul>
  <p>Check <code>WSL_PYTHON</code> / <code>WIN_PROJECT</code> at the top of
     <code>genre_app.pyw</code> if the paths above look wrong.</p>
</div>
"""


# --------------------------------------------------------------------------- #
# GUI (pywebview). Imported lazily so --selftest runs without pywebview present.
# --------------------------------------------------------------------------- #
class Api:
    """Methods callable from the page as window.pywebview.api.<name>()."""

    def __init__(self):
        self._window = None

    def bind(self, window):
        self._window = window

    def pick_folder(self):
        """Native Windows folder dialog -> WSL path string ('' if cancelled)."""
        import webview

        start_dir = os.path.join(os.path.expanduser("~"), "Music")
        if not os.path.isdir(start_dir):
            start_dir = os.path.expanduser("~")
        result = self._window.create_file_dialog(
            webview.FOLDER_DIALOG, directory=start_dir
        )
        if not result:
            return ""
        win_path = result[0] if isinstance(result, (list, tuple)) else result
        return win_to_wsl(win_path) or ""


def _boot_and_load(window):
    """Runs in pywebview's worker thread once the GUI is up: ensure the backend
    is answering, then navigate the window to the real app."""
    started = False
    if not backend_up():
        if start_backend() is None:
            window.load_html(error_html("<b>wsl.exe was not found on PATH.</b>"))
            return
        started = True

    deadline = time.time() + BOOT_TIMEOUT_S
    while time.time() < deadline:
        if backend_up():
            window.load_url(BASE_URL)  # -> triggers 'loaded' -> INJECT_JS
            return
        time.sleep(0.6)

    detail = (
        "We launched it but it never answered — the first cold start can be slow "
        "while Essentia and the models load."
        if started
        else "It doesn't look like it's running."
    )
    window.load_html(error_html(detail))


def main():
    import webview

    api = Api()
    window = webview.create_window(
        "Vibedentify",
        html=LOADING_HTML,
        js_api=api,
        width=1280,
        height=860,
        min_size=(900, 600),
        background_color="#0b0d12",
    )
    api.bind(window)

    def on_loaded():
        try:
            url = window.get_current_url() or ""
        except Exception:
            url = ""
        if url.startswith(BASE_URL):
            window.evaluate_js(INJECT_JS)

    window.events.loaded += on_loaded
    webview.start(_boot_and_load, window)


# --------------------------------------------------------------------------- #
# Self-test: exercises the pure logic without a window or WSL.
# --------------------------------------------------------------------------- #
def _selftest() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  [{'ok' if good else 'XX'}] {name}: {got!r}")
        if not good:
            print(f"        expected {want!r}")

    print("win_to_wsl:")
    check("simple", win_to_wsl(r"C:\Users\mrand\Music"), "/mnt/c/Users/mrand/Music")
    check("lowercase drive", win_to_wsl(r"d:\Beats"), "/mnt/d/Beats")
    check(
        "space in path",
        win_to_wsl(r"C:\Users\mrand\Documents\CODING\Genre Identifier"),
        "/mnt/c/Users/mrand/Documents/CODING/Genre Identifier",
    )
    check("trailing slash", win_to_wsl("E:\\Music\\"), "/mnt/e/Music")
    check("drive root", win_to_wsl("C:\\"), "/mnt/c")
    check("unc -> None", win_to_wsl(r"\\server\share\x"), None)
    check("empty -> None", win_to_wsl(""), None)

    print("derived WSL project path:")
    check(
        "WSL_PROJECT",
        WSL_PROJECT,
        "/mnt/c/Users/mrand/Documents/CODING/Genre Identifier",
    )

    print("wsl launch command:")
    cmd = _wsl_cmd()
    print("  ", cmd)
    # the project path must be one intact argv element (space preserved)
    check("project is one arg", WSL_PROJECT in cmd, True)
    check("runs the module", cmd[-3:], [WSL_PYTHON, "-m", "vibedentify"])

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    main()
