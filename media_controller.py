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

__version__ = '0.21.0'

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
    ('Local Media', 'Shift+L', 'Toggle the Media Deck window'),
    ('Local Media', 'Ctrl+Alt+M', 'Play / pause'),
    ('Local Media', 'Ctrl+Alt+N', 'Next track'),
    ('Local Media', 'Ctrl+Alt+B', 'Previous track / restart'),
    ('Local Media', 'Ctrl+Alt+Shift+M', 'Toggle shuffle'),
    ('Local Media', 'Esc Esc', 'Close the Media Deck window (press twice)'),
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


# Background library-scan cache, keyed by resolved media-dir string.  Populated
# by MediaController.prescan_async() so the ~842-track walk+tag-read (the
# controller's biggest startup cost, ~0.6s) overlaps the app's splash screen
# instead of stalling boot after it.  Mirrors ImageShowcase's prefetch cache.
_LIBRARY_SCAN_CACHE: dict[str, list[dict[str, Any]]] = {}
_LIBRARY_SCAN_LOCK = threading.Lock()


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
        # --aout=pulse: route through PulseAudio (pipewire-pulse on modern
        # Linux).  VLC's native PipeWire output module segfaults in
        # pw_thread_loop_lock when audio_set_volume races output creation
        # (observed crash 2026-07-16); the pulse module handles it safely.
        args = ['--no-video', '--quiet', '--aout=pulse']
        self._instance = vlc.Instance(args)
        # Two players so tracks can overlap for a true crossfade: the active
        # one carries the current track; the other fades the old one out.
        self._players = [self._instance.media_player_new(),
                         self._instance.media_player_new()]
        self._active = 0
        self._volume = max(0, min(200, volume))
        self._fade: list | None = None    # [elapsed_s, fade_s, outgoing idx]
        self._pending_volume = False      # apply volume once playback clock runs
        # Never set volume on a player without a live audio output: libvlc
        # aout modules can crash (see --aout=pulse note above).  Volume is
        # deferred to tick() after the playback clock starts.

    @property
    def _player(self):
        return self._players[self._active]

    def play(self, path: Path) -> None:
        self._end_fade(hard=True)
        media = self._instance.media_new(str(path))
        self._player.set_media(media)
        self._player.play()
        # Volume applies in tick() once get_time() advances: play() is async
        # and the audio output does not exist yet at this point.
        self._pending_volume = True

    can_crossfade = True

    def crossfade_to(self, path: Path, fade_s: float = 3.0) -> bool:
        """Start *path* on the idle player at volume 0 and begin the ramp.

        The incoming player becomes active immediately (position/duration
        report the new track); :meth:`tick` drives the volume ramp and stops
        the outgoing player when it lands.
        """
        try:
            out_idx = self._active
            inc = self._players[1 - out_idx]
            inc.set_media(self._instance.media_new(str(path)))
            inc.play()
            try:
                inc.audio_set_volume(0)   # start silent; ramp brings it up
            except Exception:
                pass
            self._end_fade(hard=True)     # collapse any fade already running
            self._active = 1 - out_idx
            self._fade = [0.0, max(0.2, float(fade_s)), out_idx]
            return True
        except Exception:
            return False

    def tick(self, dt: float) -> None:
        """Apply deferred volume + advance the crossfade ramp (every frame)."""
        if self._pending_volume:
            try:
                if self._player.get_time() > 0:   # audio output is live now
                    self._player.audio_set_volume(self._volume)
                    self._pending_volume = False
            except Exception:
                self._pending_volume = False
        if self._fade is None:
            return
        self._fade[0] += max(0.0, float(dt))
        t = min(1.0, self._fade[0] / self._fade[1])
        out_idx = self._fade[2]
        self._players[self._active].audio_set_volume(int(self._volume * t))
        self._players[out_idx].audio_set_volume(int(self._volume * (1.0 - t)))
        if t >= 1.0:
            self._end_fade()

    def _end_fade(self, hard: bool = False) -> None:
        if self._fade is None:
            return
        out_idx = self._fade[2]
        self._fade = None
        try:
            self._players[out_idx].stop()
            self._players[out_idx].audio_set_volume(self._volume)
            if hard:
                self._players[self._active].audio_set_volume(self._volume)
        except Exception:
            pass

    def pause(self) -> None:
        self._player.pause()

    def resume(self) -> None:
        if self._player.is_playing():
            return
        self._player.play()

    def stop(self) -> None:
        self._end_fade(hard=True)
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
        self._volume = max(0, min(200, volume))
        # Mid-fade tick() re-applies the ramp.  Before the playback clock
        # starts the aout may not exist (crash risk) — defer to tick().
        if self._fade is not None:
            return
        try:
            if self._player.get_time() > 0:
                self._player.audio_set_volume(self._volume)
            else:
                self._pending_volume = True
        except Exception:
            self._pending_volume = True

    can_seek = True
    can_volume = True

    def seek_s(self, seconds: float) -> None:
        try:
            self._player.set_time(int(max(0.0, seconds) * 1000))
        except Exception:
            pass

    def shutdown(self) -> None:
        try:
            for p in self._players:
                p.stop()
                p.release()
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

    can_seek = False
    can_volume = False
    can_crossfade = False

    def seek_s(self, seconds: float) -> None:
        pass  # subprocess backends can't seek

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

    @staticmethod
    def _resolve_media_dir(cfg: dict[str, Any]) -> Path:
        """Resolve the media directory from config (or the bundled default)."""
        raw_dir = str((cfg or {}).get('media_dir', '') or '').strip()
        if raw_dir:
            return Path(raw_dir).expanduser()
        return Path(__file__).parent / 'media'

    @classmethod
    def prescan_async(cls, cfg: dict[str, Any]) -> None:
        """Walk + tag-read the media library on a background thread.

        Called from app startup *before* the splash screen so the scan (the
        controller's dominant construction cost) overlaps the splash instead
        of stalling boot after it.  No SDL/GL/VLC — pure disk I/O + tag
        reads, safe off the main thread.  ``rescan_library`` later finds the
        result already cached and skips the walk.  Mirrors
        ``ImageShowcase.prefetch_async``.
        """
        if not bool((cfg or {}).get('enabled', False)):
            return
        media_dir = cls._resolve_media_dir(cfg)
        key = str(media_dir.resolve()) if media_dir.exists() else str(media_dir)
        with _LIBRARY_SCAN_LOCK:
            if key in _LIBRARY_SCAN_CACHE:
                return

        def _scan() -> None:
            tracks = _scan_library(media_dir)
            with _LIBRARY_SCAN_LOCK:
                _LIBRARY_SCAN_CACHE.setdefault(key, tracks)
            log.info('media-01: prescanned %d track(s) from %s', len(tracks), media_dir)

        threading.Thread(target=_scan, daemon=True, name='media-prescan').start()

    def __init__(self, app: Any, cfg: dict[str, Any] | None = None) -> None:
        self._app = app
        self._cfg = dict(cfg or {})

        self.enabled: bool = bool(self._cfg.get('enabled', False))

        self._media_dir = self._resolve_media_dir(self._cfg)

        self._shuffle: bool = bool(self._cfg.get('shuffle', True))
        # 2026-08-07: boot-time auto-play for unattended/headless use (e.g.
        # training-daemon --media-source) -- see App._maybe_auto_play_media().
        # Set before the `enabled` early-return below so the attribute
        # always exists, matching self.enabled's always-set pattern.
        self.auto_play: bool = bool(self._cfg.get('auto_play', False))
        # 2026-08-14: boot-time named-playlist selection for unattended/
        # headless training runs (training-daemon --source media
        # --media-favorites, which passes boot_playlist='favorites' --
        # matching the actual named playlist in the playlists store, NOT
        # the separate hearted-tracks favorites set) -- see
        # _maybe_boot_playlist(), called at the end of __init__.
        self._boot_playlist: str = str(self._cfg.get('boot_playlist', '') or '').strip()
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

        self.rescan_library()
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
        # Offer ourselves as a playlist destination, so another drop-in (the
        # DJ mixer) can hand us a set without either of us importing the
        # other.  Registered late in __init__ so _playlists exists by the time
        # anything can call in.
        self._vj_for_sink = vj

        self._window = None       # Media Deck window (Shift+L)
        # Playlists + library view state (accordion crates / sort / excludes).
        # Playback (next/prev/auto-advance) follows the ACTIVE VIEW: the open
        # playlist, else the open crate minus excluded crates.
        self._playlists = None
        try:
            import importlib.util as _ilu  # noqa: PLC0415

            _pp = Path(__file__).resolve().parent / 'playlists.py'
            _spec = _ilu.spec_from_file_location('media_playlists', _pp)
            _mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            store_path = self._cfg.get('playlists_path') or (
                Path(__file__).resolve().parents[2] / 'runtime'
                / 'media_playlists.json')
            self._playlists = _mod.MediaPlaylists(store_path)
        except Exception as exc:
            log.warning('media-01: playlists store unavailable: %s', exc)
        # Now that the store exists, offer ourselves as a destination other
        # drop-ins can hand a playlist to (the DJ mixer's "send set to media
        # player").  Neither side imports the other; the core hub is the seam.
        sink = getattr(self._vj_for_sink, 'register_playlist_sink', None)
        if callable(sink) and self._playlists is not None:
            try:
                sink('media player', self.accept_playlist)
            except Exception as exc:
                log.debug('media-01: playlist sink registration failed: %s', exc)
        self.mode = 'library'              # 'library' | 'playlists'
        rep = str(self._cfg.get('repeat', 'all') or 'all').lower()
        # Default OFF as of 0.20.0, matching the DJ mixer's loop flip: a
        # list that never ends cannot be walked away from, and an
        # unattended run needs 'the set is over' to be reachable at all.
        # ALL and ONE remain one button-press away.
        self.repeat = rep if rep in ('all', 'one', 'off') else 'off'
        self._set_over = False     # latched; see _note_set_over()
        # Crossfade between tracks (VLC backend: true overlapping fade).
        self.crossfade_enabled = bool(self._cfg.get('crossfade', True))
        self._crossfade_s = max(0.5, float(
            self._cfg.get('crossfade_ms', 3000) or 3000) / 1000.0)
        self._end_handled = False    # early-fade fired for the current track
        self._restore_prefs()        # remembered prefs override the defaults
        # The corner platter ('Now Spinning') is core now — the checkbox
        # drives vj_api.set/toggle_now_spinning (no-op on older cores).
        self.active_playlist: str | None = None
        self.bucket: str | None = (self._playlists.last_bucket
                                   if self._playlists else None)
        self._sort_key: str | None = None  # 'title' | 'artist' | None
        self._sort_asc = True
        # Closing the Media Deck leaves the music playing by default (the
        # window is a remote control, not the player).  The in-window checkbox
        # toggles this; config sets the startup default.
        self.keep_playing_on_close = bool(
            self._cfg.get('keep_playing_on_close', True))
        self._maybe_boot_playlist()

    def _maybe_boot_playlist(self) -> None:
        """Force a specific named playlist for an unattended training run.

        Gated by ``boot_playlist`` (a playlist name, empty = disabled) so
        normal interactive starts are unaffected. Runs last in __init__,
        after _restore_prefs(), so an explicit boot request always wins
        over whatever a previous interactive session left saved in prefs
        -- the point of an unattended run is deterministic, requested
        behavior, not resuming a human's last state (contrast dj-mixer-01's
        autoplay boot, which deliberately does *not* override a restored
        mode -- that one protects a real DJ's last live toggle; there is
        no such state to protect here). Falls back to the library view
        (logged) if the named playlist doesn't exist or is empty, rather
        than booting into an empty view that would silently never start
        playback.
        """
        if not self._boot_playlist or self._playlists is None:
            return
        if self._boot_playlist not in self._playlists.names():
            log.warning(
                'media-01: boot_playlist %r not found (have: %s); '
                'falling back to the library view',
                self._boot_playlist, self._playlists.names(),
            )
            return
        self.mode = 'playlists'
        self.active_playlist = self._boot_playlist
        view = self.view_indices()
        if not view:
            log.warning(
                'media-01: boot_playlist %r is empty; falling back to '
                'the library view', self._boot_playlist,
            )
            self.mode = 'library'
            return
        self._index = view[0]

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
        self._advance(1)
        return f'Media: next → {self._title or "track"}'

    def prev_track(self) -> str:
        """Jump back to the previous track (or restart if past 3 s)."""
        if not self.enabled or not self._library:
            return 'Media: library empty'
        if self._backend is not None and self._backend.position_s() > 3.0:
            self._start_track(self._index)
            return f'Media: restart {self._title or "track"}'
        self._advance(-1)
        return f'Media: prev → {self._title or "track"}'

    def _restore_prefs(self) -> None:
        """Re-apply the remembered session prefs (playlists-store 'prefs')."""
        p = self._playlists.prefs if self._playlists else {}
        if not p:
            return
        if p.get('repeat') in ('all', 'one', 'off'):
            self.repeat = p['repeat']
        if 'shuffle' in p:
            self._shuffle = bool(p['shuffle'])
        if 'crossfade' in p:
            self.crossfade_enabled = bool(p['crossfade'])
        if 'volume' in p:
            self.set_volume(int(p['volume']))
        if p.get('mode') in ('library', 'playlists', 'favorites'):
            self.mode = p['mode']
        if p.get('active_playlist'):
            self.active_playlist = str(p['active_playlist'])
        if p.get('last_track'):        # restore the selection, never autoplay
            for i, t in enumerate(self._library):
                if str(t.get('path', '')) == p['last_track']:
                    self._index = i
                    break
        log.info('media-01: session prefs restored')

    def _save_prefs(self) -> None:
        if self._playlists is None:
            return
        self._playlists.prefs = {
            'repeat': self.repeat,
            'shuffle': self._shuffle,
            'crossfade': self.crossfade_enabled,
            'volume': self._volume,
            'mode': self.mode,
            'active_playlist': self.active_playlist,
            'last_track': self.snapshot_path_at(self._index) or '',
        }
        self._playlists.save()

    @property
    def can_crossfade(self) -> bool:
        return bool(getattr(self._backend, 'can_crossfade', False))

    def toggle_crossfade(self) -> str:
        self.crossfade_enabled = not self.crossfade_enabled
        return ('Media: crossfade on' if self.crossfade_enabled
                else 'Media: crossfade off')

    def cycle_repeat(self) -> str:
        """Loop button: cycle repeat ALL -> ONE -> OFF (playback wrap mode)."""
        order = ('all', 'one', 'off')
        self.repeat = order[(order.index(self.repeat) + 1) % 3]
        return f'Media: repeat {self.repeat.upper()}'

    def toggle_shuffle(self) -> str:
        """Toggle shuffle mode (takes effect on next track advance)."""
        self._shuffle = not self._shuffle
        return f'Media: shuffle {"ON" if self._shuffle else "OFF"}'

    def set_volume(self, volume: int) -> None:
        """Set playback volume (0-200, 100 = 100%)."""
        self._volume = max(0, min(200, volume))
        if self._backend is not None:
            self._backend.set_volume(self._volume)

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def can_seek(self) -> bool:
        return bool(getattr(self._backend, 'can_seek', False))

    @property
    def can_volume(self) -> bool:
        return bool(getattr(self._backend, 'can_volume', False))

    def seek_to(self, seconds: float) -> None:
        """Jump within the current track (VLC backend only)."""
        if self._backend is not None and self.can_seek:
            self._backend.seek_s(seconds)
            self._position_s = max(0.0, float(seconds))

    def play_index(self, index: int) -> str:
        """Play library row *index* (browser double-click / Enter)."""
        if not self.enabled or not self._library or self._backend is None:
            return 'Media: library empty'
        self._start_track(int(index) % len(self._library))
        return f'Media: playing {self._title or "track"}'

    @property
    def library_index(self) -> int:
        return self._index

    def library_names(self) -> list[str]:
        """Display names ('Artist - Title' or stem) for the browser list."""
        out: list[str] = []
        for t in self._library:
            title = str(t.get('title', '') or '') or Path(str(t.get('path', ''))).stem
            artist = str(t.get('artist', '') or '')
            out.append(f'{artist} - {title}' if artist else title)
        return out

    def library_parts(self) -> list[tuple[str, str, float]]:
        """``(artist, title, duration_s)`` per library row.

        The browser draws artist and title as separate columns, so it needs
        them apart rather than pre-joined -- a joined string cannot
        column-align, and on a long list the eye scans one field at a time.
        An untagged file reports an empty artist, which is the signal the UI
        uses to give the filename the whole width.
        """
        out: list[tuple[str, str, float]] = []
        for t in self._library:
            title = str(t.get('title', '') or '') or Path(str(t.get('path', ''))).stem
            artist = str(t.get('artist', '') or '')
            try:
                dur = float(t.get('duration_s', 0.0) or 0.0)
            except (TypeError, ValueError):
                dur = 0.0
            out.append((artist, title, dur))
        return out

    def view_parts(self) -> list[tuple[int, str, str, float]]:
        """:meth:`library_parts` for the active view, with library indices."""
        parts = self.library_parts()
        return [(i, *parts[i]) for i in self.view_indices()
                if 0 <= i < len(parts)]

    def view_stats(self) -> dict:
        """Count, running time and how much of it is unknown, for the header.

        A row count answers "how much have I got" only if you already know the
        tracks.  Files whose length never got read contribute zero and are
        counted separately, so the total reads as *at least* this long rather
        than silently short.
        """
        rows = self.view_parts()
        total = sum(d for _i, _a, _t, d in rows if d > 0.0)
        unknown = sum(1 for _i, _a, _t, d in rows if d <= 0.0)
        return {'count': len(rows), 'duration_s': total, 'unknown': unknown}

    # ------------------------------------------------------------------
    # Subsystem interface (called by app.py)
    # ------------------------------------------------------------------

    def update(self, dt: float, audio: Any) -> None:  # noqa: ARG002
        """Poll playback state; advance to next track when current one ends."""
        if not self.enabled or self._backend is None:
            return

        now = time.monotonic()

        tick = getattr(self._backend, 'tick', None)
        if callable(tick):
            tick(dt)                      # advance any crossfade ramp

        if self._backend.is_playing():
            self._position_s = self._backend.position_s()
            # Prefer VLC's duration once it becomes available after playback starts.
            backend_dur = self._backend.duration_s()
            if backend_dur > 0.0:
                self._duration_s = backend_dur
            self._is_playing = True
            self._status = 'playing'
            # Crossfade begins *before* the outro ends so the tracks overlap.
            if (self.crossfade_enabled and not self._end_handled
                    and getattr(self._backend, 'can_crossfade', False)
                    and self._duration_s > 0.0
                    and (self._duration_s - self._position_s)
                    <= self._crossfade_s):
                self._end_handled = True
                if self.repeat == 'one':
                    self._start_track(self._index)
                else:
                    view = self.view_indices()
                    at_end = bool(view) and self._index == view[-1]
                    if not (self.repeat == 'off' and at_end):
                        self._advance(1)
        elif self._status == 'playing' and self._backend.is_ended():
            # Track finished — auto-advance within the active view, honouring
            # the repeat mode (ALL wraps, ONE restarts, OFF stops at the end).
            if self._library:
                if self.repeat == 'one':
                    self._start_track(self._index)
                else:
                    view = self.view_indices()
                    at_end = bool(view) and self._index == view[-1]
                    if self.repeat == 'off' and at_end:
                        self._is_playing = False
                        self._status = 'stopped'
                        self._note_set_over()
                    else:
                        self._advance(1)
        elif self._status == 'paused':
            pass
        else:
            self._is_playing = False

        # Promote the banner change counter once the announce delay has elapsed.
        if self._banner_promote_at > 0.0 and now >= self._banner_promote_at:
            self._banner_change_counter = self._change_counter
            self._banner_promote_at = 0.0

        win = self._window
        if win is not None and not win.is_open:
            self.close_window()          # window self-closed (Esc Esc)

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
        if shift and not ctrl and not alt and sym == sdl2.SDLK_l:
            self.toggle_window()
            return None

        return False

    # -- active view: crates / sort / playlists --------------------------------

    def buckets(self) -> list[tuple]:
        """Accordion crate tree rows: (key, label, count, excluded, depth).

        ALL first (depth 0), then top-level crates; the crates along the open
        path expand inline (their children indented one depth deeper), classic
        accordion — everything else stays folded.
        """
        paths = [str(t.get('bucket', '') or '') for t in self._library]
        exc = self._playlists.excluded if self._playlists else set()

        def count_under(prefix: str) -> int:
            return sum(1 for b in paths
                       if self._under(b, prefix) and not self._hidden(b))

        visible_total = sum(1 for b in paths if not self._hidden(b))
        rows: list[tuple] = [(None, 'ALL', visible_total, False, 0)]
        open_parts = self.bucket.split('/') if self.bucket else []

        def children(prefix_parts: list[str]) -> list[str]:
            depth = len(prefix_parts)
            prefix = '/'.join(prefix_parts)
            names = set()
            for b in paths:
                parts = b.split('/') if b else []
                if len(parts) > depth and (not prefix
                                           or self._under(b, prefix)):
                    names.add(parts[depth])
            return sorted(names)

        def emit(prefix_parts: list[str], depth: int) -> None:
            for name in children(prefix_parts):
                path = '/'.join([*prefix_parts, name])
                rows.append((path, name.upper(), count_under(path),
                             path in exc, depth))
                # Expand only along the open chain (one branch at a time).
                if open_parts[:depth] == [*prefix_parts, name][:depth]                         and len(open_parts) >= depth                         and open_parts[depth - 1] == name:
                    emit([*prefix_parts, name], depth + 1)

        emit([], 1)
        return rows

    def set_bucket(self, name: str | None) -> None:
        """Open a crate; clicking the already-open crate folds it up a level."""
        if name and name == self.bucket:
            name = name.rsplit('/', 1)[0] if '/' in name else None
        self.bucket = name or None
        if self._playlists:
            self._playlists.set_last_bucket(self.bucket)

    def toggle_exclude(self, bucket: str) -> bool:
        """Hide/show a crate everywhere (library, ALL, and playback)."""
        if not self._playlists or not bucket:
            return False
        state = self._playlists.toggle_excluded(bucket)
        if self.bucket == bucket and state:
            self.set_bucket(None)          # excluded the open crate -> ALL
        return state

    def set_sort(self, key: str | None, ascending: bool = True) -> None:
        self._sort_key = key if key in ('title', 'artist') else None
        self._sort_asc = bool(ascending)

    @property
    def sort(self) -> tuple:
        return self._sort_key, self._sort_asc

    def set_mode(self, mode: str) -> None:
        if mode in ('library', 'playlists', 'favorites'):
            self.mode = mode
            if (mode == 'playlists' and self._playlists
                    and self.active_playlist not in self._playlists.names()):
                names = self._playlists.names()
                self.active_playlist = names[0] if names else None

    def toggle_favorite(self, index: int) -> str:
        """Heart a library row: flips membership in the persisted favorites."""
        path = self.snapshot_path_at(index)
        if not path or self._playlists is None:
            return 'Media: nothing to favorite'
        fav = self._playlists.toggle_favorite(path)
        name = Path(path).stem
        return (f'Media: ♥ {name}' if fav else f'Media: unhearted {name}')

    def is_favorite(self, index: int) -> bool:
        path = self.snapshot_path_at(index)
        favs = self._playlists.favorites if self._playlists else set()
        return bool(path) and path in favs

    @property
    def playlists(self):
        return self._playlists

    def view_indices(self) -> list[int]:
        """Library indices in active-view order — the playback sequence."""
        if self.mode == 'playlists' and self._playlists and self.active_playlist:
            by_path = {str(t.get('path', '')): i
                       for i, t in enumerate(self._library)}
            return [by_path[p] for p in
                    self._playlists.tracks(self.active_playlist)
                    if p in by_path]
        if self.mode == 'favorites':
            # Explicit picks: crate excludes don't apply, sort still does.
            favs = self._playlists.favorites if self._playlists else set()
            idx = [i for i, t in enumerate(self._library)
                   if str(t.get('path', '')) in favs]
        else:
            idx = [i for i, t in enumerate(self._library)
                   if not self._hidden(str(t.get('bucket', '') or ''))
                   and (self.bucket is None
                        or self._under(str(t.get('bucket', '') or ''),
                                       self.bucket))]
        if self._sort_key:
            def _key(i: int):
                t = self._library[i]
                v = str(t.get(self._sort_key, '') or '')
                return (v.lower() or '~', str(t.get('title', '')).lower())
            idx.sort(key=_key, reverse=not self._sort_asc)
        return idx

    def rescan_library(self) -> None:
        """(Re)walk the media dir and tag each track's crate *path*.

        Crates nest: a track's bucket is its directory path relative to the
        media dir, '/'-joined, truncated to ``crate_depth`` (default 5).
        """
        depth = max(1, int(self._cfg.get('crate_depth', 5) or 5))
        # Prefer a background prescan result (kicked off during the splash) to
        # avoid re-walking the library on the main thread; fall back to a
        # synchronous scan when no prescan ran (e.g. a runtime rescan).
        key = str(self._media_dir.resolve()) if self._media_dir.exists() else str(self._media_dir)
        cached: list[dict[str, Any]] | None
        with _LIBRARY_SCAN_LOCK:
            cached = _LIBRARY_SCAN_CACHE.pop(key, None)
        self._library = cached if cached is not None else _scan_library(self._media_dir)
        for t in self._library:
            try:
                rel = Path(t['path']).relative_to(self._media_dir)
                t['bucket'] = '/'.join(rel.parts[:-1][:depth])
            except Exception:
                t['bucket'] = ''

    @staticmethod
    def _under(bucket: str, prefix: str) -> bool:
        return bucket == prefix or bucket.startswith(prefix + '/')

    def _hidden(self, bucket: str) -> bool:
        exc = self._playlists.excluded if self._playlists else set()
        return any(self._under(bucket, e) for e in exc)

    def delete_track(self, index: int) -> str:
        """Delete library row *index*'s FILE from disk (context menu, after a
        SURE? confirm in the UI) and rescan.  A playing track keeps playing —
        the backend holds its own handle."""
        path = self.snapshot_path_at(index)
        if not path:
            return 'Media: nothing to delete'
        try:
            Path(path).unlink(missing_ok=True)
            log.warning('media-01: deleted file %s', path)
        except Exception as exc:
            return f'Media: delete failed ({exc})'
        self.rescan_library()
        return f'Media: deleted {Path(path).name}'

    def snapshot_path_at(self, index: int) -> str | None:
        """Absolute path of library row *index* (playlist membership)."""
        if 0 <= index < len(self._library):
            return str(self._library[index].get('path', '') or '') or None
        return None

    def view_rows(self) -> list[tuple[int, str]]:
        """(library index, display name) rows for the active view."""
        names = self.library_names()
        return [(i, names[i]) for i in self.view_indices()]

    def _note_set_over(self) -> None:
        """The playlist finished with REPEAT OFF: say so on the session bus.

        The stopping already worked -- REPEAT OFF has always halted at the end
        of the list.  What was missing was *announcing* it: the player simply
        went quiet, which is invisible to anything downstream, including an
        unattended run waiting to package a recording.

        Publishes the same ``phase: 'over'`` the DJ mixer publishes when its
        set clock runs out or its playlist is exhausted, so a consumer never
        has to know which source ended the session, or which app it came from.
        Announced once and re-armed by :meth:`_start_track`.
        """
        if self._set_over:
            return
        self._set_over = True
        log.info('media-01: set over — playlist finished with REPEAT OFF')
        vj = getattr(self._app, 'vj_api', None)
        publish = getattr(vj, 'publish_session', None)
        if callable(publish):
            try:
                publish('media', {'phase': 'over', 'source': 'list_exhausted',
                                  'seconds_left': 0.0, 'minutes_left': 0.0})
            except Exception as exc:       # pragma: no cover - defensive
                log.debug('media-01: set-over publish failed: %s', exc)

    def _advance(self, delta: int) -> None:
        """Move within the active view (auto-advance / next / prev)."""
        view = self.view_indices()
        if not view:
            return
        try:
            pos = (view.index(self._index) + delta) % len(view)
        except ValueError:
            pos = 0                        # current track fell out of the view
        self._start_track(view[pos])

    def toggle_ambience(self) -> str:
        """Checkbox: the core Now Spinning corner platter on the main output."""
        vj = getattr(self._app, 'vj_api', None)
        if vj is not None and hasattr(vj, 'toggle_now_spinning'):
            on = vj.toggle_now_spinning()
            return f'Media: now-spinning platter {"on" if on else "off"}'
        return 'Media: now-spinning unavailable on this core'

    @property
    def ambience_enabled(self) -> bool:
        vj = getattr(self._app, 'vj_api', None)
        return bool(getattr(vj, 'now_spinning_enabled', False))

    # -- Media Deck window (Shift+L) ------------------------------------------

    @property
    def is_window_open(self) -> bool:
        return self._window is not None and self._window.is_open

    @property
    def is_open(self) -> bool:
        """Subsystem cursor protocol: the app shows the mouse cursor while any
        subsystem window is open (SDL_ShowCursor is process-global)."""
        return self.is_window_open

    def open_window(self) -> None:
        if not self.enabled or self._window is not None:
            return
        try:
            import importlib.util  # noqa: PLC0415

            path = Path(__file__).resolve().parent / 'ui.py'
            spec = importlib.util.spec_from_file_location('media_deck_ui', path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._window = mod.MediaDeckWindow(
                self, self._cfg, vj_api=getattr(self._app, 'vj_api', None))
            log.info('media-01: Media Deck window opened')
        except Exception as exc:
            self._window = None
            log.warning('media-01: Media Deck window failed to open: %s', exc)
        finally:
            vj = getattr(self._app, 'vj_api', None)
            if vj is not None:      # opening a 2nd SDL window can migrate GL
                try:
                    vj.rebind_main_gl_context()
                except Exception:
                    pass

    def toggle_keep_playing(self) -> str:
        """Checkbox in the Media Deck: keep playing after the window closes."""
        self.keep_playing_on_close = not self.keep_playing_on_close
        return ('Media: keeps playing when closed'
                if self.keep_playing_on_close else 'Media: stops when closed')

    def close_window(self) -> None:
        if (not self.keep_playing_on_close and self._backend is not None
                and self._backend.is_playing()):
            try:
                self._backend.pause()
                self._is_playing = False
                self._status = 'paused'
                log.info('media-01: paused on window close')
            except Exception:
                pass
        win = self._window
        self._window = None
        if win is not None:
            try:
                win.close_now()
            except Exception:
                pass
            vj = getattr(self._app, 'vj_api', None)
            if vj is not None:
                try:
                    vj.rebind_main_gl_context()
                except Exception:
                    pass

    def toggle_window(self) -> None:
        if self.is_window_open:
            self.close_window()
        else:
            self.open_window()

    def present(self) -> None:
        """Blit the Media Deck window (called each frame by the app)."""
        win = self._window
        if win is not None:
            try:
                win.present()
            except Exception as exc:
                log.debug('media-01: present failed: %s', exc)

    def shutdown(self) -> None:
        """Stop playback and release resources."""
        vj = getattr(self._app, 'vj_api', None)
        if vj is not None and hasattr(vj, 'unregister_now_playing'):
            try:
                vj.unregister_now_playing('media')
            except Exception:
                pass
        self.close_window()
        try:
            self._save_prefs()
        except Exception:
            pass
        if self._backend is not None:
            self._backend.shutdown()
            self._backend = None

    def accept_playlist(self, name: str, paths: list) -> int:
        """Take a playlist handed over by another drop-in.

        Keeps the sender's name, which is the point of the feature: a set
        called "Friday Peak" in the mixer should be "Friday Peak" here, not
        "Playlist 7".  An existing list of that name is **added to** rather
        than replaced -- the sender may be sending a second batch, and
        silently discarding what was already there is not recoverable.

        Returns how many tracks were actually taken (duplicates already in the
        list are skipped by the store), so the sender can report honestly
        rather than echoing back what it asked for.
        """
        store = self._playlists
        if store is None:
            raise RuntimeError('media-01 has no playlist store')
        rows = [str(p) for p in (paths or []) if p]
        if not rows:
            return 0
        title = str(name or '').strip() or 'Imported'
        if title not in store.names():
            store.create(title)
        took = int(store.add(title, rows))
        store.save()
        log.info('media-01: accepted %d/%d track(s) into playlist %r',
                 took, len(rows), title)
        return took

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
            'repeat': self.repeat,
            'ambience': self.ambience_enabled,
            'crossfade': self.crossfade_enabled,
            'can_crossfade': self.can_crossfade,
            'keep_playing_on_close': self.keep_playing_on_close,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_track(self, index: int) -> None:
        # Playing again, so a later exhaustion is a new event to announce.
        self._set_over = False
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

        faded = False
        if (self.crossfade_enabled and self._is_playing
                and getattr(self._backend, 'can_crossfade', False)):
            faded = bool(self._backend.crossfade_to(path, self._crossfade_s))
        if not faded:
            self._backend.play(path)
        self._end_handled = False
        self._is_playing = True
        self._status = 'playing'

        log.info(
            'media-01: playing [%d/%d] %s — %s',
            index + 1,
            len(self._library),
            self._artist or 'Unknown artist',
            self._title,
        )
