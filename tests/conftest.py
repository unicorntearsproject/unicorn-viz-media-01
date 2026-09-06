"""media-01 test fixtures.

Every test scan writes its tag cache into a per-test temp directory.  Until
2026-09-05 the scans of five-file temp libraries in this suite rewrote the
owner's real ``runtime/media_library_cache.json`` -- on every commit hook --
and the next launch re-read all 10,843 tags on the main thread (7-14 s).
The cache is now keyed per media root as well, so this is the second lock
on that door, not the only one.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_media_cache(tmp_path_factory, monkeypatch):
    monkeypatch.setenv('UNICORNVIZ_MEDIA_CACHE_DIR',
                       str(tmp_path_factory.mktemp('media-cache')))
    root = tmp_path_factory.mktemp('app-root')
    (root / 'runtime').mkdir()
    monkeypatch.setenv('UNICORNVIZ_APP_ROOT', str(root))   # playlists store etc.
    yield


_REPO = Path(__file__).resolve().parents[3]
_GUARDED = ('runtime', 'logs')


def _snapshot() -> dict[str, set[str]]:
    return {n: ({p.name for p in (_REPO / n).iterdir()} if (_REPO / n).is_dir() else set())
            for n in _GUARDED}


@pytest.fixture(scope='session', autouse=True)
def _owner_state_guard():
    """Fail the session if any test left a new file in the real runtime/ or logs/."""
    before = _snapshot()
    yield
    after = _snapshot()
    leaked = [f'{n}/{fn}' for n in _GUARDED for fn in sorted(after[n] - before[n])]
    if leaked and not os.environ.get('UNICORNVIZ_ALLOW_STATE_WRITES'):
        pytest.fail('Tests wrote into the owner\'s state directories:\n  '
                    + '\n  '.join(leaked), pytrace=False)
