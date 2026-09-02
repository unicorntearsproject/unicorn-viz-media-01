"""The folder pane must never crowd out the track list.

Regression for the 2026-09-02 report: 'i've added a ton of tracks & folders
... and now our library browser can only show two songs at a time'.  The
accordion it replaced grew a row per crate along the open path, so a deep
tree consumed the pane and left a couple of track rows behind it.  The
split is now a fixed fraction, independent of how many crates exist.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _split(top: int, bot: int, row_h: int = 18) -> tuple[int, int]:
    path = Path(__file__).resolve().parent.parent / 'ui.py'
    spec = importlib.util.spec_from_file_location('media_deck_ui', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MediaDeckWindow._folder_split(top, bot, row_h)


def test_folders_take_about_a_third_leaving_two_thirds_for_tracks() -> None:
    top, bot = 100, 700                      # 600px of pane
    folder_bot, _rows = _split(top, bot)
    folders = folder_bot - top
    tracks = bot - folder_bot
    assert 180 <= folders <= 220, folders    # ~1/3
    assert tracks >= 2 * folders * 0.9       # ~2/3 left for the list


def test_the_split_does_not_move_with_the_number_of_crates() -> None:
    """The accordion's actual defect: geometry that grew with the tree."""
    assert _split(100, 700) == _split(100, 700)


def test_a_short_pane_still_leaves_the_folder_pane_visible() -> None:
    folder_bot, rows = _split(0, 40)
    assert folder_bot <= 40                  # never overruns the pane
    assert rows >= 1                         # never collapses to nothing


def test_a_tall_pane_gives_the_track_list_the_majority() -> None:
    top, bot = 0, 1200
    folder_bot, rows = _split(top, bot)
    assert bot - folder_bot > folder_bot - top
    assert rows == (folder_bot - top) // 18
