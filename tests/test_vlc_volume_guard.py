"""libvlc volume calls must never touch a player without a live audio output.

Regression for the SIGSEGV on 2026-09-04: clicking a track in the media deck
crashed the whole process inside ``libvlc_audio_set_volume``, reached from
``crossfade_to``.  ``play()`` is asynchronous, so the incoming player had no
aout object yet, and the pulse/PipeWire module walked a null thread loop.

That is a native crash, not an exception -- the ``try/except`` wrapped around
the call could not have caught it, and did not.  ``_VlcBackend.__init__``
already states the rule ("never set volume on a player without a live audio
output"); these tests pin every path to it.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import media_controller  # noqa: E402


class _FakePlayer:
    """A libvlc media player that records unsafe volume calls instead of dying.

    ``time`` mimics ``get_time()``: <= 0 means playback has not started, which
    is exactly the window in which the real library segfaults.
    """

    def __init__(self) -> None:
        self.time = 0
        self.volumes: list[int] = []
        self.unsafe_calls = 0
        self.played = 0
        self.stopped = 0

    def get_time(self) -> int:
        return self.time

    def audio_set_volume(self, volume: int) -> None:
        if self.time <= 0:
            self.unsafe_calls += 1   # the real libvlc would SIGSEGV here
        self.volumes.append(int(volume))

    def set_media(self, media) -> None:  # noqa: ANN001, ARG002
        pass

    def play(self) -> None:
        self.played += 1

    def stop(self) -> None:
        self.stopped += 1
        self.time = 0                # output torn down with the playback clock


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> media_controller._VlcBackend:
    players = [_FakePlayer(), _FakePlayer()]

    class _Instance:
        def media_player_new(self):
            return players.pop(0)

        def media_new(self, path):  # noqa: ANN001, ARG002
            return object()

    fake_vlc = types.SimpleNamespace(Instance=lambda *a, **k: _Instance())
    monkeypatch.setitem(sys.modules, 'vlc', fake_vlc)
    return media_controller._VlcBackend(volume=100)


def _players(backend) -> list[_FakePlayer]:  # noqa: ANN001
    return backend._players


def test_crossfade_does_not_set_volume_before_playback_starts(backend) -> None:
    """The exact 2026-09-04 crash: play() then audio_set_volume() immediately."""
    assert backend.crossfade_to(Path('/music/next.mp3'), fade_s=3.0) is True
    incoming = _players(backend)[1]
    assert incoming.played == 1
    assert incoming.unsafe_calls == 0


def test_crossfade_ramp_skips_the_incoming_player_until_its_clock_runs(
    backend,
) -> None:
    """tick() ran the same unguarded call on both players, every frame."""
    outgoing, incoming = _players(backend)
    outgoing.time = 5_000                     # the old track is audible
    backend.crossfade_to(Path('/music/next.mp3'), fade_s=3.0)
    for _ in range(4):                        # incoming still buffering
        backend.tick(0.1)
    assert incoming.unsafe_calls == 0
    assert incoming.volumes == []
    assert outgoing.volumes, 'the audible outgoing player must still ramp down'


def test_crossfade_ramp_resumes_once_the_incoming_output_is_live(
    backend,
) -> None:
    """Skipping is a deferral, not a drop -- the ramp joins in progress."""
    outgoing, incoming = _players(backend)
    outgoing.time = 5_000
    backend.crossfade_to(Path('/music/next.mp3'), fade_s=3.0)
    backend.tick(0.1)
    assert incoming.volumes == []
    incoming.time = 120                       # first decoded frame lands
    backend.tick(0.1)
    assert incoming.volumes, 'ramp must pick the incoming player up'
    assert incoming.unsafe_calls == 0


def test_end_fade_does_not_set_volume_on_the_player_it_just_stopped(
    backend,
) -> None:
    """The mirror-image race: stop() tears the output down first."""
    outgoing, incoming = _players(backend)
    outgoing.time = 5_000
    incoming.time = 120
    backend.crossfade_to(Path('/music/next.mp3'), fade_s=0.2)
    backend.tick(1.0)                         # runs past the end of the fade
    assert outgoing.stopped == 1
    assert outgoing.unsafe_calls == 0


def test_play_still_defers_its_volume(backend) -> None:
    """The pre-existing deferral must survive the refactor."""
    backend.play(Path('/music/first.mp3'))
    active = _players(backend)[0]
    assert active.unsafe_calls == 0
    assert backend._pending_volume is True
    active.time = 200
    backend.tick(0.016)
    assert active.volumes == [100]
    assert backend._pending_volume is False
