"""media-01's Control Room operator panel (P3 of
docs/planning/control-room-drop-in-integration-plan-2026-09-05.md).
No title/artist in the panel by design -- media-01 already feeds the core
now-playing hub and INFO shows the active source (plan §2 "now-playing
dedupe" note).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f'media_op_panel_{name}', _ROOT / f'{name}.py')
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mc = _load('media_controller')
MediaController = _mc.MediaController


class _FakeVJApi:
    def __init__(self) -> None:
        self.panels: dict[str, object] = {}
        self.now_playing: dict[str, object] = {}

    def register_midi_actions(self, section, actions) -> None:
        pass

    def register_midi_action_handler(self, action, fn) -> None:
        pass

    def register_now_playing(self, name, fn, priority=0) -> None:
        self.now_playing[name] = fn

    def unregister_now_playing(self, name) -> None:
        self.now_playing.pop(name, None)

    def register_operator_panel(self, panel) -> None:
        self.panels[panel.name] = panel

    def unregister_operator_panel(self, name: str) -> bool:
        return self.panels.pop(name, None) is not None


def _empty_library(tmp_path: Path) -> Path:
    d = tmp_path / 'lib'
    d.mkdir()
    return d


def _make(tmp_path, api: '_FakeVJApi', **cfg) -> 'MediaController':
    d = _empty_library(tmp_path)
    app = SimpleNamespace(vj_api=api)
    cfg.setdefault('enabled', True)
    cfg.setdefault('media_dir', str(d))
    # Isolate from the real repo's runtime/media_playlists.json: _restore_prefs()
    # would otherwise overwrite every cfg flag below with whatever the owner's
    # actual session last saved there.
    cfg.setdefault('playlists_path', str(tmp_path / 'media_playlists.json'))
    return MediaController(app, cfg)


def test_enabled_controller_registers_a_medium_panel_on_sources(tmp_path) -> None:
    api = _FakeVJApi()
    ctl = _make(tmp_path, api)
    panel = api.panels['media']
    assert panel.title == 'Media Player' and panel.page == 'sources' and panel.size == 'medium'
    assert panel.content == ctl._operator_panel_content
    assert panel.on_action == ctl._operator_panel_action
    assert set(panel.tooltips) == {
        'play_pause', 'prev', 'next', 'shuffle', 'repeat', 'crossfade', 'deck',
    }
    ctl.shutdown()


def test_disabled_controller_never_registers_a_panel(tmp_path) -> None:
    api = _FakeVJApi()
    _make(tmp_path, api, enabled=False)
    assert 'media' not in api.panels


def test_panel_content_reflects_mode_flags_no_title_or_artist(tmp_path) -> None:
    api = _FakeVJApi()
    ctl = _make(tmp_path, api, shuffle=False, crossfade=True, auto_level=False, repeat='all')
    content = ctl._operator_panel_content()
    rows = {r.label: r.value for r in content.rows}
    assert rows == {
        'Shuffle': 'OFF', 'Repeat': 'ALL', 'Crossfade': 'ON', 'Auto-level': 'OFF',
    }
    assert not any('title' in r.label.lower() or 'artist' in r.label.lower() for r in content.rows)
    ctl.shutdown()


def test_shuffle_repeat_crossfade_actions_toggle_the_same_state(tmp_path) -> None:
    api = _FakeVJApi()
    ctl = _make(tmp_path, api, shuffle=False)
    assert ctl._shuffle is False
    msg = ctl._operator_panel_action('shuffle', None)
    assert ctl._shuffle is True
    assert 'ON' in msg.upper()

    before_repeat = ctl.repeat
    ctl._operator_panel_action('repeat', None)
    assert ctl.repeat != before_repeat

    assert ctl.crossfade_enabled is True
    ctl._operator_panel_action('crossfade', None)
    assert ctl.crossfade_enabled is False
    ctl.shutdown()


def test_deck_action_toggles_the_window_flag_reported_by_the_panel(tmp_path) -> None:
    api = _FakeVJApi()
    ctl = _make(tmp_path, api)
    ctl.open_window = lambda: setattr(ctl, '_window', SimpleNamespace(is_open=True))
    ctl.close_window = lambda: setattr(ctl, '_window', None)
    assert ctl.is_window_open is False
    msg = ctl._operator_panel_action('deck', None)
    assert ctl.is_window_open is True
    assert msg == 'Media Deck open'
    content = ctl._operator_panel_content()
    assert next(b.active for b in content.buttons if b.action == 'deck') is True
    ctl.shutdown()


def test_unknown_action_returns_none(tmp_path) -> None:
    api = _FakeVJApi()
    ctl = _make(tmp_path, api)
    assert ctl._operator_panel_action('nope', None) is None
    ctl.shutdown()


def test_shutdown_unregisters_the_panel(tmp_path) -> None:
    api = _FakeVJApi()
    ctl = _make(tmp_path, api)
    assert 'media' in api.panels
    ctl.shutdown()
    assert 'media' not in api.panels
