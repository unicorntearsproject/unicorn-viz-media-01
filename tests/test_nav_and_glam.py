"""Navigation sophistication + auto-level tests (media-01 0.27.0).

The wheel scrolls the view (not the selection) at desktop speed, every
dropdown caps to the window and scrolls within it, scrollbars drag, PgUp /
PgDn / Home / End work, and the deck loudness-matches tracks the way the
mixer's decks do.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, _ROOT / f'{name}.py')
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


_pl = _load('playlists', 'media_playlists_nav')
_mc = _load('media_controller', 'media_controller_nav')


# -- auto-level: store cache --------------------------------------------------

def test_gain_cache_roundtrip_and_junk_tolerance(tmp_path) -> None:
    s = _pl.MediaPlaylists(tmp_path / 'pl.json')
    assert s.gain_for('/a.mp3') is None
    s.remember_gain('/a.mp3', 1.2345678)
    assert s.gain_for('/a.mp3') == pytest.approx(1.2346, abs=1e-4)
    again = _pl.MediaPlaylists(tmp_path / 'pl.json')
    assert again.gain_for('/a.mp3') == pytest.approx(1.2346, abs=1e-4)
    s.gains['/bad.mp3'] = 'not a number'
    assert s.gain_for('/bad.mp3') is None


# -- auto-level: measurement (the mixer's methodology) -------------------------

def test_quiet_tracks_get_more_gain_than_loud_ones(tmp_path) -> None:
    np = pytest.importorskip('numpy')
    sf = pytest.importorskip('soundfile')
    sr = 48_000
    t = np.linspace(0, 4.0, sr * 4, endpoint=False)
    tone = np.sin(2 * np.pi * 440.0 * t).astype('float32')
    loud = tmp_path / 'loud.wav'
    quiet = tmp_path / 'quiet.wav'
    sf.write(str(loud), np.stack([tone * 0.5] * 2, axis=1), sr)
    sf.write(str(quiet), np.stack([tone * 0.02] * 2, axis=1), sr)
    g_loud = _mc.measure_gain(loud)
    g_quiet = _mc.measure_gain(quiet)
    assert g_loud is not None and g_quiet is not None
    assert g_quiet > g_loud
    for g in (g_loud, g_quiet):           # the mixer's clamps hold
        assert _mc._AUTOGAIN_MIN <= g <= _mc._AUTOGAIN_MAX


def test_unmeasurable_files_read_as_none(tmp_path) -> None:
    bad = tmp_path / 'not_audio.wav'
    bad.write_bytes(b'this is not a wav file')
    assert _mc.measure_gain(bad) is None


# -- auto-level: controller wiring ---------------------------------------------

class _GainBackend:
    can_seek = can_volume = True
    can_crossfade = False

    def __init__(self):
        self.gains: list[float] = []

    def set_track_gain(self, g):
        self.gains.append(round(float(g), 3))

    def play(self, path, duration_s=0.0):
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

    def seek_s(self, s):
        pass

    def shutdown(self):
        pass


def _leveled_controller(tmp_path):
    class _App:
        vj_api = None

    ctl = _mc.MediaController(_App(), {
        'enabled': True, 'media_dir': str(tmp_path), 'shuffle': False,
        'playlists_path': str(tmp_path / 'pl.json')})
    ctl._backend = _GainBackend()
    ctl._library = [{'path': tmp_path / 'a.mp3', 'title': 'A', 'artist': '',
                     'album': '', 'genre': '', 'year': '',
                     'duration_s': 100.0, 'bucket': ''}]
    return ctl


def test_cached_gain_applies_immediately_on_start(tmp_path, monkeypatch) -> None:
    ctl = _leveled_controller(tmp_path)
    monkeypatch.setattr(_mc, 'level_decoder_available', lambda: True)
    ctl.playlists.remember_gain(str(tmp_path / 'a.mp3'), 1.7)
    ctl.play_index(0)
    assert 1.7 in ctl._backend.gains
    ctl.shutdown()


def test_uncached_gain_is_measured_in_the_background(tmp_path, monkeypatch) -> None:
    ctl = _leveled_controller(tmp_path)
    monkeypatch.setattr(_mc, 'level_decoder_available', lambda: True)
    monkeypatch.setattr(_mc, 'measure_gain', lambda p: 0.8)
    ctl.play_index(0)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and 0.8 not in ctl._backend.gains:
        time.sleep(0.01)
    assert 0.8 in ctl._backend.gains       # applied once measured
    assert ctl.playlists.gain_for(str(tmp_path / 'a.mp3')) == 0.8  # cached
    ctl.shutdown()


def test_stale_measurement_never_relevels_the_next_track(tmp_path,
                                                         monkeypatch) -> None:
    ctl = _leveled_controller(tmp_path)
    monkeypatch.setattr(_mc, 'level_decoder_available', lambda: True)
    started = []

    def _slow_measure(p):
        started.append(str(p))
        time.sleep(0.15)
        return 2.0

    monkeypatch.setattr(_mc, 'measure_gain', _slow_measure)
    ctl.play_index(0)
    ctl._track_id = 'media:something-else'   # the track moved on mid-measure
    time.sleep(0.4)
    assert 2.0 not in ctl._backend.gains     # result discarded, only cached
    ctl.shutdown()


def test_toggle_off_returns_to_unity_and_prefs_persist(tmp_path,
                                                       monkeypatch) -> None:
    ctl = _leveled_controller(tmp_path)
    monkeypatch.setattr(_mc, 'level_decoder_available', lambda: True)
    assert ctl.auto_level_enabled is True    # default ON
    assert 'off' in ctl.toggle_auto_level()
    assert ctl._backend.gains[-1] == 1.0
    ctl._save_prefs()
    ctl2 = _leveled_controller(tmp_path)
    assert ctl2.auto_level_enabled is False  # remembered
    ctl.shutdown()
    ctl2.shutdown()


def test_snapshot_reports_auto_level_state(tmp_path, monkeypatch) -> None:
    ctl = _leveled_controller(tmp_path)
    monkeypatch.setattr(_mc, 'level_decoder_available', lambda: True)
    snap = ctl.snapshot()
    assert snap['auto_level'] is True
    assert snap['can_auto_level'] is True
    ctl.shutdown()


# -- window: wheel, paging, capped dropdowns, drags ----------------------------

sdl2 = pytest.importorskip('sdl2')
pytest.importorskip('PIL')

import os  # noqa: E402

_ui = _load('ui', 'media_ui_nav')


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


@pytest.fixture(autouse=True)
def _sdl_dummy(monkeypatch):
    os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
    assert sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) == 0
    monkeypatch.setattr(_ui, 'SecondaryGLWindow', _FakeGLWindow)
    yield
    sdl2.SDL_Quit()


class _BigStub:
    """A large library + many playlists, to exercise every overflow path."""

    mode = 'library'
    active_playlist = 'Playlist 00'
    bucket = None
    can_seek = can_volume = True
    volume = 100
    sort = (None, True)
    playing = False
    keep_playing_on_close = True

    def __init__(self, n=300, npl=40):
        self.names = [f'Artist {i} - Track {i}' for i in range(n)]
        self.calls: list = []
        store = self

        class _Store:
            def names(self):
                return [f'Playlist {i:02d}' for i in range(npl)]

            def tracks(self, name):  # noqa: ARG002
                return ['/a'] * 5

            def missing(self, name):  # noqa: ARG002
                return []

        self.playlists = _Store()
        self._ctl = store

    def snapshot(self):
        return {'is_playing': False, 'status': 'paused', 'title': 'T',
                'artist': 'A', 'album': '', 'previous_title': '',
                'previous_artist': '', 'position_s': 10.0,
                'duration_s': 200.0, 'shuffle': False}

    def library_names(self):
        return list(self.names)

    def play_pause(self):
        self.calls.append('play_pause')

    def next_track(self):
        pass

    def prev_track(self):
        pass

    def toggle_shuffle(self):
        pass

    def play_index(self, i):
        self.calls.append(('play_index', i))

    def seek_to(self, s):
        self.calls.append(('seek', round(s, 1)))

    def set_volume(self, v):
        self.volume = v
        self.calls.append(('volume', v))


def _window(ctl, **cfg):
    win = _ui.MediaDeckWindow(ctl, {'render_interval': 999.0, **cfg})
    win._stop_thread()
    win._render_ui()
    return win


def _mid(r):
    return int((r[0] + r[2]) / 2), int((r[1] + r[3]) / 2)


def test_wheel_scrolls_the_view_not_the_selection() -> None:
    win = _window(_BigStub())
    win._mouse_xy = (win._width - 60, win._height - 100)   # over the rows
    win._sel = 0
    win._wheel(1)
    assert win._scroll == _ui._WHEEL_ROWS                  # desktop speed
    assert win._sel == 0                                   # selection stays
    win._wheel(-1)
    assert win._scroll == 0
    win.close_now()


def test_wheel_over_the_folder_pane_scrolls_folders() -> None:
    class _Crates(_BigStub):
        def buckets(self):
            return ([(None, 'ALL', 300, False, 0)]
                    + [(f'c{i}', f'C{i}', 5, False, 1) for i in range(30)])

    win = _window(_Crates())
    assert win._folder_rect is not None
    fx = (win._folder_rect[0] + win._folder_rect[2]) // 2
    fy = (win._folder_rect[1] + win._folder_rect[3]) // 2
    win._mouse_xy = (fx, fy)
    before = win._scroll
    win._wheel(1)
    assert win._folder_scroll == 2 and win._scroll == before
    win.close_now()


def test_page_and_home_end_keys() -> None:
    win = _window(_BigStub())
    win._on_key(sdl2.SDLK_PAGEDOWN, 0, False)
    assert win._sel == max(1, win._rows_visible - 1)
    win._on_key(sdl2.SDLK_END, 0, False)
    assert win._sel == len(win._view()) - 1
    win._on_key(sdl2.SDLK_HOME, 0, False)
    assert win._sel == 0
    win.close_now()


def test_playlist_picker_caps_to_the_window_and_scrolls() -> None:
    ctl = _BigStub()
    ctl.mode = 'playlists'
    win = _window(ctl)
    win._pl_open = True
    win._render_ui()
    assert win._flyout_rect is not None
    assert win._flyout_rect[3] <= win._height - 8          # inside the pane
    first = {r[4] for r in win._pl_rects}
    assert 'Playlist 39' not in first                      # off the first page
    # Wheel over the flyout walks the rest into view.
    win._mouse_xy = _mid(win._flyout_rect)
    for _ in range(30):
        win._wheel(1)
    win._render_ui()
    assert 'Playlist 39' in {r[4] for r in win._pl_rects}  # tail reachable
    win.close_now()


def test_filter_flyout_scrolls_through_a_big_facet() -> None:
    class _Facets(_BigStub):
        active_filter_count = 0

        def distinct(self, facet):
            return ([f'Artist {i:03d}' for i in range(150)]
                    if facet == 'artist' else [])

        def toggle_filter(self, f, v):
            self.calls.append(('filter', f, v))

        def is_filtered(self, f, v):  # noqa: ARG002
            return False

        def clear_filters(self):
            pass

    win = _window(_Facets())
    win._filter_open = True
    win._filter_expanded = 'artist'
    win._render_ui()
    vals = [r[5][1] for r in win._regions if r[4] == 'filter_val']
    assert vals and 'Artist 149' not in vals               # capped, not cut
    assert win._flyout_rect is not None
    win._mouse_xy = _mid(win._flyout_rect)
    for _ in range(80):
        win._wheel(1)
    win._render_ui()
    vals = [r[5][1] for r in win._regions if r[4] == 'filter_val']
    assert 'Artist 149' in vals                            # tail reachable
    win.close_now()


def test_scrollbar_thumb_drags_the_list() -> None:
    win = _window(_BigStub())
    win._render_ui()
    bar = next(b for b in win._scrollbars if b[4] == 'list')
    x = (bar[0] + bar[2]) // 2
    assert win._begin_drag(x, bar[1] + 5) is True          # grab near the top
    win._drag_to(x, bar[3])                                # yank to the bottom
    assert win._scroll == len(win._view()) - win._rows_visible
    win.on_sdl_event(type('E', (), {'type': sdl2.SDL_MOUSEBUTTONUP})())
    assert win._drag is None
    win.close_now()


def test_seek_and_volume_drag_follow_the_pointer() -> None:
    ctl = _BigStub()
    win = _window(ctl)
    seek = next(r for r in win._regions if r[4] == 'seek')
    assert win._begin_drag(*_mid(seek)) is True
    win._drag_to(seek[2], _mid(seek)[1])                   # scrub to the end
    assert ctl.calls[-1][0] == 'seek' and ctl.calls[-1][1] >= 190
    win._drag = None
    vol = next(r for r in win._regions if r[4] == 'volume')
    assert win._begin_drag(*_mid(vol)) is True
    win._drag_to(vol[5][0], _mid(vol)[1])                  # drag to zero
    assert ('volume', 0) in ctl.calls
    win.close_now()


def test_long_context_menu_caps_and_wheels() -> None:
    win = _window(_BigStub(), window_width=760, window_height=520)
    win._render_ui()
    win._menu = {'x': 500, 'y': 60, 'name': 'Track',
                 'items': [(f'→ P{i:02d}', 'add_to', (f'p{i}', 1))
                           for i in range(40)]}
    win._menu_scroll = 0
    win._render_ui()
    fit = len(win._menu_rects)
    assert fit < 40                                        # capped to fit
    assert win._menu_rects[-1][3] <= win._height           # on screen
    win._wheel(1)                                          # menu owns wheel
    win._render_ui()
    labels = [r[5] for r in win._menu_rects]
    assert ('p0', 1) not in labels                         # scrolled past
    win.close_now()


def test_auto_level_checkbox_drives_the_controller() -> None:
    class _Level(_BigStub):
        def snapshot(self):
            s = super().snapshot()
            s.update({'auto_level': True, 'can_auto_level': True,
                      'crossfade': False, 'can_crossfade': False})
            return s

        def toggle_auto_level(self):
            self.calls.append('auto_level')

    ctl = _Level()
    win = _window(ctl)
    hit = next(r for r in win._regions if r[4] == 'auto_level_toggle')
    win._click(*_mid(hit), 1)
    assert 'auto_level' in ctl.calls
    win.close_now()


def test_auto_level_checkbox_is_inert_without_a_capable_backend() -> None:
    win = _window(_BigStub())      # snapshot has no can_auto_level
    assert not any(r[4] == 'auto_level_toggle' for r in win._regions)
    win.close_now()


# -- up-next queue: controller (0.27.0) ----------------------------------------

class _NullBackend:
    can_seek = can_volume = can_crossfade = False

    def play(self, path, duration_s=0.0):
        self.last = str(path)

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


def _queue_controller(tmp_path, tracks=4):
    class _App:
        vj_api = None

    ctl = _mc.MediaController(_App(), {
        'enabled': True, 'media_dir': str(tmp_path), 'shuffle': False,
        'playlists_path': str(tmp_path / 'pl.json')})
    ctl._library = [{'path': tmp_path / f'{i}.mp3', 'title': f'T{i}',
                     'artist': '', 'album': '', 'genre': '', 'year': '',
                     'duration_s': 100.0, 'bucket': ''}
                    for i in range(tracks)]
    ctl._backend = _NullBackend()
    return ctl


def test_queue_add_rejects_out_of_range_and_reports_position(tmp_path) -> None:
    ctl = _queue_controller(tmp_path)
    assert 'nothing to queue' in ctl.queue_add(-1)
    assert 'nothing to queue' in ctl.queue_add(99)
    assert ctl.queue_len == 0
    msg = ctl.queue_add(2)
    assert '#1' in msg and ctl.queue_len == 1
    ctl.shutdown()


def test_forward_advance_drains_the_queue_before_the_view(tmp_path) -> None:
    """FIFO, and it wins over shuffle/sort/crate -- a literal array, not a
    view-relative thing."""
    ctl = _queue_controller(tmp_path)
    ctl._shuffle = True
    ctl.queue_add(3)
    ctl.queue_add(1)
    ctl._start_track(0)
    ctl.next_track()
    assert ctl._index == 3                 # first queued, first played
    ctl.next_track()
    assert ctl._index == 1                 # second queued, second played
    assert ctl.queue_len == 0              # drained
    ctl.shutdown()


def test_queue_works_identically_regardless_of_active_mode(tmp_path) -> None:
    ctl = _queue_controller(tmp_path)
    ctl.playlists.create('Gig')
    ctl.playlists.add('Gig', [str(ctl._library[2]['path'])])
    ctl.set_mode('playlists')
    ctl.active_playlist = 'Gig'
    ctl.queue_add(0)                       # queue a track NOT in this playlist
    ctl._start_track(1)
    ctl.next_track()
    assert ctl._index == 0                 # queue still wins, mode ignored
    ctl.shutdown()


def test_prev_never_touches_the_queue(tmp_path) -> None:
    ctl = _queue_controller(tmp_path)
    ctl.queue_add(3)
    ctl._start_track(1)
    ctl._backend.position_s = lambda: 10.0  # past the 3s restart threshold
    ctl.prev_track()
    assert ctl._index != 3 or ctl.queue_len == 1  # queue untouched either way
    assert ctl.queue_len == 1
    ctl.shutdown()


def test_vanished_queued_track_is_skipped_not_fatal(tmp_path) -> None:
    ctl = _queue_controller(tmp_path)
    ctl.queue_add(1)
    ctl._queue.insert(0, 999)              # simulate a track that left the library
    ctl._start_track(0)
    ctl.next_track()
    assert ctl._index == 1                 # skipped the bad entry, played the real one
    assert ctl.queue_len == 0
    ctl.shutdown()


def test_queue_preview_and_clear(tmp_path) -> None:
    ctl = _queue_controller(tmp_path)
    ctl.queue_add(0)
    ctl.queue_add(1)
    ctl.queue_add(2)
    preview = ctl.queue_preview(2)
    assert len(preview) == 2 and preview[0] == ctl.library_names()[0]
    ctl.clear_queue()
    assert ctl.queue_len == 0
    ctl.shutdown()


def test_dequeue_removes_a_single_entry(tmp_path) -> None:
    ctl = _queue_controller(tmp_path)
    ctl.queue_add(0)
    ctl.queue_add(1)
    assert ctl.dequeue(0) is True
    assert ctl._queue == [1]
    assert ctl.dequeue(5) is False
    ctl.shutdown()


# -- up-next queue: window ------------------------------------------------------

class _QueueStub(_BigStub):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.queue_len = 0
        self._q: list[int] = []

    def queue_add(self, idx):
        self._q.append(idx)
        self.queue_len = len(self._q)
        self.calls.append(('queue_add', idx))

    def queue_preview(self, n):
        return [self.names[i] for i in self._q[:n]]

    def clear_queue(self):
        self._q = []
        self.queue_len = 0
        self.calls.append('clear_queue')

    def is_favorite(self, i):  # noqa: ARG002
        return False

    def snapshot_path_at(self, i):
        return f'/m/{i}.mp3'


def test_add_to_queue_appears_in_every_mode_and_uses_library_index() -> None:
    for mode in ('library', 'playlists', 'favorites', 'recent'):
        ctl = _QueueStub()
        ctl.mode = mode
        win = _window(ctl)
        win._right_click(*_mid(win._rows[2]))
        assert win._menu is not None, mode
        labels = [i[0] for i in win._menu['items']]
        assert 'Add to queue' in labels, mode
        win._render_ui()
        hit = next(m for m in win._menu_rects if m[4] == 'queue_add')
        win._click_menu(*_mid(hit))
        assert ('queue_add', 2) in ctl.calls, mode
        win.close_now()


def test_queue_badge_shows_only_when_something_is_queued_and_clears_on_click() -> None:
    ctl = _QueueStub()
    win = _window(ctl)
    assert not any(r[4] == 'queue_flyout' for r in win._regions)
    ctl.queue_add(0)
    win._render_ui()
    badge = next(r for r in win._regions if r[4] == 'queue_flyout')
    win._click(*_mid(badge), 1)
    assert 'clear_queue' in ctl.calls
    win.close_now()


# -- side-show: the platter's flanking EQ + sparks -----------------------------

def test_side_show_renders_without_crashing_at_every_size() -> None:
    ctl = _BigStub()
    ctl.playing = True
    for width, height in ((980, 640), (760, 520), (1600, 900)):
        win = _window(ctl, window_width=width, window_height=height)
        win._render_ui()                   # smoke: must not raise
        win.close_now()


def test_side_show_skips_gracefully_with_no_spectrum() -> None:
    ctl = _BigStub()
    ctl.playing = True
    win = _window(ctl)
    win._draw_side_show(
        __import__('PIL.ImageDraw', fromlist=['Draw']).Draw(
            __import__('PIL.Image', fromlist=['new']).new('RGBA', (400, 300))),
        10, 390, 200, 150, 120, True, (0.5, 0.3, 0.6, 0.9), ())
    win.close_now()
