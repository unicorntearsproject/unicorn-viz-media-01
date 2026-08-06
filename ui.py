"""Media Deck — the media player's own window (media-01).

A single-deck sibling of the DJ mixer's board, in the same visual language:
dark navy panels, cyan neon, Liberation Mono, a **big glowing platter** that
spins with playback (grooves, strobe spokes, orbiting sparkles), a chunky
transport bar (PREV / PLAY / NEXT / shuffle), a click-to-seek progress bar
(VLC backend), a volume slider, and a windowed library browser with search,
wheel scroll, keyboard nav (↑/↓ + Enter) and double-click-to-play.

Architecture matches control-room-01 / dj-mixer-01: its own SDL window
(``SecondaryGLWindow``), a throttled background render thread drawing with
PIL, hit regions published under a frame lock, and
``vj_api.claim_window_events`` for input.  Toggled with **Shift+L**.
"""
from __future__ import annotations

import ctypes
import importlib.util
import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:
    import sdl2
    from unicornviz.secondary_gl_window import SecondaryGLWindow
    _SDL_OK = True
except Exception:  # pragma: no cover - headless import guard
    _SDL_OK = False

try:
    from PIL import Image, ImageDraw
    _PIL_OK = True
except Exception:  # pragma: no cover
    _PIL_OK = False

# Palette — the dj-mixer-01 board language (kept in sync by eye, not import:
# drop-ins stay independent).
_BG      = (8, 10, 18, 255)

# Tag read/write (optional: the editor simply disappears without mutagen).  A
# deliberate copy of the dj-mixer-01 module -- the two drop-ins run in
# isolation, so each carries its own.
try:
    _tg_spec = importlib.util.spec_from_file_location(
        'media_tags_ui', Path(__file__).resolve().parent / 'tags.py')
    _tags_mod = importlib.util.module_from_spec(_tg_spec)
    _tg_spec.loader.exec_module(_tags_mod)
except Exception as _exc:  # pragma: no cover - tag editing just vanishes
    logging.getLogger(__name__).warning(
        'media-01: tag editing unavailable: %s', _exc)
    _tags_mod = None
_PANEL   = (16, 20, 34, 255)
_PANEL2  = (22, 26, 44, 255)
_EDGE    = (0, 180, 255, 255)
_FAINT   = (70, 88, 122, 255)
_TEXT    = (210, 225, 245, 255)
_DIM     = (110, 130, 165, 255)
_HOT     = (60, 255, 160, 255)
_YELLOW  = (255, 210, 70, 255)
_WARN    = (255, 120, 90, 255)
_WAVE_HI = (0, 200, 255, 255)
# The row the room is hearing.  A bed, not a text colour: the name colour is
# already carrying other meaning, and "what is playing" has to survive it.
# Purple because nothing else on this surface uses it.
_PLAYING_BED = (58, 26, 92, 255)
_SEL_ON_PLAYING = (74, 44, 116, 255)     # selected *and* playing
_ROW_HOVER_BED = (26, 34, 52, 255)       # hover with nothing else on the row
_HOVER_LIFT = 26                         # how much a hovered row's bed lifts
_ARTIST_MUTE = 0.85                      # the artist sits behind the title...
_ARTIST_MIN_LUM = 484                    # ...but never duller than a mid grey


def _muted(col) -> tuple[int, int, int, int]:
    """A row colour toned back for the artist column.

    Keeps the hue while sitting behind the title.  The lift back is
    proportional, so it changes how bright the colour is without changing
    which colour it is -- muting a saturated colour by a flat factor lands
    *below* a mid grey, which made the "quieter" field the duller one.
    """
    base = [c * _ARTIST_MUTE for c in col[:3]]
    lum = sum(base)
    if 0.0 < lum < _ARTIST_MIN_LUM:
        k = min(_ARTIST_MIN_LUM / lum, 1.0 / _ARTIST_MUTE)
        base = [min(255.0, c * k) for c in base]
    return tuple(int(c) for c in base) + (255,)


def _hover_bed(bed):
    """Lighten whatever bed a row already had, or give it one.

    Additive rather than a replacement colour, so hovering never hides what
    the row was already saying: a playing row stays purple, a selected row
    stays blue, both just come up under the pointer.
    """
    if bed is None:
        return _ROW_HOVER_BED
    return tuple(min(255, c + _HOVER_LIFT) for c in bed[:3]) + (255,)


def _mid_ellipsis(text: str, max_chars: int) -> str:
    """Trim from the middle, so both ends of a name survive.

    'Artist … (Extended Mix)' keeps the part that identifies the track *and*
    the part that says which version it is -- cutting the tail loses the one
    that usually matters when you have three edits of the same tune.
    """
    if max_chars <= 1 or len(text) <= max_chars:
        return text
    keep = max_chars - 1
    head = (keep + 1) // 2
    return text[:head] + '…' + text[len(text) - (keep - head):]

def _font(size: int):
    if _PIL_OK:
        # Shared resolver: bundled ui-font.ttf first, then per-platform
        # system dirs (incl. Windows) — see unicornviz/fonts.py.
        try:
            from unicornviz.fonts import load_font
            return load_font(size)
        except Exception:
            return None
    return None


def _fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f'{h:d}:{m:02d}:{s:02d}' if h else f'{m:d}:{s:02d}'


