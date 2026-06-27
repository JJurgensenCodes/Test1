# CryptoLounge — merged engine + single web GUI

## What changed from your original files

- **One engine**: `core.py` (your most advanced version — confluence
  scoring, Bridge/Physics/Synthesis models, session-aware gating,
  retraining) is the single source of truth. Nothing else computes a
  signal independently.
- **Kalshi fetch is now resilient**: tries 3 known Kalshi API hosts and
  caches whichever one actually answers, instead of hardcoding one host
  (ported from your `kalshi.py`).
- **Polymarket added as an independent benchmark**: every 15-min BTC
  signal now also pulls Polymarket's own crowd-priced odds for the same
  window (ported from your `polymarket.py`) and tracks its standalone
  win/loss record exactly like the Bridge/Physics trackers already did —
  so over time you can see, with real numbers, whether your model is
  beating that crowd's price or just echoing it.
- **One frontend, not three**: `streamlit_app.py`, `desktop_app.py`
  (Tkinter), and the old `webgui_app.py` are retired. There's now exactly
  one UI (`static/index.html` + the Flask API), so there's only one thing
  to restyle and only one thing that can go out of sync.
- **`webapp.py` / `desktop_launcher.py` split**: `webapp.py` defines the
  Flask app and is what you deploy to a real webpage. `desktop_launcher.py`
  imports that *exact same* app object and wraps it in a native window —
  this is what PyInstaller packages into the exe/.app. They share one
  Flask instance, so the web version and the desktop version cannot drift
  apart.
- **Retired** (superseded, kept in your uploads folder but not part of
  this build): `bot.py`, `monitor.py` (the pre-split monolith — 97%
  identical to `core.py`), `app.py`/`exchanges.py`/`indicators.py`/
  `store.py`/`discord_notify.py`/`launcher.py` (the earlier FastAPI
  prototype — its real-odds idea is now folded into `core.py`).

## Run it locally

```
pip install -r requirements.txt
python webapp.py
```
Opens at `http://127.0.0.1:8743`.

## Deploy to a real webpage

Any host that runs a Python web app (Render, Railway, Fly.io, a VPS, PaaS
of your choice) works. A `Procfile` is included:
```
web: gunicorn -w 1 -b 0.0.0.0:$PORT webapp:app
```
Push this folder to the host, point it at that Procfile (or run that
command directly), and it's live. Keep `-w 1` — the in-memory state
assumes one process.

## Package into a Windows .exe or Mac .app

**Important**: PyInstaller does not cross-compile. You build the
Windows .exe *on a Windows machine* and the Mac .app *on a Mac*. Both
scripts are included and produce a one-file, no-console-window app:

- Windows: `build_windows.bat`
- Mac: `chmod +x build_mac.sh && ./build_mac.sh`

Both call `desktop_launcher.py`, which starts the same Flask app on a
free local port and opens it in a native window (falls back to your
default browser if no native webview runtime is found).

## Files

```
core.py               the engine — all signal/model logic lives here
webapp.py              Flask API + static file serving (deploy this)
desktop_launcher.py     native-window wrapper around webapp.py's app (build this into the exe)
static/index.html       the dashboard UI
requirements.txt
Procfile                for web deploy platforms
build_windows.bat        Windows packaging script
build_mac.sh             Mac packaging script
```

## Known limitation from this build session

I patched and unit-tested the new logic (Kalshi multi-host fallback,
Polymarket fetch/parsing, the new win/loss tracker) with mocked network
responses, since this sandbox can't reach Kalshi/Polymarket/exchange
APIs directly. Run it locally for a real end-to-end check before relying
on it — the patterns are verified, but I haven't seen it hit the live
APIs.
