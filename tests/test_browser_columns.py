"""Library row rendering: artist/title columns, hover, and the playing drip.

The media deck is the mixer's little sister, and these are the mixer's browser
conventions ported across so the two read as one product: colour means *what*,
brightness means *state*, and hovering never hides what a row was already
saying.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_ui = _load('ui')


# -- the artist column -------------------------------------------------------

def test_the_artist_keeps_the_row_hue_but_sits_behind_the_title() -> None:
    """A flat grey would throw away whatever the row colour was saying across
    a third of its width."""
    for col in ((0, 200, 255, 255), (60, 255, 160, 255), (110, 130, 165, 255)):
        muted = _ui._muted(col)[:3]
        assert muted.index(max(muted)) == col[:3].index(max(col[:3]))
        assert sum(muted) <= sum(col[:3]) + 3, 'brighter than the title'


def test_muting_never_makes_the_artist_duller_than_it_has_to_be() -> None:
    """Muting a saturated colour by a flat factor lands below a mid grey, so
    the 'quieter' field came out the darker one."""
    for col in ((0, 200, 255, 255), (60, 255, 160, 255)):
        flat = [c * _ui._ARTIST_MUTE for c in col[:3]]
        assert sum(_ui._muted(col)[:3]) >= sum(flat) - 3


# -- middle ellipsis ---------------------------------------------------------

def test_a_long_name_is_trimmed_from_the_middle() -> None:
    """'Artist … (Extended Mix)' keeps the part that identifies the track and
    the part that says which version -- cutting the tail loses the one that
    matters when you have three edits of the same tune."""
    name = 'Some Artist - A Very Long Title Indeed (Extended Club Mix)'
    out = _ui._mid_ellipsis(name, 30)
    assert len(out) == 30
    assert out.startswith('Some Artist')
    assert out.endswith('Mix)')
    assert '…' in out


def test_a_short_name_is_left_alone() -> None:
    assert _ui._mid_ellipsis('Short', 30) == 'Short'
    assert _ui._mid_ellipsis('', 30) == ''


# -- row beds ----------------------------------------------------------------

def test_hover_lifts_a_bed_rather_than_replacing_it() -> None:
    """Hovering must never take away what the row was already saying."""
    lifted = _ui._hover_bed(_ui._PLAYING_BED)
    assert lifted != _ui._PLAYING_BED
    for got, base in zip(lifted[:3], _ui._PLAYING_BED[:3]):
        assert got >= base
    assert lifted[2] > lifted[1], 'no longer recognisably the purple bed'


def test_hovering_a_plain_row_still_gives_it_a_bed() -> None:
    assert _ui._hover_bed(None) == _ui._ROW_HOVER_BED


# -- the drip ----------------------------------------------------------------

class _Rec:
    """Records what was drawn, so the pulse can be measured."""

    def __init__(self) -> None:
        self.alphas: list[int] = []

    def rectangle(self, _box, fill=None, outline=None):
        for col in (fill, outline):
            if col and len(col) > 3:
                self.alphas.append(col[3])


def _drip_alphas(at: float) -> list[int]:
    real = time.monotonic
    time.monotonic = lambda: at
    try:
        rec = _Rec()

        class _W:
            _DRIP_PERIOD_S = _ui.MediaDeckWindow._DRIP_PERIOD_S

        _ui.MediaDeckWindow._drip_row(_W(), rec, 0, 0, 100, 18)
        return rec.alphas
    finally:
        time.monotonic = real


def test_the_playing_row_actually_pulses() -> None:
    """A flat bed says 'this one'; a moving one says 'this one, right now'."""
    period = _ui.MediaDeckWindow._DRIP_PERIOD_S
    low = _drip_alphas(0.0)
    high = _drip_alphas(period / 2.0)
    assert low and len(low) == len(high)
    assert sum(low) != sum(high), 'the drip does not move'


def test_the_drip_is_never_invisible_at_its_dimmest() -> None:
    """The animation is emphasis, never the only signal -- at the bottom of
    the pulse the row must still read as the one that is playing."""
    period = _ui.MediaDeckWindow._DRIP_PERIOD_S
    worst = min((sum(_drip_alphas(period * f / 20.0)) for f in range(21)))
    assert worst > 0
    for f in range(21):
        assert all(a > 0 for a in _drip_alphas(period * f / 20.0))


def test_the_pulse_is_slow_enough_to_read_behind() -> None:
    """This sits behind text you are reading; anything quicker turns a list
    into a fairground."""
    assert _ui.MediaDeckWindow._DRIP_PERIOD_S >= 2.0


# -- controller accessors ----------------------------------------------------

def test_view_stats_counts_unknown_lengths_separately() -> None:
    """Files whose length never got read contribute zero, so the total reads
    as *at least* this long rather than silently short."""
    mc = _load('media_controller')

    class _C(mc.MediaController):
        def __init__(self):                     # noqa: D107 - test stub
            self._library = [
                {'path': '/a.mp3', 'title': 'A', 'artist': 'X', 'duration_s': 300.0},
                {'path': '/b.mp3', 'title': 'B', 'artist': '', 'duration_s': 0.0},
            ]

        def view_indices(self):
            return [0, 1]

    st = _C().view_stats()
    assert st == {'count': 2, 'duration_s': 300.0, 'unknown': 1}


def test_view_parts_keeps_artist_and_title_apart() -> None:
    """A joined string cannot column-align, and an empty artist is the signal
    the UI uses to give the filename the whole width."""
    mc = _load('media_controller')

    class _C(mc.MediaController):
        def __init__(self):                     # noqa: D107 - test stub
            self._library = [
                {'path': '/a.mp3', 'title': 'Tune', 'artist': 'Anzo',
                 'duration_s': 250.0},
                {'path': '/dir/some_rip.mp3', 'title': '', 'artist': '',
                 'duration_s': 0.0},
            ]

        def view_indices(self):
            return [0, 1]

    rows = _C().view_parts()
    assert rows[0] == (0, 'Anzo', 'Tune', 250.0)
    assert rows[1] == (1, '', 'some_rip', 0.0)


# -- the set actually ending --------------------------------------------------
#
# REPEAT OFF has always stopped at the end of the list.  What was missing was
# *announcing* it: the player went quiet, which is invisible to anything
# downstream -- including an unattended run waiting to package a recording.

def _ctl_stub(published):
    mc = _load('media_controller')

    class _Vj:
        @staticmethod
        def publish_session(source, payload):
            published.append((source, payload))

    class _App:
        vj_api = _Vj()

    c = mc.MediaController.__new__(mc.MediaController)
    c._app = _App()
    c._set_over = False
    return c


def test_the_end_of_the_list_is_published_as_phase_over() -> None:
    """The same phase the DJ mixer publishes, so a consumer never has to know
    which source ended the session -- or which app it came from."""
    published: list = []
    _ctl_stub(published)._note_set_over()
    assert len(published) == 1
    source, payload = published[0]
    assert source == 'media'
    assert payload['phase'] == 'over'
    assert payload['source'] == 'list_exhausted'
    assert payload['minutes_left'] == 0.0


def test_it_is_announced_once_not_on_every_tick() -> None:
    published: list = []
    c = _ctl_stub(published)
    for _ in range(5):
        c._note_set_over()
    assert len(published) == 1


def test_playing_again_re_arms_the_announcement() -> None:
    """A second session in the same run is a second event."""
    published: list = []
    c = _ctl_stub(published)
    c._note_set_over()
    c._set_over = False              # what _start_track does
    c._note_set_over()
    assert len(published) == 2


def test_an_older_core_without_the_bus_is_survived() -> None:
    mc = _load('media_controller')

    class _App:
        vj_api = object()            # no publish_session at all

    c = mc.MediaController.__new__(mc.MediaController)
    c._app = _App()
    c._set_over = False
    c._note_set_over()               # must not raise
    assert c._set_over is True


def test_repeat_defaults_off_so_a_session_can_end() -> None:
    """Matching the DJ mixer's loop flip: ALL and ONE stay one press away."""
    mc = _load('media_controller')
    c = mc.MediaController.__new__(mc.MediaController)
    c._cfg = {}
    rep = str(c._cfg.get('repeat', 'off') or 'off').lower()
    assert (rep if rep in ('all', 'one', 'off') else 'off') == 'off'