class MediaDeckWindow:
    """Own-window renderer/controller UI for the local media player."""

    _REV_PER_S = 0.45          # platter revolutions per second of playback

    def __init__(self, controller: Any, cfg: dict[str, Any],
                 vj_api: Any = None) -> None:
        self._c = controller
        self._cfg = dict(cfg or {})
        self._vj = vj_api
        self._display_index = int(self._cfg.get('display_index', 0) or 0)
        self._width = max(760, int(self._cfg.get('window_width', 980) or 980))
        self._height = max(520, int(self._cfg.get('window_height', 640) or 640))
        self._interval = max(0.02, float(self._cfg.get('render_interval', 0.05)
                                         or 0.05))

        self._window = None
        self._gl_window = None
        self._window_id = -1
        self._pending_shutdown = False
        self._frame_lock = threading.Lock()
        self._regions: list = []          # (x0,y0,x1,y1, action, payload)
        self._rows: list = []             # (x0,y0,x1,y1, index)
        self._mouse_xy = (-1, -1)         # cursor, for row hover
        self._mouse_scale_x = 1.0
        self._mouse_scale_y = 1.0
        self._sel = 0                     # library selection (view row)
        self._query = ''                  # search filter
        self._search_focused = False
        self._rows_visible = 10
        self._scroll = 0                  # stable list scroll (no recentering)
        self._close_confirm_deadline = 0.0
        self._menu: dict | None = None      # right-click context menu
        self._menu_rects: list = []
        self._tag_edit: dict | None = None  # open tag editor
        self._tag_rects: list = []
        self._pl_open = False               # playlist picker flyout
        self._sort_open = False             # sort flyout
        self._frame_id = 0
        self._presented_id = -1
        self._raw: bytes | None = None

        self._font_title = _font(22)
        self._font_h = _font(16)
        self._font_b = _font(12)
        self._font_s = _font(10)
        self._font_heart = _font(15)

        self._create_window()
        self._thread_stop = threading.Event()
        self._thread = threading.Thread(target=self._render_loop,
                                        name='MediaDeckUI', daemon=True)
        self._thread.start()

    # -- lifecycle ------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._window is not None and not self._pending_shutdown

    def _create_window(self) -> None:
        if not _SDL_OK:
            raise RuntimeError('SDL2 unavailable')
        count = max(1, int(sdl2.SDL_GetNumVideoDisplays()))
        disp = max(0, min(self._display_index, count - 1))
        bounds = sdl2.SDL_Rect()
        sdl2.SDL_GetDisplayBounds(disp, bounds)
        x = bounds.x + max(0, (bounds.w - self._width) // 2)
        y = bounds.y + max(0, (bounds.h - self._height) // 2)
        self._gl_window = SecondaryGLWindow(
            b'Unicorn Viz Media Deck', x, y, self._width, self._height,
            extra_flags=sdl2.SDL_WINDOW_RESIZABLE,
        )
        self._gl_window.create()
        self._window = self._gl_window.window
        self._width, self._height = self._gl_window.width, self._gl_window.height
        self._window_id = self._gl_window.window_id
        self._refresh_mouse_scale()
        if self._vj is not None:
            try:
                self._vj.claim_window_events(self._window_id, self.on_sdl_event)
            except Exception as exc:
                log.warning('media-deck: claim_window_events failed: %s', exc)
        log.info('media-deck: window opened on display %d (%dx%d)',
                 disp, self._width, self._height)

    def _refresh_mouse_scale(self) -> None:
        try:
            lw, lh = ctypes.c_int(0), ctypes.c_int(0)
            sdl2.SDL_GetWindowSize(self._window, ctypes.byref(lw),
                                   ctypes.byref(lh))
            if lw.value > 0:
                self._mouse_scale_x = self._width / lw.value
            if lh.value > 0:
                self._mouse_scale_y = self._height / lh.value
        except Exception:
            pass

    def shutdown(self) -> None:
        self._pending_shutdown = True

    def close_now(self) -> None:
        self._pending_shutdown = True
        self._thread_stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        if self._vj is not None and self._window_id >= 0:
            try:
                self._vj.release_window_events(self._window_id)
            except Exception:
                pass
        if self._gl_window is not None:
            try:
                self._gl_window.destroy()
            except Exception:
                pass
        self._gl_window = None
        self._window = None

    def _stop_thread(self) -> None:
        """Test hook: render synchronously via _render_ui() only."""
        self._thread_stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    # -- render loop ------------------------------------------------------------

    def _render_loop(self) -> None:
        while not self._thread_stop.is_set() and not self._pending_shutdown:
            try:
                img = self._render_ui()
                self._raw = img.tobytes()
                self._frame_id += 1
            except Exception as exc:
                log.warning('media-deck render error: %s', exc)
            time.sleep(self._interval)

    def update(self, dt: float) -> None:      # noqa: ARG002 - protocol shape
        pass

    def present(self) -> None:
        """Blit the latest raster (called on the main thread)."""
        if self._gl_window is None or self._raw is None:
            return
        if self._presented_id == self._frame_id:
            return
        try:
            self._gl_window.present(self._raw, self._width, self._height)
            self._presented_id = self._frame_id
        except Exception as exc:
            log.debug('media-deck present failed: %s', exc)

    # -- input ------------------------------------------------------------------

    def on_sdl_event(self, event: Any) -> None:
        if self._pending_shutdown or self._window is None or not _SDL_OK:
            return
        if event.type == sdl2.SDL_WINDOWEVENT:
            if event.window.event == sdl2.SDL_WINDOWEVENT_CLOSE:
                self.shutdown()
            elif event.window.event in (sdl2.SDL_WINDOWEVENT_RESIZED,
                                        sdl2.SDL_WINDOWEVENT_SIZE_CHANGED):
                w_i, h_i = ctypes.c_int(0), ctypes.c_int(0)
                sdl2.SDL_GL_GetDrawableSize(self._window, ctypes.byref(w_i),
                                            ctypes.byref(h_i))
                if w_i.value > 0 and h_i.value > 0:
                    self._width = max(760, int(w_i.value))
                    self._height = max(520, int(h_i.value))
                self._refresh_mouse_scale()
            return
        if event.type == sdl2.SDL_KEYDOWN:
            self._on_key(int(event.key.keysym.sym), int(event.key.keysym.mod),
                         bool(event.key.repeat))
            return
        if event.type == sdl2.SDL_MOUSEWHEEL:
            self._move_sel(-int(event.wheel.y) or 1)
            return
        if event.type == sdl2.SDL_MOUSEMOTION:
            # Tracked purely for the hover highlight; the render thread reads
            # it, which is safe because it is two ints written atomically.
            self._mouse_xy = (int(event.motion.x * self._mouse_scale_x),
                              int(event.motion.y * self._mouse_scale_y))
            return
        if event.type == sdl2.SDL_WINDOWEVENT and getattr(
                event.window, 'event', None) == sdl2.SDL_WINDOWEVENT_LEAVE:
            self._mouse_xy = (-1, -1)     # cursor gone: nothing is hovered
            return
        if (event.type == sdl2.SDL_MOUSEBUTTONDOWN
                and event.button.button == sdl2.SDL_BUTTON_RIGHT):
            bx = int(event.button.x * self._mouse_scale_x)
            by = int(event.button.y * self._mouse_scale_y)
            self._right_click(bx, by)
            return
        if (event.type == sdl2.SDL_MOUSEBUTTONDOWN
                and event.button.button == sdl2.SDL_BUTTON_LEFT):
            bx = int(event.button.x * self._mouse_scale_x)
            by = int(event.button.y * self._mouse_scale_y)
            if self._tag_edit is not None:
                self._click_tag_editor(bx, by)
                return
            if self._menu is not None:
                self._click_menu(bx, by)
                return
            self._click(bx, by, int(event.button.clicks))
            return

    def _on_key(self, sym: int, mod: int, repeat: bool) -> None:  # noqa: ARG002
        if self._tag_edit is not None:
            self._tag_edit_key(sym, mod)
            return
        if self._search_focused and self._search_key(sym):
            return
        if sym == sdl2.SDLK_ESCAPE:
            if time.monotonic() < self._close_confirm_deadline:
                self.shutdown()
            else:
                self._close_confirm_deadline = time.monotonic() + 2.0
            return
        if sym == sdl2.SDLK_UP:
            self._move_sel(-1)
            return
        if sym == sdl2.SDLK_DOWN:
            self._move_sel(1)
            return
        if sym in (sdl2.SDLK_RETURN, sdl2.SDLK_KP_ENTER):
            view = self._view()
            if 0 <= self._sel < len(view):
                self._c.play_index(view[self._sel][0])
            return
        if sym == sdl2.SDLK_SPACE:
            self._c.play_pause()
            return
        if sym == sdl2.SDLK_f and not (mod & (sdl2.KMOD_CTRL | sdl2.KMOD_ALT
                                              | sdl2.KMOD_SHIFT)):
            self._toggle_fullscreen()      # F: this window, not the visualizer
            return
        if self._vj is not None:              # everything else: global hotkeys
            fwd = getattr(self._vj, 'dispatch_subwindow_keydown', None)
            if callable(fwd):
                fwd(sym, mod, repeat)

    def _toggle_fullscreen(self) -> None:
        """Borderless-desktop fullscreen toggle for the Media Deck window."""
        try:
            self._fullscreen = not getattr(self, '_fullscreen', False)
            flag = sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP if self._fullscreen else 0
            sdl2.SDL_SetWindowFullscreen(self._window, flag)
            # Drawable size changes land via the RESIZED window event.
            log.info('media-deck: fullscreen %s',
                     'on' if self._fullscreen else 'off')
        except Exception as exc:
            log.warning('media-deck: fullscreen toggle failed: %s', exc)

    def _search_key(self, sym: int) -> bool:
        if sym in (sdl2.SDLK_RETURN, sdl2.SDLK_KP_ENTER, sdl2.SDLK_ESCAPE):
            self._search_focused = False
            return True
        if sym == sdl2.SDLK_BACKSPACE:
            self._query = self._query[:-1]
            return True
        if 32 <= sym < 127:
            self._query += chr(sym)
            return True
        return False

    def _move_sel(self, delta: int) -> None:
        n = len(self._view())
        if n:
            self._sel = max(0, min(n - 1, self._sel + int(delta)))
            # Keyboard/wheel keep the selection on screen; mouse clicks never
            # scroll the list (so a double-click hits the row it started on).
            vis = max(1, self._rows_visible)
            if self._sel < self._scroll:
                self._scroll = self._sel
            elif self._sel >= self._scroll + vis:
                self._scroll = self._sel - vis + 1

    def _click(self, x: int, y: int, clicks: int) -> None:
        with self._frame_lock:
            hit = next((r for r in self._regions
                        if r[0] <= x <= r[2] and r[1] <= y <= r[3]), None)
            row = next((r for r in self._rows
                        if r[0] <= x <= r[2] and r[1] <= y <= r[3]), None)
        self._search_focused = False
        # Regions win over rows: per-row controls (the ♥) and open flyouts
        # sit on top of the track list.
        if row is not None and hit is None:
            self._sel = row[4]
            if clicks >= 2:
                view = self._view()
                if 0 <= self._sel < len(view):
                    self._c.play_index(view[self._sel][0])
            return
        if hit is None:
            return
        action, payload = hit[4], hit[5]
        try:
            if action == 'play':
                self._c.play_pause()
            elif action == 'next':
                self._c.next_track()
            elif action == 'prev':
                self._c.prev_track()
            elif action == 'shuffle':
                self._c.toggle_shuffle()
            elif action == 'repeat':
                self._c.cycle_repeat()
            elif action == 'focus_search':
                self._search_focused = True
            elif action == 'keep_toggle':
                self._c.toggle_keep_playing()
            elif action == 'ambience_toggle':
                self._c.toggle_ambience()
            elif action == 'crossfade_toggle':
                self._c.toggle_crossfade()
            elif action == 'set_mode':
                self._c.set_mode(payload)
                self._pl_open = self._sort_open = False
                self._sel = 0
            elif action == 'set_bucket':
                self._c.set_bucket(payload)
                self._sel = 0
            elif action == 'exclude_bucket':
                self._c.toggle_exclude(payload)
            elif action == 'fav_toggle':
                self._c.toggle_favorite(payload)
            elif action == 'toggle_sort':
                self._sort_open = not self._sort_open
            elif action == 'set_sort':
                self._c.set_sort(payload[0], payload[1])
                self._sort_open = False
            elif action == 'pl_flyout':
                self._pl_open = not self._pl_open
            elif action == 'pl_select':
                self._c.active_playlist = payload
                self._pl_open = False
                self._sel = 0
            elif action == 'pl_new':
                pls = getattr(self._c, 'playlists', None)
                if pls is not None:
                    self._c.active_playlist = pls.create()
            elif action == 'seek' and getattr(self._c, 'can_seek', False):
                x0, x1, dur = payload
                frac = min(1.0, max(0.0, (x - x0) / max(1, x1 - x0)))
                self._c.seek_to(frac * dur)
            elif action == 'volume':
                x0, x1 = payload
                frac = min(1.0, max(0.0, (x - x0) / max(1, x1 - x0)))
                self._c.set_volume(int(frac * 200))
        except Exception as exc:
            log.warning('media-deck: action %s failed: %s', action, exc)

    # -- library view -----------------------------------------------------------

    def _view(self) -> list[tuple[int, str]]:
        """(library index, display name) rows of the controller's active view
        (open crate or playlist), further narrowed by the search box."""
        rows_fn = getattr(self._c, 'view_rows', None)
        rows = rows_fn() if callable(rows_fn) else             list(enumerate(self._c.library_names()))
        q = self._query.strip().lower()
        return [(i, n) for i, n in rows if not q or q in n.lower()]

    # -- right-click context menu ------------------------------------------

    def _right_click(self, x: int, y: int) -> None:
        if self._menu is not None:
            self._menu = None
            return
        with self._frame_lock:
            row = next((r for r in self._rows
                        if r[0] <= x <= r[2] and r[1] <= y <= r[3]), None)
        if row is None:
            return
        self._sel = row[4]
        view = self._view()
        if not (0 <= self._sel < len(view)):
            return
        idx, name = view[self._sel]
        items: list[tuple[str, str, Any]] = [('Play', 'play_row', idx)]
        pls = getattr(self._c, 'playlists', None)
        if getattr(self._c, 'mode', 'library') == 'playlists':
            items += [('Remove from playlist', 'pl_remove', self._sel),
                      ('Move up', 'pl_move', (self._sel, -1)),
                      ('Move down', 'pl_move', (self._sel, 1))]
        elif pls is not None:
            items.append(('Add to playlist »', 'add_menu', idx))
        if hasattr(self._c, 'is_favorite'):
            fav = bool(self._c.is_favorite(idx))
            items.append(('Unfavorite ♥' if fav else 'Favorite ♥', 'fav', idx))
        if _tags_mod is not None and hasattr(self._c, 'snapshot_path_at'):
            tp = self._c.snapshot_path_at(idx)
            if tp and _tags_mod.can_edit(tp):
                items.append(('Edit tags…', 'tag_edit', idx))
        items.append(('Delete file…', 'del_arm', idx))
        self._menu = {'x': x, 'y': y, 'name': name, 'items': items}

    def _tag_edit_key(self, sym: int, mod: int) -> None:
        """Keystrokes while the tag editor is open (Tab/arrows move, Enter
        saves, Esc cancels).  ASCII only -- the console font has no wider set."""
        ed = self._tag_edit
        if ed is None or _tags_mod is None:
            return
        fields = _tags_mod.TAG_FIELDS
        if sym == sdl2.SDLK_ESCAPE:
            self._tag_edit = None
            return
        if sym in (sdl2.SDLK_RETURN, sdl2.SDLK_KP_ENTER):
            self._tag_edit_save()
            return
        if sym in (sdl2.SDLK_TAB, sdl2.SDLK_DOWN):
            ed['focus'] = (ed['focus'] + 1) % len(fields)
            return
        if sym == sdl2.SDLK_UP:
            ed['focus'] = (ed['focus'] - 1) % len(fields)
            return
        key = fields[ed['focus'] % len(fields)]
        cur = ed['fields'].get(key, '')
        if sym == sdl2.SDLK_BACKSPACE:
            ed['fields'][key] = cur[:-1]
        elif 32 <= sym < 127:
            ch = chr(sym)
            if mod & (sdl2.KMOD_LSHIFT | sdl2.KMOD_RSHIFT):
                ch = ch.upper()
            ed['fields'][key] = cur + ch
        else:
            return
        ed['msg'] = ''

    def _tag_edit_save(self) -> None:
        ed = self._tag_edit
        if ed is None or _tags_mod is None:
            return
        ok, msg = _tags_mod.write_tags(ed['path'], ed['fields'])
        if not ok:
            ed['msg'] = msg
            return
        if hasattr(self._c, 'rescan_library'):
            try:                               # the row's name moved
                self._c.rescan_library()
            except Exception as exc:
                log.warning('media-deck: rescan after tag edit: %s', exc)
        self._tag_edit = None

    def _click_tag_editor(self, x: int, y: int) -> None:
        ed = self._tag_edit
        if ed is None:
            return
        with self._frame_lock:
            hit = next((r for r in self._tag_rects
                        if r[0] <= x <= r[2] and r[1] <= y <= r[3]), None)
        if hit is None:
            return                             # modal: outside clicks do nothing
        action, payload = hit[4], hit[5]
        if action == 'tag_focus':
            ed['focus'] = int(payload)
        elif action == 'tag_save':
            self._tag_edit_save()
        elif action == 'tag_cancel':
            self._tag_edit = None

    def _draw_tag_editor(self, d, w, h) -> list:
        """The tag-edit modal; returns its click rects."""
        ed = self._tag_edit
        if ed is None or _tags_mod is None:
            return []
        fields = _tags_mod.TAG_FIELDS
        pw, rowh = 440, 26
        ph = 60 + rowh * len(fields) + 44
        x0 = max(8, (w - pw) // 2)
        y0 = max(8, (h - ph) // 2)
        tr: list = []
        d.rectangle([x0 - 2, y0 - 2, x0 + pw + 2, y0 + ph + 2], outline=_EDGE)
        d.rectangle([x0, y0, x0 + pw, y0 + ph], fill=_PANEL, outline=_HOT)
        d.text((x0 + 14, y0 + 10), 'EDIT TAGS', font=self._font_b, fill=_WAVE_HI)
        d.text((x0 + 14, y0 + 30), Path(str(ed['path'])).name[:54],
               font=self._font_s, fill=_FAINT)
        yy = y0 + 54
        for i, key in enumerate(fields):
            label = _tags_mod.FIELD_LABELS.get(key, key.upper())
            focused = (ed['focus'] % len(fields)) == i
            d.text((x0 + 14, yy + 6), f'{label:>6s}', font=self._font_s,
                   fill=_TEXT if focused else _DIM)
            bx0, bx1 = x0 + 72, x0 + pw - 14
            d.rectangle([bx0, yy, bx1, yy + rowh - 4], fill=_BG,
                        outline=_EDGE if focused else _FAINT)
            val = str(ed['fields'].get(key, ''))[-54:]
            d.text((bx0 + 6, yy + 6), val, font=self._font_s,
                   fill=_TEXT if focused else _DIM)
            if focused:
                cx = bx0 + 6 + len(val) * 6
                d.rectangle([cx, yy + 5, cx + 1, yy + rowh - 9], fill=_HOT)
            tr.append((bx0, yy, bx1, yy + rowh - 4, 'tag_focus', i))
            yy += rowh
        if ed.get('msg'):
            d.text((x0 + 14, yy + 2), str(ed['msg'])[:58], font=self._font_s,
                   fill=_WARN)
        yy += 16
        for lab, act, col in (('SAVE', 'tag_save', _HOT),
                              ('CANCEL', 'tag_cancel', _DIM)):
            bw = len(lab) * 6 + 20
            bx = (x0 + pw - 14 - bw) if act == 'tag_save' else (x0 + 14)
            d.rectangle([bx, yy, bx + bw, yy + 20], fill=_BG, outline=col)
            d.text((bx + 10, yy + 5), lab, font=self._font_s, fill=col)
            tr.append((bx, yy, bx + bw, yy + 20, act, None))
        return tr

    def _click_menu(self, x: int, y: int) -> None:
        menu = self._menu
        with self._frame_lock:
            hit = next((r for r in self._menu_rects
                        if r[0] <= x <= r[2] and r[1] <= y <= r[3]), None)
        if hit is None or menu is None:
            self._menu = None
            return
        action, payload = hit[4], hit[5]
        pls = getattr(self._c, 'playlists', None)
        try:
            if action == 'play_row':
                self._c.play_index(payload)
                self._menu = None
            elif action == 'add_menu':      # fly out to the playlist choices
                names = pls.names() if pls else []
                items = [(f'→ {n[:20]}', 'add_to', (n, payload)) for n in names]
                items.append(('→ + new playlist', 'add_to', (None, payload)))
                menu['items'] = items
            elif action == 'add_to':
                name, idx = payload
                if pls is not None:
                    if name is None:
                        name = pls.create()
                    path = str(self._c.snapshot_path_at(idx) or '')
                    if path:
                        pls.add(name, [path])
                        log.info('media-deck: added to %s', name)
                self._menu = None
            elif action == 'pl_remove':
                if pls is not None and self._c.active_playlist:
                    pls.remove(self._c.active_playlist, payload)
                self._menu = None
            elif action == 'fav':
                self._c.toggle_favorite(payload)
                self._menu = None
            elif action == 'tag_edit':
                path = (self._c.snapshot_path_at(payload)
                        if hasattr(self._c, 'snapshot_path_at') else None)
                if path and _tags_mod is not None:
                    self._tag_edit = {'path': path,
                                      'fields': _tags_mod.read_tags(path),
                                      'focus': 0, 'msg': ''}
                self._menu = None
            elif action == 'del_arm':      # arm: the next click really deletes
                menu['items'] = [('SURE? DELETE FILE', 'del_do', payload),
                                 ('Cancel', 'del_cancel', None)]
            elif action == 'del_do':
                if hasattr(self._c, 'delete_track'):
                    self._c.delete_track(payload)
                self._menu = None
            elif action == 'del_cancel':
                self._menu = None
            elif action == 'pl_move':
                if pls is not None and self._c.active_playlist:
                    pos, delta = payload
                    pls.move(self._c.active_playlist, pos, delta)
                    self._sel = max(0, pos + delta)
                self._menu = None
        except Exception as exc:
            log.warning('media-deck: menu action %s failed: %s', action, exc)
            self._menu = None

    # -- drawing ------------------------------------------------------------

    def _render_ui(self) -> 'Image.Image':
        w, h = self._width, self._height
        img = Image.new('RGBA', (w, h), _BG)
        d = ImageDraw.Draw(img, 'RGBA')
        regions: list = []
        rows: list = []
        snap = self._c.snapshot()
        playing = bool(snap.get('is_playing'))
        pos = float(snap.get('position_s', 0.0) or 0.0)
        dur = float(snap.get('duration_s', 0.0) or 0.0)

        # Header band.
        d.rectangle([0, 0, w, 46], fill=_PANEL2)
        d.line([0, 46, w, 46], fill=_EDGE)
        d.text((16, 12), 'UNICORN VIZ  //  MEDIA DECK', font=self._font_title,
               fill=_EDGE)
        status = str(snap.get('status', 'off') or 'off').upper()
        scol = _HOT if playing else _DIM
        d.text((w - len(status) * 7 - 18, 16), status, font=self._font_b,
               fill=scol)

        # Split: deck pane (left ~58%) | library (right).
        gap = 8
        deck_w = int(w * 0.58)
        lx0, lx1 = gap, deck_w - gap
        rx0, rx1 = deck_w + gap, w - gap
        top = 46 + gap
        d.rectangle([gap - 4, top, deck_w - 4, h - gap], fill=_PANEL)
        d.rectangle([gap - 4, top, deck_w - 4, top + 3], fill=_EDGE)

        # Platter — the showpiece.
        pl_h = int(h * 0.46)
        self._draw_platter(d, (lx0 + lx1) // 2, top + 14 + pl_h // 2,
                           min(pl_h // 2 - 8, (lx1 - lx0) // 2 - 30),
                           playing, pos, dur)
        cy = top + 24 + pl_h

        # Now playing block.
        title = str(snap.get('title', '') or '') or '—'
        artist = str(snap.get('artist', '') or '')
        album = str(snap.get('album', '') or '')
        prev_t = str(snap.get('previous_title', '') or '')
        prev_a = str(snap.get('previous_artist', '') or '')
        maxc = max(10, (lx1 - lx0 - 8) // 10)
        d.text((lx0 + 6, cy), title[:maxc], font=self._font_h, fill=_WAVE_HI)
        cy += 22
        line2 = artist + (f'  ·  {album}' if album else '')
        d.text((lx0 + 6, cy), line2[:maxc + 14] or ' ', font=self._font_b,
               fill=_TEXT)
        cy += 18
        if prev_t:
            d.text((lx0 + 6, cy), f'prev: {prev_a} - {prev_t}'[:maxc + 20],
                   font=self._font_s, fill=_FAINT)
        cy += 18

        # Progress bar (click-to-seek on the VLC backend).
        can_seek = bool(getattr(self._c, 'can_seek', False))
        bar_y = cy + 4
        frac = min(1.0, pos / dur) if dur > 0 else 0.0
        d.rectangle([lx0 + 6, bar_y, lx1 - 6, bar_y + 12],
                    fill=(0, 0, 0, 90), outline=_DIM)
        fill_w = int((lx1 - lx0 - 12) * frac)
        if fill_w > 0:
            d.rectangle([lx0 + 6, bar_y, lx0 + 6 + fill_w, bar_y + 12],
                        fill=_WAVE_HI if playing else _DIM)
        if can_seek:
            regions.append((lx0 + 6, bar_y - 4, lx1 - 6, bar_y + 16, 'seek',
                            (lx0 + 6, lx1 - 6, dur)))
        d.text((lx0 + 6, bar_y + 16), _fmt_time(pos), font=self._font_s,
               fill=_TEXT)
        tw = len(_fmt_time(dur)) * 6
        d.text((lx1 - 6 - tw, bar_y + 16), _fmt_time(dur), font=self._font_s,
               fill=_DIM)
        cy = bar_y + 34

        # Transport: |◄ PREV | PLAY (wide, pulsing) | NEXT ►| | SHUF | volume.
        th = 34
        # 7-unit grid: PREV 1 | PLAY 2 | NEXT 1 | LOOP 1 | SHUF 1 | VOL 1.
        bw = (lx1 - lx0 - 12 - 5 * 6) // 7
        bx = lx0 + 6
        pulse = 0.5 + 0.5 * abs((time.monotonic() * 0.8) % 2.0 - 1.0)
        buttons = [
            ('|◄', 'prev', _TEXT, None, 1),
            ('▌▌  PAUSE' if playing else '►  PLAY', 'play',
             _HOT, (10, int(30 + 40 * pulse), 26, 255) if playing else None, 2),
            ('►|', 'next', _TEXT, None, 1),
        ]
        for label, act, col, fill, units in buttons:
            bwu = bw * units + (6 * (units - 1))
            d.rectangle([bx, cy, bx + bwu, cy + th],
                        fill=fill if fill else (0, 0, 0, 90),
                        outline=col, width=2 if act == 'play' else 1)
            d.text((bx + max(4, (bwu - len(label) * 7) // 2), cy + 10), label,
                   font=self._font_b, fill=col)
            regions.append((bx, cy, bx + bwu, cy + th, act, None))
            bx += bwu + 6
        # Repeat (loop) toggle: ALL (lit) -> ONE (lit + 1) -> OFF (dim).
        rep = str(snap.get('repeat', 'all') or 'all')
        ron = rep != 'off'
        d.rectangle([bx, cy, bx + bw, cy + th],
                    fill=_HOT if ron else (0, 0, 0, 90),
                    outline=_HOT if ron else _DIM)
        icx, icy = bx + bw // 2, cy + th // 2
        icol = (4, 18, 26, 255) if ron else _DIM
        d.ellipse([icx - 7, icy - 6, icx + 7, icy + 6], outline=icol)
        d.rectangle([icx + 2, icy - 8, icx + 9, icy - 3],
                    fill=_HOT if ron else (0, 0, 0, 90))
        d.line([icx + 6, icy - 6, icx + 1, icy - 8], fill=icol)
        d.line([icx + 6, icy - 6, icx + 3, icy - 1], fill=icol)
        if rep == 'one':
            d.text((icx - 2, icy - 5), '1', font=self._font_s, fill=icol)
        regions.append((bx, cy, bx + bw, cy + th, 'repeat', None))
        bx += bw + 6
        # Shuffle icon toggle (crossing arrows, mixer-style).
        on = bool(snap.get('shuffle', False))
        d.rectangle([bx, cy, bx + bw, cy + th],
                    fill=_HOT if on else (0, 0, 0, 90),
                    outline=_HOT if on else _DIM)
        icx, icy = bx + bw // 2, cy + th // 2
        icol = (4, 18, 26, 255) if on else _DIM
        d.line([icx - 9, icy - 5, icx + 7, icy + 5], fill=icol)
        d.line([icx - 9, icy + 5, icx + 7, icy - 5], fill=icol)
        d.line([icx + 7, icy - 5, icx + 2, icy - 5], fill=icol)
        d.line([icx + 7, icy + 5, icx + 2, icy + 5], fill=icol)
        regions.append((bx, cy, bx + bw, cy + th, 'shuffle', None))
        bx += bw + 6
        # Volume slider (VLC backend only).
        if getattr(self._c, 'can_volume', False):
            vol = int(getattr(self._c, 'volume', 100))
            vx1 = min(lx1 - 6, bx + bw)
            d.text((bx, cy - 2), 'VOL', font=self._font_s, fill=_DIM)
            d.rectangle([bx, cy + 12, vx1, cy + 22], fill=(0, 0, 0, 90),
                        outline=_DIM)
            vw = int((vx1 - bx) * min(1.0, vol / 200.0))
            if vw > 0:
                d.rectangle([bx, cy + 12, bx + vw, cy + 22], fill=_HOT)
            d.text((bx, cy + 24), f'{vol}%', font=self._font_s, fill=_FAINT)
            regions.append((bx, cy + 6, vx1, cy + 28, 'volume', (bx, vx1)))

        # Options checkboxes (bottom-left stack).  One helper so every box has
        # the same generous full-row hit target — small/adjacent click regions
        # made these feel unreliable.
        def _checkbox(y: int, label: str, on: bool, action: str,
                      enabled: bool = True) -> None:
            col = (_HOT if on else _DIM) if enabled else _FAINT
            d.rectangle([lx0 + 6, y, lx0 + 20, y + 14],
                        fill=_HOT if on else (0, 0, 0, 90), outline=col)
            if on:
                d.line([lx0 + 9, y + 7, lx0 + 12, y + 10], fill=(4, 18, 26, 255))
                d.line([lx0 + 12, y + 10, lx0 + 17, y + 4], fill=(4, 18, 26, 255))
            d.text((lx0 + 27, y + 2), label, font=self._font_s,
                   fill=(_TEXT if on else _DIM) if enabled else _FAINT)
            if enabled:
                regions.append((lx0 + 2, y - 2, lx0 + 31 + len(label) * 6,
                                y + 16, action, None))

        kb_y = h - gap - 22
        # Never let the stack ride up into the transport row at small heights.
        cb_top = max(kb_y - 48, cy + th + 8)
        cb_step = max(17, (kb_y - cb_top) // 2)
        can_xf = bool(snap.get('can_crossfade', False))
        _checkbox(cb_top,
                  'crossfade between tracks' + ('' if can_xf else ' (needs VLC)'),
                  bool(snap.get('crossfade', False)) and can_xf,
                  'crossfade_toggle', enabled=can_xf)
        _checkbox(cb_top + cb_step, 'corner platter on the visualizer',
                  bool(snap.get('ambience', True)), 'ambience_toggle')
        _checkbox(cb_top + 2 * cb_step, 'keep playing when window is closed',
                  bool(snap.get('keep_playing_on_close', True)), 'keep_toggle')

        # Library pane (right column): LIBRARY | PLAYLISTS tabs, accordion
        # crates with per-crate exclude toggles, sort flyout, playlist picker.
        d.rectangle([rx0 - 4, top, rx1 + 4, h - gap],
                    fill=(13, 16, 28, 255), outline=(40, 52, 78, 255))
        mode = getattr(self._c, 'mode', 'library')
        pls = getattr(self._c, 'playlists', None)
        ty = top + 6
        tabx = rx0 + 4
        for label, m in (('LIBRARY', 'library'), ('PLAYLISTS', 'playlists'),
                         ('♥ FAVS', 'favorites')):  # heart sized by tab font
            on = mode == m
            tw2 = len(label) * 6 + 16
            if on:
                d.rectangle([tabx, ty, tabx + tw2, ty + 18],
                            fill=(0, 45, 90, 255), outline=_EDGE)
            else:
                d.rectangle([tabx, ty, tabx + tw2, ty + 18], outline=_DIM)
            d.text((tabx + 8, ty + 4), label, font=self._font_s,
                   fill=_TEXT if on else _DIM)
            regions.append((tabx, ty, tabx + tw2, ty + 18, 'set_mode', m))
            tabx += tw2 + 4
        # How long this view is, not just how many rows -- a count answers
        # "how much have I got" only if you already know the tracks.  Files
        # whose length never got read count as zero and are flagged, so the
        # total reads as *at least* this long rather than silently short.
        stats_fn = getattr(self._c, 'view_stats', None)
        st = stats_fn() if callable(stats_fn) else {}
        has_stats = bool(st.get('count'))
        if has_stats:
            span = _fmt_time(st.get('duration_s', 0.0))
            unknown = int(st.get('unknown', 0))
            d.text((rx0 + 4, ty + 22),
                   f'{st["count"]} tracks · {span}' + ('+' if unknown else ''),
                   font=self._font_s, fill=_FAINT)
        # Sort flyout icon at the right edge of the tab row.
        if mode in ('library', 'favorites'):
            sox = rx1 - 26
            skey = getattr(self._c, 'sort', (None, True))[0]
            scol = _HOT if (self._sort_open or skey) else _DIM
            d.rectangle([sox, ty, sox + 22, ty + 18], outline=scol)
            d.text((sox + 4, ty + 4), '↑↓', font=self._font_s, fill=scol)
            regions.append((sox, ty, sox + 22, ty + 18, 'toggle_sort', None))
        # The search row sits below the stats line when there is one, so the
        # two never share a strip -- inserted, not overlaid.
        sy = ty + (38 if has_stats else 24)
        if mode == 'playlists':
            cur = getattr(self._c, 'active_playlist', None) or '(none)'
            lab = f'PL: {cur[:16]} ▼'
            plw = len(lab) * 6 + 14
            d.rectangle([rx0 + 4, sy, rx0 + 4 + plw, sy + 18],
                        outline=_HOT, fill=(0, 0, 0, 60))
            d.text((rx0 + 11, sy + 4), lab, font=self._font_s, fill=_HOT)
            regions.append((rx0 + 4, sy, rx0 + 4 + plw, sy + 18,
                            'pl_flyout', None))
            nx = rx0 + 4 + plw + 6
            d.rectangle([nx, sy, nx + 46, sy + 18], outline=_DIM,
                        fill=(0, 0, 0, 60))
            d.text((nx + 6, sy + 4), '+ NEW', font=self._font_s, fill=_DIM)
            regions.append((nx, sy, nx + 46, sy + 18, 'pl_new', None))
            sy += 24
        # Search box.
        d.rectangle([rx0 + 4, sy, rx1 - 4, sy + 18],
                    outline=_EDGE if self._search_focused else _DIM)
        q = self._query
        hint = (q + ('|' if self._search_focused else '')) if q else \
            ('» |' if self._search_focused else '» search…')
        d.text((rx0 + 10, sy + 4), hint[:38], font=self._font_s,
               fill=_TEXT if q else _FAINT)
        regions.append((rx0 + 4, sy, rx1 - 4, sy + 18, 'focus_search', None))
        list_top = sy + 26
        list_bot = h - gap
        # Accordion crates: nested tree along the open path (depth-tinted).
        buckets = (self._c.buckets()
                   if mode == 'library' and hasattr(self._c, 'buckets') else [])
        if len(buckets) > 1:
            hh = 18
            open_b = getattr(self._c, 'bucket', None)
            depth_fill = ((16, 20, 34, 255), (19, 23, 42, 255),
                          (23, 24, 50, 255), (27, 24, 56, 255),
                          (31, 25, 62, 255), (35, 26, 68, 255))
            hy = list_top
            for bkey, blabel, bcount, excluded, depth in buckets:
                if hy + hh > list_bot - 40:      # keep room for some rows
                    break
                is_open = bkey == open_b or (bkey is None and open_b is None)
                indent = 10 * depth
                if is_open:
                    d.rectangle([rx0, hy, rx1, hy + hh - 2],
                                fill=(0, 45, 90, 255), outline=_EDGE)
                else:
                    d.rectangle([rx0, hy, rx1, hy + hh - 2],
                                fill=depth_fill[min(depth, 5)],
                                outline=(40, 52, 78, 255))
                on_chain = (bkey is not None and open_b is not None
                            and (open_b == bkey
                                 or open_b.startswith(bkey + '/')))
                lcol = (_WARN if excluded else
                        (_TEXT if (is_open or on_chain) else _DIM))
                tag = '× ' if excluded else ''
                arrow = '▼' if (is_open or on_chain) else '►'
                d.text((rx0 + 6 + indent, hy + 3),
                       f'{arrow} {tag}{blabel[:16]} ({bcount})',
                       font=self._font_s, fill=lcol)
                regions.append((rx0, hy, rx1 - 30, hy + hh - 2,
                                'set_bucket', bkey))
                if bkey is not None:             # exclude eye (not on ALL)
                    ex = rx1 - 24
                    d.rectangle([ex, hy + 2, ex + 18, hy + hh - 4],
                                outline=_WARN if excluded else _DIM)
                    d.text((ex + 5, hy + 3), '×' if excluded else 'o',
                           font=self._font_s,
                           fill=_WARN if excluded else _DIM)
                    regions.append((ex, hy + 2, ex + 18, hy + hh - 4,
                                    'exclude_bucket', bkey))
                hy += hh
            list_top = hy + 2
        # Rows.
        view = self._view()
        self._sel = min(self._sel, max(0, len(view) - 1))
        d.text((rx0 + 4, top + 6 - 0), '', font=self._font_s, fill=_FAINT)
        row_h = 20
        self._rows_visible = max(1, (list_bot - list_top) // row_h)
        self._scroll = max(0, min(max(0, len(view) - self._rows_visible),
                                  self._scroll))
        offset = self._scroll
        playing_name = (f'{artist} - {title}' if artist else title)
        # ARTIST and TITLE get their own columns rather than one joined
        # string: joined text cannot column-align, and on a long list the eye
        # scans one field at a time.  Artist takes the smaller share -- it is
        # the coarser sort key and repeats down the list, so it needs to be
        # readable, not complete.  A length column sits on the right.
        parts_fn = getattr(self._c, 'view_parts', None)
        parts = {i: (a, t, dur) for i, a, t, dur
                 in (parts_fn() if callable(parts_fn) else [])}
        len_w = 34
        name_px = max(60, (rx1 - rx0) - 40 - len_w)
        artist_px = int(name_px * 0.34)
        artist_chars = max(4, artist_px // 6)
        title_chars = max(6, (name_px - artist_px) // 6)
        hx, hy_m = self._mouse_xy
        for i in range(offset, min(len(view), offset + self._rows_visible)):
            idx, name = view[i]
            ry = list_top + (i - offset) * row_h
            is_sel = i == self._sel
            is_live = playing and name == playing_name
            hovered = (rx0 <= hx <= rx1 and ry - 1 <= hy_m <= ry + row_h - 3)
            # Bed order: playing, then selection, then hover on top -- each
            # replaces the last as a *statement*, and hover only lifts what is
            # already there so it never hides what the row was saying.
            bed = _PLAYING_BED if is_live else None
            if is_sel:
                bed = _SEL_ON_PLAYING if is_live else (0, 45, 90, 255)
            if hovered:
                bed = _hover_bed(bed)
            if bed is not None:
                d.rectangle([rx0, ry - 1, rx1, ry + row_h - 3], fill=bed)
            if is_live:
                # The drip: the playing row breathes rather than sitting
                # still.  A flat bed says "this one"; a moving one says "this
                # one, right now" -- which is the whole difference between a
                # marker and a readout, and it is the row you look for first
                # every time you glance at the list.
                self._drip_row(d, rx0, ry - 1, rx1, ry + row_h - 3)
            if is_sel:
                d.text((rx0 + 2, ry), '►', font=self._font_s, fill=_HOT)
            ncol = _WAVE_HI if is_live else (_TEXT if is_sel else _DIM)
            artist, title, dur = parts.get(idx, ('', name, 0.0))
            tx = rx0 + 14
            if artist:
                d.text((tx, ry), _mid_ellipsis(artist, artist_chars),
                       font=self._font_s, fill=_muted(ncol))
                d.text((tx + artist_px, ry),
                       _mid_ellipsis(title, title_chars),
                       font=self._font_s, fill=ncol)
            else:
                # Untagged: the filename is the whole line, as it always was.
                d.text((tx, ry),
                       _mid_ellipsis(title or name, artist_chars + title_chars),
                       font=self._font_s, fill=ncol)
            if dur > 0.0:
                d.text((rx1 - 26 - len_w, ry), f'{_fmt_time(dur):>6}',
                       font=self._font_s, fill=_muted(ncol))
            fav = bool(getattr(self._c, 'is_favorite', lambda _i: False)(idx))
            d.text((rx1 - 21, ry - 2), '♥', font=self._font_heart,
                   fill=(255, 80, 160, 255) if fav else (46, 58, 84, 255))
            regions.append((rx1 - 26, ry - 1, rx1 - 3, ry + row_h - 3,
                            'fav_toggle', idx))
            rows.append((rx0, ry - 1, rx1, ry + row_h - 3, i))
        if not view and mode == 'favorites':
            d.text((rx0 + 14, list_top + 8), 'no favorites yet —',
                   font=self._font_s, fill=_FAINT)
            d.text((rx0 + 14, list_top + 22), 'click a track\u2019s ♥ to heart it',
                   font=self._font_s, fill=_FAINT)
        if len(view) > self._rows_visible:
            track_h = list_bot - list_top
            bar_h = max(12, int(track_h * self._rows_visible / len(view)))
            bar_y = list_top + int(track_h * offset / len(view))
            d.rectangle([rx1 - 2, list_top, rx1, list_bot],
                        fill=(30, 40, 60, 255))
            d.rectangle([rx1 - 2, bar_y, rx1, bar_y + bar_h], fill=_DIM)
        # Flyouts + context menu paint over the rows.
        menu_rects: list = []
        if self._sort_open and mode in ('library', 'favorites'):
            opts = (('Artist ↑', 'artist', True), ('Artist ↓', 'artist', False),
                    ('Title ↑', 'title', True), ('Title ↓', 'title', False),
                    ('Scan order', None, True))
            fy = ty + 22
            fx = rx1 - 130
            d.rectangle([fx, fy, rx1 - 4, fy + 6 + 18 * len(opts)],
                        fill=_PANEL, outline=_EDGE)
            cur = getattr(self._c, 'sort', (None, True))
            ry2 = fy + 3
            for lab, k, asc in opts:
                on = cur == (k, asc)
                d.text((fx + 8, ry2), f'{"●" if on else "○"} {lab}',
                       font=self._font_s, fill=_HOT if on else _DIM)
                regions.append((fx, ry2 - 1, rx1 - 4, ry2 + 17,
                                'set_sort', (k, asc)))
                ry2 += 18
        if self._pl_open and mode == 'playlists' and pls is not None:
            names = pls.names() or []
            fy = sy - 2
            d.rectangle([rx0 + 4, fy, rx0 + 200, fy + 6 + 18 * max(1, len(names))],
                        fill=_PANEL, outline=_EDGE)
            ry2 = fy + 3
            for n in names:
                on = n == getattr(self._c, 'active_playlist', None)
                d.text((rx0 + 12, ry2),
                       f'{"●" if on else "○"} {n[:20]} ({len(pls.tracks(n))})',
                       font=self._font_s, fill=_HOT if on else _DIM)
                regions.append((rx0 + 4, ry2 - 1, rx0 + 200, ry2 + 17,
                                'pl_select', n))
                ry2 += 18
            if not names:
                d.text((rx0 + 12, ry2), '(none — use + NEW)',
                       font=self._font_s, fill=_FAINT)
        if self._menu is not None:
            items = self._menu.get('items') or []
            mw, mh = 190, 24 + 20 * len(items)
            mx = max(4, min(int(self._menu['x']), w - mw - 4))
            my = max(4, min(int(self._menu['y']), h - mh - 4))
            d.rectangle([mx, my, mx + mw, my + mh], fill=_PANEL, outline=_HOT)
            d.text((mx + 8, my + 4), str(self._menu.get('name', ''))[:28],
                   font=self._font_s, fill=_FAINT)
            ry2 = my + 22
            for lab, act, payload in items:
                d.text((mx + 10, ry2 + 3), lab, font=self._font_s, fill=_TEXT)
                menu_rects.append((mx, ry2, mx + mw, ry2 + 20, act, payload))
                ry2 += 20

        # Esc-Esc confirmation banner.
        if time.monotonic() < self._close_confirm_deadline:
            d.rectangle([0, h - 34, w, h], fill=_PANEL2)
            d.text((16, h - 26), 'Press ESC again to close the media deck',
                   font=self._font_b, fill=_WARN)

        tag_rects: list = []
        if self._tag_edit is not None:      # modal: drawn last, over everything
            tag_rects = self._draw_tag_editor(d, w, h)

        with self._frame_lock:
            self._regions = regions
            self._rows = rows
            self._menu_rects = menu_rects
            self._tag_rects = tag_rects
        return img

    # The playing row's pulse, in seconds per cycle.  Slow on purpose: this
    # sits behind text you are reading, and anything quicker turns a list into
    # a fairground.  Roughly one breath.
    _DRIP_PERIOD_S = 2.6

    def _drip_row(self, d, x0, y0, x1, y1) -> None:
        """A breathing edge + inner glow on the row that is playing.

        Drawn *over* the bed rather than replacing it, so the purple still
        says "playing" on a frame where the pulse is at its dimmest -- the
        animation is emphasis, never the only signal.  Everything here is
        translucent over an already-opaque bed, which the raster proxy
        composites correctly because the bed underneath is solid.
        """
        t = time.monotonic() / self._DRIP_PERIOD_S
        pulse = 0.5 + 0.5 * abs((t * 2.0) % 2.0 - 1.0)      # 0..1 triangle
        glow = tuple(int(c) for c in _WAVE_HI[:3])
        # A left-edge bar is the anchor: it reads as "here" even at the bottom
        # of the pulse, where a whole-row wash would fade to nothing.
        d.rectangle([x0, y0, x0 + 2, y1], fill=glow + (150 + int(105 * pulse),))
        d.rectangle([x0, y0, x1, y1], outline=glow + (40 + int(90 * pulse),))
        # ...and a soft inner top-light, which is what makes it look lit from
        # within rather than outlined.
        d.rectangle([x0 + 3, y0 + 1, x1 - 1, y0 + max(2, (y1 - y0) // 3)],
                    fill=glow + (14 + int(34 * pulse),))

    def _draw_platter(self, d, cx, cyc, r, playing, pos, dur) -> None:
        """The neon platter, media-deck flavour: cyan glow breathing with a
        slow pulse while playing, grooves, strobe spokes, orbiting sparkles,
        and a progress arc dot riding the rim."""
        if r < 40:
            return
        ring = _EDGE if playing else _DIM
        pulse = 0.5 + 0.5 * abs((time.monotonic() * 0.9) % 2.0 - 1.0)
        glow = 3 if playing else 2
        for i in range(glow, 0, -1):
            a = int((70 + 50 * pulse) / i) if playing else int(50 / i)
            d.ellipse([cx - r - 2 * i, cyc - r - 2 * i,
                       cx + r + 2 * i, cyc + r + 2 * i],
                      outline=ring[:3] + (a,))
        d.ellipse([cx - r, cyc - r, cx + r, cyc + r], fill=(7, 8, 15, 255),
                  outline=ring)
        for gi in range(4, 10):
            gr = int(r * gi / 10)
            d.ellipse([cx - gr, cyc - gr, cx + gr, cyc + gr],
                      outline=(70, 88, 122, 36))
        import math
        ang = 2.0 * math.pi * pos * self._REV_PER_S
        for spoke_a, col in ((ang, _TEXT), (ang + math.pi, (110, 130, 165, 140))):
            sx = cx + (r - 4) * math.cos(spoke_a)
            sy = cyc + (r - 4) * math.sin(spoke_a)
            lx = cx + (r * 0.42) * math.cos(spoke_a)
            ly = cyc + (r * 0.42) * math.sin(spoke_a)
            d.line([lx, ly, sx, sy], fill=col, width=2)
        if playing:
            phase = int(pos * 8)
            for i in range(12):
                if (i * 7 + phase) % 5 >= 2:
                    continue
                sa = i * 2.399963 + ang * 0.5
                sr2 = r * (0.5 + 0.45 * ((i * 37 % 97) / 97.0))
                px = cx + sr2 * math.cos(sa)
                py = cyc + sr2 * math.sin(sa)
                col = (_WAVE_HI, _TEXT, _YELLOW)[i % 3]
                d.line([px - 2, py, px + 2, py], fill=col)
                d.line([px, py - 2, px, py + 2], fill=col)
        # Progress dot riding the rim (a needle on the record's edge).
        if dur > 0:
            import math as _m
            pa = -_m.pi / 2 + 2.0 * _m.pi * min(1.0, pos / dur)
            px = cx + (r + 6) * _m.cos(pa)
            py = cyc + (r + 6) * _m.sin(pa)
            d.ellipse([px - 3, py - 3, px + 3, py + 3],
                      fill=_YELLOW if playing else _DIM)
        # Label: big elapsed time.
        lr = int(r * 0.30)
        lf = tuple(int(c * (0.55 if playing else 0.30)) for c in _EDGE[:3])
        d.ellipse([cx - lr, cyc - lr, cx + lr, cyc + lr], fill=lf + (255,),
                  outline=ring)
        txt = _fmt_time(pos)
        d.text((cx - len(txt) * 5, cyc - 16), txt, font=self._font_h, fill=_TEXT)
        if dur > 0:
            sub = f'of {_fmt_time(dur)}'
            d.text((cx - len(sub) * 3, cyc + 4), sub, font=self._font_s,
                   fill=(8, 24, 34, 255) if playing else _DIM)
