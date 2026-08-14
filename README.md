# media-01 — Local Media Player

Owner: Drop-in Maintainers
Version: 0.22.0
Status: beta
Last updated: 2026-08-14

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

- **0.22.0** — Publishes the currently playing track's local file path
  on the shared `vj_api` track-path bus (`publish_track_path()`, new
  `_publish_track_path()` called from `update()`). Owner: "essentia
  should work on media player data as well, those are using the same
  local files as the mixer." Same bus dj-mixer-01's `_exchange_bpm()`
  already uses; lets training-kit-01's packaging step run real
  independent analysis (Essentia) against the actual file regardless of
  which drop-in was the audio source. See `docs/adr/vj-system.md`
  (main repo).
- **0.21.0** — New `[media] boot_playlist` config key: force a specific
  named playlist (from the playlists store, not the separate hearted-
  tracks favorites set) at boot, unshuffled index landing on the
  playlist's own first entry. Built for unattended/headless use —
  `unicornviz --media-source --media-favorites` (training-daemon
  `--source media`) sets `boot_playlist='favorites'` plus `shuffle=false`/
  `repeat='off'` via CLI override. Gated by its own config key so normal
  interactive starts are unaffected; runs after `_restore_prefs()` so an
  explicit boot request always wins over a previous interactive
  session's saved mode. Falls back to the library view (logged) if the
  named playlist doesn't exist or is empty.
- **0.20.0** — **A session can now end, and says so.**  `repeat` defaults to **OFF** (was ALL), matching the DJ mixer's loop flip — a list that never ends cannot be walked away from, and an unattended run needs "the set is over" to be reachable at all; ALL and ONE stay one button-press away.  REPEAT OFF already *stopped* at the end of the list; what was missing was **announcing** it.  It now publishes `phase: 'over'` on the session bus — the same phase the DJ mixer publishes when its set clock runs out or its playlist is exhausted — so a consumer never has to know which source ended the session, or which app it came from.  Announced once, re-armed when something plays again, and survives an older core without the bus.
- **0.19.0** — New `[media] auto_play` config key: start playback of the first (post-shuffle) library track automatically at boot, no `Ctrl+Alt+M`/MIDI action needed. Built for unattended/headless use — `unicornviz --media-source` (training-daemon `--source media`) sets this via a CLI override. Consumed by a new `App._maybe_auto_play_media()` boot trigger in core, mirroring the RTMP streamer's existing `auto_start` pattern. See `docs/adr/vj-system.md` (main repo) and `drop-ins/training-kit-01/docs/headless-training.md`.
- **0.18.0** — **The library browser catches up with the mixer's.**  **ARTIST and TITLE get their own columns** instead of one joined string — joined text cannot column-align, and on a long list the eye scans one field at a time; an untagged file still shows its filename across the full width.  A **length column**, and the header says how long the whole view is (`5 tracks · 19:11`) rather than only how many rows, with a `+` when some lengths were never read so the figure reads as *at least* that.  Names trim from the **middle**, so `Artist … (Extended Mix)` keeps both the part that identifies the track and the part that says which version.  **Rows highlight on hover**, additively — hovering a playing row lifts it rather than hiding what it was saying.  And **the playing row drips**: a breathing cyan edge and inner glow over the purple bed, because a flat marker says *this one* and a moving one says *this one, right now*.  Slow on purpose — it sits behind text you are reading — and never invisible at its dimmest, since the animation is emphasis and not the only signal.
- **0.17.0** — **Accepts playlists handed over by other drop-ins.**  Registers as a *playlist sink* on the core hub, so the DJ mixer can right-click a set and send it here as a playlist **of the same name** — a set called "Friday Peak" arrives as "Friday Peak", not "Playlist 7".  Neither drop-in imports the other; the core hub is the seam, so either works installed alone.  Sending to a name that already exists **adds** rather than replacing, since the sender may be sending a second batch and discarding what was there is not recoverable, and the accepted count is what was actually taken (duplicates skipped) rather than an echo of what was asked for.
- **0.16.1** — fonts now resolve through the shared `unicornviz.fonts` resolver (bundled ui-font.ttf first, then per-platform system dirs including Windows) — fixes tiny bitmap-font fallback on Windows.
- **0.16.0** — **In-app tag editing.** Right-click a library track → **Edit tags…** to fix artist / title / album / genre / year without leaving the media deck. The write touches only the file's metadata — the audio is never re-encoded — and the library rescans so the row name updates. WAV/AIFF are skipped (no portable tag format). This is a deliberate copy of the dj-mixer-01 tag module; the two drop-ins run in isolation so each carries its own.

- **0.15.2** — Bigger favorite hearts in the library rows (15px, wider click target)
- **0.15.1** — Fix the VLC startup/play segfault for real: force the PulseAudio output (`--aout=pulse`, served by pipewire-pulse) whose volume path is race-safe, and defer every volume call until the playback clock is running (VLC’s native PipeWire output crashed in pw_thread_loop_lock when audio_set_volume raced output creation)
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
