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

import ntpath
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------- #
# Config — the only machine-specific knobs.
# --------------------------------------------------------------------------- #
# Port for this launch. If GENRE_PORT is set we honour it (a fixed custom port);
# otherwise configure() picks a free loopback port at launch so there is no
# predictable, well-known port sitting open. BASE_URL is recomputed in configure().
_FIXED_PORT = os.environ.get("GENRE_PORT")
PORT = int(_FIXED_PORT) if _FIXED_PORT else 5005
BASE_URL = f"http://127.0.0.1:{PORT}"

# Per-session shared secret. The backend (when handed GENRE_TOKEN) requires it on
# every request, so other local processes and malicious localhost web pages can't
# drive the API. A fresh random token is generated per launch unless one is given.
TOKEN = os.environ.get("GENRE_TOKEN", "")

# Windows-side location of the project = the folder that contains this desktop/
# dir. Deriving it from the script's own location (not a hardcoded path) means the
# shell boots whatever copy of the app it ships with — the original checkout, or a
# branch/worktree — so a "Windows version" branch is truly self-contained.
WIN_PROJECT = os.environ.get(
    "GENRE_WIN_PROJECT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
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
    # ntpath (not os.path) so this is correct off Windows too: os.path.abspath on
    # POSIX treats "C:\\..." as relative and prepends the CWD, mangling the drive
    # path; ntpath.abspath keeps Windows semantics on every platform, so the
    # --selftest (and thus CI on Linux) exercises the real translation.
    p = ntpath.abspath(path)
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if not m:
        return None
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
    rest = rest.rstrip("/")
    return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"


WSL_PROJECT = win_to_wsl(WIN_PROJECT) or WIN_PROJECT


def _free_loopback_port() -> int:
    """Ask the OS for an unused loopback port (bind :0, read it, release)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def configure():
    """Fix the port + token for this launch: a free loopback port unless the user
    pinned GENRE_PORT, and a fresh per-session secret unless GENRE_TOKEN was set."""
    global PORT, BASE_URL, TOKEN
    if not _FIXED_PORT:
        PORT = _free_loopback_port()
    BASE_URL = f"http://127.0.0.1:{PORT}"
    if not TOKEN:
        TOKEN = secrets.token_urlsafe(24)


def backend_up() -> bool:
    """True if something is answering on the app's port. A 4xx (e.g. the 403 from
    the auth guard when we probe without the token) still means the server is up."""
    try:
        with urllib.request.urlopen(BASE_URL + "/", timeout=2) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False


def _wsl_cmd() -> list[str]:
    """The argv (list form => the space in the folder name is safe) that starts
    the Flask backend inside WSL."""
    cmd = ["wsl.exe"]
    if WSL_DISTRO:
        cmd += ["-d", WSL_DISTRO]
    # `env VAR=... ` in front of the interpreter injects config into the WSL
    # process. (The space in --cd's path is safe: this is a real argv, not a shell
    # line.) The backend reads GENRE_PORT (bind) and GENRE_TOKEN (require on auth).
    cmd += ["--cd", WSL_PROJECT, "--", "env", f"GENRE_PORT={PORT}", f"GENRE_TOKEN={TOKEN}"]
    if FAKE:
        cmd += ["FAKE_ANALYZER=1"]
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


_STARTED_BY_US = False  # True once we launch our own backend, so we can stop it


def _shutdown_backend():
    """Stop the backend we started, killing precisely by port so we never touch a
    server the user launched themselves on another port. No-op if we reused one."""
    if not _STARTED_BY_US:
        return
    kill = (
        f"fuser -k {PORT}/tcp 2>/dev/null || "
        f"(command -v lsof >/dev/null && kill $(lsof -ti tcp:{PORT}) 2>/dev/null)"
    )
    cmd = ["wsl.exe"]
    if WSL_DISTRO:
        cmd += ["-d", WSL_DISTRO]
    cmd += ["--", "bash", "-lc", kill]
    try:
        subprocess.run(
            cmd,
            creationflags=CREATE_NO_WINDOW,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


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

  // Drop the one-time ?k=<token> from the address so it isn't left in history.
  try {
    if (location.search.indexOf('k=') !== -1) {
      history.replaceState({}, '', location.pathname + location.hash);
    }
  } catch (e) {}

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
<!-- Palette taken from the ACTIVE :root in vibedentify/static/app.css — the
     "Neon-DJ" theme whose :root overrides the earlier WINAMP SKIN block: chassis
     --bg #06080D, panel --panel #0C1016, hairline --line #1E2632, text
     --text #EAF2F8, dim --dim #7C8998, cyan --accent-a #22D3EE + violet
     --accent-b #7C5CFF, LCD --lcd #5DE9FF on well --lcd-bg #03141B. -->
<style>
  html,body{height:100%;margin:0;background:#06080D;color:#EAF2F8;
    font:15px/1.5 system-ui,'Segoe UI',sans-serif;display:grid;place-items:center}
  /* flat panel + hairline border + cyan glow, matching the Neon-DJ chassis */
  .box{text-align:center;background:#0C1016;padding:28px 44px;
    border:1px solid #1E2632;border-radius:12px;box-shadow:0 0 10px rgba(34,211,238,.35)}
  /* wordmark: cyan->violet gradient text with a cyan glow, like the app header */
  h1{margin:0 0 6px;font-weight:800;letter-spacing:-.01em;color:#22D3EE;
    background:linear-gradient(90deg,#22D3EE,#7C5CFF);
    -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;
    filter:drop-shadow(0 0 10px rgba(34,211,238,.5))}
  p{color:#7C8998;margin:.3em 0}
  /* dark cyan-tinted LCD well holding the readout dots, like the app's BPM cell */
  .lcd{display:inline-block;margin-top:12px;padding:9px 13px;background:#03141B;
    border:1px solid rgba(34,211,238,.20);border-radius:6px}
  .dot{width:9px;height:9px;border-radius:50%;background:#5DE9FF;display:inline-block;
    margin:0 3px;box-shadow:0 0 8px rgba(93,233,255,.7);animation:p 1s infinite ease-in-out}
  .dot:nth-child(2){animation-delay:.15s}.dot:nth-child(3){animation-delay:.3s}
  @keyframes p{0%,80%,100%{opacity:.25;transform:translateY(0)}40%{opacity:1;transform:translateY(-5px)}}
</style>
<div class="box">
  <h1>Vibedentify</h1>
  <p id="msg">Starting the analysis engine&hellip;</p>
  <div class="lcd"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
</div>
"""


