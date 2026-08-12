# media-01 Configuration

Owner: Drop-in Maintainers
Status: active
Last updated: 2026-07-07

Keys live under `[media]` in `config.toml`, which already ships a
commented-out `[media]` block — uncomment and adjust to enable the drop-in
(it is not currently mirrored in `config.full.example.toml`). There is no
dedicated `config_validator.py` entry for `[media]` (unlike some other
drop-ins) — bad values fall back to the defaults below rather than being
flagged at startup.

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Load the drop-in at all |
| `media_dir` | str | `""` | Library root, recursively scanned. Empty = the bundled `drop-ins/media-01/media/` directory |
| `shuffle` | bool | `true` | Randomize play order once at load; toggle live with `Ctrl+Alt+Shift+M` |
| `auto_play` | bool | `false` | Start playback of the first (post-shuffle) library track automatically at boot — no `Ctrl+Alt+M` / MIDI action needed. For unattended/headless use, e.g. `unicornviz --media-source` (training-daemon `--source media`). |
| `boot_playlist` | str | `""` | Force a named playlist (from the playlists store, not the separate hearted-tracks favorites set) active at boot, index landing on the playlist's own first entry. Empty = disabled (normal library/restored-prefs behavior). Falls back to the library view (logged) if the name doesn't exist or is empty. `unicornviz --media-source --media-favorites` sets this to `'favorites'` plus forces `shuffle=false`/`repeat='off'` as CLI overrides (not written to `config.toml`). |
| `volume` | int | `100` | `0`-`200` (`100` = unity gain); only takes effect live on the VLC backend |
| `now_playing_banner` | bool | `true` | Show the track-change overlay banner (same as Spotify's) |
| `now_playing_banner_hold_s` | float | `10.0` | Seconds the banner stays visible, floor `1.0` |
| `now_playing_banner_delay_s` | float | `2.0` | Delay after a track starts before the banner fires, floor `0.0` |
| `crossfade_ms` | int | `0` | **Reserved, not yet implemented** — parsed and clamped to `>= 0` but has no playback effect yet (see [operations.md](operations.md) TODO) |

## Supported audio formats

Scanned by extension: `.mp3 .flac .ogg .oga .opus .wav .aiff .aif .m4a .aac
.wma .ape .mpc .wv`. Tags (title/artist/album/duration) are read via
`mutagen` when available; if `mutagen` is missing or a file has no tags, the
filename stem is used as the title and artist/album are left blank — this
is a soft fallback, not a startup error.

## Example

```toml
[media]
enabled = true
media_dir = "/home/you/Music"   # or leave empty for the bundled folder
shuffle = true
volume = 100
now_playing_banner = true
now_playing_banner_hold_s = 10.0
now_playing_banner_delay_s = 2.0
```
