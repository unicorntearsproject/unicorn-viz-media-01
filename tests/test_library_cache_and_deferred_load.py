"""Per-root tag cache and the non-blocking startup library load (0.29.1).

Two defects behind "the media player's library load is killing us":

* one shared cache file for every scan, so the test suite's temp-library
  scans (run by every commit hook) wiped the real 10,843-entry cache and
  the next launch re-read every tag: nine of the last ten sessions paid
  7-14 s for it, on the main thread;
* the constructor blocked on the splash-time prescan (wait(300)), so that
  re-read froze boot instead of overlapping it.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f'media_cache_{name}', _ROOT / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mc = _load('media_controller')
MediaController = _mc.MediaController


def _lib(tmp_path: Path, name: str, n: int) -> Path:
    d = tmp_path / name
    d.mkdir()
    for i in range(n):
        (d / f'track{i}.mp3').write_bytes(b'\x00' * 16)
    return d


# -- cache isolation ---------------------------------------------------------

def test_cache_file_is_keyed_per_root(tmp_path, monkeypatch):
    monkeypatch.setenv('UNICORNVIZ_MEDIA_CACHE_DIR', str(tmp_path / 'cache'))
    a = _mc._cache_path(Path('/music/a'))
    b = _mc._cache_path(Path('/music/b'))
    assert a != b and a.parent == b.parent == tmp_path / 'cache'
    assert a.name.startswith('media_library_cache-') and a.name.endswith('.json')


def test_scanning_one_root_never_touches_another_roots_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('UNICORNVIZ_MEDIA_CACHE_DIR', str(tmp_path / 'cache'))
    real_root = Path('/home/someone/Music')
    real_cache = _mc._cache_path(real_root)
    real_cache.parent.mkdir(parents=True)
    real_cache.write_text(json.dumps({'version': _mc._CACHE_VERSION,
                                      'entries': {str(real_root / 'x.mp3'): {'size': 1, 'mtime': 1.0}}}))
    before = real_cache.read_bytes()
    _mc._scan_library(_lib(tmp_path, 'scratch', 3))          # a "test" scan
    assert real_cache.read_bytes() == before                  # untouched
    assert _mc._cache_path(_lib_root(tmp_path, 'scratch')).exists()


def _lib_root(tmp_path: Path, name: str) -> Path:
    return (tmp_path / name).resolve()


def test_legacy_single_file_cache_is_adopted_only_for_matching_root(tmp_path, monkeypatch):
    monkeypatch.setenv('UNICORNVIZ_MEDIA_CACHE_DIR', str(tmp_path / 'cache'))
    lib = _lib(tmp_path, 'lib', 2)
    root = lib.resolve()
    legacy = _mc._cache_path(None)
    legacy.parent.mkdir(parents=True)
    mine = str(root / 'track0.mp3')
    # Same track layout under a *foreign* root: must be filtered out.
    junk = str((tmp_path / 'other-machine' / 'lib' / 'track0.mp3').resolve())
    legacy.write_text(json.dumps({'version': _mc._CACHE_VERSION, 'entries': {
        mine: {'size': 16, 'mtime': 0.0, 'title': 'from-legacy'},
        junk: {'size': 16, 'mtime': 0.0, 'title': 'junk'},
    }}))
    entries = _mc._load_tag_cache(root)
    assert set(entries) == {mine}                             # junk filtered out
    assert _mc._load_tag_cache(Path('/elsewhere')) == {}      # nothing matches -> miss


def test_warm_scan_reads_no_tags(tmp_path, monkeypatch):
    monkeypatch.setenv('UNICORNVIZ_MEDIA_CACHE_DIR', str(tmp_path / 'cache'))
    lib = _lib(tmp_path, 'lib', 5)
    reads = 0
    real = _mc._read_tags

    def counting(p):
        nonlocal reads
        reads += 1
        return real(p)
    monkeypatch.setattr(_mc, '_read_tags', counting)
    _mc._scan_library(lib)
    assert reads == 5
    _mc._scan_library(lib)
    assert reads == 5                                         # second pass: cache hits only


def test_warm_scan_reads_no_tags_when_the_library_has_subfolders(tmp_path, monkeypatch):
    """2026-09-13 real-world miss: ``_scan_library`` computed the media root
    into a local named ``root``, then reused that same name as the ``os.walk``
    loop variable a few lines later.  With a flat library the walk visits
    exactly one directory (the root itself) so the shadowing was invisible;
    the instant the library has even one subfolder, ``root`` is left pointing
    at the *last* subdirectory walked, the save path stops matching the load
    path, and every boot reports 0 cached / N read forever.  A flat-only
    fixture (see ``test_warm_scan_reads_no_tags`` above) cannot catch this --
    this one must have a subfolder to be a real regression guard.
    """
    monkeypatch.setenv('UNICORNVIZ_MEDIA_CACHE_DIR', str(tmp_path / 'cache'))
    lib = _lib(tmp_path, 'lib', 3)
    (lib / 'vocals').mkdir()
    (lib / 'vocals' / 'sub0.mp3').write_bytes(b'\x00' * 16)
    (lib / 'vocals' / 'sub1.mp3').write_bytes(b'\x00' * 16)
    reads = 0
    real = _mc._read_tags

    def counting(p):
        nonlocal reads
        reads += 1
        return real(p)
    monkeypatch.setattr(_mc, '_read_tags', counting)
    _mc._scan_library(lib)
    assert reads == 5
    _mc._scan_library(lib)
    assert reads == 5                                         # second pass: cache hits only
    assert _mc._cache_path(_lib_root(tmp_path, 'lib')).exists()


# -- deferred startup install -------------------------------------------------

class _App:
    vj_api = None


def _pending(key: str) -> threading.Event:
    ev = threading.Event()
    with _mc._LIBRARY_SCAN_LOCK:
        _mc._LIBRARY_SCAN_CACHE.pop(key, None)
        _mc._LIBRARY_SCAN_PENDING[key] = ev
    return ev


def test_constructor_does_not_wait_for_a_running_prescan(tmp_path):
    lib = _lib(tmp_path, 'lib', 3)
    key = str(lib.resolve())
    ev = _pending(key)                                         # "prescan still walking"
    t0 = time.perf_counter()
    ctl = MediaController(_App(), {'enabled': True, 'media_dir': str(lib)})
    assert (time.perf_counter() - t0) < 1.0
    assert ctl.library_loading is True and ctl.snapshot()['library_loading'] is True
    assert len(ctl._library) == 0 and ctl.snapshot()['available'] is False
    # The prescan lands...
    with _mc._LIBRARY_SCAN_LOCK:
        _mc._LIBRARY_SCAN_CACHE[key] = [{'path': p} for p in sorted(lib.glob('*.mp3'))]
    ev.set()
    # ...and update() installs it on the next frame.
    ctl.update(0.016, None)
    assert ctl.library_loading is False
    assert len(ctl._library) == 3 and ctl.snapshot()['library_size'] == 3
    assert all('bucket' in t for t in ctl._library)


def test_constructor_installs_a_finished_prescan_immediately(tmp_path):
    lib = _lib(tmp_path, 'lib', 2)
    key = str(lib.resolve())
    with _mc._LIBRARY_SCAN_LOCK:
        _mc._LIBRARY_SCAN_PENDING.pop(key, None)
        _mc._LIBRARY_SCAN_CACHE[key] = [{'path': p} for p in sorted(lib.glob('*.mp3'))]
    ctl = MediaController(_App(), {'enabled': True, 'media_dir': str(lib)})
    assert ctl.library_loading is False and len(ctl._library) == 2


def test_constructor_walks_synchronously_when_no_prescan_ran(tmp_path):
    lib = _lib(tmp_path, 'lib', 4)
    key = str(lib.resolve())
    with _mc._LIBRARY_SCAN_LOCK:
        _mc._LIBRARY_SCAN_PENDING.pop(key, None)
        _mc._LIBRARY_SCAN_CACHE.pop(key, None)
    ctl = MediaController(_App(), {'enabled': True, 'media_dir': str(lib)})
    assert ctl.library_loading is False and len(ctl._library) == 4


def test_a_prescan_that_died_is_retried_off_thread_then_gives_up(tmp_path):
    lib = _lib(tmp_path, 'lib', 2)
    key = str(lib.resolve())
    ev = _pending(key)
    ctl = MediaController(_App(), {'enabled': True, 'media_dir': str(lib)})
    ev.set()                                                   # signalled with no result
    ctl.update(0.016, None)
    # A fresh prescan was kicked; wait for it and let update() install it.
    end = time.monotonic() + 5.0
    while ctl.library_loading and time.monotonic() < end:
        ctl.update(0.016, None)
        time.sleep(0.01)
    assert ctl.library_loading is False
    assert len(ctl._library) == 2                              # the retry found them


def test_boot_auto_play_waits_for_the_library(tmp_path):
    lib = _lib(tmp_path, 'lib', 2)
    key = str(lib.resolve())
    ev = _pending(key)
    ctl = MediaController(_App(), {'enabled': True, 'media_dir': str(lib), 'auto_play': True})
    started: list[int] = []
    ctl._start_track = lambda i: started.append(i)             # no real playback
    msg = ctl.play_pause()                                     # what core does at boot
    assert 'loading' in msg.lower()
    assert started == []
    with _mc._LIBRARY_SCAN_LOCK:
        _mc._LIBRARY_SCAN_CACHE[key] = [{'path': p} for p in sorted(lib.glob('*.mp3'))]
    ev.set()
    ctl.update(0.016, None)
    assert started == [0]                                      # played once the library landed
