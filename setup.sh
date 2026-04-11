#!/bin/bash
set -euo pipefail

echo "=================================="
echo "  Frameo Email Bridge - Setup"
echo "=================================="
echo

# --- Check Python version ---
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        major=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo 0)
        minor=$("$cmd" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo 0)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$cmd"
            echo "  Python: $cmd ($major.$minor)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "  ERROR: Python 3.10+ is required."
    echo "  Install with: sudo apt install python3"
    exit 1
fi

# --- Check ADB ---
if command -v adb &>/dev/null; then
    echo "  ADB: $(adb version | head -1)"
else
    echo "  WARNING: adb is not installed."
    echo "  Install with: sudo apt install adb  (Linux)"
    echo "                brew install android-platform-tools  (macOS)"
    echo "  You need it for the initial frame setup."
fi

# --- Check ffmpeg (optional, for video support) ---
if command -v ffmpeg &>/dev/null; then
    echo "  ffmpeg: $(ffmpeg -version 2>&1 | head -1 | awk '{print $1, $2, $3}')"
else
    echo "  WARNING: ffmpeg is not installed. Video support will be disabled."
    echo "  Install with: sudo apt install ffmpeg  (Linux)"
    echo "                brew install ffmpeg  (macOS)"
fi

# --- Create virtual environment ---
if [ ! -d ".venv" ]; then
    echo
    echo "  Creating virtual environment..."
    $PYTHON -m venv .venv
    echo "  Virtual environment created at .venv/"
else
    echo "  Virtual environment already exists"
fi

# --- Activate and install dependencies ---
echo
echo "  Installing Python dependencies..."
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo "  Dependencies installed"

# --- Create runtime directories ---
echo
echo "  Creating runtime directories..."
mkdir -p inbox processed archive archive/failed logs data
echo "  Directories created"

# --- Ensure config.yaml exists (from example) ---
if [ ! -f "config.yaml" ]; then
    if [ -f "config.yaml.example" ]; then
        cp config.yaml.example config.yaml
        echo "  Created config.yaml from config.yaml.example"
    else
        echo "  ERROR: config.yaml.example is missing."
        exit 1
    fi
fi

# --- Run interactive configuration ---
echo
echo "=================================="
echo "  Launching interactive setup..."
echo "=================================="
echo
python configure.py
