# media-01 — Upcoming Work / Task Queue

Owner: DJ Unicorn Tears
Status: planning — agreed in principle, sequencing TBD
Last updated: 2026-09-05

The lined-up items for the media player, in the same shape as
dj-mixer-01's `docs/upcoming-work.md`: enough design per item to start,
ordered when we get to them.  Smaller feature ideas still live under
"Planned (future)" in the README; this file is for work with a design.

---

## A. SQLite library store (soon, but not very) — owner-requested 2026-09-05

**Where it stands today.**  The tag cache is a JSON file per media root
(`runtime/media_library_cache-<hash>.json`, 0.29.1) holding size, mtime and
tags for every track — ~3 MB for 10,843.  It is read whole at startup,
rewritten whole whenever anything changed, and it is only a *cache*: the
library itself is rebuilt by walking the folder on every launch, with tag
reads for anything the cache misses.  When the cache is intact that costs
~300 ms; when it is not (which, until 0.29.1, was nearly every launch) it
costs 7–14 s of mutagen.  Playlists, favorites and recents are separate
JSON files keyed by path.  Search lower-cases display names per keystroke.

Every real player — foobar2000, MusicBee, Mixxx, Rekordbox — keeps the
library in a database and treats the filesystem as something to
*reconcile against*, never something to rebuild from.

**The design.**
- **stdlib `sqlite3`**, no new dependency.  One file per media root:
  `runtime/media_library-<hash>.sqlite`, WAL journal mode, so a force-quit
  loses at most the last uncommitted transaction.
- **Schema:** `tracks(path PRIMARY KEY, size, mtime, title, artist, album,
  genre, year, duration_s, bucket, added_at, last_played_at)` with indexes
  on `artist`, `genre`, `bucket`.  `playlists(name PRIMARY KEY, ...)` and
  `playlist_items(playlist, position, path)` replace `media_playlists.json`
  (favorites and recents are playlists with a flag).  A `search` FTS5
  virtual table over title/artist/album/genre when FTS5 is compiled in
  (Fedora's is), `LIKE` on lower-cased columns when it is not.
- **Startup reads nothing but the DB.**  The library is the `tracks` table
  the moment the file opens (milliseconds).  The prescan becomes a
  *reconcile* on the same thread it already uses: `stat` every file, diff
  against `tracks`, read tags only for new or changed files, delete rows for
  files that are gone, commit in batches.  `update()` installs the diff the
  frame it lands, exactly as the 0.29.1 deferred install does today.
- **Search and sort hit the DB**, not the tag cache: an FTS query for the
  search box, `ORDER BY` for the column sorts, `WHERE bucket LIKE` for
  crates.  The per-keystroke lower-casing goes away.
- **Playlists become rows** — adding a track is one INSERT, not a rewrite
  of the 160 KB playlists file on every track change.
- **Migration once:** first start with the JSON cache present and no DB
  imports it in a transaction and renames it `.migrated`; playlists the
  same.  Nothing about tags or playback changes.
- **Not shared code.**  dj-mixer-01 wants the same thing (its
  `docs/upcoming-work.md`, item O); drop-ins do not import each other, so
  each carries its own small `sqlite_store.py` with a parallel schema.  Do
  the mixer first — it is the harder case (16 MB, more writers) — and port
  the shape here.

**Acceptance.**  Cold start with a warm DB shows the full library before
the splash ends; a new folder of 500 files appears within one reconcile
pass without touching the other 10k; search over 20k rows < 20 ms; adding
to a playlist < 5 ms; a `kill -9` during a reconcile loses nothing
committed.

**Why "not very" soon.**  0.29.1 removed the two faults that made the
JSON cache painful (it can no longer be destroyed by a test run, and its
load no longer blocks boot).  What is left is a good-to-have until the
library grows another order of magnitude or playlists get heavy.  Queue it
after the mixer's version lands so the schema shape is proven once.

---

## Proposed order

1. **A** — SQLite library store, after dj-mixer-01's item O.

---

## Changelog
- 2026-09-05: created with **A — SQLite library store** (owner-requested,
  "soon but not very") after the tag-cache post-mortem in 0.29.1.
