"""Track tag reading and writing for media-01.

The library has always *read* tags (see :mod:`library`); this module adds
writing so a wrong artist or a missing genre can be fixed in place instead of
in another application followed by a rescan.

**The one hard rule: never re-encode.**  Everything the mixer knows about a
track — hot cues, loops, the beat grid, extracted stems, saved stem configs —
is keyed by the *audio hash*.  A tag write that rewrote the audio stream would
change that hash and orphan the lot.  The write path here touches the metadata
container only, and ``test_tags.py`` pins the behaviour by hashing the decoded
audio before and after every supported format.

Supported containers are the ones with real, portable tag support — MP3, FLAC,
OGG, M4A and friends — via mutagen's ``easy=True`` mapping.

**WAV and AIFF are deliberately not supported.**  They *can* be made to carry
an ID3 chunk, but that is a de-facto extension rather than a standard: native
WAV metadata is the very limited LIST/INFO chunk, and support for ID3-in-WAV is
inconsistent across software, so tags written there may simply not travel.
Rather than write metadata that may never be read back, those formats are
refused with a clear reason.

Anything that cannot carry tags is refused with a reason rather than silently
doing nothing.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

try:
    import mutagen
    _MUTAGEN_OK = True
except Exception:                              # pragma: no cover - optional dep
    mutagen = None
    _MUTAGEN_OK = False

# The editable set.  Deliberately small: these are the fields the browser
# filters and displays on, so editing them is what actually changes the DJ's
# view of their library.  BPM and key stay ours (analysis-owned) -- see the
# plan's open question about writing those back.
TAG_FIELDS: tuple[str, ...] = ('artist', 'title', 'album', 'genre', 'date')

FIELD_LABELS: dict[str, str] = {
    'artist': 'ARTIST', 'title': 'TITLE', 'album': 'ALBUM',
    'genre': 'GENRE', 'date': 'YEAR',
}

# Containers with no portable tagging story -- see the module docstring.
_UNTAGGABLE = {'.wav', '.wave', '.aif', '.aiff', '.aifc'}


def available() -> bool:
    """Whether tag editing can work at all (mutagen present)."""
    return _MUTAGEN_OK


def can_edit(path: str | Path) -> bool:
    """Whether *path*'s container carries portable tags we can write."""
    return _MUTAGEN_OK and Path(path).suffix.lower() not in _UNTAGGABLE


def read_tags(path: str | Path) -> dict[str, str]:
    """Current values for :data:`TAG_FIELDS`; anything missing reads as ''."""
    out = {f: '' for f in TAG_FIELDS}
    if not _MUTAGEN_OK:
        return out
    p = Path(path)
    if p.suffix.lower() in _UNTAGGABLE:
        return out
    try:
        f = mutagen.File(str(p), easy=True)
        if f is None:
            return out
        for field in TAG_FIELDS:
            val = f.get(field)
            if val:
                out[field] = str(val[0])
    except Exception as exc:
        log.debug('dj-mixer-01: tag read failed for %s: %s', p, exc)
    return out


def write_tags(path: str | Path, fields: dict[str, str]) -> tuple[bool, str]:
    """Write *fields* into the file's metadata.  Returns ``(ok, message)``.

    Only keys in :data:`TAG_FIELDS` are honoured.  An empty string clears that
    tag.  The audio stream is never touched, so the track's audio hash — and
    therefore its cues, loops and stems — survives.
    """
    if not _MUTAGEN_OK:
        return False, 'tag editing needs mutagen'
    p = Path(path)
    if not p.is_file():
        return False, 'file not found'
    if p.suffix.lower() in _UNTAGGABLE:
        return False, f'{p.suffix.upper().lstrip(".")} has no portable tag format'
    clean = {k: str(v).strip() for k, v in fields.items() if k in TAG_FIELDS}
    if not clean:
        return False, 'nothing to write'
    try:
        return _write_easy(p, clean)
    except Exception as exc:
        log.warning('dj-mixer-01: tag write failed for %s: %s', p, exc)
        return False, f'{type(exc).__name__}: {exc}'


def _write_easy(p: Path, clean: dict[str, str]) -> tuple[bool, str]:
    f = mutagen.File(str(p), easy=True)
    if f is None:
        return False, f'{p.suffix or "this format"} cannot carry tags'
    if f.tags is None:
        try:
            f.add_tags()
        except Exception:
            pass
    for field, value in clean.items():
        if value:
            f[field] = [value]
        elif field in f:
            del f[field]
    f.save()
    return True, 'saved'


def display_name(fields: dict[str, str], fallback: str = '') -> str:
    """'Artist - Title' from *fields*, else title, else *fallback*."""
    artist = (fields.get('artist') or '').strip()
    title = (fields.get('title') or '').strip()
    if artist and title:
        return f'{artist} - {title}'
    return title or fallback
