"""
webapp.py — the ONE Flask backend
===================================
This is the single source of truth for the HTTP API and the static
dashboard. Everything else (the desktop exe, a plain local run, a real
web deployment) is just a different way of starting this same `app`
object — so the web version and the desktop version can never drift out
of sync with each other.

Run it directly as a normal local web server:
    python webapp.py
    -> http://127.0.0.1:8743

Deploy it to a real webpage (any host that runs a Python WSGI app —
Render, Railway, Fly.io, a VPS, etc.) with a production server, e.g.:
    pip install gunicorn
    gunicorn -w 1 -b 0.0.0.0:$PORT webapp:app

(-w 1 matters: core.py's in-memory exchange/session state and Stats file
locking assume a single worker process. Scale by running it on a bigger
box, not more workers.)

For the desktop .exe/.app build, see desktop_launcher.py — it imports
`app` from this exact file and wraps it in a native window.
"""
import os
import sys

from flask import Flask, jsonify, request, send_from_directory

import core

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8743))


def resource_path(relative: str) -> str:
    """Resolves a path that works running from source, bundled by
    PyInstaller (--add-data unpacks into sys._MEIPASS), or deployed
    normally on a server."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


STATIC_DIR = resource_path("static")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")


@app.after_request
def no_cache_headers(response):
    """Without this, browsers cache the dashboard/JS on disk since every
    run hits the same localhost URL — the classic 'I updated the files but
    the UI still looks old' bug."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/signal")
def api_signal():
    asset = request.args.get("asset", core.DEFAULT_ASSET).upper()
    horizon = request.args.get("horizon", "15m")
    if asset not in core.SYMBOLS:
        return jsonify({"error": f"Unknown asset {asset}"}), 400
    sig = core.get_signal(asset, horizon)
    # votes are tuples -> make JSON-friendly lists
    if "votes" in sig:
        sig["votes"] = [list(v) for v in sig["votes"]]
    return jsonify(sig)


@app.route("/api/stats")
def api_stats():
    asset = request.args.get("asset", core.DEFAULT_ASSET).upper()
    stats = core.Stats(asset)
    total = stats.wins + stats.losses
    rate = (stats.wins / total * 100) if total else None
    poly_total = stats.poly_wins + stats.poly_losses
    poly_rate = (stats.poly_wins / poly_total * 100) if poly_total else None
    return jsonify({
        "asset": asset, "wins": stats.wins, "losses": stats.losses,
        "win_rate": rate, "streak": stats.streak,
        "poly_wins": stats.poly_wins, "poly_losses": stats.poly_losses,
        "poly_win_rate": poly_rate, "poly_streak": stats.poly_streak,
        "history": list(reversed(stats.signals[-25:])),
    })


@app.route("/api/changelog")
def api_changelog():
    return jsonify({
        "beta_version": core.BETA_VERSION,
        "engine_version": core.VERSION,
        "entries": core.CHANGELOG,
        "symbols": {k: v for k, v in core.SYMBOLS.items()},
    })


@app.route("/api/discord", methods=["POST"])
def api_discord():
    body = request.get_json(force=True) or {}
    asset = body.get("asset", core.DEFAULT_ASSET).upper()
    horizon = body.get("horizon", "15m")
    webhook = body.get("webhook", "")
    if not webhook:
        return jsonify({"ok": False, "error": "No webhook URL provided"}), 400
    sig = core.get_signal(asset, horizon)
    ok = core.send_discord(core.format_signal_for_discord(sig), webhook)
    return jsonify({"ok": ok})


if __name__ == "__main__":
    import threading
    import time as _time
    import webbrowser

    def _open_browser():
        _time.sleep(1.0)  # give Flask a moment to bind first
        webbrowser.open(f"http://{HOST if HOST != '0.0.0.0' else '127.0.0.1'}:{PORT}/")

    threading.Thread(target=_open_browser, daemon=True).start()
    print(f"\nCryptoLounge_Beta_{core.BETA_VERSION} web server running at http://{HOST}:{PORT}\n")
    print("Opening in your default browser... close this window (or Ctrl+C) to stop the server.\n")
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
