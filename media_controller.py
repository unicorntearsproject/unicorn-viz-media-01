"""Local media player subsystem for unicorn-viz.

Scans a directory of audio files, reads ID3/Vorbis/FLAC tags via mutagen,
and plays them back via python-vlc (libvlc).  Exposes a snapshot() interface
identical to SpotifyController so the existing now-playing HUD band and
overlay banner require zero format changes.

Playback engine priority:
  1. python-vlc (libvlc) — cross-platform; works on Linux, Windows, macOS
  2. subprocess mpv — Linux/macOS fallback when VLC is not installed
  3. subprocess ffplay — last-resort fallback

Position tracking uses VLC's native get_position() / get_length() when
available; the mpv/ffplay paths estimate position via wall-clock elapsed time.

TODO(future): cross-fade + auto-mixing via two VLC MediaPlayer instances with
overlapping playback and volume ramping.  The _crossfade_ms config key is
reserved for that feature and parsed but not yet acted upon.
"""
from __future__ import annotations

__version__ = '0.6.0'

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

HELP_ENTRIES = [
    ('Local Media', 'Ctrl+Alt+M', 'Play / pause'),
    ('Local Media', 'Ctrl+Alt+N', 'Next track'),
    ('Local Media', 'Ctrl+Alt+B', 'Previous track / restart'),
    ('Local Media', 'Ctrl+Alt+Shift+M', 'Toggle shuffle'),
]

# Audio file extensions to scan for
_AUDIO_EXTS: frozenset[str] = frozenset({
    '.mp3', '.flac', '.ogg', '.oga', '.opus',
    '.wav', '.aiff', '.aif', '.m4a', '.aac',
    '.wma', '.ape', '.mpc', '.wv',
})

# Fallback subprocess players tried in order when VLC is unavailable
_FALLBACK_PLAYERS: tuple[str, ...] = ('mpv', 'ffplay', 'aplay')


def _read_tags(path: Path) -> dict[str, str]:
    """Return {'title', 'artist', 'album', 'duration_s'} from audio tags.

    Falls back gracefully when mutagen is unavailable or tags are absent.
    """
    title = path.stem
    artist = ''
    album = ''
    duration_s = 0.0

    try:
        import mutagen  # noqa: PLC0415
        f = mutagen.File(str(path), easy=True)
        if f is None:
            return {'title': title, 'artist': artist, 'album': album, 'duration_s': duration_s}
        if f.info and hasattr(f.info, 'length'):
            duration_s = max(0.0, float(f.info.length or 0.0))
        tags = f.tags or {}
        def _tag(key: str) -> str:
            v = tags.get(key)
            if v is None:
                return ''
            if isinstance(v, list):
                return str(v[0]).strip() if v else ''
            return str(v).strip()
        t = _tag('title')
        if t:
            title = t
        a = _tag('artist') or _tag('albumartist')
        if a:
            artist = a
        al = _tag('album')
        if al:
            album = al
    except Exception as exc:
        log.debug('media-01: mutagen error on %s: %s', path.name, exc)

    return {'title': title, 'artist': artist, 'album': album, 'duration_s': duration_s}


def _scan_library(directory: Path) -> list[dict[str, Any]]:
    """Walk *directory* and return a list of track metadata dicts."""
    tracks: list[dict[str, Any]] = []
    try:
        for root, _dirs, files in os.walk(directory):
            for name in sorted(files):
                p = Path(root) / name
                if p.suffix.lower() in _AUDIO_EXTS:
                    meta = _read_tags(p)
                    tracks.append({
                        'path': p,
                        **meta,
                    })
    except Exception as exc:
        log.warning('media-01: library scan error: %s', exc)
    return tracks


class _VlcBackend:
    """python-vlc playback backend."""

    def __init__(self, volume: int) -> None:
        import vlc  # noqa: PLC0415
        self._vlc = vlc
        args = ['--no-video', '--quiet']
        self._instance = vlc.Instance(args)
        self._player = self._instance.media_player_new()
        self._player.audio_set_volume(max(0, min(200, volume)))

    def play(self, path: Path) -> None:
        media = self._instance.media_new(str(path))
        self._player.set_media(media)
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def resume(self) -> None:
        if self._player.is_playing():
            return
        self._player.play()

    def stop(self) -> None:
        self._player.stop()

    def is_playing(self) -> bool:
        return bool(self._player.is_playing())

    def is_ended(self) -> bool:
        state = self._player.get_state()
        return state in (self._vlc.State.Ended, self._vlc.State.Error, self._vlc.State.NothingSpecial)

    def position_s(self) -> float:
        ms = self._player.get_time()
        return max(0.0, ms / 1000.0) if ms >= 0 else 0.0

    def duration_s(self) -> float:
        ms = self._player.get_length()
        return max(0.0, ms / 1000.0) if ms > 0 else 0.0

    def set_volume(self, volume: int) -> None:
        self._player.audio_set_volume(max(0, min(200, volume)))

    def shutdown(self) -> None:
        try:
            self._player.stop()
            self._player.release()
            self._instance.release()
        except Exception:
            pass


