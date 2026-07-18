"""Background library-prescan tests (media-01).

The library walk + tag-read is the controller's dominant startup cost; the
app kicks off ``MediaController.prescan_async`` before the splash so it
overlaps the splash instead of stalling boot after it, and
``rescan_library`` consumes the cached result. Device-free: no SDL/GL/VLC,
just a temp dir of fake audio files.
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f'media_prescan_{name}', _ROOT / f'{name}.py')
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mc = _load('media_controller')
MediaController = _mc.MediaController


def _make_library(tmp_path: Path, audio: int = 3, other: int = 2) -> Path:
    d = tmp_path / 'lib'
    d.mkdir()
    for i in range(audio):
        (d / f'track{i}.mp3').write_bytes(b'\x00' * 16)
    for i in range(other):
        (d / f'note{i}.txt').write_bytes(b'x')
    return d


def _key(d: Path) -> str:
    return str(d.resolve()) if d.exists() else str(d)


def _wait_for_cache(key: str, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _mc._LIBRARY_SCAN_LOCK:
            if key in _mc._LIBRARY_SCAN_CACHE:
                return
        time.sleep(0.01)
    raise AssertionError('prescan did not populate the cache in time')


def _clear_cache() -> None:
    with _mc._LIBRARY_SCAN_LOCK:
        _mc._LIBRARY_SCAN_CACHE.clear()


def test_prescan_populates_cache_off_thread(tmp_path) -> None:
    _clear_cache()
    d = _make_library(tmp_path, audio=3, other=2)
    cfg = {'enabled': True, 'media_dir': str(d)}
    MediaController.prescan_async(cfg)
    _wait_for_cache(_key(d))
    with _mc._LIBRARY_SCAN_LOCK:
        cached = _mc._LIBRARY_SCAN_CACHE[_key(d)]
    assert len(cached) == 3  # only the audio files


def test_rescan_library_consumes_the_prescan_cache(tmp_path) -> None:
    _clear_cache()
    d = _make_library(tmp_path, audio=2)
    cfg = {'enabled': True, 'media_dir': str(d)}
    MediaController.prescan_async(cfg)
    _wait_for_cache(_key(d))

    inst = object.__new__(MediaController)
    inst._cfg = cfg
    inst._media_dir = MediaController._resolve_media_dir(cfg)
    inst._library = []
    inst.rescan_library()

    assert len(inst._library) == 2
    assert all('bucket' in t for t in inst._library)  # bucket tagging still runs
    # Cache is consumed (popped) so a later runtime rescan re-walks fresh.
    with _mc._LIBRARY_SCAN_LOCK:
        assert _key(d) not in _mc._LIBRARY_SCAN_CACHE


def test_rescan_falls_back_to_synchronous_walk_without_prescan(tmp_path) -> None:
    _clear_cache()
    d = _make_library(tmp_path, audio=4)
    cfg = {'enabled': True, 'media_dir': str(d)}
    inst = object.__new__(MediaController)
    inst._cfg = cfg
    inst._media_dir = MediaController._resolve_media_dir(cfg)
    inst._library = []
    inst.rescan_library()  # no prescan ran
    assert len(inst._library) == 4


def test_prescan_is_a_noop_when_disabled(tmp_path) -> None:
    _clear_cache()
    d = _make_library(tmp_path, audio=3)
    MediaController.prescan_async({'enabled': False, 'media_dir': str(d)})
    time.sleep(0.1)
    with _mc._LIBRARY_SCAN_LOCK:
        assert not _mc._LIBRARY_SCAN_CACHE


def test_resolve_media_dir_matches_config_and_default() -> None:
    assert MediaController._resolve_media_dir({'media_dir': '~/Music/set'}) == Path('~/Music/set').expanduser()
    default = MediaController._resolve_media_dir({})
    assert default.name == 'media'
