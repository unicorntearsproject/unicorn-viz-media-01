# media-01 — Local Media Player

Owner: Drop-in Maintainers
Version: 0.5.0
Status: active
Last updated: 2026-06-29

`media-01` is a local audio playback subsystem for unicorn-viz.  It scans a
directory of audio files, reads ID3/Vorbis/FLAC tags via `mutagen`, and plays
them back via `python-vlc` (libvlc) with fallback to `mpv` or `ffplay`.

The now-playing HUD band and overlay banner display the currently playing track
in the exact same format as the Spotify drop-in — no visual changes.

## Features

- **Full tag support** — ID3v2, Vorbis comments, FLAC, AAC/MP4, WMA via mutagen
- **Supported formats** — mp3, flac, ogg, opus, wav, aiff, m4a, aac, wma, ape, wv
- **Cross-platform playback** — python-vlc (libvlc) on Linux, Windows, macOS
- **Fallback chain** — mpv → ffplay when VLC is unavailable
- **Shuffle** — randomized play order on load; toggle at runtime
- **Auto-advance** — plays through the full library, loops at end
- **Now-playing banner** — same overlay as Spotify with track/artist/album/progress
- **Same HUD band** — artist | track | progress rail, identical to Spotify display

## Planned (future)

- **Cross-fade** — `crossfade_ms` config key is reserved; backend support TBD
- **Auto-mixing** — BPM-aware transition timing based on beat grid

## Hotkeys

| Key | Action |
|-----|--------|
| `Ctrl+Alt+M` | Play / Pause |
| `Ctrl+Alt+N` | Next track |
| `Ctrl+Alt+B` | Previous track (or restart current if > 3 s in) |
| `Ctrl+Alt+Shift+M` | Toggle shuffle |

## Installation

```bash
bash drop-ins/media-01/install.sh
```

This installs `python-vlc` and `mutagen`.  VLC must also be installed:

```bash
# Fedora / RHEL
sudo dnf install vlc

# Ubuntu / Debian
sudo apt install vlc

# macOS
brew install vlc

# Windows — download from https://www.videolan.org/
```

## Configuration

Add to `config.toml`:

```toml
[media]
enabled = true
media_dir = "/path/to/your/music"   # or leave empty for drop-ins/media-01/media/
shuffle = true
volume = 100                          # 0-200; 100 = 100%
now_playing_banner = true
now_playing_banner_hold_s = 10.0
now_playing_banner_delay_s = 2.0
```

## Priority vs Spotify

When both `media-01` and `spotify-01` are enabled, the HUD band shows whichever
source is **actively playing**.  If media-01 is playing, it takes the band; if
media-01 is paused/stopped, the Spotify track (if playing) takes the band.

## Source

Private repository: `https://github.com/iDoMeteor/unicorn-viz-media-01`

## Changelog

- **0.5.0** — initial versioned release.