class _SubprocessBackend:
    """Subprocess fallback backend (mpv / ffplay)."""

    def __init__(self, player_bin: str) -> None:
        self._bin = player_bin
        self._proc: subprocess.Popen | None = None
        self._started_at: float = 0.0
        self._duration: float = 0.0
        self._paused: bool = False
        self._paused_at: float = 0.0
        self._position_offset: float = 0.0

    def play(self, path: Path, duration_s: float = 0.0) -> None:
        self.stop()
        self._duration = duration_s
        self._position_offset = 0.0
        self._paused = False

        if 'mpv' in self._bin:
            cmd = [self._bin, '--no-video', '--really-quiet', str(path)]
        elif 'ffplay' in self._bin:
            cmd = [self._bin, '-nodisp', '-autoexit', '-loglevel', 'quiet', str(path)]
        else:
            cmd = [self._bin, str(path)]

        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._started_at = time.monotonic()
        except Exception as exc:
            log.warning('media-01: subprocess backend error: %s', exc)
            self._proc = None

    def pause(self) -> None:
        if self._proc and not self._paused:
            try:
                self._proc.send_signal(__import__('signal').SIGSTOP)
            except Exception:
                pass
            self._paused = True
            self._paused_at = time.monotonic()
            self._position_offset += self._paused_at - self._started_at

    def resume(self) -> None:
        if self._proc and self._paused:
            try:
                self._proc.send_signal(__import__('signal').SIGCONT)
            except Exception:
                pass
            self._paused = False
            self._started_at = time.monotonic()

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def is_playing(self) -> bool:
        if self._paused:
            return False
        if self._proc is None:
            return False
        return self._proc.poll() is None

    def is_ended(self) -> bool:
        if self._proc is None:
            return True
        return self._proc.poll() is not None

    def position_s(self) -> float:
        if self._paused:
            return self._position_offset
        if self._proc is None:
            return 0.0
        return self._position_offset + max(0.0, time.monotonic() - self._started_at)

    def duration_s(self) -> float:
        return self._duration

    def set_volume(self, volume: int) -> None:
        pass  # subprocess backends don't support runtime volume changes

    def shutdown(self) -> None:
        self.stop()


def _make_backend(volume: int) -> _VlcBackend | _SubprocessBackend | None:
    """Return the best available playback backend."""
    try:
        return _VlcBackend(volume)
    except Exception as exc:
        log.info('media-01: python-vlc unavailable (%s), trying subprocess', exc)

    for name in _FALLBACK_PLAYERS:
        found = shutil.which(name)
        if found:
            log.info('media-01: using subprocess backend: %s', found)
            return _SubprocessBackend(found)

    log.warning('media-01: no playback backend found (install python-vlc or mpv)')
    return None


