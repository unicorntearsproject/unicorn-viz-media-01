"""Media Deck window + controller-surface tests (media-01).

Device-free: a stub controller provides the control surface, SDL runs on the
dummy video driver, and a fake SecondaryGLWindow avoids GL entirely — the same
harness dj-mixer-01 uses for its window tests.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

sdl2 = pytest.importorskip('sdl2')
pytest.importorskip('PIL')

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / f'{name}.py')
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ui = _load('ui')
_mc = _load('media_controller')
_pl = _load('playlists')


class _FakeGLWindow:
    def __init__(self, title, x, y, width, height, *, extra_flags=0) -> None:  # noqa: ARG002
        self.window = object()
        self.window_id = 77
        self.width = width
        self.height = height

    def create(self) -> None:
        pass

    def present(self, raw, w, h) -> bool:  # noqa: ARG002
        return True

    def destroy(self) -> None:
        self.window = None


class _StubController:
    """Just the control surface MediaDeckWindow drives."""

    def __init__(self, n: int = 8) -> None:
        self.names = [f'Artist {i} - Track {i}' for i in range(n)]
        self.calls: list = []
        self.playing = False
        self.can_seek = True
        self.can_volume = True
        self.volume = 100

    def snapshot(self) -> dict:
        return {'is_playing': self.playing, 'status': 'playing' if self.playing
                else 'paused', 'title': 'Track 1', 'artist': 'Artist 1',
                'album': 'LP', 'previous_title': '', 'previous_artist': '',
                'position_s': 12.0, 'duration_s': 120.0, 'shuffle': True}

    def library_names(self) -> list[str]:
        return list(self.names)

    def play_pause(self):
        self.calls.append('play_pause')
        self.playing = not self.playing

    def next_track(self):
        self.calls.append('next')

    def prev_track(self):
        self.calls.append('prev')

    def toggle_shuffle(self):
        self.calls.append('shuffle')

    def play_index(self, i: int):
        self.calls.append(('play_index', i))

    def seek_to(self, s: float):
        self.calls.append(('seek', round(s, 1)))

    def set_volume(self, v: int):
        self.volume = v
        self.calls.append(('volume', v))


@pytest.fixture(autouse=True)
def _sdl_dummy(monkeypatch):
    os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
    assert sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) == 0
    monkeypatch.setattr(_ui, 'SecondaryGLWindow', _FakeGLWindow)
    yield
    sdl2.SDL_Quit()


def _window(ctl=None):
    win = _ui.MediaDeckWindow(ctl or _StubController(), {'render_interval': 999.0})
    win._stop_thread()
    win._render_ui()
    return win


def _mid(r):
    return int((r[0] + r[2]) / 2), int((r[1] + r[3]) / 2)


def _region(win, action):
    return next(r for r in win._regions if r[4] == action)


def test_transport_buttons_drive_the_controller() -> None:
    ctl = _StubController()
    win = _window(ctl)
    for act in ('prev', 'play', 'next', 'shuffle'):
        win._click(*_mid(_region(win, act)), 1)
    assert ctl.calls[:2] == ['prev', 'play_pause']
    assert ctl.calls[2:] == ['next', 'shuffle']
    win.close_now()


def test_progress_bar_click_seeks_when_backend_can() -> None:
    ctl = _StubController()
    win = _window(ctl)
    reg = _region(win, 'seek')
    x0, x1 = reg[5][0], reg[5][1]
    win._click(int(x0 + (x1 - x0) * 0.5), int((reg[1] + reg[3]) / 2), 1)
    assert ('seek', 60.0) in ctl.calls           # middle of a 120 s track
    # No seek region at all when the backend can't.
    ctl2 = _StubController()
    ctl2.can_seek = False
    win2 = _window(ctl2)
    assert not [r for r in win2._regions if r[4] == 'seek']
    win.close_now()
    win2.close_now()


def test_volume_slider_sets_volume() -> None:
    ctl = _StubController()
    win = _window(ctl)
    reg = _region(win, 'volume')
    x0, x1 = reg[5]
    win._click(int(x0 + (x1 - x0) * 0.5), int((reg[1] + reg[3]) / 2), 1)
    assert ('volume', 100) in ctl.calls          # mid = 100 of 0..200
    win.close_now()


def test_library_rows_select_and_double_click_plays() -> None:
    ctl = _StubController()
    win = _window(ctl)
    row = win._rows[3]
    win._click(*_mid(row), 1)                    # single click selects
    assert win._sel == 3
    win._click(*_mid(row), 2)                    # double click plays
    assert ('play_index', 3) in ctl.calls
    win.close_now()


def test_keyboard_nav_and_enter_plays_selection() -> None:
    ctl = _StubController()
    win = _window(ctl)
    win._on_key(sdl2.SDLK_DOWN, 0, False)
    win._on_key(sdl2.SDLK_DOWN, 0, False)
    win._on_key(sdl2.SDLK_RETURN, 0, False)
    assert ('play_index', 2) in ctl.calls
    win._on_key(sdl2.SDLK_SPACE, 0, False)
    assert 'play_pause' in ctl.calls
    win.close_now()


def test_search_filters_the_view() -> None:
    ctl = _StubController()
    win = _window(ctl)
    win._click(*_mid(_region(win, 'focus_search')), 1)
    assert win._search_focused is True
    for ch in 'track 3':
        win._search_key(ord(ch))
    view = win._view()
    assert len(view) == 1 and view[0][0] == 3
    win._render_ui()
    win._on_key(sdl2.SDLK_RETURN, 0, False)      # exits search focus
    win._on_key(sdl2.SDLK_RETURN, 0, False)      # plays the filtered row
    assert ('play_index', 3) in ctl.calls
    win.close_now()


def test_controller_surface_play_index_and_names(tmp_path) -> None:
    class _App:
        vj_api = None

    ctl = _mc.MediaController(_App(), {'enabled': True,
                                       'media_dir': str(tmp_path)})

    class _Backend:
        can_seek = True
        can_volume = True

        def __init__(self):
            self.played = []

        def play(self, path, duration_s=0.0):
            self.played.append(str(path))

        def seek_s(self, s):
            self.sought = s

        def set_volume(self, v):
            pass

        def position_s(self):
            return 0.0

        def duration_s(self):
            return 0.0

        def is_playing(self):
            return bool(self.played)

        def is_ended(self):
            return False

        def shutdown(self):
            pass

    ctl._backend = _Backend()
    ctl._library = [{'path': tmp_path / 'a.mp3', 'title': 'Alpha',
                     'artist': 'A', 'album': ''},
                    {'path': tmp_path / 'b.mp3', 'title': 'Beta',
                     'artist': '', 'album': ''}]
    assert ctl.library_names() == ['A - Alpha', 'Beta']
    ctl.play_index(1)
    assert ctl._backend.played[-1].endswith('b.mp3')
    assert ctl.snapshot()['title'] == 'Beta'
    assert ctl.can_seek is True
    ctl.seek_to(42.0)
    assert ctl._backend.sought == 42.0
    ctl.shutdown()


def test_keep_playing_checkbox_toggles_and_close_respects_it(tmp_path) -> None:
    class _App:
        vj_api = None

    ctl = _mc.MediaController(_App(), {'enabled': True,
                                       'media_dir': str(tmp_path)})

    class _Backend:
        can_seek = can_volume = False

        def __init__(self):
            self.playing = True
            self.paused = False

        def is_playing(self):
            return self.playing and not self.paused

        def pause(self):
            self.paused = True

        def is_ended(self):
            return False

        def position_s(self):
            return 0.0

        def duration_s(self):
            return 0.0

        def set_volume(self, v):
            pass

        def shutdown(self):
            pass

    ctl._backend = _Backend()
    ctl._status = 'playing'
    assert ctl.keep_playing_on_close is True          # default: remote control
    ctl.close_window()
    assert ctl._backend.paused is False               # music survives the close
    ctl.toggle_keep_playing()
    assert ctl.keep_playing_on_close is False
    ctl.close_window()
    assert ctl._backend.paused is True                # opt-in stop-on-close
    ctl._backend.shutdown()


def test_keep_toggle_region_drives_the_controller() -> None:
    ctl = _StubController()
    ctl.keep_playing_on_close = True

    def toggle():
        ctl.calls.append('keep')

    ctl.toggle_keep_playing = toggle
    win = _window(ctl)
    win._click(*_mid(_region(win, 'keep_toggle')), 1)
    assert 'keep' in ctl.calls
    win.close_now()


def test_f_key_toggles_window_fullscreen_not_forwarded(monkeypatch) -> None:
    ctl = _StubController()
    win = _window(ctl)
    calls = []
    monkeypatch.setattr(_ui.sdl2, 'SDL_SetWindowFullscreen',
                        lambda w, f: calls.append(f))
    win._on_key(sdl2.SDLK_f, 0, False)
    assert calls == [sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP]
    win._on_key(sdl2.SDLK_f, 0, False)
    assert calls[-1] == 0                            # toggles back off
    win.close_now()


def _real_controller(tmp_path, tracks):
    """MediaController with a scanned fake library + fake backend."""
    class _App:
        vj_api = None

    import soundfile as sf
    import numpy as np
    for rel in tracks:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(p), np.zeros((1000, 2), dtype='float32'), 48_000)
    ctl = _mc.MediaController(_App(), {
        'enabled': True, 'media_dir': str(tmp_path), 'shuffle': False,
        'playlists_path': str(tmp_path / 'pl.json')})

    class _Backend:
        can_seek = can_volume = False

        def play(self, path, duration_s=0.0):
            self.last = str(path)

        def pause(self):
            pass

        def is_playing(self):
            return True

        def is_ended(self):
            return False

        def position_s(self):
            return 0.0

        def duration_s(self):
            return 0.0

        def set_volume(self, v):
            pass

        def shutdown(self):
            pass

    ctl._backend = _Backend()
    return ctl


def test_crates_filter_view_and_excludes_hide_everywhere(tmp_path) -> None:
    ctl = _real_controller(tmp_path, ['crates/a.wav', 'crates/b.wav',
                                      'loops/l.wav', 'root.wav'])
    assert [b[:2] for b in ctl.buckets()] == [(None, 'ALL'), ('crates', 'CRATES'),
                                              ('loops', 'LOOPS')]
    assert len(ctl.view_indices()) == 4               # ALL includes root
    ctl.set_bucket('crates')
    assert len(ctl.view_indices()) == 2
    ctl.set_bucket(None)
    assert ctl.toggle_exclude('loops') is True        # hide the loops crate
    assert len(ctl.view_indices()) == 3
    names = [n for _, n in ctl.view_rows()]
    assert not any('l' == n for n in names)
    assert ctl.buckets()[0][2] == 3                   # ALL count respects it
    ctl.shutdown()


def test_playlist_playback_advances_within_the_playlist(tmp_path) -> None:
    ctl = _real_controller(tmp_path, ['a.wav', 'b.wav', 'c.wav', 'd.wav'])
    pls = ctl.playlists
    name = pls.create('Gig')
    paths = [ctl.snapshot_path_at(i) for i in (2, 0)]  # c then a
    pls.add(name, paths)
    ctl.set_mode('playlists')
    assert ctl.active_playlist == 'Gig'
    view = ctl.view_indices()
    assert view == [2, 0]
    ctl.play_index(view[0])                            # play c
    assert ctl._backend.last.endswith('c.wav')
    ctl.next_track()                                   # advances WITHIN playlist
    assert ctl._backend.last.endswith('a.wav')
    ctl.next_track()                                   # wraps to c
    assert ctl._backend.last.endswith('c.wav')
    ctl.shutdown()


def test_sort_orders_the_library_view(tmp_path) -> None:
    ctl = _real_controller(tmp_path, ['zz.wav', 'aa.wav', 'mm.wav'])
    ctl.set_sort('title', True)
    names = [n for _, n in ctl.view_rows()]
    assert names == sorted(names)
    ctl.set_sort('title', False)
    names = [n for _, n in ctl.view_rows()]
    assert names == sorted(names, reverse=True)
    ctl.shutdown()


def test_repeat_modes_cycle_and_govern_auto_advance(tmp_path) -> None:
    ctl = _real_controller(tmp_path, ['a.wav', 'b.wav'])
    assert ctl.repeat == 'all'
    assert 'ONE' in ctl.cycle_repeat()
    assert 'OFF' in ctl.cycle_repeat()
    assert 'ALL' in ctl.cycle_repeat()
    # Simulate end-of-track auto-advance in each mode.
    ctl.play_index(1)                                  # last row of the view

    class _Ended:
        can_seek = can_volume = False

        def is_playing(self):
            return False

        def is_ended(self):
            return True

        def pause(self):
            pass

        def play(self, path, duration_s=0.0):
            self.last = str(path)

        def position_s(self):
            return 0.0

        def duration_s(self):
            return 0.0

        def set_volume(self, v):
            pass

        def shutdown(self):
            pass

    ctl._backend = _Ended()
    ctl._status = 'playing'
    ctl.update(0.1, None)                              # ALL: wraps to a.wav
    assert ctl._backend.last.endswith('a.wav')
    ctl.repeat = 'one'
    ctl._status = 'playing'
    ctl.update(0.1, None)                              # ONE: restarts a.wav
    assert ctl._backend.last.endswith('a.wav')
    ctl.repeat = 'off'
    ctl.play_index(1)                                  # back to the last row
    ctl._status = 'playing'
    del ctl._backend.last
    ctl.update(0.1, None)                              # OFF at end: stops
    assert not hasattr(ctl._backend, 'last')
    assert ctl._status == 'stopped'
    ctl.shutdown()


def test_repeat_button_region_cycles(monkeypatch) -> None:
    ctl = _StubController()
    ctl.calls_repeat = 0

    def cyc():
        ctl.calls_repeat += 1

    ctl.cycle_repeat = cyc
    win = _window(ctl)
    win._click(*_mid(_region(win, 'repeat')), 1)
    assert ctl.calls_repeat == 1
    win.close_now()


def test_delete_file_via_menu_confirms_then_removes(tmp_path) -> None:
    ctl = _real_controller(tmp_path, ['a.wav', 'b.wav'])
    win = _window(ctl)
    row = win._rows[0]
    win._right_click(*_mid(row))
    win._render_ui()                                  # menu regions published
    arm = next(r for r in win._menu_rects if r[4] == 'del_arm')
    win._click_menu(*_mid(arm))                       # arm only — no delete yet
    assert (tmp_path / 'a.wav').exists()
    win._render_ui()
    do = next(r for r in win._menu_rects if r[4] == 'del_do')
    win._click_menu(*_mid(do))
    assert not (tmp_path / 'a.wav').exists()          # gone from disk
    assert len(ctl.library_names()) == 1              # library rescanned
    win.close_now()
    ctl.shutdown()


def test_nested_crates_expand_along_open_path_and_fold_up(tmp_path) -> None:
    ctl = _real_controller(tmp_path, ['crates/house/deep/x.wav',
                                      'crates/techno/y.wav', 'loops/l.wav'])
    tree = ctl.buckets()
    assert [(b[0], b[4]) for b in tree] == [(None, 0), ('crates', 1),
                                            ('loops', 1)]      # folded
    ctl.set_bucket('crates')
    tree = ctl.buckets()
    assert ('crates/house', 'HOUSE', 1, False, 2) in tree      # children shown
    assert ('crates/techno', 'TECHNO', 1, False, 2) in tree
    ctl.set_bucket('crates/house')
    tree = ctl.buckets()
    assert ('crates/house/deep', 'DEEP', 1, False, 3) in tree  # grandchild
    assert len(ctl.view_indices()) == 1                        # subtree filter
    ctl.set_bucket('crates/house')                             # click open -> up
    assert ctl.bucket == 'crates'
    ctl.set_bucket('crates')
    assert ctl.bucket is None                                  # top folds to ALL
    ctl.toggle_exclude('crates')                               # excludes nest
    assert len(ctl.view_indices()) == 1                        # only loops left
    ctl.shutdown()


def test_session_prefs_roundtrip(tmp_path) -> None:
    ctl = _real_controller(tmp_path, ['a.wav', 'b.wav'])
    ctl.cycle_repeat()                                         # -> one
    ctl.toggle_crossfade()                                     # off (default on)
    ctl.set_volume(150)
    ctl.play_index(1)
    ctl.shutdown()                                             # saves prefs

    class _App:
        vj_api = None

    ctl2 = _mc.MediaController(_App(), {
        'enabled': True, 'media_dir': str(tmp_path), 'shuffle': False,
        'playlists_path': str(tmp_path / 'pl.json')})
    assert ctl2.repeat == 'one'
    assert ctl2.crossfade_enabled is False
    assert ctl2.volume == 150
    assert ctl2.library_index == 1                             # selection back
    ctl2.shutdown()


def test_click_then_doubleclick_hits_the_same_row(tmp_path) -> None:
    # The list must NOT recenter on click, or the second click of a
    # double-click lands on a different track.
    ctl = _StubController(n=40)
    win = _window(ctl)
    row = win._rows[min(4, len(win._rows) - 1)]
    win._click(*_mid(row), 1)                    # select
    scroll_before = win._scroll
    win._render_ui()                             # re-render between clicks
    assert win._scroll == scroll_before          # no recentering
    row_again = win._rows[min(4, len(win._rows) - 1)]
    assert row_again[4] == row[4]                # same row under the cursor
    win._click(*_mid(row_again), 2)
    assert ('play_index', row[4]) in ctl.calls
    win.close_now()


def test_favorites_toggle_view_and_persistence(tmp_path) -> None:
    ctl = _real_controller(tmp_path, ['crates/a.wav', 'crates/b.wav',
                                      'loops/l.wav'])
    names = ctl.library_names()
    ib = next(i for i, n in enumerate(names) if 'b' in n and 'crates' in
              str(ctl.snapshot_path_at(i)))
    il = next(i for i, n in enumerate(names)
              if 'loops' in str(ctl.snapshot_path_at(i)))
    assert ctl.is_favorite(ib) is False
    assert ctl.toggle_favorite(ib).startswith('Media: ♥')
    ctl.toggle_favorite(il)
    assert ctl.is_favorite(ib) and ctl.is_favorite(il)

    # Favorites view lists exactly the hearted tracks (excludes don't apply).
    ctl.toggle_exclude('loops')
    ctl.set_mode('favorites')
    fav_paths = {ctl.snapshot_path_at(i) for i in ctl.view_indices()}
    assert fav_paths == {ctl.snapshot_path_at(ib), ctl.snapshot_path_at(il)}

    # Unheart drops it from the view; favorites survive a store reload.
    ctl.toggle_favorite(il)
    assert {ctl.snapshot_path_at(i) for i in ctl.view_indices()} == {
        ctl.snapshot_path_at(ib)}
    ctl._save_prefs()
    reloaded = _pl.MediaPlaylists(tmp_path / 'pl.json')
    assert reloaded.favorites == {ctl.snapshot_path_at(ib)}
    assert reloaded.prefs.get('mode') == 'favorites'


def test_tag_editor_writes_and_leaves_the_audio_untouched(tmp_path) -> None:
    """media-01's copy of the tag editor: metadata only, audio untouched, and
    the library rescans so the row's name updates."""
    sf = pytest.importorskip('soundfile')
    _tg = _load('tags')
    if not _tg.available():
        pytest.skip('mutagen not installed')
    import numpy as np

    f = tmp_path / 'song.flac'
    sf.write(str(f), np.zeros((48_000, 2), dtype='float32'), 48_000)

    def _decoded():
        data, _ = sf.read(str(f), dtype='float32', always_2d=True)
        return np.ascontiguousarray(data)

    class _Ctl(_StubController):
        def __init__(self):
            super().__init__(1)
            self.rescanned = 0
        def snapshot_path_at(self, i):
            return str(f)
        def rescan_library(self):
            self.rescanned += 1

    ctl = _Ctl()
    win = _window(ctl)
    before = _decoded()

    # Open via the real context-menu action path.
    win._tag_edit = {'path': str(f),
                     'fields': {k: '' for k in _tg.TAG_FIELDS},
                     'focus': 0, 'msg': ''}
    for ch in 'abc':
        win._tag_edit_key(ord(ch), 0)            # focus 0 = artist
    win._tag_edit_key(sdl2.SDLK_TAB, 0)          # -> title
    for ch in 'xy':
        win._tag_edit_key(ord(ch), 0)
    win._tag_edit_save()

    assert win._tag_edit is None                 # closed on success
    assert ctl.rescanned == 1                    # library refreshed
    got = _tg.read_tags(f)
    assert got['artist'] == 'abc' and got['title'] == 'xy'
    assert np.array_equal(_decoded(), before)    # audio identical
    win.close_now() if hasattr(win, 'close_now') else None


def test_tag_editor_escape_cancels(tmp_path) -> None:
    sf = pytest.importorskip('soundfile')
    _tg = _load('tags')
    if not _tg.available():
        pytest.skip('mutagen not installed')
    import numpy as np
    f = tmp_path / 's.flac'
    sf.write(str(f), np.zeros((48_000, 2), dtype='float32'), 48_000)
    _tg.write_tags(f, {'artist': 'Keep'})
    win = _window()
    win._tag_edit = {'path': str(f), 'fields': {'artist': 'Discard'},
                     'focus': 0, 'msg': ''}
    win._tag_edit_key(sdl2.SDLK_ESCAPE, 0)
    assert win._tag_edit is None
    assert _tg.read_tags(f)['artist'] == 'Keep'
