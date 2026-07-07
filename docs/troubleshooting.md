# media-01 Troubleshooting

Owner: Drop-in Maintainers
Status: active
Last updated: 2026-07-07

## Nothing plays / "no backend" message

`play_pause()` returns `'Media: no backend'` when no playback backend could
be constructed at startup. Check the log for
`media-01: no playback backend found (install python-vlc or mpv)`. Install
one of:

```bash
# python-vlc (preferred — supports live volume, native position/duration)
pip install python-vlc
sudo dnf install vlc        # Fedora/RHEL — the VLC library itself, not just the binding
sudo apt install vlc        # Ubuntu/Debian

# or a subprocess fallback
sudo dnf install mpv        # or: sudo apt install mpv
```

`bash drop-ins/media-01/install.sh` installs the Python side
(`python-vlc`, `mutagen`); the system VLC/mpv package is separate and must be
installed via your distro's package manager.

## "Media: library empty"

`media_dir` (or the bundled `drop-ins/media-01/media/` if unset) contains no
files with a recognized extension (see the list in
[configuration.md](configuration.md)). Confirm the path and that at least
one file has a supported extension — the scan is recursive, so subfolders
are fine, but files must actually match one of the known extensions
(no extension sniffing beyond the filename suffix).

## Falls back to `aplay` and nothing (or only noise) plays

`aplay` is listed as a last-resort subprocess fallback, but unlike the
`mpv`/`ffplay` command lines it is invoked with **no format-conversion
flags** (`[aplay, path]`). `aplay` natively only understands WAV/raw PCM —
if your library is mostly MP3/FLAC/etc. and VLC, mpv, *and* ffplay are all
unavailable, playback will fail or produce noise on non-WAV files even
though a "backend" was found. Installing `mpv` or `ffplay` (or, better,
`python-vlc` + system VLC) resolves this properly.

## Tags show as the filename instead of real title/artist/album

- `mutagen` isn't installed, or the file has no embedded tags mutagen can
  read for its format. This is a soft fallback (filename stem as title, blank
  artist/album), not an error — check the log at `DEBUG` level for
  `media-01: mutagen error on <file>: ...` if you expect tags that aren't
  showing.

## Volume slider / `set_volume()` has no effect

Only the VLC backend supports live volume changes
(`_VlcBackend.set_volume()`). The `mpv`/`ffplay`/`aplay` subprocess
fallbacks silently ignore `set_volume()` calls — there's no cross-backend
way to adjust an already-running subprocess's output level. Install
`python-vlc` if live volume control matters for your setup.

## Position/progress bar drifts or looks wrong

On the subprocess fallback backends (`mpv`/`ffplay`/`aplay`), position is
**estimated from wall-clock elapsed time** since playback started (with a
pause/resume offset adjustment), not read from the actual player — small
drift versus the real playhead is expected and is a backend limitation, not
a bug. The VLC backend reads native position/duration from libvlc and does
not have this issue.

## Track doesn't auto-advance at the end

Auto-advance runs inside `update()`, which is only called while the app is
not showing a preset/effects/ProjectM manager modal (`manager_modal_active`
pauses the entire generic subsystem update loop, not just media-01 — see
[integration.md](integration.md)). If you leave a manager modal open through
an entire track, advancing resumes as soon as you close it.

## Cross-fade / auto-mixing isn't happening

Not implemented yet. `crossfade_ms` is parsed and clamped in config but has
no playback effect — it's reserved for a future two-`MediaPlayer`-instance
overlap/ramp implementation (see the module docstring's `TODO(future)`).

## Tests

No dedicated test suite covers this drop-in's playback/scanning logic today
(only `tests/test_help_section_dropin_dedup.py` touches this file, and only
to check its `HELP_ENTRIES`). Note: `tests/test_media_groups.py` is *not*
this drop-in despite the name — it covers directory-group scanning for the
images-01/textures-01/sims-01 asset drop-ins, unrelated to media-01's audio
library scan.
