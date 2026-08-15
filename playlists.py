"""Playlists + library preferences for media-01.

Ordered ``name -> [track paths]`` playlists (same functionality as the DJ
mixer's *sets*, media-player naming), plus the library's persisted view
preferences: the set of **excluded crates** (top-level subfolders hidden from
the library and playback), the last open crate, hearted favorites, and the
**recently-played history** (newest first, capped).  One JSON file
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
        self.excluded: set[str] = set()      # crate paths hidden everywhere
        self.last_bucket: str | None = None  # last open crate (None = ALL)
        self.prefs: dict = {}                # repeat/shuffle/volume/mode/...
        self.favorites: set[str] = set()     # favorited track paths
        self.recent: list[str] = []          # recently-played paths, newest first
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

    def index_of(self, name: str, path: str) -> int:
        """Stored position of *path* in the playlist, or -1.  Paths are unique
        within a list (:meth:`add` dedupes), so this is unambiguous."""
        try:
            return (self._lists.get(name) or []).index(str(path))
        except ValueError:
            return -1

    def remove_path(self, name: str, path: str) -> bool:
        """Remove *path* from the playlist.  Returns whether it was there.

        The identity-based counterpart to :meth:`remove`.  Callers driven by a
        *displayed* row must use this one: the view can be sorted or filtered,
        so its row numbers are not the stored ones, and removing by row would
        delete a different track than the one right-clicked.
        """
        i = self.index_of(name, path)
        if i < 0:
            return False
        self.remove(name, i)
        return True

    def move_path(self, name: str, path: str, delta: int) -> int:
        """Move *path* by *delta* rows in the stored order; -1 if absent."""
        i = self.index_of(name, path)
        if i < 0:
            return -1
        return self.move(name, i, delta)

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

    def rename(self, old: str, new: str) -> bool:
        """Rename a playlist, keeping its position in the picker.

        Order matters -- the picker lists playlists in creation order, and a
        rename that sent one to the bottom would read as "it moved", so the
        dict is rebuilt in place rather than popped and re-added.  Refuses a
        blank name or one already taken; a silent overwrite would merge two
        playlists into one.
        """
        new = str(new or '').strip()
        if not new or old not in self._lists or new == old:
            return False
        if new in self._lists:
            return False
        self._lists = {(new if k == old else k): v
                       for k, v in self._lists.items()}
        self.save()
        return True

    def duplicate(self, name: str) -> str | None:
        """Copy a playlist under a free ``name (2)``-style name.

        Useful before a heavy edit: fork the list, cut it about, and the
        original is still there if the new shape does not work.
        """
        if name not in self._lists:
            return None
        n = 2
        while f'{name} ({n})' in self._lists:
            n += 1
        copy = f'{name} ({n})'
        self._lists[copy] = list(self._lists[name])
        self.save()
        return copy

    def missing(self, name: str) -> list[str]:
        """The paths in playlist *name* whose file no longer exists.

        A playlist holds paths, not tracks, so nothing notices when the file
        behind one is moved or deleted -- the row just stops loading.
        """
        out = []
        for p in self._lists.get(name, []):
            try:
                if not Path(p).is_file():
                    out.append(p)
            except OSError:
                out.append(p)
        return out

    def prune_missing(self, name: str) -> int:
        """Drop every dead path from playlist *name*; returns how many went.

        Only ever removes entries whose file is gone, so a playlist of real
        tracks is untouched.
        """
        tracks = self._lists.get(name)
        if not tracks:
            return 0
        dead = set(self.missing(name))
        if not dead:
            return 0
        self._lists[name] = [p for p in tracks if p not in dead]
        self.save()
        return len(dead)

    # -- recently played --------------------------------------------------------

    _RECENT_CAP = 50

    def note_played(self, path: str) -> None:
        """Record *path* as most recently played (deduped, capped)."""
        p = str(path)
        if not p:
            return
        if self.recent and self.recent[0] == p:
            return                          # already the freshest: no churn
        self.recent = [p] + [r for r in self.recent if r != p]
        del self.recent[self._RECENT_CAP:]
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

    def toggle_favorite(self, path: str) -> bool:
        """Flip a track's favorite state; returns the new state."""
        if path in self.favorites:
            self.favorites.discard(path)
        else:
            self.favorites.add(path)
        self.save()
        return path in self.favorites

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
            self.prefs = dict(data.get('prefs') or {})
            self.favorites = {str(f) for f in (data.get('favorites') or [])}
            self.recent = [str(r) for r in (data.get('recent') or [])]
            log.info('media playlists: %d list(s), %d crate(s) excluded',
                     len(self._lists), len(self.excluded))
        except Exception as exc:
            log.warning('media playlists: load failed (%s): %s', self._path, exc)

    def save(self) -> bool:
        payload = {'_meta': {'schema': SCHEMA, 'schema_version': SCHEMA_VERSION},
                   'playlists': self._lists,
                   'excluded': sorted(self.excluded),
                   'last_bucket': self.last_bucket,
                   'prefs': dict(self.prefs),
                   'favorites': sorted(self.favorites),
                   'recent': list(self.recent)}
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
