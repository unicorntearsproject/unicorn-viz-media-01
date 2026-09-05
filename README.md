# media-01 — Local Media Player

Owner: Drop-in Maintainers
Version: 0.29.0
Status: beta
Last updated: 2026-09-04

`media-01` is a local audio playback subsystem for unicorn-viz.  It scans a
directory of audio files, reads ID3/Vorbis/FLAC tags via `mutagen`, and plays
them back via `python-vlc` (libvlc) with fallback to `mpv` or `ffplay`.

The now-playing HUD band and overlay banner display the currently playing track
in the exact same format as the Spotify drop-in — no visual changes.

## Features

- **Full tag support** — ID3v2, Vorbis comments, FLAC, AAC/MP4, WMA via mutagen
  (title / artist / album / genre / year)
- **Media Deck window** (`Shift+L`) — the mixer board's single-deck sibling:
  audio-reactive neon platter with a live spectrum ring, a full-width
  64-band spectrum strip with dripping peak-holds under the transport, a
  SPARKLE knob for the decorative twinkle layer, library browser with
  crates, faceted filters (artist / album / genre / decade), search,
  playlists / favorites / recent views, an up-next queue, hover tooltips
  on everything, real desktop-speed scrolling with draggable scrollbars
  everywhere, and in-place playlist management (rename / duplicate /
  prune / delete)
- **Auto-level** — loudness-matches tracks the way the mixer's decks do
  (K-weighted RMS over the loudest half, clamped gain, measured once per
  track and cached); on by default, one checkbox to turn it off
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

- **0.29.0** — **Control Room panel (P3 of
  `docs/planning/control-room-drop-in-integration-plan-2026-09-05.md`).**
  A medium panel on the shared `sources` page: PLAY/PAUSE, PREV, NEXT,
  SHUFFLE, REPEAT, XFADE, and DECK (open/close the Media Deck window),
  all routed through the same verbs the hotkeys use. Rows show
  shuffle/repeat/crossfade/auto-level state, deliberately with no
  title/artist -- media-01 already feeds the core now-playing hub and
  INFO shows the active source, so repeating it here would be a second,
  driftable copy. Registered only past the `enabled` early-return in
  `__init__` (a disabled instance is discarded outright by `app.py`, so
  registering before that guard would orphan the panel with no
  `shutdown()` ever called to unregister it); `shutdown()` now
  unregisters it.
- **0.28.4** — **Fixed a hard crash (SIGSEGV) when starting a track with
  crossfade on.** `crossfade_to()` called `audio_set_volume(0)` on the
  incoming player the instant after `play()`, but `play()` is asynchronous:
  libvlc has no audio output object yet, and the pulse/PipeWire module
  dereferenced a null thread loop and killed the process. The surrounding
  `try/except` never saw it — a native segfault is not a Python exception.
  Two more paths had the same unguarded call: the crossfade ramp in `tick()`
  (both players, every frame) and `_end_fade()` (on a player it had just
  stopped). All three now go through one guard that only touches a player
  whose audio output is actually live, which is the rule `_VlcBackend`
  already documented but did not enforce.

