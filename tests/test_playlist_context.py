"""Regression tests for media-01's snapshot() playlist_context field.

2026-08-14, round three: was hardcoded None -- self.active_playlist is
real, already-tracked state, just never surfaced here. Same shape
spotify-01 populates, so auto-vj-01's existing playlist-name inference
(previously Spotify-only) picks it up for training-kit-01's packaging
step. Owner: "our training packager is supposed to be self-naming it's
playlist when the data is available (and it should be from the mixer &
mediaplayer) but i'm having to type it in every time."
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f'media_playlist_context_{name}', _ROOT / f'{name}.py')
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mc = _load('media_controller')
MediaController = _mc.MediaController


def _empty_library(tmp_path: Path) -> Path:
    d = tmp_path / 'lib'
    d.mkdir()
    return d


def test_playlist_context_none_without_an_active_playlist(tmp_path) -> None:
    d = _empty_library(tmp_path)
    app = SimpleNamespace(vj_api=None)
    ctl = MediaController(app, {'enabled': True, 'media_dir': str(d)})

    assert ctl.active_playlist is None
    assert ctl.snapshot()['playlist_context'] is None


def test_playlist_context_reflects_the_active_playlist(tmp_path) -> None:
    d = _empty_library(tmp_path)
    app = SimpleNamespace(vj_api=None)
    ctl = MediaController(app, {'enabled': True, 'media_dir': str(d)})
    ctl.active_playlist = 'house-training-01'

    pc = ctl.snapshot()['playlist_context']

    assert pc == {'id': 'house-training-01', 'name': 'house-training-01',
                  'uri': '', 'total_tracks': 0, 'owner': ''}
