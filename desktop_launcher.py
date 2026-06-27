"""
desktop_launcher.py — the ONE desktop/exe entry point
========================================================
Imports the exact same Flask `app` from webapp.py, runs it on a free local
port, and opens it in your default web browser. No native-window
dependency (pywebview) -- one less thing that can fail to bundle into the
.exe/.app, and it's what was actually asked for.

Run directly:
    python desktop_launcher.py

Package into a one-file Windows .exe / Mac .app with PyInstaller — see
build_windows.bat / build_mac.sh alongside this file.
"""
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Make sure imports resolve whether running from source or from a
# PyInstaller-frozen bundle.
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
sys.path.insert(0, str(BASE_DIR))

import core
from webapp import app

LOCAL_HOST = "127.0.0.1"


def find_free_port(preferred=8743):
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((LOCAL_HOST, port)) != 0:
                return port
    return preferred


def main():
    port = find_free_port()
    url = f"http://{LOCAL_HOST}:{port}/"
    title = f"CryptoLounge_Beta_{core.BETA_VERSION}"

    def run_flask():
        app.run(host=LOCAL_HOST, port=port, debug=False, use_reloader=False)

    threading.Thread(target=run_flask, daemon=True).start()
    time.sleep(0.8)  # give Flask a moment to bind before the browser loads it

    print(f"\n{title} running at {url}")
    print("Opening in your default browser. Close this window (or press Ctrl+C) to stop it.\n")
    webbrowser.open(url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