- **0.28.3** — **The spectrum strip was still flat after 0.28.1's fix, for
  real this time.** 0.28.1 correctly diagnosed `audio.fft`'s per-frame
  max-normalization as the cause, but its own fix (rescaling the raw
  linear FFT's bass/mid/treble thirds by the analyzer's scalars) was still
  built on a linearly-binned spectrum, which can't actually be balanced by
  rescaling alone. Owner: "check out the audio spectrum core effect for
  it's solution, it had that problem once." It had: the core `Audio
  Spectrum` effect (`unicornviz/effects/audio_spectrum.py`) reads
  `audio.bands` — the analyzer's own already-**log-spaced**, already-
  normalized 64-band perceptual spectrum (shared across every consumer,
  effects and Auto VJ alike) — instead of the raw linear `audio.fft`, and
  applies a pink-noise visual gain curve `(center_hz / 30.0) ** 0.35` plus
  a soft-knee compressor (`_BAR_KNEE = 0.6`) on top, boosting higher bands
  to offset music's natural rolloff. Copied that exact approach rather
  than reinvent a third one: same 64 bands (already matched), same Hz
  range (30–16000), same gain exponent, same knee. The bass/mid/treble
  rescue hack is gone entirely — log spacing was the fix, not rescaling.
  New regression tests: a realistic decaying `audio.bands` fixture proves
  the gain curve brings the upper bands to life, and a no-`bands` fixture
  proves the fallback stays empty rather than guessing.
- **0.28.2** — Three more bugs from the same round of 0.28.x screenshots,
  plus a styling ask ("the library/folder texts are drab af, they need
  some sparkle baby!"):
  - **Spectrum strip overlapping the toggle text.** The reflection under
    each bar drew up to 16px *below* the strip's own bottom edge and
    straight into the settings checkboxes below it. Its room is now
    reserved inside the box the strip is given, so nothing it draws can
    ever land outside it; the strip also declines to render at all below
    a usable height (raised 24px → 36px) rather than cramming reflections
    into no space.
  - **PLAYING chip needed right-side padding.** The text sat 2px shy of
    the pill's own border. Padding budget increased.
  - **The active-song highlight was matching by display-name string, not
    library index.** Two tracks sharing a name (duplicate metadata, or
    both falling back to the same untagged filename) could both light up,
    or the wrong one could, while the actually-playing row stayed dark —
    reported as "illegible and mis-aligned". Now compares the row's real
    library index against `MediaController.library_index`, which is exact
    regardless of what any track happens to be named.
  - **Library/folder text glam.** A plain library row's title and a
    folded (non-empty, non-excluded) crate label were flat grey no matter
    what; both now cycle the same cyan→pink accent ramp the depth-tinted
    crate backgrounds already used (`_crate_accent`), and the open crate /
    active playlist get a soft glow-echo behind their label, the same
    trick the header wordmark uses.
  - 5 new regression tests: strict containment (worst-case bars into a
    tightly-cropped canvas, assert nothing paints past y1), a pixel check
    for real chip padding, a duplicate-name fixture proving the highlight
    follows index not string, and two pins on the accent ramp's spread and
    wiring.
- **0.28.1** — **Fixed the spectrum strip's "bass spike then a flat line"
  bug**, reported the same day as 0.28.0 shipped. Root cause:
  `AudioData.fft` is normalized once per frame against its own single
  loudest bin (the analyzer's own comment calls it a "shape fraction"),
  and real music's magnitude rolls off sharply with frequency, so that
  loudest bin is almost always deep in the bass — every other bin arrived
  as a tiny fraction of it. A synthetic reproduction confirmed a naive
  64-band fold left only 3 of 64 bars with any visible signal. Fix (the
  owner's own diagnosis: "probably combining them is the right call"):
  recover shape *within* each of the bass/mid/treble thirds by
  renormalizing each against its own local max (undoing the global
  squash), then rescale each third by the analyzer's own
  already-gain-balanced `bass`/`mid`/`treble` scalars — the same levels
  the platter's glow and beat flash already trust. All 64 bars now read
  live. New regression test reproduces the exact bass-dominated,
  globally-normalized fixture and pins both invariants (most bars live,
  and a real multi-x improvement in the upper bands over the old fold).
- **0.28.0** — **The spectrum visualization moves, gets the full FFT, and
  gets a lot more dramatic — plus a SPARKLE knob.** Owner feedback on the
  0.27.0 pass: the flanking EQ bars beside the platter were "in the wrong
  spot," should "make sure you're using the full 64 band fft," should be
  "neon face melting drip baby," and should "respect the global sparkle
  setting."
  - **Relocated**: the old flanking bar meters (beside the platter, never
    wide enough to read as a real analyzer) are gone; a full-width
    **spectrum strip** now fills the previously dead gap between the
    transport and the settings stack.
  - **Full 64-band FFT**: `audio_spectrum` no longer folds the analyzer's
    512 bins down to 16 — it's 64 bands now (`MediaController._SPECTRUM_BANDS`),
    one bar per band, no averaging beyond the raw 8:1 fold every band already
    needs. The platter's own radial spectrum ring reads the same data, so it
    got sharper for free.
  - **Neon drip**: each bar is a short vertical gradient (cyan base, hot
    pink/yellow tip, matching how loud that band is), with a soft glow, a
    **peak-hold cap that drips down slower than the bar itself falls** (the
    read of "analyzer" over "bar graph"), and a fading reflection below the
    baseline. A beat-reactive scanline sweeps the strip on the kick.
  - **A local SPARKLE knob.** No global sparkle setting exists anywhere in
    the codebase (checked core `vj_api`, `config.toml`, `overlays.py`) — the
    only precedent is the DJ mixer's own *private* SPARKLE gain, so this
    mirrors that: a slider next to the settings checkboxes (`sparkle_gain`,
    0–1, default 1.0, persisted, also settable via the new `[media]
    sparkle_gain` config key) governing only the decorative twinkle layer —
    the platter's orbiting glints, the flanking sparks left beside the
    platter, and the strip's sparkle flecks. 0 leaves the bars, rings, and
    glow untouched and just kills the twinkle.
  - The platter shrank slightly again (0.46 → 0.40 of the pane height) so
    the strip gets real room at the application's actual default window
    size (980×640), not only in a manually-resized taller window — the
    previous pass looked great at ~1125×1015 and was a barely-visible
    sliver at the shipped default. Both the strip and the SPARKLE slider
    skip themselves gracefully (no crash, no overlap) on a window too short
    for either, the same degradation pattern the rest of the deck already
    follows.
  - Fixed along the way: the SPARKLE slider's first implementation
    positioned itself at a fixed offset from the window's bottom edge,
    independent of the checkbox stack above it — on a short window where
    the stack's own row spacing compresses, the two collided. Caught by
    eye in a screenshot, then pinned with a direct regression test
    (`test_footer_controls_never_overlap_across_window_heights`) sweeping
    a range of window heights, so it can't come back silently.
- **0.27.0** — **Navigation sophistication + a full visual pass.** Owner
  feedback: the Deck was "still super boring and bland" and its dropdowns
  "extend off screen... and don't scroll" while in-browser lists "scroll too
  slow." Fixed both:
  - **Real navigation.** The wheel now scrolls the *view* (3 rows/notch,
    matching normal desktop feel) instead of walking the selection one row
    at a time; PgUp/PgDn/Home/End join arrows + Enter. Every scrollbar
    (track list, folder/playlist split pane, flyouts) is a **draggable neon
    thumb**, not a static hint. The sort flyout, filter panel, playlist
    picker, and every right-click context menu (including "Add to
    playlist »" over a big collection) now **cap to the window and scroll
    within it** — previously a picker or menu taller than the window simply
    ran off the bottom with the tail unreachable.
  - **The glam pass.** A numpy-built, cached vertical navy gradient +
    scanline base coat; a two-tone glowing wordmark (cyan → pink) with a
    beat-tinted light sweep under the header rule; a live status chip with
    a pulsing dot; floating drop-shadowed rounded panels throughout; a
    gradient (cyan→pink) progress bar with a glowing playhead bead; rounded
    hover-reactive transport buttons, tabs, and flyout rows; a gradient
    volume fill that turns gold past unity; zebra-striped list rows.
  - **Auto-level**: a new checkbox (mixer methodology, its own section
    below) next to crossfade.
  - **Up-next queue**: right-click any track in any list (library,
    playlist, favorites, recent) → **Add to queue** — a literal FIFO of
    library indices, so "play this next" means the same thing everywhere
    and wins over shuffle/sort/crate on the next forward advance. A pink
    "▶ N queued" badge appears in the header (hover for a preview, click to
    clear) only when something actually is.
  - **The platter's flanks come alive.** A round platter in a rectangular
    pane leaves two dead strips of flat panel colour beside it; each now
    carries a small mirrored EQ (built from the app's own 16-band spectrum)
    and a slow drift of rising sparks that pop on the beat — filling the
    "big empty space" the owner flagged, in the same visual language as
    the platter's own spectrum ring.
  - Auto-level itself: loudness-matches tracks the way the mixer's decks
    do — K-weighted RMS (ITU-R BS.1770) over the loudest half of the
    track, normalized to the mixer's target and clamped
    (`_AUTOGAIN_MIN`/`_AUTOGAIN_MAX`, kept in step with dj-mixer-01's
    `deck.py` per that module's own docstring). Measured once per track on
    a background thread (never blocks playback), cached in the playlists
    store (`gains`) so a file is measured once, ever; a stale in-flight
    measurement is discarded if the track moved on before it finished
    (never re-levels the wrong track). Per-VLC-player gain (not global),
    so a crossfading pair keeps each side's own measured level through the
    ramp. On by default; the checkbox needs VLC (a gain-capable backend)
    and a decoder (`soundfile` or `av`) to measure with.
- **0.26.0** — Publishes the playing track's authored BPM tag (ID3 TBPM /
  Vorbis `bpm`) on the vj_api BPM bus as source `media`, read once per
  track and republished every frame while playing. Auto VJ primes its
  tracker from that bus the way it already does from the mixer's
  analysis, which settles the half-time-vs-double-time ambiguity audio
  evidence cannot (v3 phase 4). Untagged files publish nothing.
- **0.25.0** — **Split browser pane, replacing the accordion.** Folders
  take a fixed top third with their own scroll; tracks keep the bottom two
  thirds no matter how deep the crate tree goes. The accordion grew a row
  per crate along the open path, so a large library left room for about two
  track rows. The selected crate is background-highlighted and empty crates
  read dimmer than merely-unselected ones. Playlists get the same treatment:
  the list of lists sits in that top pane, so switching lists is a click in
  view instead of a trip through a dropdown. The mouse wheel scrolls
  whichever pane the cursor is over.

- **0.24.2** — Shuffle now covers the whole list: a pass ends when every
  track has been *played*, not when playback happens to reach the last
  *row* (starting anywhere but row 0 used to end a 10k library after a
  handful of tracks). Library scans are cached on disk by path+size+mtime,
  so a warm start costs one `stat` per file instead of a tag parse —
  10,758 tracks went 11.04s to 0.12s. A constructor now waits for an
  in-flight background prescan instead of racing it with a second
  concurrent walk of the same library.

- **0.24.1** — Fixed a routine internal `save()` (fired on every track
  change via `note_played()`'s recent-history bookkeeping, nothing to
  do with editing playlists) silently overwriting playlist entries an
  external process had added to the same file since this instance's
  own load. `save()` now merges in any playlist name foreign to this
  session from the on-disk file rather than blindly overwriting; this
  instance's own edits still win for names it already knows about.
- **0.24.0** — **The Media Player 2.0 treatment: the library grows up, and
  the deck starts hearing the room.**  Everything here is the mixer browser's
  UX ported across, minus the DJ-only parts (no BPM, no keys, no stems).
  **Faceted filters** (`≡` next to sort): artist / album / genre / **decade**
  — a listener thinks "90s" the way a DJ thinks in BPM ranges — AND across
  facets, OR within one, applied in *every* view so FILTER means one thing
  wherever you stand; the header names the narrowing (`30 tracks · 2:03 · 1
  filter`) so a filtered library never reads as a shrunken one.  A **RECENT
  tab**: the last 50 tracks that actually played, newest first, persisted.
  **Sort by album / year / length** joins artist / title — view-only, a
  playlist's stored order is never rewritten.  **Playlist management** grown
  in place: the picker rows now say `name (7 · 33m)` with a dead-path badge,
  and right-clicking one offers rename (position preserved, collisions
  refused), duplicate (`name (2)`), prune-missing, and an armed SURE? delete.
  Playlist row edits (remove / move) now resolve **by path**, not view row,
  so a sorted or filtered view can no longer edit the wrong track.  **Hover
  tooltips everywhere** via the core tooltip system — a row shows its whole
  tag card.  And the **drip**: the platter's glow breathes with live bass, a
  rim ring flashes on the beat, treble opens the sparkle gate, and a 32-bar
  **radial spectrum ring** orbits the record counter-to-the-spin, all fed by
  the app's own analyzer (captured only while the deck is open; a core
  without audio levels degrades to the old time-driven look).  Also: honest
  empty states ("nothing matches — clear the filters" vs "nothing played
  yet"), heart-glyph hover previews, and flyout clicks now land on the
  flyout instead of the search box underneath it (hit-test walks regions
  top-most first — the first playlist row in the picker was previously
  unclickable).
- **0.23.0** — `snapshot()`'s `playlist_context` was hardcoded `None` —
  now reflects `self.active_playlist` (real, already-tracked state) in
  the same shape spotify-01 populates, so training-kit-01's playlist-
  name inference (previously Spotify-only) picks it up for free. Owner:
  "our training packager is supposed to be self-naming it's playlist
  when the data is available (and it should be from the mixer &
  mediaplayer) but i'm having to type it in every time." See
  `docs/adr/vj-system.md` (main repo).
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
