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
