# media-01 Operations

Owner: Drop-in Maintainers
Status: active
Last updated: 2026-08-15

## Purpose

Play a local library of audio files (own playback + tags), presenting the
exact same now-playing HUD band and banner as the Spotify drop-in so the
operator gets local-file playback with zero UI changes.

## Lifecycle

- Loaded by `app.py` during startup (after Spotify, before chat) when
  `[media] enabled = true`. Constructing `MediaController` scans `media_dir`
  for audio files, reads their tags, optionally shuffles the library, and
  opens a playback backend (VLC, falling back to `mpv`/`ffplay`/`aplay`).
- Registered as the `media` subsystem (`vj_api.register_subsystem('media',
  ...)`) and, if playback started, as a key handler
  (`vj_api.register_key_handler('media', ...)`).
- `update(dt, audio)` runs every frame through the **generic** subsystem
  update loop (`app.py`'s `for name, subsystem in self._subsystems.items():
  ... updater(dt, self._audio)`), not a dedicated call site — it's paused
  along with every other subsystem while a preset/effects manager modal is
  open (`manager_modal_active`).
- `update()` polls the backend each frame: while playing it refreshes
  position/duration from the backend; once the backend reports the track
  ended, it auto-advances to the next library index and starts it.
- Released via `shutdown()` (through the generic subsystem shutdown loop at
  app exit), which stops and releases the backend.

## Live operation

- `Ctrl+Alt+M` — play / pause (resumes if paused, starts the current index
  if stopped).
- `Ctrl+Alt+N` — next track (wraps to the start of the library).
- `Ctrl+Alt+B` — previous track, or restart the current track if more than
  3 seconds into it.
- `Ctrl+Alt+Shift+M` — toggle shuffle (affects future advances, does not
  reshuffle the currently-playing order retroactively).
- `Shift+L` — the **Media Deck** window (see below); Esc-Esc closes it.

## The Media Deck browser (0.24.0)

The deck's library pane has four tabs — **LIBRARY** (accordion crates, one
per subfolder, each with an exclude toggle), **PLAYLISTS**, **♥ FAVS**, and
**RECENT** (the last 50 tracks that actually played, newest first,
persisted).  Two icons sit at the right of the tab row:

- `↑↓` **sort** — artist / title / album / year / length / scan order.
  Sort is a *view* operation: a playlist's stored running order is never
  rewritten.
- `≡` **facet filters** — artist / album / genre / decade, checkbox values
  drawn from the whole library.  AND across facets, OR within one, applied
  in every view (playback follows what they leave visible).  The header
  reports the narrowing (`30 tracks · 2:03 · 1 filter`); `clear` in the
  panel drops the lot.

The playlist picker rows carry count + running time and a `!n` badge when
entries point at files that no longer exist; right-clicking a row offers
**Rename…** (order-preserving, collisions refused), **Duplicate**,
**Prune n missing**, and an armed **Delete**.  Hovering nearly anything —
rows, transport, tabs, crates — raises a tooltip through the core tooltip
system; a row's tooltip is its full tag card (artist / album / genre / year /
length / crate).

**Right-click any row, in any of the four views, for Add to queue** — a
literal FIFO of library indices, so it means the same thing everywhere: the
next forward advance (Next, a crossfade trigger, or the track ending)
always plays the front of the queue before it consults the active view's
own order, shuffle bag, or sort.  A pink `▶ N queued` badge appears in the
header only while something is actually queued (hover it for a preview of
what's coming, click to clear it).  Stepping backward never touches the
queue.

### Navigation (0.27.0)

The mouse wheel scrolls the **view**, 3 rows per notch (not the selection,
and not 1 row per notch as before) — over the track list, the folder/
playlist split pane, an open flyout, or a long context menu, whichever is
under the pointer.  PgUp/PgDn/Home/End join the existing arrow+Enter
keyboard nav.  Every scrollbar — the track list, the folder/playlist pane,
flyouts — is a draggable thumb, not a static hint: press and drag it, or
press-and-drag the seek bar / volume slider the same way.  The sort flyout,
filter panel, playlist picker, and every right-click menu (including "Add
to playlist »" over a large collection) cap their height to the window and
scroll within it, so nothing runs off the bottom unreachable regardless of
how many playlists, filter values, or menu items there are.

### The platter's chrome (0.24.0–0.27.0)

While music plays, the platter is fed by the app's own audio analyzer: the
glow breathes with bass, a rim ring flashes on the beat, treble drives the
sparkles, and a radial spectrum ring orbits the record.  The two flanking
strips beside the round platter — otherwise flat panel colour — carry a
small mirrored EQ built from the same 16-band spectrum plus a slow drift of
beat-reactive sparks.  Levels are captured only while the deck window is
open; on a core without audio levels the platter and flanks fall back to
their old purely time-driven look.

### Auto-level (0.27.0)

A checkbox next to crossfade loudness-matches tracks the way the mixer's
decks do: K-weighted RMS (ITU-R BS.1770) over the loudest half of the
track, normalized and clamped.  Measured once per track on a background
thread — never blocks playback — and cached in the playlists store, so a
file is only ever decoded for this once.  Needs a gain-capable backend
(VLC) and a decoder (`soundfile` or `av`) to measure with; the checkbox
reads "(needs VLC)" and stays informational otherwise.  On by default.

## Now-playing display priority

`app.py` picks whichever now-playing source is **actively playing**: if
media-01's `snapshot()` reports `is_playing`, its metadata drives the HUD
band and banner; otherwise it falls back to spotify-01's snapshot (or shows
nothing if neither is playing). Both controllers expose an identical
`snapshot()` shape specifically so this fallback needs no format handling.

## Backend selection

Tried in order at startup, first success wins for the whole session (no
runtime re-probe):

1. **python-vlc** (libvlc) — cross-platform, supports live volume changes and
   native position/duration reporting.
2. **mpv** (subprocess) — position is estimated from wall-clock elapsed time;
   pause/resume uses `SIGSTOP`/`SIGCONT`.
3. **ffplay** (subprocess) — same estimation/signal approach as mpv.
4. **aplay** — listed as a last-resort fallback binary; not necessarily video
   suppressed if picked (no `--no-video`-equivalent flag for aplay), so VLC or
   mpv are the practical choices.

If none of these are available, `MediaController` logs a warning and
`enabled` effectively has no backend — see
[troubleshooting.md](troubleshooting.md).
