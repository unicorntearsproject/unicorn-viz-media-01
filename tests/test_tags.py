"""Tag read/write tests for media-01 (a copy of the dj-mixer-01 module).

The load-bearing one is that a write touches metadata only and never the audio
stream -- verified by comparing the decoded audio before and after.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / f'{name}.py')
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_tags = _load('tags')


def _decoded(path: Path) -> np.ndarray:
    """The decoded audio, which is what the track store fingerprints.

    Compared array-for-array rather than hashed: stricter than a digest, and it
    keeps the test free of a hash function it does not need.
    """
    import soundfile as sf  # noqa: PLC0415
    data, _sr = sf.read(str(path), dtype='float32', always_2d=True)
    return np.ascontiguousarray(data)


def _make(path: Path) -> None:
    import soundfile as sf  # noqa: PLC0415
    rng = np.random.default_rng(11)
    sf.write(str(path), rng.normal(0, 0.1, (48_000, 2)).astype('float32'), 48_000)


# Containers with portable tag support.  WAV/AIFF are deliberately excluded --
# see test_untaggable_formats_are_refused_not_faked.
_FORMATS = ['flac', 'ogg', 'mp3']


@pytest.mark.parametrize('ext', _FORMATS)
def test_write_never_changes_the_audio(tmp_path, ext) -> None:
    """THE safety property: metadata only, audio bit-identical."""
    pytest.importorskip('soundfile')
    if not _tags.available():
        pytest.skip('mutagen not installed')
    p = tmp_path / f'track.{ext}'
    try:
        _make(p)
    except Exception:
        pytest.skip(f'cannot author {ext} here')
    before = _decoded(p)
    ok, msg = _tags.write_tags(p, {'artist': 'Unicorn', 'title': 'Tears',
                                   'genre': 'Techno', 'date': '2026'})
    assert ok is True, f'{ext}: {msg}'
    assert np.array_equal(_decoded(p), before), \
        f'{ext}: tag write altered the audio'


@pytest.mark.parametrize('ext', _FORMATS)
def test_round_trip(tmp_path, ext) -> None:
    pytest.importorskip('soundfile')
    if not _tags.available():
        pytest.skip('mutagen not installed')
    p = tmp_path / f'track.{ext}'
    try:
        _make(p)
    except Exception:
        pytest.skip(f'cannot author {ext} here')
    want = {'artist': 'DJ Unicorn Tears', 'title': 'Horn of Plenty',
            'album': 'Alpha', 'genre': 'Hard Techno', 'date': '2026'}
    ok, msg = _tags.write_tags(p, want)
    assert ok is True, msg
    got = _tags.read_tags(p)
    for field, value in want.items():
        assert got[field] == value, (ext, field, got)


def test_empty_value_clears_a_tag(tmp_path) -> None:
    pytest.importorskip('soundfile')
    if not _tags.available():
        pytest.skip('mutagen not installed')
    p = tmp_path / 'c.flac'
    _make(p)
    _tags.write_tags(p, {'artist': 'Someone', 'title': 'Thing'})
    assert _tags.read_tags(p)['artist'] == 'Someone'
    _tags.write_tags(p, {'artist': ''})
    assert _tags.read_tags(p)['artist'] == ''
    assert _tags.read_tags(p)['title'] == 'Thing'      # untouched


def test_unreadable_target_refuses_with_a_reason(tmp_path) -> None:
    ok, msg = _tags.write_tags(tmp_path / 'nope.mp3', {'artist': 'X'})
    assert ok is False and msg                          # never a silent no-op


@pytest.mark.parametrize('ext', ['wav', 'aiff'])
def test_untaggable_formats_are_refused_not_faked(tmp_path, ext) -> None:
    """ID3-in-WAV/AIFF is a de-facto extension, not a standard, so tags written
    there may never be read back.  Refuse with a reason rather than write
    metadata that does not travel."""
    pytest.importorskip('soundfile')
    p = tmp_path / f'x.{ext}'
    _make(p)
    assert _tags.can_edit(p) is False
    ok, msg = _tags.write_tags(p, {'artist': 'X'})
    assert ok is False
    assert 'portable' in msg.lower(), msg
    assert _tags.read_tags(p) == {f: '' for f in _tags.TAG_FIELDS}


def test_can_edit_accepts_normal_music_formats(tmp_path) -> None:
    for ext in ('mp3', 'flac', 'ogg', 'm4a'):
        assert _tags.can_edit(tmp_path / f'a.{ext}') is True


def test_read_of_an_untagged_file_is_all_blank(tmp_path) -> None:
    pytest.importorskip('soundfile')
    p = tmp_path / 'blank.flac'
    _make(p)
    got = _tags.read_tags(p)
    assert set(got) == set(_tags.TAG_FIELDS)
    assert all(v == '' for v in got.values())


def test_display_name_prefers_artist_title() -> None:
    assert _tags.display_name({'artist': 'A', 'title': 'T'}) == 'A - T'
    assert _tags.display_name({'artist': '', 'title': 'T'}) == 'T'
    assert _tags.display_name({'artist': '', 'title': ''}, 'stem') == 'stem'
