"""Media Player 2.0 upgrade tests (media-01 0.24.0).

The library grows up to the mixer's browser: faceted filters, a RECENT view,
album/year/length sorting, playlist management (rename / duplicate / prune),
and the audio-reactive deck chrome.  Store and controller tests run headless;
window tests ride the same dummy-SDL harness as test_media_deck.py.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, alias: str | None = None):
    spec = importlib.util.spec_from_file_location(alias or name,
                                                  _ROOT / f'{name}.py')
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias or name] = mod
    spec.loader.exec_module(mod)
    return mod


_pl = _load('playlists', 'media_playlists_two_oh')
_mc = _load('media_controller', 'media_controller_two_oh')


# -- store: rename / duplicate / recent / prune ------------------------------

def _store(tmp_path):
    return _pl.MediaPlaylists(tmp_path / 'pl.json')


def test_rename_keeps_picker_order_and_refuses_collisions(tmp_path) -> None:
    s = _store(tmp_path)
    for n in ('First', 'Second', 'Third'):
        s.create(n)
    assert s.rename('Second', 'Middle') is True
    assert s.names() == ['First', 'Middle', 'Third']   # position survives
    assert s.rename('Middle', 'Third') is False        # taken
    assert s.rename('Middle', '  ') is False           # blank
    assert s.rename('Ghost', 'X') is False             # no such list


def test_duplicate_forks_under_a_free_name(tmp_path) -> None:
    s = _store(tmp_path)
    s.create('Gig')
    s.add('Gig', ['/a.mp3', '/b.mp3'])
    assert s.duplicate('Gig') == 'Gig (2)'
    assert s.duplicate('Gig') == 'Gig (3)'
    assert s.tracks('Gig (2)') == ['/a.mp3', '/b.mp3']
    s.add('Gig (2)', ['/c.mp3'])
    assert s.tracks('Gig') == ['/a.mp3', '/b.mp3']      # the original is safe
    assert s.duplicate('Ghost') is None


def test_note_played_dedupes_caps_and_persists(tmp_path) -> None:
    s = _store(tmp_path)
    s.note_played('/a.mp3')
    s.note_played('/b.mp3')
    s.note_played('/a.mp3')                 # replayed: moves to the front once
    assert s.recent == ['/a.mp3', '/b.mp3']
    for i in range(60):
        s.note_played(f'/t{i}.mp3')
    assert len(s.recent) == s._RECENT_CAP
    assert s.recent[0] == '/t59.mp3'
    again = _pl.MediaPlaylists(tmp_path / 'pl.json')
    assert again.recent == s.recent          # survives a restart


def test_save_preserves_externally_added_playlists(tmp_path) -> None:
    """2026-09-01: a live instance's routine internal save (note_played()
    fires on every track change during plain playback, no playlist edit
    involved) must not clobber a playlist another process wrote to the
    same file after this instance's own _load() -- only this instance's
    own known list names are authoritative; foreign names on disk get
    merged in rather than dropped."""
    s = _store(tmp_path)
    s.create('Mine')
    s.add('Mine', ['/a.mp3'])           # persists 'Mine' to disk

    # external process (e.g. a tuning tool editing the JSON directly)
    # adds a new list this instance never loaded
    on_disk = json.loads((tmp_path / 'pl.json').read_text())
    on_disk['playlists']['training - added externally'] = ['/e1.mp3', '/e2.mp3']
    (tmp_path / 'pl.json').write_text(json.dumps(on_disk))

    s.note_played('/a.mp3')             # unrelated bookkeeping save

    again = _pl.MediaPlaylists(tmp_path / 'pl.json')
    assert again.tracks('Mine') == ['/a.mp3']                        # own list intact
    assert again.tracks('training - added externally') == ['/e1.mp3', '/e2.mp3']  # not clobbered


def test_save_own_edits_win_over_stale_disk_for_known_names(tmp_path) -> None:
    """The merge only rescues names this instance never knew about --
    for a name both sides know, this instance's own in-memory edits are
    authoritative (an external rewrite of a list this session is actively
    editing should not silently win)."""
    s = _store(tmp_path)
    s.create('Shared')
    s.add('Shared', ['/a.mp3'])

    on_disk = json.loads((tmp_path / 'pl.json').read_text())
    on_disk['playlists']['Shared'] = ['/stale.mp3']
    (tmp_path / 'pl.json').write_text(json.dumps(on_disk))

    s.add('Shared', ['/b.mp3'])         # this instance's own mutation + save

    again = _pl.MediaPlaylists(tmp_path / 'pl.json')
    assert again.tracks('Shared') == ['/a.mp3', '/b.mp3']


def test_prune_missing_removes_only_dead_paths(tmp_path) -> None:
    live = tmp_path / 'real.mp3'
    live.write_bytes(b'x')
    s = _store(tmp_path)
    s.create('Gig')
    s.add('Gig', [str(live), str(tmp_path / 'gone.mp3')])
    assert s.missing('Gig') == [str(tmp_path / 'gone.mp3')]
    assert s.prune_missing('Gig') == 1
    assert s.tracks('Gig') == [str(live)]
    assert s.prune_missing('Gig') == 0       # nothing left to prune


def test_remove_and_move_by_path_identity(tmp_path) -> None:
    s = _store(tmp_path)
    s.create('Gig')
    s.add('Gig', ['/a.mp3', '/b.mp3', '/c.mp3'])
    assert s.move_path('Gig', '/c.mp3', -1) == 1
    assert s.tracks('Gig') == ['/a.mp3', '/c.mp3', '/b.mp3']
    assert s.remove_path('Gig', '/c.mp3') is True
    assert s.tracks('Gig') == ['/a.mp3', '/b.mp3']
    assert s.remove_path('Gig', '/ghost.mp3') is False


# -- controller: tags, facets, sort, recent ----------------------------------

def test_year_and_decade_parsing() -> None:
    assert _mc._year_of('1994') == '1994'
    assert _mc._year_of('1994-06-01') == '1994'
    assert _mc._year_of('June 94') == ''
    assert _mc._decade_of('1994') == '1990s'
    assert _mc._decade_of('2001') == '2000s'
    assert _mc._decade_of('') == ''


def _controller(tmp_path, tracks):
    """A real controller over an empty dir, library injected directly."""
    class _App:
        vj_api = None

    ctl = _mc.MediaController(_App(), {
        'enabled': True, 'media_dir': str(tmp_path), 'shuffle': False,
        'playlists_path': str(tmp_path / 'pl.json')})
    ctl._library = tracks
    return ctl


_TRACKS = [
    {'path': Path('/m/one.mp3'), 'title': 'One', 'artist': 'Anzo',
     'album': 'LP1', 'genre': 'House', 'year': '1994', 'duration_s': 300.0,
     'bucket': ''},
    {'path': Path('/m/two.mp3'), 'title': 'Two', 'artist': 'Anzo',
     'album': 'LP2', 'genre': 'Techno', 'year': '2001', 'duration_s': 200.0,
     'bucket': ''},
    {'path': Path('/m/three.mp3'), 'title': 'Three', 'artist': 'Blix',
     'album': 'LP1', 'genre': 'House', 'year': '1997', 'duration_s': 100.0,
     'bucket': ''},
]


def test_distinct_facet_values_include_decades(tmp_path) -> None:
    ctl = _controller(tmp_path, [dict(t) for t in _TRACKS])
    assert ctl.distinct('artist') == ['Anzo', 'Blix']
    assert ctl.distinct('genre') == ['House', 'Techno']
    assert ctl.distinct('album') == ['LP1', 'LP2']
    assert ctl.distinct('decade') == ['1990s', '2000s']
    assert ctl.distinct('bpm') == []          # DJ facets stay the mixer's
    ctl.shutdown()


def test_facet_filters_narrow_every_view(tmp_path) -> None:
    ctl = _controller(tmp_path, [dict(t) for t in _TRACKS])
    ctl.toggle_filter('genre', 'House')
    assert ctl.active_filter_count == 1
    assert ctl.view_indices() == [0, 2]        # library view
    ctl.toggle_filter('artist', 'Blix')        # AND across facets
    assert ctl.view_indices() == [2]
    # ...and in favorites too: FILTER means one thing everywhere.
    ctl.clear_filters()
    ctl.playlists.toggle_favorite('/m/one.mp3')
    ctl.playlists.toggle_favorite('/m/two.mp3')
    ctl.set_mode('favorites')
    assert ctl.view_indices() == [0, 1]
    ctl.toggle_filter('genre', 'House')
    assert ctl.view_indices() == [0]
    # OR within one facet: a second genre widens it again.
    ctl.toggle_filter('genre', 'Techno')
    assert ctl.view_indices() == [0, 1]
    ctl.clear_filters()
    assert ctl.active_filter_count == 0
    ctl.shutdown()


def test_sort_by_album_year_and_length(tmp_path) -> None:
    ctl = _controller(tmp_path, [dict(t) for t in _TRACKS])
    ctl.set_sort('year', True)
    assert ctl.view_indices() == [0, 2, 1]
    ctl.set_sort('length', False)
    assert ctl.view_indices() == [0, 1, 2]
    ctl.set_sort('album', True)                # ties broken by title
    assert [ctl._library[i]['album'] for i in ctl.view_indices()] == \
        ['LP1', 'LP1', 'LP2']
    ctl.set_sort('bogus', True)                # unknown key -> scan order
    assert ctl.sort == (None, True)
    ctl.shutdown()


def test_recent_view_is_newest_first_and_playback_feeds_it(tmp_path) -> None:
    ctl = _controller(tmp_path, [dict(t) for t in _TRACKS])

    class _Backend:
        can_seek = can_volume = False

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

    ctl._backend = _Backend()
    ctl.play_index(1)
    ctl.play_index(0)
    ctl.set_mode('recent')
    assert ctl.view_indices() == [0, 1]        # newest first
    # A path that has left the library is skipped, not an error.
    ctl.playlists.note_played('/m/ghost.mp3')
    assert ctl.view_indices() == [0, 1]
    ctl.shutdown()


def test_playlist_stats_report_length_and_dead_paths(tmp_path) -> None:
    ctl = _controller(tmp_path, [dict(t) for t in _TRACKS])
    ctl.playlists.create('Gig')
    ctl.playlists.add('Gig', ['/m/one.mp3', '/m/two.mp3', '/m/ghost.mp3'])
    st = ctl.playlist_stats('Gig')
    assert st == {'count': 3, 'duration_s': 500.0, 'unknown': 0, 'missing': 1}
    ctl.shutdown()


def test_track_info_carries_the_whole_tag_card(tmp_path) -> None:
    ctl = _controller(tmp_path, [dict(t) for t in _TRACKS])
    info = ctl.track_info(0)
    assert info['title'] == 'One' and info['genre'] == 'House'
    assert info['year'] == '1994' and info['duration_s'] == 300.0
    assert ctl.track_info(99) == {}
    ctl.shutdown()


def test_audio_levels_are_captured_only_while_the_deck_is_open(tmp_path) -> None:
    np = pytest.importorskip('numpy')
    ctl = _controller(tmp_path, [dict(t) for t in _TRACKS])

    class _Audio:
        bass, mid, treble, beat = 0.5, 0.25, 2.0, 0.9   # treble overdriven
        fft = np.linspace(0.0, 1.0, 512, dtype=np.float32)

    ctl._capture_audio_levels(_Audio())            # window closed: no work
    assert ctl.audio_levels == (0.0, 0.0, 0.0, 0.0)
    ctl._window = object()
    ctl._capture_audio_levels(_Audio())
    assert ctl.audio_levels == (0.5, 0.25, 1.0, 0.9)   # clamped to [0, 1]
    assert len(ctl.audio_spectrum) == 64        # full FFT, not folded down
    assert all(0.0 <= v <= 1.0 for v in ctl.audio_spectrum)
    assert ctl.audio_spectrum[0] < ctl.audio_spectrum[-1]
    ctl._window = None
    ctl.shutdown()


def test_snapshot_carries_genre_and_year(tmp_path) -> None:
    ctl = _controller(tmp_path, [dict(t) for t in _TRACKS])

    class _Backend:
        can_seek = can_volume = False

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

        def shutdown(self):
            pass

    ctl._backend = _Backend()
    ctl.play_index(0)
    snap = ctl.snapshot()
    assert snap['genre'] == 'House' and snap['year'] == '1994'
    ctl.shutdown()


# -- window: filter flyout, RECENT tab, playlist management ------------------

sdl2 = pytest.importorskip('sdl2')
pytest.importorskip('PIL')

import os  # noqa: E402


@pytest.fixture(autouse=True)
def _sdl_dummy(monkeypatch):
    os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
    assert sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) == 0
    monkeypatch.setattr(_ui, 'SecondaryGLWindow', _FakeGLWindow)
    yield
    sdl2.SDL_Quit()


_ui = _load('ui', 'media_ui_two_oh')


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


class _FilterStub:
    """The 2.0 control surface: facets + modes + a playlist store."""

    def __init__(self) -> None:
        self.names = [f'Artist {i} - Track {i}' for i in range(4)]
        self.calls: list = []
        self.playing = False
        self.can_seek = self.can_volume = False
        self.mode = 'library'
        self.filters: set = set()
        self.audio_levels = (0.3, 0.2, 0.6, 0.8)
        self.audio_spectrum = tuple(i / 16.0 for i in range(16))

    def snapshot(self) -> dict:
        return {'is_playing': self.playing, 'status': 'paused',
                'title': 'Track 1', 'artist': 'Artist 1', 'album': 'LP',
                'genre': 'House', 'year': '1994',
                'previous_title': '', 'previous_artist': '',
                'position_s': 12.0, 'duration_s': 120.0, 'shuffle': True}

    def library_names(self) -> list[str]:
        return list(self.names)

    def play_pause(self):
        self.calls.append('play_pause')

    def next_track(self):
        self.calls.append('next')

    def prev_track(self):
        self.calls.append('prev')

    def toggle_shuffle(self):
        self.calls.append('shuffle')

    def play_index(self, i: int):
        self.calls.append(('play_index', i))

    def set_mode(self, mode: str):
        self.mode = mode
        self.calls.append(('mode', mode))

    # facets
    def distinct(self, facet: str):
        return ['House', 'Techno'] if facet == 'genre' else []

    def toggle_filter(self, facet: str, value: str):
        self.filters.symmetric_difference_update({(facet, value)})
        self.calls.append(('filter', facet, value))

    def is_filtered(self, facet: str, value: str) -> bool:
        return (facet, value) in self.filters

    def clear_filters(self):
        self.filters = set()
        self.calls.append('clear_filters')

    @property
    def active_filter_count(self) -> int:
        return len(self.filters)


def _window(ctl):
    win = _ui.MediaDeckWindow(ctl, {'render_interval': 999.0})
    win._stop_thread()
    win._render_ui()
    return win


def _mid(r):
    return int((r[0] + r[2]) / 2), int((r[1] + r[3]) / 2)


def _region(win, action, payload=None):
    return next(r for r in win._regions
                if r[4] == action and (payload is None or r[5] == payload))


def test_recent_tab_exists_and_switches_mode() -> None:
    ctl = _FilterStub()
    win = _window(ctl)
    win._click(*_mid(_region(win, 'set_mode', 'recent')), 1)
    assert ('mode', 'recent') in ctl.calls
    win.close_now()


def test_filter_flyout_toggles_facets_and_clears() -> None:
    ctl = _FilterStub()
    win = _window(ctl)
    win._click(*_mid(_region(win, 'toggle_filter_flyout')), 1)
    win._render_ui()
    win._click(*_mid(_region(win, 'filter_cat', 'genre')), 1)
    win._render_ui()                       # expanded: values now published
    win._click(*_mid(_region(win, 'filter_val', ('genre', 'House'))), 1)
    assert ('filter', 'genre', 'House') in ctl.calls
    assert ctl.is_filtered('genre', 'House')
    win._render_ui()
    win._click(*_mid(_region(win, 'filter_clear')), 1)
    assert 'clear_filters' in ctl.calls and not ctl.filters
    win.close_now()


def test_filter_icon_absent_on_a_pre_two_oh_controller() -> None:
    class _Old(_FilterStub):
        @property
        def toggle_filter(self):           # a controller without facets
            raise AttributeError('pre-2.0 controller')

    win = _window(_Old())
    assert not any(r[4] == 'toggle_filter_flyout' for r in win._regions)
    win.close_now()


def test_sort_flyout_offers_album_year_and_length() -> None:
    ctl = _FilterStub()
    sorts: list = []
    ctl.set_sort = lambda k, asc: sorts.append((k, asc))
    ctl.sort = (None, True)
    win = _window(ctl)
    win._click(*_mid(_region(win, 'toggle_sort')), 1)
    win._render_ui()
    for key in ('album', 'year', 'length'):
        assert any(r[4] == 'set_sort' and r[5] == (key, True)
                   for r in win._regions), key
    win._click(*_mid(_region(win, 'set_sort', ('year', False))), 1)
    assert sorts == [('year', False)]
    win.close_now()


class _PlStore:
    def __init__(self) -> None:
        self.lists = {'Gig': ['/a.mp3', '/b.mp3']}
        self.calls: list = []

    def names(self):
        return list(self.lists)

    def tracks(self, name):
        return list(self.lists.get(name, []))

    def missing(self, name):  # noqa: ARG002
        return []

    def rename(self, old, new):
        self.calls.append(('rename', old, new))
        if not new or new in self.lists:
            return False
        self.lists[new] = self.lists.pop(old)
        return True

    def duplicate(self, name):
        self.calls.append(('dup', name))
        copy = f'{name} (2)'
        self.lists[copy] = list(self.lists[name])
        return copy

    def prune_missing(self, name):
        self.calls.append(('prune', name))
        return 0

    def delete(self, name):
        self.calls.append(('delete', name))
        self.lists.pop(name, None)


class _PlaylistStub(_FilterStub):
    def __init__(self) -> None:
        super().__init__()
        self.mode = 'playlists'
        self.active_playlist = 'Gig'
        self._store = _PlStore()

    @property
    def playlists(self):
        return self._store

    def playlist_stats(self, name):  # noqa: ARG002
        return {'count': 2, 'duration_s': 500.0, 'unknown': 0, 'missing': 0}


def _open_pl_flyout(win):
    win._click(*_mid(_region(win, 'pl_flyout')), 1)
    win._render_ui()
    assert win._pl_rects, 'flyout rows should be right-clickable'


def test_playlist_rename_via_right_click_prompt() -> None:
    ctl = _PlaylistStub()
    win = _window(ctl)
    _open_pl_flyout(win)
    r = win._pl_rects[0]
    win._right_click(*_mid(r))
    assert win._menu is not None
    labels = [i[0] for i in win._menu['items']]
    assert labels[0].startswith('Rename') and 'Duplicate' in labels
    win._render_ui()                       # publish menu rects
    hit = next(m for m in win._menu_rects if m[4] == 'pl_rename')
    win._click_menu(*_mid(hit))
    assert win._prompt is not None and win._prompt['value'] == 'Gig'
    for _ in range(3):                     # Gig -> ''
        win._prompt_key(sdl2.SDLK_BACKSPACE, 0)
    for ch in 'rave':
        win._prompt_key(ord(ch), 0)
    win._prompt_key(sdl2.SDLK_RETURN, 0)
    assert win._prompt is None
    assert ('rename', 'Gig', 'rave') in ctl._store.calls
    assert ctl.active_playlist == 'rave'   # the open list follows its name
    win.close_now()


def test_playlist_delete_is_armed_before_it_fires() -> None:
    ctl = _PlaylistStub()
    win = _window(ctl)
    _open_pl_flyout(win)
    win._right_click(*_mid(win._pl_rects[0]))
    win._render_ui()
    hit = next(m for m in win._menu_rects if m[4] == 'pl_del_arm')
    win._click_menu(*_mid(hit))
    assert ('delete', 'Gig') not in ctl._store.calls    # armed, not fired
    win._render_ui()
    hit = next(m for m in win._menu_rects if m[4] == 'pl_del_do')
    win._click_menu(*_mid(hit))
    assert ('delete', 'Gig') in ctl._store.calls
    win.close_now()


def test_prompt_escape_cancels_without_touching_the_store() -> None:
    ctl = _PlaylistStub()
    win = _window(ctl)
    _open_pl_flyout(win)
    win._right_click(*_mid(win._pl_rects[0]))
    win._render_ui()
    hit = next(m for m in win._menu_rects if m[4] == 'pl_rename')
    win._click_menu(*_mid(hit))
    win._prompt_key(sdl2.SDLK_ESCAPE, 0)
    assert win._prompt is None
    assert not any(c[0] == 'rename' for c in ctl._store.calls)
    win.close_now()


def test_row_remove_from_playlist_resolves_by_path() -> None:
    removed: list = []

    class _Store(_PlStore):
        def remove_path(self, name, path):
            removed.append((name, path))
            return True

    ctl = _PlaylistStub()
    ctl._store = _Store()
    ctl.view_rows = lambda: [(0, 'Artist 0 - Track 0'), (1, 'Artist 1 - Track 1')]
    ctl.snapshot_path_at = lambda i: f'/m/{i}.mp3'
    win = _window(ctl)
    row = win._rows[1]
    win._right_click(*_mid(row))
    assert win._menu is not None
    win._render_ui()
    hit = next(m for m in win._menu_rects if m[4] == 'pl_remove')
    assert hit[5] == '/m/1.mp3'            # a path, not a view row number
    win._click_menu(*_mid(hit))
    assert removed == [('Gig', '/m/1.mp3')]
    win.close_now()


def test_reactive_platter_renders_with_levels_and_spectrum() -> None:
    ctl = _FilterStub()
    ctl.playing = True
    win = _window(ctl)                     # smoke: spectrum ring + beat flash
    win._render_ui()
    win.close_now()


def test_hms_is_coarse_on_purpose() -> None:
    assert _ui._hms(0) == '0m'
    assert _ui._hms(59) == '0m'
    assert _ui._hms(23 * 60) == '23m'
    assert _ui._hms(83 * 60) == '1h23m'


def test_recent_mode_survives_a_prefs_roundtrip(tmp_path) -> None:
    ctl = _controller(tmp_path, [dict(t) for t in _TRACKS])
    ctl.set_mode('recent')
    ctl._save_prefs()
    raw = json.loads((tmp_path / 'pl.json').read_text())
    assert raw['prefs']['mode'] == 'recent'
    ctl2 = _controller(tmp_path, [dict(t) for t in _TRACKS])
    assert ctl2.mode == 'recent'
    ctl.shutdown()
    ctl2.shutdown()
