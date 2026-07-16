"""Playlists + library preferences for media-01.

Ordered ``name -> [track paths]`` playlists (same functionality as the DJ
mixer's *sets*, media-player naming), plus the library's persisted view
preferences: the set of **excluded crates** (top-level subfolders hidden from
the library and playback) and the last open crate.  One JSON file
(``runtime/media_playlists.json``), atomic writes, corrupt/missing files load
as empty — never an error.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = 'media-01.playlists'
SCHEMA_VERSION = 1


class MediaPlaylists:
    """JSON-backed playlists + library view preferences."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lists: dict[str, list[str]] = {}
        self.excluded: set[str] = set()      # crate names hidden everywhere
        self.last_bucket: str | None = None  # last open crate (None = ALL)
        self._load()

    # -- playlists -------------------------------------------------------------

    def names(self) -> list[str]:
        return list(self._lists.keys())

    def tracks(self, name: str) -> list[str]:
        return list(self._lists.get(name, []))

    def create(self, name: str | None = None) -> str:
        if not name:
            n = 1
            while f'Playlist {n}' in self._lists:
                n += 1
            name = f'Playlist {n}'
        self._lists.setdefault(name, [])
        self.save()
        return name

    def add(self, name: str, paths: list[str]) -> int:
        dest = self._lists.setdefault(name, [])
        have = set(dest)
        added = 0
        for p in paths:
            if p and p not in have:
                dest.append(p)
                have.add(p)
                added += 1
        self.save()
        return added

    def remove(self, name: str, index: int) -> None:
        tracks = self._lists.get(name)
        if tracks and 0 <= index < len(tracks):
            tracks.pop(index)
            self.save()

    def move(self, name: str, index: int, delta: int) -> int:
        tracks = self._lists.get(name)
        if not tracks or not (0 <= index < len(tracks)):
            return index
        new = max(0, min(len(tracks) - 1, index + int(delta)))
        if new != index:
            tracks.insert(new, tracks.pop(index))
            self.save()
        return new

    def delete(self, name: str) -> None:
        if self._lists.pop(name, None) is not None:
            self.save()

    # -- library preferences ----------------------------------------------------

    def toggle_excluded(self, bucket: str) -> bool:
        """Flip a crate's excluded state; returns the new state."""
        if bucket in self.excluded:
            self.excluded.discard(bucket)
        else:
            self.excluded.add(bucket)
        self.save()
        return bucket in self.excluded

    def set_last_bucket(self, bucket: str | None) -> None:
        if bucket != self.last_bucket:
            self.last_bucket = bucket
            self.save()

    # -- persistence -------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding='utf-8'))
            meta = data.get('_meta') or {}
            if meta.get('schema') not in (None, SCHEMA):
                log.warning('media playlists: ignoring foreign file %s', self._path)
                return
            raw = data.get('playlists') or {}
            self._lists = {str(k): [str(p) for p in v]
                           for k, v in raw.items() if isinstance(v, list)}
            self.excluded = {str(b) for b in (data.get('excluded') or [])}
            lb = data.get('last_bucket')
            self.last_bucket = str(lb) if lb else None
            log.info('media playlists: %d list(s), %d crate(s) excluded',
                     len(self._lists), len(self.excluded))
        except Exception as exc:
            log.warning('media playlists: load failed (%s): %s', self._path, exc)

    def save(self) -> bool:
        payload = {'_meta': {'schema': SCHEMA, 'schema_version': SCHEMA_VERSION},
                   'playlists': self._lists,
                   'excluded': sorted(self.excluded),
                   'last_bucket': self.last_bucket}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent),
                                       prefix='.media_pl_', suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                    json.dump(payload, fh, indent=1)
                os.replace(tmp, self._path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return True
        except Exception as exc:
            log.warning('media playlists: save failed (%s): %s', self._path, exc)
            return False
