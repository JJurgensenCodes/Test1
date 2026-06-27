#!/usr/bin/env bash
# Builds CryptoLounge.app (one file) for macOS.
# Run from the project folder: chmod +x build_mac.sh && ./build_mac.sh
set -e

echo "Installing requirements..."
python3 -m pip install -r requirements.txt

echo "Building CryptoLounge.app with PyInstaller..."
python3 -m PyInstaller --noconfirm --onefile --console --name CryptoLounge \
    --add-data "static:static" \
    desktop_launcher.py

if [ ! -d "dist/CryptoLounge.app" ] && [ ! -f "dist/CryptoLounge" ]; then
    echo "ERROR: build finished but nothing was found in dist/. Check the PyInstaller output above."
    exit 1
fi

echo ""
echo "SUCCESS. Find CryptoLounge in the dist/ folder."
echo "First launch: right-click -> Open (macOS Gatekeeper will warn once since"
echo "the app isn't notarized/signed -- normal for an unsigned personal build)."
