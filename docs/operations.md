# media-01 Operations

Owner: Drop-in Maintainers
Status: active
Last updated: 2026-07-07

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
