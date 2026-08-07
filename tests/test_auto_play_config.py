"""Regression test for media-01's boot-time auto_play config key.

2026-08-07: [media] auto_play feeds unicornviz's App._maybe_auto_play_media()
-- built for unattended/headless use (training-daemon --media-source). This
just confirms the config plumbing: MediaController.auto_play reflects the
'auto_play' cfg key, default False.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f'media_auto_play_{name}', _ROOT / f'{name}.py')
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


def test_auto_play_defaults_to_false(tmp_path) -> None:
    d = _empty_library(tmp_path)
    app = SimpleNamespace(vj_api=None)
    ctl = MediaController(app, {'enabled': True, 'media_dir': str(d)})

    assert ctl.auto_play is False


def test_auto_play_reflects_config_true(tmp_path) -> None:
    d = _empty_library(tmp_path)
    app = SimpleNamespace(vj_api=None)
    ctl = MediaController(app, {'enabled': True, 'media_dir': str(d), 'auto_play': True})

    assert ctl.auto_play is True


def test_auto_play_attribute_exists_even_when_disabled(tmp_path) -> None:
    """auto_play is set before the `enabled` early-return, same guarantee
    as self.enabled itself -- App._maybe_auto_play_media() reads it via
    getattr() but should never need to fall back."""
    d = _empty_library(tmp_path)
    app = SimpleNamespace(vj_api=None)
    ctl = MediaController(app, {'enabled': False, 'media_dir': str(d), 'auto_play': True})

    assert ctl.auto_play is True
    assert ctl.enabled is False
