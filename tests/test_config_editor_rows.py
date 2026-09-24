"""Config-editor rows (0.30.0): window refresh (live) + tag workers (restart)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import media_controller as mc  # noqa: E402


def _ctrl():
    c = object.__new__(mc.MediaController)
    c._cfg = {'render_interval': 0.05}
    c._tag_workers = 8
    return c


def test_rows_and_setters() -> None:
    c = _ctrl()
    rows = {r['name']: r for r in c.config_editor_settings()}
    assert rows['render_fps']['value'] == 2          # 0.05 s -> 20 fps
    assert rows['tag_workers']['value'] == 2 and rows['tag_workers']['restart'] == 'media'
    window = SimpleNamespace(_interval=0.05)
    for attr in ('_deck_window', '_ui', '_window'):
        setattr(c, attr, window)
    assert c.set_config_setting('render_fps', 4.0) is None
    assert abs(c._cfg['render_interval'] - 0.02) < 1e-9
    assert abs(window._interval - 0.02) < 1e-9
    assert c.set_config_setting('tag_workers', 0.0) == {'tag_workers': 2}
    assert c.config_editor_settings()[1]['value'] == 0
    assert c._tag_workers == 8                       # live scans keep the boot value


def test_scan_library_takes_a_worker_count(tmp_path: Path) -> None:
    assert mc._scan_library(tmp_path, workers=1) == []


def test_media_folder_row_restarts_and_names_its_config_line() -> None:
    c = _ctrl()
    c._cfg['media_dir'] = '~/Videos/vj'
    rows = {r['name']: r for r in c.config_editor_settings()}
    assert rows['media_dir']['kind'] == 'text' and rows['media_dir']['value'] == '~/Videos/vj'
    assert rows['media_dir']['restart'] == 'media' and rows['media_dir']['config'] == 'media.media_dir'
    assert rows['tag_workers']['config'] == 'media.tag_workers'
    assert c.set_config_setting('media_dir', ' /mnt/media ') == {'media_dir': '/mnt/media'}
