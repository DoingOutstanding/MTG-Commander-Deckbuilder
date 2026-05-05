#!/bin/bash
# ---------------------------------------------------------------------
#  MTG Commander Deckbuilder — one-click launcher (macOS)
#
#  Double-click this file in Finder to start the deckbuilder.  It will:
#    1. Verify Python is installed
#    2. Install Flask if it isn't already
#    3. Build cards.jsonl if missing
#    4. Start the local web server
#    5. Open your default browser to http://127.0.0.1:5000
#
#  Close the Terminal window to stop the server.
#
#  If macOS refuses to run this file the first time, right-click it and
#  pick "Open"; it'll ask once for permission and then remember.
# ---------------------------------------------------------------------

cd "$(dirname "$0")"

echo
echo "============================================================"
echo "  MTG Commander Deckbuilder"
echo "============================================================"
echo

# Find a Python 3 interpreter
PY=""
if   command -v python3 >/dev/null 2>&1; then PY="python3"
elif command -v python  >/dev/null 2>&1; then PY="python"
fi

if [ -z "$PY" ]; then
    echo "[ERROR] Python 3 is not installed."
    echo
    echo "Install it from https://www.python.org/downloads/ or via Homebrew:"
    echo "    brew install python"
    echo
    read -p "Press Enter to exit..."
    exit 1
fi

echo "Using Python: $PY"
$PY --version

# Make sure Flask is available
if ! $PY -c "import flask" 2>/dev/null; then
    echo
    echo "Flask is not installed.  Installing now..."
    $PY -m pip install --quiet flask || {
        echo "[ERROR] Failed to install Flask."
        read -p "Press Enter to exit..."
        exit 1
    }
fi

# Build cards.jsonl if missing
if [ ! -f cards.jsonl ]; then
    echo
    echo "cards.jsonl not found.  Building it from oracle-cards-*.json..."
    $PY profile.py || {
        echo
        echo "[ERROR] Couldn't build cards.jsonl."
        echo "Make sure an oracle-cards-*.json file from Scryfall is in this folder"
        echo "or in your Downloads / Desktop.  Download it from:"
        echo "    https://scryfall.com/docs/api/bulk-data"
        echo "and pick 'Oracle Cards'."
        read -p "Press Enter to exit..."
        exit 1
    }
fi

# Open the browser ~3 seconds after starting the server
( sleep 3 && open http://127.0.0.1:5000 ) &

echo
echo "Starting server at http://127.0.0.1:5000"
echo "Close this window to stop the deckbuilder."
echo
exec $PY app.py
