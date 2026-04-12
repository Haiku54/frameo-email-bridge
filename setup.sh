#!/bin/bash
set -euo pipefail

# Always run from the script's own directory so relative paths resolve correctly,
# even when the user calls `bash /path/to/setup.sh` from somewhere else.
cd "$(dirname "$0")"

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
    echo "  Install with: sudo apt install python3 python3-venv  (Linux)"
    echo "                brew install python                     (macOS)"
    exit 1
fi

# --- Require ADB ---
if command -v adb &>/dev/null; then
    echo "  ADB: $(adb version | head -1)"
else
    echo "  ERROR: adb is not installed. It is required for the initial frame setup."
    echo "  Install with: sudo apt install adb                    (Linux)"
    echo "                brew install android-platform-tools     (macOS)"
    exit 1
fi

# --- Check ffmpeg (optional, for video support) ---
if command -v ffmpeg &>/dev/null; then
    ffmpeg_line="$(ffmpeg -version 2>&1 | head -1 | awk '{print $1, $2, $3}')"
    echo "  ffmpeg: $ffmpeg_line"
else
    echo "  WARNING: ffmpeg is not installed. Video support will fail when videos arrive."
    echo "  Install with: sudo apt install ffmpeg                 (Linux)"
    echo "                brew install ffmpeg                     (macOS)"
    echo "  You can set 'accept_videos: false' in config.yaml to disable video handling."
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

# --- Install dependencies directly into the venv (never relies on `source`) ---
echo
echo "  Installing Python dependencies..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "  Dependencies installed (includes Google Photos sync libraries)"

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
        echo "  ERROR: config.yaml.example is missing from the project."
        exit 1
    fi
fi

# --- Run interactive configuration using the venv Python explicitly ---
echo
echo "=================================="
echo "  Launching interactive setup..."
echo "=================================="
echo
if ! .venv/bin/python configure.py; then
    echo
    echo "  ERROR: Interactive setup failed. Review the messages above."
    echo "  You can re-run setup later with: bash setup.sh"
    echo "  Or just re-run configuration with: .venv/bin/python configure.py"
    exit 1
fi
