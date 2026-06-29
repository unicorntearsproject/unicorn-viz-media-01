#!/usr/bin/env bash
# media-01 installer
# Installs python-vlc and mutagen into the active Python environment.
# VLC must be installed separately via your system package manager.
#
# Linux:   sudo dnf install vlc   OR   sudo apt install vlc
# macOS:   brew install vlc
# Windows: install VLC from https://www.videolan.org/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing media-01 Python dependencies..."
pip install -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "==> Checking for VLC..."
if command -v vlc &>/dev/null; then
    echo "    VLC found: $(vlc --version 2>&1 | head -1)"
else
    echo "    WARNING: VLC not found in PATH."
    echo "    Install VLC before enabling media-01:"
    echo "      Fedora/RHEL:  sudo dnf install vlc"
    echo "      Ubuntu/Debian: sudo apt install vlc"
    echo "      macOS:         brew install vlc"
    echo "      Windows:       https://www.videolan.org/"
    echo ""
    echo "    Fallback: mpv or ffplay will be used if VLC is unavailable."
fi

echo ""
echo "==> media-01 install complete."
echo "    Enable in config.toml:"
echo "    [media]"
echo "    enabled = true"
echo "    media_dir = \"/path/to/your/music\""
