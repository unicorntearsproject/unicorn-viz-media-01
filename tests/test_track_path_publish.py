"""Tests for MediaController._publish_track_path() (2026-08-14, round three).

Device-free: exercises the method directly against a bare instance with
just the attributes it reads, same style as auto-vj-01's
test_get_mixer_track_path tests -- media-01 plays from the same kind of
local files dj-mixer-01 does, so it publishes on the same vj_api
track-path bus for training-kit-01's offline Essentia analysis.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location('media_controller', _ROOT / 'media_controller.py')
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)
MediaController = _MOD.MediaController


def _bare(*, is_playing: bool, index: int, library: list[dict], vj_api) -> MediaController:
    inst = object.__new__(MediaController)
    inst._is_playing = is_playing
    inst._index = index
    inst._library = library
    inst._app = SimpleNamespace(vj_api=vj_api)
    return inst


class _FakeVJ:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    def publish_track_path(self, source: str, path: str) -> None:
        self.published.append((source, path))


def test_publishes_the_playing_track_path() -> None:
    vj = _FakeVJ()
    inst = _bare(is_playing=True, index=0, library=[{'path': '/media/track.mp3'}], vj_api=vj)
    inst._publish_track_path()
    assert vj.published == [('media', '/media/track.mp3')]


def test_does_not_publish_when_not_playing() -> None:
    vj = _FakeVJ()
    inst = _bare(is_playing=False, index=0, library=[{'path': '/media/track.mp3'}], vj_api=vj)
    inst._publish_track_path()
    assert vj.published == []


def test_does_not_publish_when_index_out_of_range() -> None:
    vj = _FakeVJ()
    inst = _bare(is_playing=True, index=5, library=[{'path': '/media/track.mp3'}], vj_api=vj)
    inst._publish_track_path()
    assert vj.published == []


def test_does_not_raise_without_a_vj_api() -> None:
    inst = _bare(is_playing=True, index=0, library=[{'path': '/media/track.mp3'}], vj_api=None)
    inst._publish_track_path()   # must not raise


def test_swallows_a_publish_error() -> None:
    class _Raising:
        def publish_track_path(self, source, path):
            raise RuntimeError('boom')
    inst = _bare(is_playing=True, index=0, library=[{'path': '/media/track.mp3'}], vj_api=_Raising())
    inst._publish_track_path()   # must not raise
