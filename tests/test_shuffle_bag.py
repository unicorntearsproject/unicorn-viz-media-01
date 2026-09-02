"""Shuffle must cover the whole view, not stop at whichever row is last.

Regression for the 2026-09-02 report: 'on shuffle it will stop if it plays
the last song in the list rather than stopping after every song has been
played regardless of order'.  The old end-of-pass test was
``self._index == view[-1]`` -- a *row position*.  Shuffle permutes the
library once at scan time and playback rarely starts at row 0, so walking
forward from anywhere hit the final row after a handful of tracks and
declared the set over with most of the library unheard.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load(name: str):
    path = Path(__file__).resolve().parent.parent / f'{name}.py'
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ctl(view: list[int], *, shuffle: bool = True, index: int = 0):
    """A controller carrying only the state the bag logic reads."""
    mc = _load('media_controller')
    c = mc.MediaController.__new__(mc.MediaController)
    c._shuffle = shuffle
    c._index = index
    c._played = set()
    c._is_playing = False
    c.view_indices = lambda: list(view)      # type: ignore[method-assign]
    return c


VIEW = [0, 1, 2, 3, 4]


def test_standing_on_the_last_row_does_not_end_a_shuffle_pass() -> None:
    """The bug, stated directly: last *row* is not last *track*."""
    c = _ctl(VIEW, index=4)
    c._played = {4}                       # started here; nothing else heard
    assert c._pass_complete(VIEW) is False


def test_a_shuffle_pass_ends_only_when_every_track_has_played() -> None:
    c = _ctl(VIEW, index=2)
    c._played = {0, 1, 2, 3}
    assert c._pass_complete(VIEW) is False
    c._played = set(VIEW)
    assert c._pass_complete(VIEW) is True


def test_next_unplayed_wraps_so_a_mid_list_start_covers_everything() -> None:
    """Starting at row 3 must still reach rows 0-2 by wrapping."""
    c = _ctl(VIEW, index=3)
    c._played = {3}
    seen = [3]
    for _ in range(len(VIEW) - 1):
        nxt = c._next_unplayed(VIEW)
        assert nxt is not None, 'bag emptied early — tracks were skipped'
        c._index = nxt
        c._played.add(nxt)
        seen.append(nxt)
    assert sorted(seen) == VIEW           # every track, exactly once
    assert c._next_unplayed(VIEW) is None  # now genuinely exhausted


def test_sequential_play_still_ends_at_the_last_row() -> None:
    """Shuffle off keeps row-order semantics — no regression."""
    c = _ctl(VIEW, shuffle=False, index=4)
    assert c._pass_complete(VIEW) is True
    c._index = 2
    assert c._pass_complete(VIEW) is False


def test_an_empty_view_never_reports_a_completed_pass() -> None:
    c = _ctl([])
    assert c._pass_complete([]) is False
    assert c._next_unplayed([]) is None


def test_bag_reset_keeps_the_playing_track_so_it_is_not_replayed() -> None:
    c = _ctl(VIEW, index=2)
    c._played = {0, 1, 2}
    c._is_playing = True
    c.bag_reset()
    assert c._played == {2}
    c._is_playing = False
    c.bag_reset()
    assert c._played == set()
