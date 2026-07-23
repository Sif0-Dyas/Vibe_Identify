# Vibedentify — Windows desktop shell

A native, chromeless Windows window around the existing Vibedentify web app. It
does **not** change any app code: it opens the same UI (served from WSL on
`:5005`) in an Edge WebView2 window, starts the WSL backend for you if it isn't
already running, and adds two things a plain browser can't do.

```
 Launch Vibedentify.vbs   ──►  pythonw genre_app.pyw
                                  ├─ probe http://127.0.0.1:5005
                                  ├─ if down: wsl --cd "…/Genre Identifier" -- <venv-py> -m vibedentify
                                  ├─ show splash until it answers
                                  ├─ open the app in a native window (no browser chrome)
                                  └─ inject JS:  ⊕ batch folder → native folder dialog
                                                 ♪ add files    → native file dialog
```

## What it adds

| Button | Plain browser | In this shell |
| --- | --- | --- |
| **⊕ batch folder** | text prompt: *type a WSL path* `/mnt/c/...` | **native Windows folder dialog**; the picked `C:\…` folder is auto-translated to `/mnt/c/…` and handed to the app's existing `runBatch()` |
| **♪ add files** | (only the drop zone / empty-state click) | always-visible button that opens the **native file dialog** and reuses the app's existing upload path |

Everything else — the list, the 3-D map, waveforms, playback — is the unchanged
web app.

## One-time setup

1. Install the shell's only dependency into your **Windows** Python:
   ```
   py -m pip install -r requirements-desktop.txt
   ```
   (The Edge WebView2 runtime is already on Windows 11.)
2. Double-click **`Launch Vibedentify.vbs`**.

That's it. To make it feel installed: right-click the `.vbs` → *Create shortcut*,
then pin the shortcut to Start / the taskbar. (For a real icon and a standalone
`.exe`, see *Packaging* below.)

## Configuration

The only machine-specific values live at the top of `genre_app.pyw`, each also
overridable by an environment variable:

| Constant | Env var | Default |
| --- | --- | --- |
| `WIN_PROJECT` | `GENRE_WIN_PROJECT` | *auto-detected: the folder containing `desktop/`* |
| `WSL_PYTHON` | `GENRE_WSL_PYTHON` | `/home/euphy/genre/bin/python` |
| `WSL_DISTRO` | `GENRE_WSL_DISTRO` | *(default distro)* |
| `PORT` | `GENRE_PORT` | *random free loopback port* (set to pin one) |
| `TOKEN` | `GENRE_TOKEN` | *fresh per-session secret* (set to pin one) |
| `FAKE` | `GENRE_DESKTOP_FAKE=1` | off (real Essentia) |

`WSL_PROJECT` (the `/mnt/c/...` path) is derived from `WIN_PROJECT`, which in turn
defaults to this script's own project folder — so the shell boots whichever copy
of the app it ships with (this branch/worktree, or the original checkout).

Set `GENRE_DESKTOP_FAKE=1` to boot the backend in fake-analyzer mode (instant
results, no model load) — handy for trying the window itself.

### On a different machine

The `WIN_PROJECT` and `WSL_PYTHON` defaults are **this developer's exact paths**
(`WSL_PYTHON` is `/home/euphy/genre/bin/python`). They will not be right on your
machine — set the ones that describe your setup, either by exporting the env var
or by editing the constant at the top of `genre_app.pyw`:

- **`GENRE_WSL_PYTHON`** — the WSL venv Python that has Essentia installed. Almost
  always needs changing.
- **`GENRE_WIN_PROJECT`** — the project folder. Only needed if the auto-detected
  value (the folder containing `desktop/`) is wrong.

For example, for a WSL user `alice` whose venv is at `~/genre`:

```
set GENRE_WSL_PYTHON=/home/alice/genre/bin/python
```

If the configured project folder doesn't exist on disk, the shell now says so
directly on its error screen (naming the path and the variable to fix) instead of
only reporting a generic timeout.

## Security model

This branch runs the backend **locked down**, so it isn't just "a web server on
localhost anyone on your PC can poke":

- **Random loopback port.** Each launch binds a fresh, unused `127.0.0.1` port
  (WSL forwards it to Windows loopback only — never your LAN). No predictable,
  well-known port sits open. Pin one with `GENRE_PORT` if you need it stable.
- **Per-session secret token.** The shell generates a random token and hands it to
  the backend (`GENRE_TOKEN`). Every request must carry it — the first navigation
  passes `?k=<token>`, which the backend promotes to an httponly, `SameSite=Strict`
  cookie; the shell then strips the token from the URL. Other local processes and
  malicious localhost web pages don't have it, so they get **403**.
- **Host-header check.** Requests not addressed to a loopback host are rejected
  (403), defeating DNS-rebinding (a site resolving its own domain to 127.0.0.1 to
  reach this server from your browser).

All of this is **env-gated in the backend**: with no `GENRE_TOKEN` set (the
original `main` checkout, the plain browser workflow, the test suite) the guard is
a no-op and behavior is identical to before. Only the desktop shell turns it on.

> Trade-off: because the shell's server requires the token, you can't open its
> random port in a normal browser tab. Run `python -m vibedentify` yourself
> (no token) for a browsable instance.

## Notes / behavior

- **Own server, cleaned up.** The shell starts its own backend on the random port
  and **stops it when you close the window** (killed precisely by port, so a server
  you launched yourself on another port is never touched). If its port somehow
  already answers, it just attaches and won't kill on exit.
- **Cold start can take a while** the first time (Essentia import + model load).
  The splash waits up to `BOOT_TIMEOUT_S` (150 s) before showing a help screen.
- **Audio playback:** folder scans store the on-disk WSL path, so playback works;
  individual files added via *add files* upload their bytes (same as browser
  drag-drop) and have no server-side path, so they don't get playback — scan the
  folder instead if you want previews.

## Verify without opening a window

```
python genre_app.pyw --selftest
```

Runs the path-translation and launch-command logic and prints PASS/FAIL. No GUI,
no WSL, no pywebview needed.

## Packaging to a standalone .exe (optional)

```
py -m pip install pyinstaller
py -m PyInstaller --noconsole --onefile --name Vibedentify ^
   --icon vibedentify.ico desktop\genre_app.pyw
```

Drop a `vibedentify.ico` next to the script first if you want a custom taskbar
icon; otherwise omit `--icon`. The resulting `dist\Vibedentify.exe` launches like
installed software (still needs WSL + the venv for analysis).
