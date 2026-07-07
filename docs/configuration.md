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
