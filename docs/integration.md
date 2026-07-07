# media-01 Integration

Owner: Drop-in Maintainers
Status: active
Last updated: 2026-07-07

## Load path

`app.py` loads the controller via `_load_media_controller_class()` →
`load_dropin_symbol('media-01/media_controller.py', 'MediaController')`
inside a `try/except` that sets `self._media = None` on failure (missing
drop-in, missing `python-vlc`/mutagen with no subprocess fallback found,
etc.). The app starts cleanly and simply shows no local-media playback when
the drop-in is absent — no other core behavior depends on it.

Construction only proceeds past the `[media] enabled` check; if the
constructed instance reports `enabled == False` (e.g. it was constructed but
decided it has nothing to do), `app.py` discards it and leaves `self._media
= None` rather than keeping a half-initialized controller around.

## Runtime surface

Registered as the `media` subsystem. Reachable via
`vj_api.get_subsystem('media')`:

- `play_pause()`, `next_track()`, `prev_track()`, `toggle_shuffle()`,
  `set_volume(volume)`, `snapshot()`

Also registered as a key handler (`vj_api.register_key_handler('media',
...)`) so `Ctrl+Alt+M/N/B/Shift+M` reach `handle_key()` through the normal
drop-in key-handler dispatch — see `HELP_ENTRIES` in
`media_controller.py` for the single-source-of-truth hotkey list shown in
the `H` help overlay.

## Update / render path

Unlike some sibling drop-ins (`beat_flash`, `audio_out`, `osc_bridge`,
`lyrics`), `MediaController` has **no dedicated per-frame call site** in
`app.py`. It is driven entirely by the generic subsystem loops keyed off
`self._subsystems`:

- **Update:** `for name, subsystem in self._subsystems.items(): ...
  updater(dt, self._audio)`, skipped while a preset/effects/ProjectM manager
  modal is open (`manager_modal_active`). This is where auto-advance to the
  next track on end-of-file happens.
- **Shutdown:** the same generic loop pattern at app exit calls
  `shutdown()` on every registered subsystem, which is where
  `MediaController` stops and releases its playback backend.

There is no GL rendering here at all — `MediaController` has no
`render_overlay`/`present` hook. All visual output goes through the shared
now-playing HUD band / banner (see below), not a private draw path.

## Now-playing snapshot contract

`snapshot()` returns the **same dict shape** as `SpotifyController.snapshot()`
(source: `'media-01'` instead of `'spotify'` in the `source` field) so
`app.py`'s now-playing code (HUD band + banner formatting in `overlays.py`)
requires zero special-casing. `app.py` picks whichever of `self._media` /
`self._spotify` currently has `is_playing=True` in its snapshot, preferring
media-01 when both are simultaneously "playing" (see
[operations.md](operations.md)).

Fields present only for media-01 (ignored by the shared HUD code, available
for anything drop-in-specific): `library_size`, `library_index`, `shuffle`.

## Independence

No GL resources, no threads (deliberately — `update()` is main-thread-only,
including all backend play/pause/stop calls per the class docstring). The
subprocess fallback backends spawn a child process but never a Python
thread. Safe to disable entirely via `[media] enabled = false` with no
effect on any other subsystem, including spotify-01 (the two only interact
through the now-playing display priority described above).
