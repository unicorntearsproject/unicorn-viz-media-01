# media-01 — Local Media Player

Owner: Drop-in Maintainers
Version: 0.15.0
Status: beta
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

- **0.15.0** — **Favorites**: heart any track (row ♥ or right-click), new **♥ FAVS** tab next to LIBRARY/PLAYLISTS, persisted with the playlists store; checkbox fixes (one shared widget, bigger hit targets, no transport overlap at small sizes); **VLC startup segfault fix** — volume is never set on a media-less player (libvlc's PipeWire output crashed at init)
- **0.14.0** — **Nested accordion crates** (sub-folders expand along the open path, depth-tinted headers, `crate_depth` config, default 5); session prefs persist (repeat/shuffle/crossfade/volume/mode/playlist/track); stable list scrolling — double-clicking a row now plays that row (clicks no longer recenter the list)
- **0.13.0** — **Delete file** in the track context menu (arms a SURE?
  confirm, removes the file from disk and rescans).  The **platter ambience
  graduated to core** ("Now Spinning", fed by the now-playing hub, so the DJ
  mixer and Spotify announce on it too) — the Deck checkbox now drives
  `vj_api.toggle_now_spinning`; the private overlay code is gone.

- **0.12.0** — **Real crossfade.** The long-reserved `crossfade_ms` finally
  does something: on the VLC backend the player now runs **two players** and
  performs a true overlapping crossfade — the next track starts
  `crossfade_ms` (default 3 s) **before the outro ends** and the volumes ramp
  across; manual next/prev while playing crossfades too.  A **"crossfade
  between tracks" checkbox** sits with the others in the Deck (config
  `crossfade`, default on); on the mpv/ffplay fallbacks it shows
  "(needs VLC)" and stays informational.  Repeat modes are honoured
  (ONE crossfades into itself; OFF still stops at the end).

- **0.11.0** — **Loop button + platter ambience.** A **repeat toggle** sits
  next to shuffle (circular-arrow icon): **ALL** wraps the active
  playlist/crate (default), **ONE** repeats the current track, **OFF** stops
  at the end of the view (`repeat` config sets the startup mode).  And the
  **corner platter ambience**: while music plays, a mini neon platter card
  (glow, grooves, strobe spoke, rim progress dot, "NOW SPINNING" +
  title/artist) floats on the **main visualizer output**, projectM-overlay
  style — toggled by a checkbox in the Deck (`ambience` config default on,
  `ambience_corner` = br/bl/tr/tl).

- **0.10.0** — **Playlists, accordion crates, sort & excludes.** The Media
  Deck library gains **LIBRARY | PLAYLISTS tabs**: playlists (the mixer's
  *sets*, media-player naming) persist to `runtime/media_playlists.json`, with
  a picker flyout + **+ NEW**, right-click **Add to playlist »** on library
  rows and **Remove / Move up / Move down** in playlist view — and playback
  (next/prev/**auto-advance**) follows the open playlist.  The library shows
  **accordion crates** (one per top-level subfolder, ALL first, one open at a
  time), each with an **exclude toggle** (×) that hides that crate from the
  library, ALL, and playback — the interface way to keep loops/foley out of
  the listening rotation; excludes and the open crate persist.  A **sort
  flyout** (artist/title ↑↓, scan order) rounds out the browser.

- **0.9.2** — The mouse cursor now shows over the Media Deck (subsystem
  `is_open` cursor protocol) and **F toggles the window fullscreen**
  (borderless desktop) instead of falling through to the visualizer.

- **0.9.1** — The Media Deck is a **remote control, not the player**: closing
  the window keeps the music playing (new default), with an in-window
  **"keep playing when window is closed" checkbox** to opt into stop-on-close
  instead (`keep_playing_on_close` config sets the startup default).

- **0.9.0** — **Beta: the Media Deck window** (`Shift+L`), a single-deck
  sibling of the DJ mixer's board in the same visual language: a **big neon
  platter** (breathing glow, vinyl grooves, strobe spokes, orbiting sparkles,
  a progress dot riding the rim, elapsed time on the label), a now-playing
  block with previous-track line, a **click-to-seek progress bar** (VLC
  backend; display-only on subprocess fallbacks), chunky **PREV / PLAY / NEXT**
  transport with a pulsing play button, a **shuffle icon toggle**, a **volume
  slider** (VLC), and a **library browser** with search, wheel scroll, click
  select / double-click play, ↑/↓ + Enter keyboard nav, playing-track cyan
  highlight and a scrollbar.  Esc-Esc closes.  New backend surface:
  `seek_s`/`can_seek`/`can_volume`, controller `play_index`/`seek_to`/
  `library_names`.  Window config: `window_width`, `window_height`,
  `display_index`, `render_interval`.

- **0.6.0** — Register with the core **now-playing announcement hub**
  (`vj_api.register_now_playing`, priority 20) instead of being hard-wired in
  `app.py`; the banner/HUD machinery is now shared with Spotify and the DJ
  mixer.  Guarded no-op on older cores.

- **0.5.0** — initial versioned release.
