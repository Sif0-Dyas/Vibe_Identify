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
| `PORT` | `GENRE_PORT` | `5005` |
| `FAKE` | `GENRE_DESKTOP_FAKE=1` | off (real Essentia) |

`WSL_PROJECT` (the `/mnt/c/...` path) is derived from `WIN_PROJECT`, which in turn
defaults to this script's own project folder — so the shell boots whichever copy
of the app it ships with (this branch/worktree, or the original checkout).

Set `GENRE_DESKTOP_FAKE=1` to boot the backend in fake-analyzer mode (instant
results, no model load) — handy for trying the window itself.

## Notes / behavior

- **It reuses a running server.** If you already ran `python -m vibedentify` in
  WSL, the shell just attaches to it. If not, it starts one and **leaves it
  running** on close, so your browser workflow keeps working too. It never force-
  kills the WSL process (WSL process trees are fiddly); stop it the way you
  normally do.
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