class MediaController:
    """Local media player subsystem for unicorn-viz.

    Implements the same snapshot() interface as SpotifyController so the
    existing now-playing HUD band and overlay banner require no format changes.

    Thread-safety: update() is called on the main thread only; the backend's
    play/pause/stop calls are also main-thread-only.  No background threads are
    used internally.
    """

    def __init__(self, app: Any, cfg: dict[str, Any] | None = None) -> None:
        self._app = app
        self._cfg = dict(cfg or {})

        self.enabled: bool = bool(self._cfg.get('enabled', False))

        raw_dir = str(self._cfg.get('media_dir', '') or '').strip()
        if raw_dir:
            self._media_dir = Path(raw_dir).expanduser()
        else:
            self._media_dir = Path(__file__).parent / 'media'

        self._shuffle: bool = bool(self._cfg.get('shuffle', True))
        self._volume: int = max(0, min(200, int(self._cfg.get('volume', 100) or 100)))
        self._now_playing_banner_enabled: bool = bool(
            self._cfg.get('now_playing_banner', True)
        )
        self._now_playing_banner_hold_s: float = max(
            1.0, float(self._cfg.get('now_playing_banner_hold_s', 10.0) or 10.0)
        )
        self._now_playing_banner_delay_s: float = max(
            0.0, float(self._cfg.get('now_playing_banner_delay_s', 2.0) or 2.0)
        )
        # TODO(future): cross-fade + auto-mixing: duration of the overlap ramp.
        # Wired into config now; backend support to be added in a later iteration.
        self._crossfade_ms: int = max(0, int(self._cfg.get('crossfade_ms', 0) or 0))

        self._library: list[dict[str, Any]] = []
        self._index: int = 0
        self._track_id: str = ''
        self._title: str = ''
        self._artist: str = ''
        self._album: str = ''
        self._duration_s: float = 0.0
        self._position_s: float = 0.0
        self._is_playing: bool = False
        self._status: str = 'stopped'
        self._change_counter: int = 0
        self._banner_change_counter: int = 0
        self._banner_promote_at: float = 0.0
        self._previous_title: str = ''
        self._previous_artist: str = ''
        self._previous_album: str = ''

        self._backend: _VlcBackend | _SubprocessBackend | None = None

        try:
            vj = self._app.vj_api
            vj.register_midi_actions('Media', [
                ('media_play_pause', 'Play / pause media'),
                ('media_next',       'Next track'),
                ('media_prev',       'Previous track'),
            ])
            vj.register_midi_action_handler('media_play_pause', self.play_pause)
            vj.register_midi_action_handler('media_next',       self.next_track)
            vj.register_midi_action_handler('media_prev',       self.prev_track)
        except Exception as exc:
            import logging  # noqa: PLC0415
            logging.getLogger(__name__).debug('media-01: MIDI registration failed: %s', exc)

        if not self.enabled:
            return

        self._library = _scan_library(self._media_dir)
        if not self._library:
            log.warning(
                'media-01: no audio files found in %s; check media_dir in config',
                self._media_dir,
            )

        if self._shuffle:
            import numpy as np  # noqa: PLC0415
            rng = np.random.default_rng()
            rng.shuffle(self._library)  # type: ignore[arg-type]

        self._backend = _make_backend(self._volume)

        log.info(
            'media-01: ready — %d tracks in library, backend=%s',
            len(self._library),
            type(self._backend).__name__ if self._backend else 'none',
        )

        # Announce through the core now-playing hub (banner + HUD pane) —
        # priority 20: outranks Spotify while actually playing, yields to the
        # DJ mixer.  Guarded so older cores without the hub still work.
        vj = getattr(app, 'vj_api', None)
        if vj is not None and hasattr(vj, 'register_now_playing'):
            try:
                vj.register_now_playing('media', self.snapshot, priority=20)
            except Exception as exc:
                log.debug('media-01: now-playing registration failed: %s', exc)

    # ------------------------------------------------------------------
    # Public control surface
    # ------------------------------------------------------------------

    def play_pause(self) -> str:
        """Toggle play/pause for the current track."""
        if not self.enabled or self._backend is None:
            return 'Media: no backend'
        if not self._library:
            return 'Media: library empty'
        if self._backend.is_playing():
            self._backend.pause()
            self._is_playing = False
            self._status = 'paused'
            return 'Media: paused'
        if self._status == 'paused':
            self._backend.resume()
            self._is_playing = True
            self._status = 'playing'
            return 'Media: resumed'
        self._start_track(self._index)
        return f'Media: playing {self._title or "track"}'

    def next_track(self) -> str:
        """Advance to the next track."""
        if not self.enabled or not self._library:
            return 'Media: library empty'
        self._index = (self._index + 1) % len(self._library)
        self._start_track(self._index)
        return f'Media: next → {self._title or "track"}'

    def prev_track(self) -> str:
        """Jump back to the previous track (or restart if past 3 s)."""
        if not self.enabled or not self._library:
            return 'Media: library empty'
        if self._backend is not None and self._backend.position_s() > 3.0:
            self._start_track(self._index)
            return f'Media: restart {self._title or "track"}'
        self._index = (self._index - 1) % len(self._library)
        self._start_track(self._index)
        return f'Media: prev → {self._title or "track"}'

    def toggle_shuffle(self) -> str:
        """Toggle shuffle mode (takes effect on next track advance)."""
        self._shuffle = not self._shuffle
        return f'Media: shuffle {"ON" if self._shuffle else "OFF"}'

    def set_volume(self, volume: int) -> None:
        """Set playback volume (0-200, 100 = 100%)."""
        self._volume = max(0, min(200, volume))
        if self._backend is not None:
            self._backend.set_volume(self._volume)

    # ------------------------------------------------------------------
    # Subsystem interface (called by app.py)
    # ------------------------------------------------------------------

    def update(self, dt: float, audio: Any) -> None:  # noqa: ARG002
        """Poll playback state; advance to next track when current one ends."""
        if not self.enabled or self._backend is None:
            return

        now = time.monotonic()

        if self._backend.is_playing():
            self._position_s = self._backend.position_s()
            # Prefer VLC's duration once it becomes available after playback starts.
            backend_dur = self._backend.duration_s()
            if backend_dur > 0.0:
                self._duration_s = backend_dur
            self._is_playing = True
            self._status = 'playing'
        elif self._status == 'playing' and self._backend.is_ended():
            # Track finished — auto-advance.
            self._index = (self._index + 1) % max(1, len(self._library))
            if self._library:
                self._start_track(self._index)
        elif self._status == 'paused':
            pass
        else:
            self._is_playing = False

        # Promote the banner change counter once the announce delay has elapsed.
        if self._banner_promote_at > 0.0 and now >= self._banner_promote_at:
            self._banner_change_counter = self._change_counter
            self._banner_promote_at = 0.0

    def handle_key(self, sym: int, mod: int) -> str | None | bool:
        """Handle media hotkeys.

        Ctrl+Alt+M   — Play / Pause
        Ctrl+Alt+N   — Next track
        Ctrl+Alt+B   — Previous track
        Ctrl+Alt+Shift+M — Toggle shuffle
        """
        try:
            import sdl2  # noqa: PLC0415
        except Exception:
            return False

        ctrl = bool(mod & sdl2.KMOD_CTRL)
        alt = bool(mod & sdl2.KMOD_ALT)
        shift = bool(mod & sdl2.KMOD_SHIFT)

        if ctrl and alt and not shift and sym == sdl2.SDLK_m:
            return self.play_pause()
        if ctrl and alt and not shift and sym == sdl2.SDLK_n:
            return self.next_track()
        if ctrl and alt and not shift and sym == sdl2.SDLK_b:
            return self.prev_track()
        if ctrl and alt and shift and sym == sdl2.SDLK_m:
            return self.toggle_shuffle()

        return False

    def shutdown(self) -> None:
        """Stop playback and release resources."""
        vj = getattr(self._app, 'vj_api', None)
        if vj is not None and hasattr(vj, 'unregister_now_playing'):
            try:
                vj.unregister_now_playing('media')
            except Exception:
                pass
        if self._backend is not None:
            self._backend.shutdown()
            self._backend = None

    def snapshot(self) -> dict[str, Any]:
        """Return current state in the same shape as SpotifyController.snapshot().

        All keys consumed by app.py and overlays.py are present so the HUD band
        and now-playing banner work without any format changes.
        """
        duration = max(0.0, float(self._duration_s))
        position = max(0.0, float(self._position_s))
        progress = min(1.0, position / duration) if duration > 0.0 else 0.0

        return {
            # Core playback state
            'available': bool(self.enabled and self._backend is not None and bool(self._library)),
            'is_playing': self._is_playing,
            'status': self._status.upper(),
            # Current track
            'track_id': self._track_id,
            'raw_track_id': self._track_id,
            'title': self._title,
            'artist': self._artist,
            'album': self._album,
            # Previous track (for banner "Previous:" line)
            'previous_track_id': '',
            'previous_title': self._previous_title,
            'previous_artist': self._previous_artist,
            'previous_album': self._previous_album,
            # Timing
            'duration_s': duration,
            'position_s': position,
            'progress': progress,
            # Banner control
            'change_counter': self._change_counter,
            'banner_change_counter': self._banner_change_counter,
            'now_playing_banner_enabled': self._now_playing_banner_enabled,
            'now_playing_banner_hold_s': self._now_playing_banner_hold_s,
            'tags': [],
            'genres': [],
            'tag_confidence': 0.0,
            # Auth fields — not applicable to local media; stub values keep app.py happy
            'web_api_enabled': False,
            'auth_ready': False,
            'auth_status': 'n/a',
            'display_name': '',
            'needs_auth': False,
            'token_expires_in_s': 0.0,
            'last_error': '',
            'playback': None,
            'queue': [],
            'queue_len': max(0, len(self._library) - self._index - 1),
            'playlist_context': None,
            'poll_inflight': False,
            'source': 'media-01',
            # Extra: library size for HUD display
            'library_size': len(self._library),
            'library_index': self._index,
            'shuffle': self._shuffle,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_track(self, index: int) -> None:
        if not self._library or self._backend is None:
            return
        index = index % len(self._library)
        self._index = index
        track = self._library[index]
        path: Path = track['path']

        self._previous_title = self._title
        self._previous_artist = self._artist
        self._previous_album = self._album

        self._title = str(track.get('title', path.stem) or path.stem)
        self._artist = str(track.get('artist', '') or '')
        self._album = str(track.get('album', '') or '')
        self._duration_s = float(track.get('duration_s', 0.0) or 0.0)
        self._position_s = 0.0
        self._track_id = f'media:{path.name}'
        self._change_counter += 1
        self._banner_promote_at = time.monotonic() + self._now_playing_banner_delay_s

        self._backend.play(path)
        self._is_playing = True
        self._status = 'playing'

        log.info(
            'media-01: playing [%d/%d] %s — %s',
            index + 1,
            len(self._library),
            self._artist or 'Unknown artist',
            self._title,
        )
