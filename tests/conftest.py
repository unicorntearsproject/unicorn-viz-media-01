"""media-01 test fixtures.

Every test scan writes its tag cache into a per-test temp directory.  Until
2026-09-05 the scans of five-file temp libraries in this suite rewrote the
owner's real ``runtime/media_library_cache.json`` -- on every commit hook --
and the next launch re-read all 10,843 tags on the main thread (7-14 s).
The cache is now keyed per media root as well, so this is the second lock
on that door, not the only one.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_media_cache(tmp_path_factory, monkeypatch):
    monkeypatch.setenv('UNICORNVIZ_MEDIA_CACHE_DIR',
                       str(tmp_path_factory.mktemp('media-cache')))
    yield