def error_html(detail: str) -> str:
    return f"""
<!doctype html><meta charset="utf-8"><title>Vibedentify — can't start</title>
<!-- Palette taken from the ACTIVE :root in vibedentify/static/app.css — the
     "Neon-DJ" theme whose :root overrides the earlier WINAMP SKIN block: chassis
     --bg #06080D, panel --panel #0C1016, hairline --line #1E2632, text
     --text #EAF2F8, dim --dim #7C8998, warn/gold --gold #FFC46B, LCD --lcd
     #5DE9FF on well --lcd-bg #03141B. -->
<style>
  html,body{{height:100%;margin:0;background:#06080D;color:#EAF2F8;
    font:15px/1.6 system-ui,'Segoe UI',sans-serif;display:grid;place-items:center}}
  .box{{max-width:640px;padding:26px 32px;background:#0C1016;
    border:1px solid #1E2632;border-radius:12px;box-shadow:0 0 10px rgba(34,211,238,.25)}}
  /* amber heading — the Neon-DJ theme reserves --gold for alerts, so it reads as a warning */
  h1{{color:#FFC46B;font-weight:800;letter-spacing:-.01em}}
  p{{color:#7C8998}}
  /* code shown like the app's cyan LCD readouts: on a dark cyan-tinted well, mono */
  code{{color:#5DE9FF;background:#03141B;
    font-family:ui-monospace,Consolas,'Courier New',monospace;
    padding:2px 7px;border:1px solid rgba(34,211,238,.20);border-radius:5px;
    display:inline-block;margin:2px 0;text-shadow:0 0 6px rgba(93,233,255,.4)}}
  li{{margin:.4em 0}}
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
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=start_dir)
        if not result:
            return ""
        win_path = result[0] if isinstance(result, (list, tuple)) else result
        return win_to_wsl(win_path) or ""


def _boot_and_load(window):
    """Runs in pywebview's worker thread once the GUI is up: ensure the backend
    is answering, then navigate the window to the real app."""
    global _STARTED_BY_US
    started = False
    if not backend_up():
        if start_backend() is None:
            window.load_html(error_html("<b>wsl.exe was not found on PATH.</b>"))
            return
        started = _STARTED_BY_US = True

    deadline = time.time() + BOOT_TIMEOUT_S
    while time.time() < deadline:
        if backend_up():
            # first navigation carries the token as ?k=; the backend promotes it
            # to an httponly cookie and INJECT_JS strips it back off the URL.
            window.load_url(f"{BASE_URL}/{('?k=' + TOKEN) if TOKEN else ''}")
            return
        time.sleep(0.6)

    # Timed out. The single most common misconfiguration is a wrong project path
    # (the defaults below are one machine's exact folders), so if the configured
    # Windows folder doesn't exist on disk, say that first — it's almost certainly
    # why the backend never came up.
    if not os.path.isdir(WIN_PROJECT):
        detail = (
            f"<b>the configured project folder was not found: <code>{WIN_PROJECT}</code></b>"
            f" — edit <code>GENRE_WIN_PROJECT</code> / <code>WIN_PROJECT</code> to point at "
            f"your checkout."
        )
    elif started:
        detail = (
            "We launched it but it never answered — the first cold start can be slow "
            "while Essentia and the models load."
        )
    else:
        detail = "It doesn't look like it's running."
    window.load_html(error_html(detail))


def main():
    import webview

    configure()  # pick this launch's port + token before anything uses BASE_URL
    api = Api()
    window = webview.create_window(
        "Vibedentify",
        html=LOADING_HTML,
        js_api=api,
        width=1280,
        height=860,
        min_size=(900, 600),
        background_color="#06080D",  # Neon-DJ --bg, matches the splash chassis
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
    _shutdown_backend()  # window closed -> stop the backend we launched


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
    print(f"  WIN_PROJECT = {WIN_PROJECT!r}")
    # Assert the derivation *rule* (line: `win_to_wsl(WIN_PROJECT) or WIN_PROJECT`)
    # rather than the concrete /mnt/ value: this selftest also runs on Linux in CI,
    # where the auto-detected WIN_PROJECT is a POSIX path, not a `C:\...` one.
    check(
        "WSL_PROJECT matches its derivation rule",
        WSL_PROJECT,
        win_to_wsl(WIN_PROJECT) or WIN_PROJECT,
    )
    # And that a real Windows project path maps under /mnt/ (host-independent).
    check(
        "a Windows project path maps under /mnt/",
        win_to_wsl(r"C:\Users\me\Genre Identifier - Windows"),
        "/mnt/c/Users/me/Genre Identifier - Windows",
    )

    print("port + token (configure):")
    configure()
    check("free port is int > 1024", isinstance(PORT, int) and PORT > 1024, True)
    check("base url tracks port", BASE_URL, f"http://127.0.0.1:{PORT}")
    check("token generated", len(TOKEN) >= 24, True)

    print("wsl launch command:")
    cmd = _wsl_cmd()
    print("  ", cmd)
    # the project path must be one intact argv element (space preserved)
    check("project is one arg", WSL_PROJECT in cmd, True)
    check("passes GENRE_PORT", f"GENRE_PORT={PORT}" in cmd, True)
    check("passes GENRE_TOKEN", f"GENRE_TOKEN={TOKEN}" in cmd, True)
    check("runs the module", cmd[-3:], [WSL_PYTHON, "-m", "vibedentify"])

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    main()
