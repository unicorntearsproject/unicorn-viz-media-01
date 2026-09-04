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
import math
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

try:
    import numpy as _np
    _NP_OK = True
except Exception:  # pragma: no cover - chrome layer just goes flat
    _NP_OK = False

# Shared hover-tooltip system (core owns timing + bubble geometry; region
# content stays here) — same machinery the mixer and control room ride.
try:
    from unicornviz.tooltips import (
        TooltipHoverTracker,
        TooltipRegion,
        draw_tooltip_pil,
    )
    _TIPS_OK = True
except Exception:  # pragma: no cover - older core without tooltips
    _TIPS_OK = False

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
_GOLD    = (255, 200, 60, 255)           # the mixer's gold: names of lists,
                                         # "this is the one running" accents
_PINK    = (255, 90, 200, 255)           # the mixer's deck-B pink: the second
                                         # neon on this surface, used sparingly
_ZEBRA   = (18, 22, 38, 255)             # alternate list rows, a whisper up
_SCROLL_TRACK = (24, 32, 50, 255)        # scrollbar wells
_SHADOW  = (0, 0, 0, 110)                # drop shadow under floating panels
# Wheel feel: rows per notch.  One row per notch is why the list read as
# "scrolls too slow" -- three matches what every desktop list does.
_WHEEL_ROWS = 3
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


def _hms(seconds: float) -> str:
    """A running time as ``1h23m`` / ``23m`` / ``0m``.

    Coarse on purpose: list lengths are planning numbers, and seconds on a
    two-hour total is noise that makes the row harder to read, not more
    accurate.  (The mixer's set picker uses the same shape.)
    """
    total = max(0, int(seconds or 0))
    h, m = divmod(total // 60, 60)
    return f'{h}h{m:02d}m' if h else f'{m}m'


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
        # Folder pane (top third of the browser).  The accordion it replaced
        # grew with the tree and, on a 10k library, left room for about two
        # track rows -- so the folders crowded out the thing they select.
        # Fixed split + own scroll keeps the track list a usable size no
        # matter how deep the crates go.  Mirrors the DJ mixer's split pane.
        self._folder_scroll = 0
        self._folder_rect: tuple[int, int, int, int] | None = None
        self._close_confirm_deadline = 0.0
        self._menu: dict | None = None      # right-click context menu
        self._menu_rects: list = []
        self._tag_edit: dict | None = None  # open tag editor
        self._tag_rects: list = []
        self._pl_open = False               # playlist picker flyout
        self._sort_open = False             # sort flyout
        self._filter_open = False           # faceted filter flyout
        self._filter_expanded: str | None = None   # which facet is unfolded
        self._pl_rects: list = []           # playlist flyout rows (right-click)
        # One open flyout at a time owns these: its on-screen rect (wheel
        # routing) and its own scroll offset in rows.  Reset on open/close.
        self._flyout_rect: tuple[int, int, int, int] | None = None
        self._fly_scroll = 0
        self._menu_scroll = 0               # long context menus scroll too
        self._menu_rect: tuple[int, int, int, int] | None = None
        # Draggable things: scrollbar thumbs, the seek bar, the volume
        # slider.  ``_drag`` is (kind, geometry...) while a button is held;
        # ``_scrollbars`` is rebuilt each frame under the frame lock.
        self._drag: tuple | None = None
        self._scrollbars: list = []         # (x0,y0,x1,y1, kind, geom)
        self._chrome_cache: tuple | None = None    # (key, Image)
        self._prompt: dict | None = None    # one-line text prompt modal
        self._prompt_rects: list = []
        # Shared hover tooltips (core system; None on an older core).
        self._tips_tracker = (TooltipHoverTracker() if _TIPS_OK else None)
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
            self._wheel(-int(event.wheel.y) or 1)
            return
        if event.type == sdl2.SDL_MOUSEMOTION:
            # Tracked for the hover highlight; the render thread reads it,
            # which is safe because it is two ints written atomically.
            self._mouse_xy = (int(event.motion.x * self._mouse_scale_x),
                              int(event.motion.y * self._mouse_scale_y))
            if self._drag is not None:
                self._drag_to(*self._mouse_xy)
            return
        if event.type == sdl2.SDL_MOUSEBUTTONUP:
            self._drag = None
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
            if self._prompt is not None:
                self._click_prompt(bx, by)
                return
            if self._tag_edit is not None:
                self._click_tag_editor(bx, by)
                return
            if self._menu is not None:
                self._click_menu(bx, by)
                return
            if self._begin_drag(bx, by):
                return
            self._click(bx, by, int(event.button.clicks))
            return

    def _on_key(self, sym: int, mod: int, repeat: bool) -> None:  # noqa: ARG002
        if self._prompt is not None:
            self._prompt_key(sym, mod)
            return
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
        if sym == sdl2.SDLK_PAGEUP:
            self._move_sel(-max(1, self._rows_visible - 1))
            return
        if sym == sdl2.SDLK_PAGEDOWN:
            self._move_sel(max(1, self._rows_visible - 1))
            return
        if sym == sdl2.SDLK_HOME:
            self._move_sel(-len(self._view()))
            return
        if sym == sdl2.SDLK_END:
            self._move_sel(len(self._view()))
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
            # The keyboard keeps the selection on screen; mouse clicks never
            # scroll the list (so a double-click hits the row it started on).
            vis = max(1, self._rows_visible)
            if self._sel < self._scroll:
                self._scroll = self._sel
            elif self._sel >= self._scroll + vis:
                self._scroll = self._sel - vis + 1

    @staticmethod
    def _in(rect, x: int, y: int) -> bool:
        return (rect is not None
                and rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3])

    def _wheel(self, step: int) -> None:
        """Route a wheel notch to whatever is under the pointer.

        The wheel moves the *view*, never the selection -- turning the wheel
        used to walk the selection one row per notch, which is both the wrong
        thing and a third of the expected speed.  An open flyout or the
        folder pane under the pointer scrolls itself; everything else scrolls
        the track list, :data:`_WHEEL_ROWS` per notch, clamped at render.
        """
        mx, my = self._mouse_xy
        if self._menu is not None:
            self._menu_scroll = max(0, self._menu_scroll + step)
            return
        if self._in(self._flyout_rect, mx, my):
            self._fly_scroll = max(0, self._fly_scroll + step * 2)
            return
        if self._in(self._folder_rect, mx, my):
            self._folder_scroll = max(0, self._folder_scroll + step * 2)
            return
        self._scroll = max(0, self._scroll + step * _WHEEL_ROWS)

    def _begin_drag(self, x: int, y: int) -> bool:
        """Start a drag on a scrollbar thumb, the seek bar, or volume.

        Returns True when the press was claimed.  The press itself already
        acts (jump the scrollbar, seek, set volume), so a click without a
        drag behaves exactly as before -- the drag is the same math fed by
        motion events until the button releases.
        """
        with self._frame_lock:
            bar = next((b for b in self._scrollbars
                        if b[0] <= x <= b[2] and b[1] <= y <= b[3]), None)
            region = next((r for r in reversed(self._regions)
                           if r[4] in ('seek', 'volume')
                           and r[0] <= x <= r[2] and r[1] <= y <= r[3]), None)
        if bar is not None:
            self._drag = ('bar', bar[4], bar[1], bar[3], bar[5])
            self._drag_to(x, y)
            return True
        if region is not None:
            self._drag = (region[4], region[5])
            self._drag_to(x, y)
            return True
        return False

    def _drag_to(self, x: int, y: int) -> None:
        """Apply the live drag at pointer (x, y)."""
        drag = self._drag
        if drag is None:
            return
        try:
            if drag[0] == 'bar':
                _kind, name, y0, y1, geom = drag
                total, visible = geom
                span = max(1, (y1 - y0))
                # The pointer grabs the *middle* of the thumb: map its y to
                # the first visible row so the thumb tracks the finger.
                frac = (y - y0 - (y1 - y0) * visible / max(1, total) / 2) \
                    / max(1, span)
                first = int(round(frac * total))
                first = max(0, min(max(0, total - visible), first))
                if name == 'list':
                    self._scroll = first
                elif name == 'folders':
                    self._folder_scroll = first
                elif name == 'flyout':
                    self._fly_scroll = first
            elif drag[0] == 'seek' and getattr(self._c, 'can_seek', False):
                x0, x1, dur = drag[1]
                frac = min(1.0, max(0.0, (x - x0) / max(1, x1 - x0)))
                self._c.seek_to(frac * dur)
            elif drag[0] == 'volume':
                x0, x1 = drag[1]
                frac = min(1.0, max(0.0, (x - x0) / max(1, x1 - x0)))
                self._c.set_volume(int(frac * 200))
        except Exception as exc:            # pragma: no cover - defensive
            log.debug('media-deck: drag failed: %s', exc)

    def _click(self, x: int, y: int, clicks: int) -> None:
        with self._frame_lock:
            # Reversed: regions are appended in draw order, and what was
            # painted last sits on top -- so an open flyout's rows must win
            # over the search box and rows they cover, not lose to them.
            hit = next((r for r in reversed(self._regions)
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
            elif action == 'auto_level_toggle':
                self._c.toggle_auto_level()
            elif action == 'queue_flyout':
                if hasattr(self._c, 'clear_queue'):
                    self._c.clear_queue()
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
                self._filter_open = False
                self._fly_scroll = 0
            elif action == 'set_sort':
                self._c.set_sort(payload[0], payload[1])
                self._sort_open = False
            elif action == 'toggle_filter_flyout':
                self._filter_open = not self._filter_open
                self._sort_open = False
                self._fly_scroll = 0
            elif action == 'filter_cat':
                self._filter_expanded = (None if self._filter_expanded == payload
                                         else payload)
                if self._filter_expanded is None:
                    self._fly_scroll = 0
                else:
                    # Land the opened facet at the top of the panel, so its
                    # values are on screen rather than below the fold.
                    for fi, (kind, fct, _v) in enumerate(self._filter_rows()):
                        if kind == 'cat' and fct == payload:
                            self._fly_scroll = fi
                            break
            elif action == 'filter_val':
                if hasattr(self._c, 'toggle_filter'):
                    self._c.toggle_filter(payload[0], payload[1])
                    self._sel = 0
                    self._scroll = 0
            elif action == 'filter_clear':
                if hasattr(self._c, 'clear_filters'):
                    self._c.clear_filters()
                    self._sel = 0
                    self._scroll = 0
            elif action == 'pl_flyout':
                self._pl_open = not self._pl_open
                self._fly_scroll = 0
            elif action == 'pl_select':
                self._c.active_playlist = payload
                self._pl_open = False
                self._sel = 0
                if hasattr(self._c, 'bag_reset'):
                    self._c.bag_reset()   # a different list is a new pass
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
            pl_row = next((r for r in self._pl_rects
                           if r[0] <= x <= r[2] and r[1] <= y <= r[3]), None)
            row = next((r for r in self._rows
                        if r[0] <= x <= r[2] and r[1] <= y <= r[3]), None)
        if pl_row is not None:               # a playlist in the open flyout
            name = pl_row[4]
            pls = getattr(self._c, 'playlists', None)
            missing = (len(pls.missing(name))
                       if pls is not None and hasattr(pls, 'missing') else 0)
            items: list[tuple[str, str, Any]] = [
                ('Rename…', 'pl_rename', name),
                ('Duplicate', 'pl_dup', name),
            ]
            if missing:
                items.append((f'Prune {missing} missing', 'pl_prune', name))
            items.append(('Delete playlist…', 'pl_del_arm', name))
            self._menu_scroll = 0
            self._menu = {'x': x, 'y': y, 'name': name, 'items': items}
            return
        if row is None:
            return
        self._sel = row[4]
        view = self._view()
        if not (0 <= self._sel < len(view)):
            return
        idx, name = view[self._sel]
        items: list[tuple[str, str, Any]] = [('Play', 'play_row', idx)]
        # 'idx' is always the *library* index (view() yields (library index,
        # name) regardless of mode), so queuing works identically from the
        # library, a playlist, favorites, or recent -- no mode branch needed.
        if hasattr(self._c, 'queue_add'):
            items.append(('Add to queue', 'queue_add', idx))
        pls = getattr(self._c, 'playlists', None)
        if getattr(self._c, 'mode', 'library') == 'playlists':
            # By path, not by view row: the view can be sorted or filtered, so
            # its row numbers are not the stored ones -- editing by row would
            # hit a different track than the one right-clicked.
            rpath = (self._c.snapshot_path_at(idx)
                     if hasattr(self._c, 'snapshot_path_at') else None)
            items += [('Remove from playlist', 'pl_remove', rpath),
                      ('Move up', 'pl_move', (rpath, -1)),
                      ('Move down', 'pl_move', (rpath, 1))]
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
        self._menu_scroll = 0
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

    @staticmethod
    def _folder_split(list_top: int, list_bot: int,
                      row_h: int) -> tuple[int, int]:
        """Split the browser pane: folders on top, tracks below.

        Returns ``(folder_bot, folder_rows_visible)``.  The folder pane takes
        a *fixed* third of the pane rather than growing with the crate tree,
        which is the whole point: the accordion this replaced expanded until
        a deep library left room for about two track rows.  Floored at two
        folder rows so the pane never vanishes on a short window.
        """
        avail = max(0, list_bot - list_top)
        folder_bot = list_top + max(row_h * 2, avail // 3)
        folder_bot = min(folder_bot, list_bot)
        return folder_bot, max(1, (folder_bot - list_top) // row_h)

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
        self._panel(d, x0, y0, x0 + pw, y0 + ph, fill=_PANEL, radius=10,
                    glow=_HOT)
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

    # -- one-line text prompt (playlist rename) ----------------------------

    def _prompt_key(self, sym: int, mod: int) -> None:
        """Keystrokes while the prompt is open: Enter commits, Esc cancels.
        ASCII only -- same constraint as the tag editor."""
        pr = self._prompt
        if pr is None:
            return
        if sym == sdl2.SDLK_ESCAPE:
            self._prompt = None
            return
        if sym in (sdl2.SDLK_RETURN, sdl2.SDLK_KP_ENTER):
            self._prompt_commit()
            return
        cur = str(pr.get('value', ''))
        if sym == sdl2.SDLK_BACKSPACE:
            pr['value'] = cur[:-1]
        elif 32 <= sym < 127:
            ch = chr(sym)
            if mod & (sdl2.KMOD_LSHIFT | sdl2.KMOD_RSHIFT):
                ch = ch.upper()
            pr['value'] = cur + ch
        else:
            return
        pr['msg'] = ''

    def _prompt_commit(self) -> None:
        pr = self._prompt
        if pr is None:
            return
        action = pr.get('action')
        ok, msg = (action(str(pr.get('value', '')).strip())
                   if callable(action) else (False, 'nothing to do'))
        if not ok:
            pr['msg'] = msg
            return
        self._prompt = None

    def _click_prompt(self, x: int, y: int) -> None:
        with self._frame_lock:
            hit = next((r for r in self._prompt_rects
                        if r[0] <= x <= r[2] and r[1] <= y <= r[3]), None)
        if hit is None:
            return                         # modal: outside clicks do nothing
        if hit[4] == 'prompt_save':
            self._prompt_commit()
        elif hit[4] == 'prompt_cancel':
            self._prompt = None

    def _draw_prompt(self, d, w, h) -> list:
        """The one-field prompt modal; returns its click rects."""
        pr = self._prompt
        if pr is None:
            return []
        pw, ph = 400, 118
        x0 = max(8, (w - pw) // 2)
        y0 = max(8, (h - ph) // 2)
        rects: list = []
        self._panel(d, x0, y0, x0 + pw, y0 + ph, fill=_PANEL, radius=10,
                    glow=_HOT)
        d.text((x0 + 14, y0 + 10), str(pr.get('title', ''))[:40],
               font=self._font_b, fill=_WAVE_HI)
        d.text((x0 + 14, y0 + 28), str(pr.get('label', ''))[:52],
               font=self._font_s, fill=_FAINT)
        bx0, bx1, by = x0 + 14, x0 + pw - 14, y0 + 46
        d.rectangle([bx0, by, bx1, by + 22], fill=_BG, outline=_EDGE)
        val = str(pr.get('value', ''))[-52:]
        d.text((bx0 + 6, by + 5), val, font=self._font_s, fill=_TEXT)
        cx = bx0 + 6 + len(val) * 6
        d.rectangle([cx, by + 4, cx + 1, by + 18], fill=_HOT)
        if pr.get('msg'):
            d.text((x0 + 14, by + 26), str(pr['msg'])[:52],
                   font=self._font_s, fill=_WARN)
        yy = y0 + ph - 28
        for lab, act, col in (('SAVE', 'prompt_save', _HOT),
                              ('CANCEL', 'prompt_cancel', _DIM)):
            bw = len(lab) * 6 + 20
            bx = (x0 + pw - 14 - bw) if act == 'prompt_save' else (x0 + 14)
            d.rectangle([bx, yy, bx + bw, yy + 20], fill=_BG, outline=col)
            d.text((bx + 10, yy + 5), lab, font=self._font_s, fill=col)
            rects.append((bx, yy, bx + bw, yy + 20, act, None))
        return rects

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
            elif action == 'queue_add':
                if hasattr(self._c, 'queue_add'):
                    self._c.queue_add(payload)
                self._menu = None
            elif action == 'add_menu':      # fly out to the playlist choices
                names = pls.names() if pls else []
                items = [(f'→ {n[:20]}', 'add_to', (n, payload)) for n in names]
                items.append(('→ + new playlist', 'add_to', (None, payload)))
                menu['items'] = items
                self._menu_scroll = 0
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
                if pls is not None and self._c.active_playlist and payload:
                    if hasattr(pls, 'remove_path'):
                        pls.remove_path(self._c.active_playlist, payload)
                    else:
                        pls.remove(self._c.active_playlist,
                                   pls.tracks(self._c.active_playlist)
                                   .index(payload))
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
                    mpath, delta = payload
                    if mpath and hasattr(pls, 'move_path'):
                        pls.move_path(self._c.active_playlist, mpath, delta)
                        self._sel = max(0, min(len(self._view()) - 1,
                                               self._sel + delta))
                self._menu = None
            elif action == 'pl_rename':
                pls_name = payload

                def _do_rename(text: str) -> tuple[bool, str]:
                    if pls is None or not hasattr(pls, 'rename'):
                        return False, 'rename needs a newer store'
                    if not pls.rename(pls_name, text):
                        return False, 'name is taken or blank'
                    if self._c.active_playlist == pls_name:
                        self._c.active_playlist = text.strip()
                    return True, ''

                self._prompt = {'title': 'RENAME PLAYLIST',
                                'label': pls_name,
                                'value': str(pls_name),
                                'action': _do_rename, 'msg': ''}
                self._menu = None
            elif action == 'pl_dup':
                if pls is not None and hasattr(pls, 'duplicate'):
                    copy = pls.duplicate(payload)
                    if copy:
                        self._c.active_playlist = copy
                self._menu = None
            elif action == 'pl_prune':
                if pls is not None and hasattr(pls, 'prune_missing'):
                    gone = pls.prune_missing(payload)
                    log.info('media-deck: pruned %d dead path(s) from %r',
                             gone, payload)
                self._menu = None
            elif action == 'pl_del_arm':   # arm: the next click really deletes
                menu['items'] = [('SURE? DELETE PLAYLIST', 'pl_del_do', payload),
                                 ('Cancel', 'del_cancel', None)]
            elif action == 'pl_del_do':
                if pls is not None:
                    pls.delete(payload)
                    if self._c.active_playlist == payload:
                        names = pls.names()
                        self._c.active_playlist = names[0] if names else None
                self._menu = None
        except Exception as exc:
            log.warning('media-deck: menu action %s failed: %s', action, exc)
            self._menu = None

    # -- drawing ------------------------------------------------------------

    def _levels(self) -> tuple[float, float, float, float]:
        """(bass, mid, treble, beat) from the controller, always 4-long."""
        lv = getattr(self._c, 'audio_levels', None) or ()
        try:
            out = tuple(float(v) for v in lv)[:4]
        except (TypeError, ValueError):
            out = ()
        return out + (0.0,) * (4 - len(out))

    def _tip(self, tips: list, x0, y0, x1, y1, text: str) -> None:
        """Register a hover tooltip region (no-op on a core without them)."""
        if _TIPS_OK:
            tips.append(TooltipRegion(
                (int(x0), int(y0), int(x1 - x0), int(y1 - y0)), text))

    def _render_tooltip(self, d, tips: list, w: int, h: int) -> None:
        """Feed the shared hover tracker and draw the active bubble.

        Motion + timing + draw all happen on the render thread, fed by the
        cursor position the event handler last captured, so the core tracker
        stays single-threaded as it expects.  A modal or menu on screen
        suppresses tips: the bubble would float over an unrelated surface.
        """
        tracker = self._tips_tracker
        if tracker is None or not tips:
            return
        if (self._menu is not None or self._tag_edit is not None
                or self._prompt is not None):
            tracker.reset()
            return
        mx, my = self._mouse_xy
        if mx < 0:
            tracker.reset()
            return
        tracker.on_motion(mx, my, tips)
        tip = tracker.tick()
        if tip is not None and tracker.anchor is not None:
            draw_tooltip_pil(
                d, tip.text, tracker.anchor, (w, h), font=self._font_s,
                bg=_PANEL2[:3], border=_EDGE[:3], text_color=_TEXT[:3])

    def _chrome(self, w: int, h: int) -> 'Image.Image':
        """The window's base coat: a vertical navy fade + faint scanlines.

        Built once per window size with numpy and cached -- per-frame it is a
        single ``copy()``.  Without numpy the deck falls back to the old flat
        fill; nothing else changes.
        """
        key = (w, h)
        cached = self._chrome_cache
        if cached is not None and cached[0] == key:
            return cached[1].copy()
        if not _NP_OK:
            img = Image.new('RGBA', (w, h), _BG)
            self._chrome_cache = (key, img)
            return img.copy()
        top = _np.array([13, 15, 30], dtype=_np.float32)
        bot = _np.array([4, 5, 11], dtype=_np.float32)
        t = _np.linspace(0.0, 1.0, h, dtype=_np.float32)[:, None]
        rows = top[None, :] * (1.0 - t) + bot[None, :] * t
        arr = _np.empty((h, w, 4), dtype=_np.uint8)
        arr[:, :, :3] = _np.repeat(rows[:, None, :], w, axis=1).astype(_np.uint8)
        arr[:, :, 3] = 255
        arr[::3, :, :3] = (arr[::3, :, :3] * 0.88).astype(_np.uint8)  # scanlines
        img = Image.fromarray(arr, 'RGBA')
        self._chrome_cache = (key, img)
        return img.copy()

    @staticmethod
    def _panel(d, x0, y0, x1, y1, *, fill=_PANEL, outline=(40, 52, 78, 255),
               radius=10, shadow=True, glow=None) -> None:
        """A rounded panel with a drop shadow and an inner top-light.

        The shadow is what makes panels *float* instead of abutting; the 1px
        highlight along the inside top edge is what makes them look lit from
        above rather than cut from flat plastic.
        """
        if shadow:
            d.rounded_rectangle([x0 + 3, y0 + 4, x1 + 3, y1 + 4],
                                radius=radius, fill=_SHADOW)
        d.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill,
                            outline=glow or outline)
        hi = tuple(min(255, c + 26) for c in fill[:3]) + (255,)
        d.line([x0 + radius, y0 + 1, x1 - radius, y0 + 1], fill=hi)

    def _v_scrollbar(self, d, x: int, y0: int, y1: int, total: int,
                     visible: int, first: int, name: str,
                     sbars: list) -> None:
        """A draggable vertical scrollbar: rounded well, neon thumb.

        Registers a 12px-wide grab strip (wider than the 5px visual -- a
        2px sliver was technically clickable and practically not) in *sbars*
        as a drag target; press-and-drag anywhere in the strip maps pointer
        y straight to the first visible row.
        """
        if total <= visible or y1 - y0 < 20:
            return
        track_h = y1 - y0
        thumb_h = max(16, int(track_h * visible / total))
        thumb_y = y0 + int((track_h - thumb_h)
                           * first / max(1, total - visible))
        mx, my = self._mouse_xy
        grabbed = (self._drag is not None and self._drag[0] == 'bar'
                   and self._drag[1] == name)
        hov = x - 8 <= mx <= x + 5 and y0 <= my <= y1
        d.rounded_rectangle([x, y0, x + 5, y1], radius=3, fill=_SCROLL_TRACK)
        tcol = _WAVE_HI if (grabbed or hov) else _DIM
        if grabbed or hov:
            d.rounded_rectangle([x - 1, thumb_y - 1, x + 6, thumb_y + thumb_h + 1],
                                radius=4, outline=_WAVE_HI[:3] + (90,))
        d.rounded_rectangle([x, thumb_y, x + 5, thumb_y + thumb_h],
                            radius=3, fill=tcol)
        sbars.append((x - 8, y0, x + 6, y1, name, (total, visible)))

    def _render_ui(self) -> 'Image.Image':
        w, h = self._width, self._height
        img = self._chrome(w, h)
        d = ImageDraw.Draw(img, 'RGBA')
        regions: list = []
        rows: list = []
        tips: list = []
        sbars: list = []
        pl_rects: list = []
        self._flyout_rect = None
        snap = self._c.snapshot()
        playing = bool(snap.get('is_playing'))
        pos = float(snap.get('position_s', 0.0) or 0.0)
        dur = float(snap.get('duration_s', 0.0) or 0.0)
        levels = self._levels()

        # Header band: glowing wordmark, pink accent on the '//', a status
        # chip, and a light sweep riding the rule — brighter when the room is
        # loud, a faint patrol when it is quiet.
        d.rectangle([0, 0, w, 46], fill=_PANEL2)
        d.line([0, 46, w, 46], fill=_EDGE)
        if playing:
            seg = max(60, w // 6)
            sx = int((time.monotonic() * 0.09 % 1.0) * (w + seg)) - seg
            a = 90 + int(140 * levels[0])
            d.line([max(0, sx), 46, min(w, sx + seg), 46],
                   fill=_WAVE_HI[:3] + (min(255, a),), width=2)
            px2 = int(((time.monotonic() * 0.09 + 0.45) % 1.0) * (w + seg)) - seg
            d.line([max(0, px2), 46, min(w, px2 + seg), 46],
                   fill=_PINK[:3] + (min(200, 40 + int(120 * levels[3])),),
                   width=2)
        tx0 = 16
        for seg_text, seg_col in (('UNICORN VIZ', _EDGE), ('  //  ', _PINK),
                                  ('MEDIA DECK', _WAVE_HI)):
            # A soft echo under each word is what turns text into neon.
            d.text((tx0 + 1, 13), seg_text, font=self._font_title,
                   fill=seg_col[:3] + (70,))
            d.text((tx0, 12), seg_text, font=self._font_title, fill=seg_col)
            try:
                tx0 += int(d.textlength(seg_text, font=self._font_title))
            except Exception:
                tx0 += len(seg_text) * 11
        status = str(snap.get('status', 'off') or 'off').upper()
        scol = _HOT if playing else _DIM
        chip_w = len(status) * 7 + 26
        d.rounded_rectangle([w - chip_w - 12, 10, w - 12, 34], radius=12,
                            fill=(0, 0, 0, 70), outline=scol)
        dot_pulse = 0.5 + 0.5 * abs((time.monotonic() * 1.2) % 2.0 - 1.0)
        d.ellipse([w - chip_w - 2, 18, w - chip_w + 6, 26],
                  fill=(scol[:3] + (120 + int(135 * dot_pulse),)) if playing
                  else scol)
        d.text((w - chip_w + 12, 16), status, font=self._font_b, fill=scol)
        # Up-next badge: only when something is actually queued, so an empty
        # queue costs nothing and never competes with the status chip for
        # attention.
        qn = int(getattr(self._c, 'queue_len', 0) or 0)
        if qn:
            qlabel = f'▶ {qn} queued'
            qw = len(qlabel) * 7 + 20
            qx1 = w - chip_w - 20
            d.rounded_rectangle([qx1 - qw, 10, qx1, 34], radius=12,
                                fill=(0, 0, 0, 70), outline=_PINK)
            d.text((qx1 - qw + 10, 16), qlabel, font=self._font_b, fill=_PINK)
            regions.append((qx1 - qw, 10, qx1, 34, 'queue_flyout', None))
            prev_fn = getattr(self._c, 'queue_preview', None)
            preview = prev_fn(5) if callable(prev_fn) else []
            tip_lines = ['Up next:'] + [f'{i + 1}. {n}'
                                        for i, n in enumerate(preview)]
            if qn > len(preview):
                tip_lines.append(f'…and {qn - len(preview)} more')
            tip_lines.append('\nClick to clear the queue')
            self._tip(tips, qx1 - qw, 10, qx1, 34, '\n'.join(tip_lines))

        # Split: deck pane (left ~58%) | library (right), both floating.
        gap = 8
        deck_w = int(w * 0.58)
        lx0, lx1 = gap, deck_w - gap
        rx0, rx1 = deck_w + gap, w - gap
        top = 46 + gap
        self._panel(d, gap - 4, top, deck_w - 4, h - gap, fill=_PANEL)
        # Accent strip: cyan fading to pink, the mixer's two decks in one bar.
        strip_w = deck_w - gap
        for si in range(16):
            f = si / 15.0
            col = tuple(int(_EDGE[c] * (1 - f) + _PINK[c] * f) for c in range(3))
            sx0 = gap - 4 + 10 + int((strip_w - 20) * si / 16)
            sx1 = gap - 4 + 10 + int((strip_w - 20) * (si + 1) / 16)
            d.rectangle([sx0, top + 2, sx1, top + 4], fill=col + (255,))

        # Platter — the showpiece.  Slightly smaller than it used to be so the
        # spectrum ring around it has air to breathe into.
        pl_h = int(h * 0.46)
        plt_cx, plt_cy = (lx0 + lx1) // 2, top + 14 + pl_h // 2
        plt_r = min(pl_h // 2 - 18, (lx1 - lx0) // 2 - 30)
        self._draw_platter(d, plt_cx, plt_cy, plt_r, playing, pos, dur, levels)
        # The dead rectangles flanking a round platter in a square panel used
        # to just sit there as flat panel colour -- a pair of mirrored EQ
        # stacks plus a drift of rising sparks puts something alive in them.
        self._draw_side_show(d, lx0, lx1, plt_cx, plt_cy, plt_r, playing,
                             levels, getattr(self._c, 'audio_spectrum', ()))
        cy = top + 24 + pl_h

        # Now playing block.
        title = str(snap.get('title', '') or '') or '—'
        artist = str(snap.get('artist', '') or '')
        album = str(snap.get('album', '') or '')
        genre = str(snap.get('genre', '') or '')
        year = str(snap.get('year', '') or '')
        prev_t = str(snap.get('previous_title', '') or '')
        prev_a = str(snap.get('previous_artist', '') or '')
        maxc = max(10, (lx1 - lx0 - 8) // 10)
        d.text((lx0 + 6, cy), title[:maxc], font=self._font_h, fill=_WAVE_HI)
        np_top = cy
        cy += 22
        line2 = artist + (f'  ·  {album}' if album else '')
        if year:
            line2 += f'  ({year})'
        d.text((lx0 + 6, cy), line2[:maxc + 14] or ' ', font=self._font_b,
               fill=_TEXT)
        cy += 18
        if prev_t:
            d.text((lx0 + 6, cy), f'prev: {prev_a} - {prev_t}'[:maxc + 20],
                   font=self._font_s, fill=_FAINT)
        cy += 18
        self._tip(tips, lx0 + 6, np_top, lx1 - 6, cy - 2,
                  '\n'.join(x for x in (
                      title,
                      f'by {artist}' if artist else '',
                      f'on {album}' if album else '',
                      ' · '.join(v for v in (genre, year) if v),
                      f'prev: {prev_a} - {prev_t}' if prev_t else '') if x))

        # Progress bar (click-to-seek on the VLC backend).
        can_seek = bool(getattr(self._c, 'can_seek', False))
        bar_y = cy + 4
        frac = min(1.0, pos / dur) if dur > 0 else 0.0
        d.rounded_rectangle([lx0 + 6, bar_y, lx1 - 6, bar_y + 12], radius=6,
                            fill=(0, 0, 0, 110), outline=_FAINT)
        fill_w = int((lx1 - lx0 - 12) * frac)
        if fill_w > 4:
            base = _WAVE_HI if playing else _DIM
            d.rounded_rectangle([lx0 + 6, bar_y, lx0 + 6 + fill_w, bar_y + 12],
                                radius=6, fill=base)
            if playing and fill_w > 24:
                # Cyan running into pink toward the head, a dozen slices
                # between the rounded caps -- enough for the eye, nothing
                # for the frame budget.
                gx0, gx1 = lx0 + 12, lx0 + fill_w - 1
                segs = 12
                for si in range(segs):
                    f1 = (si + 1) / segs
                    col = tuple(int(_WAVE_HI[c] * (1 - f1) + _PINK[c] * f1)
                                for c in range(3))
                    d.rectangle([gx0 + int((gx1 - gx0) * si / segs), bar_y,
                                 gx0 + int((gx1 - gx0) * f1), bar_y + 12],
                                fill=col + (int(200 * f1),))
            hxp = lx0 + 6 + fill_w
            if playing:                     # glow bead at the playhead
                d.ellipse([hxp - 5, bar_y - 3, hxp + 5, bar_y + 15],
                          outline=_PINK[:3] + (90,))
                d.ellipse([hxp - 3, bar_y + 1, hxp + 3, bar_y + 11],
                          fill=(255, 255, 255, 230))
        if can_seek:
            regions.append((lx0 + 6, bar_y - 4, lx1 - 6, bar_y + 16, 'seek',
                            (lx0 + 6, lx1 - 6, dur)))
            self._tip(tips, lx0 + 6, bar_y - 4, lx1 - 6, bar_y + 16,
                      'Click anywhere on the bar to jump there')
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
        btn_tips = {'prev': 'Previous track — or restart this one past 3 s '
                            '(Ctrl+Alt+B)',
                    'play': 'Play / pause (Ctrl+Alt+M, or Space in here)',
                    'next': 'Next track (Ctrl+Alt+N)'}
        mx_, my_ = self._mouse_xy
        for label, act, col, fill, units in buttons:
            bwu = bw * units + (6 * (units - 1))
            hov = bx <= mx_ <= bx + bwu and cy <= my_ <= cy + th
            face = fill if fill else ((16, 22, 38, 255) if hov
                                      else (0, 0, 0, 90))
            if hov:                          # glow halo: the button answers
                d.rounded_rectangle([bx - 2, cy - 2, bx + bwu + 2, cy + th + 2],
                                    radius=10, outline=col[:3] + (90,))
            d.rounded_rectangle([bx, cy, bx + bwu, cy + th], radius=8,
                                fill=face, outline=col,
                                width=2 if act == 'play' else 1)
            d.text((bx + max(4, (bwu - len(label) * 7) // 2), cy + 10), label,
                   font=self._font_b, fill=col)
            regions.append((bx, cy, bx + bwu, cy + th, act, None))
            self._tip(tips, bx, cy, bx + bwu, cy + th, btn_tips.get(act, ''))
            bx += bwu + 6
        # Repeat (loop) toggle: ALL (lit) -> ONE (lit + 1) -> OFF (dim).
        rep = str(snap.get('repeat', 'all') or 'all')
        ron = rep != 'off'
        hov = bx <= mx_ <= bx + bw and cy <= my_ <= cy + th
        d.rounded_rectangle([bx, cy, bx + bw, cy + th], radius=8,
                            fill=_HOT if ron else
                            ((16, 22, 38, 255) if hov else (0, 0, 0, 90)),
                            outline=_HOT if ron else (_TEXT if hov else _DIM))
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
        self._tip(tips, bx, cy, bx + bw, cy + th,
                  f'Repeat: {rep.upper()}.  ALL wraps the open list, ONE '
                  f'repeats this track, OFF stops at the end of the list '
                  f'(and announces the session is over).')
        bx += bw + 6
        # Shuffle icon toggle (crossing arrows, mixer-style).
        on = bool(snap.get('shuffle', False))
        hov = bx <= mx_ <= bx + bw and cy <= my_ <= cy + th
        d.rounded_rectangle([bx, cy, bx + bw, cy + th], radius=8,
                            fill=_HOT if on else
                            ((16, 22, 38, 255) if hov else (0, 0, 0, 90)),
                            outline=_HOT if on else (_TEXT if hov else _DIM))
        icx, icy = bx + bw // 2, cy + th // 2
        icol = (4, 18, 26, 255) if on else _DIM
        d.line([icx - 9, icy - 5, icx + 7, icy + 5], fill=icol)
        d.line([icx - 9, icy + 5, icx + 7, icy - 5], fill=icol)
        d.line([icx + 7, icy - 5, icx + 2, icy - 5], fill=icol)
        d.line([icx + 7, icy + 5, icx + 2, icy + 5], fill=icol)
        regions.append((bx, cy, bx + bw, cy + th, 'shuffle', None))
        self._tip(tips, bx, cy, bx + bw, cy + th,
                  'Shuffle (Ctrl+Alt+Shift+M) — takes effect on the next '
                  'advance')
        bx += bw + 6
        # Volume slider (VLC backend only).
        if getattr(self._c, 'can_volume', False):
            vol = int(getattr(self._c, 'volume', 100))
            vx1 = min(lx1 - 6, bx + bw)
            d.text((bx, cy - 2), 'VOL', font=self._font_s, fill=_DIM)
            d.rounded_rectangle([bx, cy + 12, vx1, cy + 22], radius=5,
                                fill=(0, 0, 0, 110), outline=_FAINT)
            vw = int((vx1 - bx) * min(1.0, vol / 200.0))
            if vw > 4:
                # Green through gold past unity: past 100% VLC amplifies,
                # and the bar says so before the speakers do.
                vcol = _HOT if vol <= 100 else _YELLOW
                d.rounded_rectangle([bx, cy + 12, bx + vw, cy + 22],
                                    radius=5, fill=vcol)
                d.ellipse([bx + vw - 4, cy + 10, bx + vw + 4, cy + 24],
                          fill=(255, 255, 255, 235))
            d.text((bx, cy + 24), f'{vol}%', font=self._font_s, fill=_FAINT)
            regions.append((bx, cy + 6, vx1, cy + 28, 'volume', (bx, vx1)))
            self._tip(tips, bx, cy + 6, vx1, cy + 28,
                      'Volume, 0–200% (VLC backend).  100% is unity gain; '
                      'past it VLC amplifies.')

        # Options checkboxes (bottom-left stack).  One helper so every box has
        # the same generous full-row hit target — small/adjacent click regions
        # made these feel unreliable.
        def _checkbox(y: int, label: str, on: bool, action: str,
                      enabled: bool = True) -> None:
            col = (_HOT if on else _DIM) if enabled else _FAINT
            d.rounded_rectangle([lx0 + 6, y, lx0 + 20, y + 14], radius=4,
                                fill=_HOT if on else (0, 0, 0, 90),
                                outline=col)
            if on:
                d.line([lx0 + 9, y + 7, lx0 + 12, y + 10], fill=(4, 18, 26, 255))
                d.line([lx0 + 12, y + 10, lx0 + 17, y + 4], fill=(4, 18, 26, 255))
            d.text((lx0 + 27, y + 2), label, font=self._font_s,
                   fill=(_TEXT if on else _DIM) if enabled else _FAINT)
            if enabled:
                regions.append((lx0 + 2, y - 2, lx0 + 31 + len(label) * 6,
                                y + 16, action, None))

        # Four rows now; the step compresses before the stack is allowed to
        # ride up into the transport at small heights.
        cb_bot = h - gap - 4
        cb_top = max(cb_bot - 4 * 17, cy + th + 6)
        cb_step = max(14, (cb_bot - cb_top) // 4)
        can_xf = bool(snap.get('can_crossfade', False))
        can_al = bool(snap.get('can_auto_level', False))
        _checkbox(cb_top,
                  'crossfade between tracks' + ('' if can_xf else ' (needs VLC)'),
                  bool(snap.get('crossfade', False)) and can_xf,
                  'crossfade_toggle', enabled=can_xf)
        _checkbox(cb_top + cb_step,
                  'auto-level track loudness' + ('' if can_al
                                                 else ' (needs VLC)'),
                  bool(snap.get('auto_level', False)) and can_al,
                  'auto_level_toggle', enabled=can_al)
        _checkbox(cb_top + 2 * cb_step, 'corner platter on the visualizer',
                  bool(snap.get('ambience', True)), 'ambience_toggle')
        _checkbox(cb_top + 3 * cb_step, 'keep playing when window is closed',
                  bool(snap.get('keep_playing_on_close', True)), 'keep_toggle')
        self._tip(tips, lx0 + 2, cb_top + cb_step - 2,
                  lx0 + 31 + 25 * 6, cb_top + cb_step + 16,
                  'Loudness-match tracks the way the mixer’s decks do: '
                  'K-weighted RMS over the loudest half, normalized and '
                  'clamped.  Measured once per track in the background and '
                  'cached, so a quiet rip and a slammed master play at the '
                  'same level.')

        # Library pane (right column): LIBRARY | PLAYLISTS tabs, accordion
        # crates with per-crate exclude toggles, sort flyout, playlist picker.
        self._panel(d, rx0 - 4, top, rx1 + 4, h - gap,
                    fill=(13, 16, 28, 255))
        mode = getattr(self._c, 'mode', 'library')
        pls = getattr(self._c, 'playlists', None)
        ty = top + 6
        tabx = rx0 + 4
        for label, m, tip in (
                ('LIBRARY', 'library',
                 'The whole scanned folder, one crate per subfolder.'),
                ('PLAYLISTS', 'playlists',
                 'Your ordered lists.  Playback follows the open one.'),
                ('♥ FAVS', 'favorites',
                 'Every track you have hearted, wherever it lives.'),
                ('RECENT', 'recent',
                 'The last 50 tracks that actually played, newest first.')):
            on = mode == m
            tw2 = len(label) * 6 + 16
            t_hov = tabx <= mx_ <= tabx + tw2 and ty <= my_ <= ty + 18
            if on:
                d.rounded_rectangle([tabx, ty, tabx + tw2, ty + 18], radius=7,
                                    fill=(0, 45, 90, 255), outline=_EDGE)
                # The lit tab throws light: a glow line under the active one.
                d.line([tabx + 3, ty + 20, tabx + tw2 - 3, ty + 20],
                       fill=_EDGE[:3] + (150,), width=2)
            else:
                d.rounded_rectangle([tabx, ty, tabx + tw2, ty + 18], radius=7,
                                    fill=(16, 22, 38, 255) if t_hov else None,
                                    outline=_TEXT if t_hov else _DIM)
            acc = _PINK if m == 'favorites' else _TEXT
            d.text((tabx + 8, ty + 4), label, font=self._font_s,
                   fill=(acc if on else (_TEXT if t_hov else _DIM)))
            regions.append((tabx, ty, tabx + tw2, ty + 18, 'set_mode', m))
            self._tip(tips, tabx, ty, tabx + tw2, ty + 18, tip)
            tabx += tw2 + 4
        # How long this view is, not just how many rows -- a count answers
        # "how much have I got" only if you already know the tracks.  Files
        # whose length never got read count as zero and are flagged, so the
        # total reads as *at least* this long rather than silently short.
        stats_fn = getattr(self._c, 'view_stats', None)
        st = stats_fn() if callable(stats_fn) else {}
        has_stats = bool(st.get('count'))
        nfilt = int(getattr(self._c, 'active_filter_count', 0) or 0)
        if has_stats:
            span = _fmt_time(st.get('duration_s', 0.0))
            unknown = int(st.get('unknown', 0))
            text = f'{st["count"]} tracks · {span}' + ('+' if unknown else '')
            d.text((rx0 + 4, ty + 22), text, font=self._font_s, fill=_FAINT)
            if nfilt:
                # The narrowed count needs a why on the same line, or a
                # filtered library reads as a shrunken one.
                d.text((rx0 + 8 + len(text) * 6, ty + 22),
                       f'· {nfilt} filter{"s" if nfilt > 1 else ""}',
                       font=self._font_s, fill=_HOT)
        elif nfilt:
            d.text((rx0 + 4, ty + 22), f'0 tracks · {nfilt} filter(s) on',
                   font=self._font_s, fill=_WARN)
        # Sort + filter icons at the right edge of the tab row.  Both work in
        # every view now -- FILTER means one thing wherever you are standing.
        sox = rx1 - 26
        skey = getattr(self._c, 'sort', (None, True))[0]
        s_hov = sox <= mx_ <= sox + 22 and ty <= my_ <= ty + 18
        scol = _HOT if (self._sort_open or skey) else (_TEXT if s_hov else _DIM)
        d.rounded_rectangle([sox, ty, sox + 22, ty + 18], radius=6,
                            fill=(16, 22, 38, 255) if s_hov else None,
                            outline=scol)
        d.text((sox + 4, ty + 4), '↑↓', font=self._font_s, fill=scol)
        regions.append((sox, ty, sox + 22, ty + 18, 'toggle_sort', None))
        self._tip(tips, sox, ty, sox + 22, ty + 18,
                  'Sort the view: artist, title, album, year, length, or '
                  'scan order.  A playlist’s stored order is never '
                  'rewritten -- sorting is how you look at it.')
        if hasattr(self._c, 'toggle_filter'):
            fox = sox - 26
            f_hov = fox <= mx_ <= fox + 22 and ty <= my_ <= ty + 18
            fcol = _HOT if (self._filter_open or nfilt) else \
                (_TEXT if f_hov else _DIM)
            d.rounded_rectangle([fox, ty, fox + 22, ty + 18], radius=6,
                                fill=(16, 22, 38, 255) if f_hov else None,
                                outline=fcol)
            d.text((fox + 5, ty + 4), '≡', font=self._font_s, fill=fcol)
            if nfilt:
                d.text((fox + 13, ty + 4), str(min(nfilt, 9)),
                       font=self._font_s, fill=_HOT)
            regions.append((fox, ty, fox + 22, ty + 18,
                            'toggle_filter_flyout', None))
            self._tip(tips, fox, ty, fox + 22, ty + 18,
                      'Facet filters: artist, album, genre, decade.  They '
                      'apply in every view, and playback follows what they '
                      'leave visible.')
        # The search row sits below the stats line when there is one, so the
        # two never share a strip -- inserted, not overlaid.
        sy = ty + (38 if has_stats else 24)
        if mode == 'playlists':
            cur = getattr(self._c, 'active_playlist', None) or '(none)'
            lab = f'PL: {cur[:16]} ▼'
            plw = len(lab) * 6 + 14
            d.rounded_rectangle([rx0 + 4, sy, rx0 + 4 + plw, sy + 18],
                                radius=6, outline=_HOT, fill=(0, 0, 0, 60))
            d.text((rx0 + 11, sy + 4), lab, font=self._font_s, fill=_HOT)
            regions.append((rx0 + 4, sy, rx0 + 4 + plw, sy + 18,
                            'pl_flyout', None))
            nx = rx0 + 4 + plw + 6
            n_hov = nx <= mx_ <= nx + 46 and sy <= my_ <= sy + 18
            d.rounded_rectangle([nx, sy, nx + 46, sy + 18], radius=6,
                                outline=_TEXT if n_hov else _DIM,
                                fill=(0, 0, 0, 60))
            d.text((nx + 6, sy + 4), '+ NEW', font=self._font_s,
                   fill=_TEXT if n_hov else _DIM)
            regions.append((nx, sy, nx + 46, sy + 18, 'pl_new', None))
            sy += 24
        # Search box: a pill, glowing while it owns the keyboard.
        if self._search_focused:
            d.rounded_rectangle([rx0 + 3, sy - 1, rx1 - 3, sy + 19], radius=10,
                                outline=_EDGE[:3] + (80,))
        d.rounded_rectangle([rx0 + 4, sy, rx1 - 4, sy + 18], radius=9,
                            fill=(0, 0, 0, 80),
                            outline=_EDGE if self._search_focused else _DIM)
        q = self._query
        hint = (q + ('|' if self._search_focused else '')) if q else \
            ('» |' if self._search_focused else '» search…')
        d.text((rx0 + 10, sy + 4), hint[:38], font=self._font_s,
               fill=_TEXT if q else _FAINT)
        regions.append((rx0 + 4, sy, rx1 - 4, sy + 18, 'focus_search', None))
        self._tip(tips, rx0 + 4, sy, rx1 - 4, sy + 18,
                  'Type to narrow the list by name.  Enter or Esc leaves the '
                  'box; facet filters (≡) narrow further.')
        list_top = sy + 26
        list_bot = h - gap
        # Accordion crates: nested tree along the open path (depth-tinted).
        buckets = (self._c.buckets()
                   if mode == 'library' and hasattr(self._c, 'buckets') else [])
        self._folder_rect = None
        if len(buckets) > 1:
            hh = 18
            open_b = getattr(self._c, 'bucket', None)
            depth_fill = ((16, 20, 34, 255), (19, 23, 42, 255),
                          (23, 24, 50, 255), (27, 24, 56, 255),
                          (31, 25, 62, 255), (35, 26, 68, 255))
            folder_bot, fvis = self._folder_split(list_top, list_bot, hh)
            self._folder_scroll = max(
                0, min(max(0, len(buckets) - fvis), self._folder_scroll))
            # Keep the open crate on screen when it changes under us.
            for i, b in enumerate(buckets):
                if b[0] == open_b:
                    if i < self._folder_scroll:
                        self._folder_scroll = i
                    elif i >= self._folder_scroll + fvis:
                        self._folder_scroll = i - fvis + 1
                    break
            self._folder_rect = (rx0, list_top, rx1, folder_bot)
            shown = buckets[self._folder_scroll:self._folder_scroll + fvis]
            fol_sb = len(buckets) > fvis
            if fol_sb:                           # scroll affordance
                d.text((rx1 - 44, list_top - 11),
                       f'{self._folder_scroll + 1}-'
                       f'{self._folder_scroll + len(shown)}/{len(buckets)}',
                       font=self._font_s, fill=_FAINT)
                self._v_scrollbar(d, rx1 - 5, list_top, folder_bot - 2,
                                  len(buckets), fvis, self._folder_scroll,
                                  'folders', sbars)
            hy = list_top
            for bkey, blabel, bcount, excluded, depth in shown:
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
                # Empty crates read dimmer than merely-unselected ones, so
                # 'nothing in here' is legible before you click it.
                empty = not excluded and not bcount
                lcol = (_WARN if excluded else
                        (_TEXT if (is_open or on_chain)
                         else (_FAINT if empty else _DIM)))
                tag = '× ' if excluded else ''
                arrow = '▼' if (is_open or on_chain) else '►'
                d.text((rx0 + 6 + indent, hy + 3),
                       f'{arrow} {tag}{blabel[:16]} ({bcount})',
                       font=self._font_s, fill=lcol)
                regions.append((rx0, hy, rx1 - 30, hy + hh - 2,
                                'set_bucket', bkey))
                if bkey is not None:             # exclude eye (not on ALL)
                    # Clear of the scrollbar's grab strip when one is up.
                    ex = rx1 - 38 if fol_sb else rx1 - 24
                    d.rectangle([ex, hy + 2, ex + 18, hy + hh - 4],
                                outline=_WARN if excluded else _DIM)
                    d.text((ex + 5, hy + 3), '×' if excluded else 'o',
                           font=self._font_s,
                           fill=_WARN if excluded else _DIM)
                    regions.append((ex, hy + 2, ex + 18, hy + hh - 4,
                                    'exclude_bucket', bkey))
                    self._tip(tips, ex, hy + 2, ex + 18, hy + hh - 4,
                              'Hide this crate everywhere — library, ALL, '
                              'and playback' if not excluded else
                              'Excluded.  Click to bring this crate back.')
                hy += hh
            list_top = folder_bot + 4        # fixed split, not tree-dependent
        elif mode == 'playlists':
            # Same split for playlists: the list of lists on top, the tracks
            # of the selected one below, so switching lists is a click in
            # view rather than a trip through a dropdown.
            pl_store = getattr(self._c, 'playlists', None)
            names = list(pl_store.names()) if pl_store is not None else []
            if names:
                hh = 18
                cur = getattr(self._c, 'active_playlist', None)
                folder_bot, fvis = self._folder_split(list_top, list_bot, hh)
                self._folder_scroll = max(
                    0, min(max(0, len(names) - fvis), self._folder_scroll))
                if cur in names:             # keep the active list on screen
                    i = names.index(cur)
                    if i < self._folder_scroll:
                        self._folder_scroll = i
                    elif i >= self._folder_scroll + fvis:
                        self._folder_scroll = i - fvis + 1
                self._folder_rect = (rx0, list_top, rx1, folder_bot)
                shown = names[self._folder_scroll:self._folder_scroll + fvis]
                if len(names) > fvis:
                    d.text((rx1 - 44, list_top - 11),
                           f'{self._folder_scroll + 1}-'
                           f'{self._folder_scroll + len(shown)}/{len(names)}',
                           font=self._font_s, fill=_FAINT)
                    self._v_scrollbar(d, rx1 - 5, list_top, folder_bot - 2,
                                      len(names), fvis, self._folder_scroll,
                                      'folders', sbars)
                hy = list_top
                stats_of = getattr(self._c, 'playlist_stats', None)
                for nm in shown:
                    active = nm == cur
                    try:
                        n = len(pl_store.tracks(nm))
                    except Exception:
                        n = 0
                    p_hov = (rx0 <= mx_ <= rx1 - 12
                             and hy <= my_ <= hy + hh - 2)
                    bed_p = ((0, 45, 90, 255) if active
                             else _hover_bed((19, 23, 42, 255)) if p_hov
                             else (19, 23, 42, 255))
                    d.rectangle([rx0, hy, rx1, hy + hh - 2], fill=bed_p,
                                outline=_EDGE if active
                                else (40, 52, 78, 255))
                    pst = stats_of(nm) if callable(stats_of) else {}
                    tail = (f' ({n} · {_hms(pst.get("duration_s", 0.0))})'
                            if pst else f' ({n})')
                    d.text((rx0 + 8, hy + 3), f'{nm[:18]}{tail}',
                           font=self._font_s,
                           fill=_TEXT if active else (_FAINT if not n else _DIM))
                    dead = int(pst.get('missing', 0) or 0)
                    if dead:
                        d.text((rx1 - 54, hy + 3), f'!{dead}',
                               font=self._font_s, fill=_WARN)
                    regions.append((rx0, hy, rx1, hy + hh - 2,
                                    'pl_select', nm))
                    # Right-click manages the list, same menu as the picker.
                    pl_rects.append((rx0, hy, rx1, hy + hh - 2, nm))
                    self._tip(tips, rx0, hy, rx1 - 12, hy + hh - 2,
                              f'{nm}\n{n} track(s) · '
                              f'{_hms(pst.get("duration_s", 0.0))}'
                              + (f'\n{dead} dead path(s) — right-click to '
                                 f'prune' if dead else '')
                              + '\n\nClick opens it.  Right-click: rename, '
                                'duplicate, prune, delete.')
                    hy += hh
                list_top = folder_bot + 4
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
        name_px = max(60, (rx1 - rx0) - 50 - len_w)
        artist_px = int(name_px * 0.34)
        # A character of air before the title column, so the two never read
        # as one run-on string on a narrow pane.
        artist_chars = max(4, (artist_px - 8) // 6)
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
                d.rounded_rectangle([rx0, ry - 1, rx1, ry + row_h - 3],
                                    radius=4, fill=bed)
            elif i % 2:
                # Zebra: a whisper of a bed on alternate rows, so a long
                # list reads in lanes instead of as a wall of lines.
                d.rectangle([rx0, ry - 1, rx1, ry + row_h - 3], fill=_ZEBRA)
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
                d.text((rx1 - 36 - len_w, ry), f'{_fmt_time(dur):>6}',
                       font=self._font_s, fill=_muted(ncol))
            fav = bool(getattr(self._c, 'is_favorite', lambda _i: False)(idx))
            # Hover colours the glyph rather than boxing it: a rectangle round
            # a heart looks like a bug, and the colour doubles as a preview of
            # what clicking will do.
            h_hov = (rx1 - 36 <= hx <= rx1 - 14
                     and ry - 1 <= hy_m <= ry + row_h - 3)
            if fav:
                hcol = (255, 170, 210, 255) if h_hov else (255, 80, 160, 255)
            else:
                hcol = (190, 70, 125, 255) if h_hov else (46, 58, 84, 255)
            d.text((rx1 - 31, ry - 2), '♥', font=self._font_heart, fill=hcol)
            regions.append((rx1 - 36, ry - 1, rx1 - 14, ry + row_h - 3,
                            'fav_toggle', idx))
            self._tip(tips, rx1 - 36, ry - 1, rx1 - 14, ry + row_h - 3,
                      'Heart / unheart this track')
            rows.append((rx0, ry - 1, rx1, ry + row_h - 3, i))
            # Hovering the name shows the file's full metadata.  Registered
            # after the heart so the heart's own tip still wins over it.
            self._tip(tips, rx0, ry - 1, rx1 - 37, ry + row_h - 3,
                      self._track_tip(idx, fav))
        if not view:
            # Say *why* the list is empty -- a blank pane reads as data loss.
            q = self._query.strip()
            nfilt2 = int(getattr(self._c, 'active_filter_count', 0) or 0)
            if q or nfilt2:
                d.text((rx0 + 14, list_top + 8), 'nothing matches —',
                       font=self._font_s, fill=_FAINT)
                d.text((rx0 + 14, list_top + 22),
                       ('clear the search or the filters (≡)' if q and nfilt2
                        else 'clear the search' if q
                        else 'clear the filters (≡)'),
                       font=self._font_s, fill=_FAINT)
            elif mode == 'recent':
                d.text((rx0 + 14, list_top + 8), 'nothing played yet —',
                       font=self._font_s, fill=_FAINT)
                d.text((rx0 + 14, list_top + 22),
                       'every track that plays lands here, newest first',
                       font=self._font_s, fill=_FAINT)
            elif mode == 'playlists':
                d.text((rx0 + 14, list_top + 8), 'this playlist is empty —',
                       font=self._font_s, fill=_FAINT)
                d.text((rx0 + 14, list_top + 22),
                       'right-click library rows → Add to playlist »',
                       font=self._font_s, fill=_FAINT)
        if (not view and mode == 'favorites'
                and not self._query.strip()
                and not getattr(self._c, 'active_filter_count', 0)):
            d.text((rx0 + 14, list_top + 8), 'no favorites yet —',
                   font=self._font_s, fill=_FAINT)
            d.text((rx0 + 14, list_top + 22), 'click a track\u2019s ♥ to heart it',
                   font=self._font_s, fill=_FAINT)
        self._v_scrollbar(d, rx1 - 5, list_top, list_bot - 2, len(view),
                          self._rows_visible, offset, 'list', sbars)
        # Flyouts + context menu paint over the rows.
        menu_rects: list = []
        if self._sort_open:
            opts = (('Artist ↑', 'artist', True), ('Artist ↓', 'artist', False),
                    ('Title ↑', 'title', True), ('Title ↓', 'title', False),
                    ('Album ↑', 'album', True), ('Album ↓', 'album', False),
                    ('Year ↑', 'year', True), ('Year ↓', 'year', False),
                    ('Length ↑', 'length', True), ('Length ↓', 'length', False),
                    ('Scan order', None, True))
            fy = ty + 22
            fx = rx1 - 130
            self._panel(d, fx, fy, rx1 - 4, fy + 6 + 18 * len(opts),
                        fill=_PANEL, radius=8, glow=_EDGE)
            self._flyout_rect = (fx, fy, rx1 - 4, fy + 6 + 18 * len(opts))
            cur = getattr(self._c, 'sort', (None, True))
            ry2 = fy + 3
            for lab, k, asc in opts:
                on = cur == (k, asc)
                o_hov = fx <= mx_ <= rx1 - 4 and ry2 - 1 <= my_ <= ry2 + 17
                if o_hov:
                    d.rectangle([fx + 2, ry2 - 1, rx1 - 6, ry2 + 15],
                                fill=_ROW_HOVER_BED)
                d.text((fx + 8, ry2), f'{"●" if on else "○"} {lab}',
                       font=self._font_s,
                       fill=_HOT if on else (_TEXT if o_hov else _DIM))
                regions.append((fx, ry2 - 1, rx1 - 4, ry2 + 17,
                                'set_sort', (k, asc)))
                ry2 += 18
        if self._filter_open and hasattr(self._c, 'toggle_filter'):
            self._draw_filter_flyout(d, rx0 + 4, ty + 22, rx1 - 4, list_bot,
                                     regions, sbars)
        if self._pl_open and mode == 'playlists' and pls is not None:
            names = pls.names() or []
            fy = sy - 2
            fw = min(280, rx1 - rx0 - 8)
            # Capped to the pane and scrolled within -- a picker taller than
            # the window with the tail unreachable is how it used to be.
            fit = max(3, (list_bot - fy - 30) // 18)
            self._fly_scroll = max(0, min(max(0, len(names) - fit),
                                          self._fly_scroll))
            shown_pl = names[self._fly_scroll:self._fly_scroll + fit]
            ph2 = 6 + 18 * max(1, len(shown_pl)) + 16
            self._panel(d, rx0 + 4, fy, rx0 + 4 + fw, fy + ph2,
                        fill=_PANEL, radius=8, glow=_EDGE)
            self._flyout_rect = (rx0 + 4, fy, rx0 + 4 + fw, fy + ph2)
            ry2 = fy + 3
            stats_of = getattr(self._c, 'playlist_stats', None)
            for n in shown_pl:
                on = n == getattr(self._c, 'active_playlist', None)
                pst = stats_of(n) if callable(stats_of) else {}
                count = pst.get('count', len(pls.tracks(n)))
                # A list's length is what you plan a session around, so it
                # belongs on the row rather than behind a click.
                tail = f' ({count} · {_hms(pst.get("duration_s", 0.0))})' \
                    if pst else f' ({count})'
                dead = int(pst.get('missing', 0) or 0)
                r_hov = (rx0 + 4 <= mx_ <= rx0 + 4 + fw
                         and ry2 - 1 <= my_ <= ry2 + 17)
                if r_hov:
                    d.rectangle([rx0 + 6, ry2 - 1, rx0 + 2 + fw, ry2 + 15],
                                fill=_ROW_HOVER_BED)
                d.text((rx0 + 12, ry2),
                       f'{"●" if on else "○"} {n[:20]}{tail}',
                       font=self._font_s,
                       fill=_HOT if on else (_TEXT if r_hov else _DIM))
                if dead:
                    d.text((rx0 + 12 + (len(f'{n[:20]}{tail}') + 2) * 6, ry2),
                           f'!{dead}', font=self._font_s, fill=_WARN)
                regions.append((rx0 + 4, ry2 - 1, rx0 + 4 + fw, ry2 + 17,
                                'pl_select', n))
                pl_rects.append((rx0 + 4, ry2 - 1, rx0 + 4 + fw, ry2 + 17, n))
                self._tip(tips, rx0 + 4, ry2 - 1, rx0 + 4 + fw, ry2 + 17,
                          f'{n}\n{count} track(s) · '
                          f'{_hms(pst.get("duration_s", 0.0))}'
                          + (f'\n{dead} entry(ies) point at files that no '
                             f'longer exist -- right-click to prune them'
                             if dead else '')
                          + '\n\nClick opens it.  Right-click: rename, '
                            'duplicate, prune, delete.')
                ry2 += 18
            if not names:
                d.text((rx0 + 12, ry2), '(none — use + NEW)',
                       font=self._font_s, fill=_FAINT)
                ry2 += 18
            self._v_scrollbar(d, rx0 + fw - 4, fy + 4, fy + ph2 - 4,
                              len(names), fit, self._fly_scroll,
                              'flyout', sbars)
            d.text((rx0 + 12, ry2), 'right-click a list to manage it',
                   font=self._font_s, fill=_FAINT)
        if self._menu is not None:
            items = self._menu.get('items') or []
            # A menu taller than the window scrolls (the wheel drives it) --
            # 'Add to playlist »' over a long collection used to run off the
            # bottom with the far entries simply unreachable.
            fit_m = max(3, (h - 48) // 20)
            self._menu_scroll = max(0, min(max(0, len(items) - fit_m),
                                           self._menu_scroll))
            shown_m = items[self._menu_scroll:self._menu_scroll + fit_m]
            more_up = self._menu_scroll > 0
            more_dn = self._menu_scroll + fit_m < len(items)
            mw, mh = 190, 24 + 20 * len(shown_m) + (10 if more_up or more_dn
                                                    else 0)
            mx = max(4, min(int(self._menu['x']), w - mw - 4))
            my = max(4, min(int(self._menu['y']), h - mh - 4))
            self._panel(d, mx, my, mx + mw, my + mh, fill=_PANEL,
                        radius=8, glow=_HOT)
            d.text((mx + 8, my + 4), str(self._menu.get('name', ''))[:28],
                   font=self._font_s, fill=_FAINT)
            if more_up:
                d.text((mx + mw - 20, my + 3), '▲', font=self._font_s,
                       fill=_DIM)
            ry2 = my + 22
            for lab, act, payload in shown_m:
                m_hov = mx <= mx_ <= mx + mw and ry2 <= my_ <= ry2 + 20
                if m_hov:
                    d.rectangle([mx + 2, ry2, mx + mw - 2, ry2 + 18],
                                fill=_ROW_HOVER_BED)
                warn = 'SURE?' in lab or lab.startswith('Delete')
                d.text((mx + 10, ry2 + 3), lab, font=self._font_s,
                       fill=_WARN if warn else (_TEXT if m_hov else _DIM))
                menu_rects.append((mx, ry2, mx + mw, ry2 + 20, act, payload))
                ry2 += 20
            if more_dn:
                d.text((mx + mw - 20, ry2 - 2), '▼', font=self._font_s,
                       fill=_DIM)

        # Esc-Esc confirmation banner.
        if time.monotonic() < self._close_confirm_deadline:
            d.rectangle([0, h - 34, w, h], fill=_PANEL2)
            d.text((16, h - 26), 'Press ESC again to close the media deck',
                   font=self._font_b, fill=_WARN)

        tag_rects: list = []
        if self._tag_edit is not None:      # modal: drawn last, over everything
            tag_rects = self._draw_tag_editor(d, w, h)
        prompt_rects: list = []
        if self._prompt is not None:
            prompt_rects = self._draw_prompt(d, w, h)

        self._render_tooltip(d, tips, w, h)

        with self._frame_lock:
            self._regions = regions
            self._rows = rows
            self._menu_rects = menu_rects
            self._tag_rects = tag_rects
            self._prompt_rects = prompt_rects
            self._pl_rects = pl_rects
            self._scrollbars = sbars
        return img

    #: Filter facets, in panel order, with listener-facing labels.
    _FILTER_CATS = (('ARTIST', 'artist'), ('ALBUM', 'album'),
                    ('GENRE', 'genre'), ('DECADE', 'decade'))

    def _filter_rows(self) -> list[tuple]:
        """The filter panel's content as a flat row list.

        ``('cat', facet, label)`` headers with the expanded facet's
        ``('val', facet, value)`` rows inlined after it.  Flat so the panel
        can window any slice of it -- the old draw-until-it-hits-the-bottom
        loop simply cut off a facet with more values than fit (a real
        library has hundreds of artists) with no way to reach the rest.
        """
        out: list[tuple] = []
        for label, facet in self._FILTER_CATS:
            out.append(('cat', facet, label))
            if self._filter_expanded != facet:
                continue
            values = self._c.distinct(facet)
            if not values:
                out.append(('empty', facet, '(nothing tagged yet)'))
            for val in values:
                out.append(('val', facet, val))
        return out

    def _draw_filter_flyout(self, d, x, y, x1, y_max, regions,
                            sbars) -> None:
        """Faceted filter panel over the rows: categories -> ☑/☐ values.

        The mixer's browser filter, media-player facets (no BPM, no stems).
        Values come from the whole library, so narrowing by one facet never
        hides another facet's remaining choices.  The content scrolls (wheel
        over the panel, or the bar on its edge), so every value of a big
        facet is reachable.
        """
        row_h = 18
        frows = self._filter_rows()
        fit = max(3, (y_max - y - 26) // row_h)
        self._fly_scroll = max(0, min(max(0, len(frows) - fit),
                                      self._fly_scroll))
        shown = frows[self._fly_scroll:self._fly_scroll + fit]
        ph = 24 + row_h * len(shown)
        self._panel(d, x, y, x1, y + ph, fill=_PANEL2, radius=8, glow=_EDGE)
        self._flyout_rect = (x, y, x1, y + ph)
        cyf = y + 4
        d.text((x + 8, cyf), 'FILTER', font=self._font_s, fill=_EDGE)
        d.text((x1 - 46, cyf), 'clear', font=self._font_s, fill=_WARN)
        regions.append((x1 - 50, cyf - 1, x1 - 6, cyf + 13,
                        'filter_clear', None))
        cyf += row_h
        mx, my = self._mouse_xy
        for kind, facet, val in shown:
            r_hov = x <= mx <= x1 - 10 and cyf - 1 <= my <= cyf + row_h - 3
            if kind == 'cat':
                expanded = self._filter_expanded == facet
                if r_hov:
                    d.rectangle([x + 3, cyf - 1, x1 - 10, cyf + row_h - 4],
                                fill=_ROW_HOVER_BED)
                d.text((x + 8, cyf), f'{"▼" if expanded else "►"} {val}',
                       font=self._font_s, fill=_TEXT)
                regions.append((x, cyf - 1, x1, cyf + row_h - 3,
                                'filter_cat', facet))
            elif kind == 'empty':
                d.text((x + 24, cyf), val, font=self._font_s, fill=_FAINT)
            else:
                on = self._c.is_filtered(facet, val)
                if r_hov:
                    d.rectangle([x + 17, cyf - 1, x1 - 10, cyf + row_h - 4],
                                fill=_ROW_HOVER_BED)
                d.text((x + 24, cyf), f'{"☑" if on else "☐"} {str(val)[:30]}',
                       font=self._font_s,
                       fill=_HOT if on else (_TEXT if r_hov else _DIM))
                regions.append((x + 18, cyf - 1, x1, cyf + row_h - 3,
                                'filter_val', (facet, val)))
            cyf += row_h
        self._v_scrollbar(d, x1 - 9, y + 4, y + ph - 4, len(frows), fit,
                          self._fly_scroll, 'flyout', sbars)

    def _track_tip(self, idx: int, fav: bool) -> str:
        """The hover card for a library row: every tag the file carries."""
        info_fn = getattr(self._c, 'track_info', None)
        info = info_fn(idx) if callable(info_fn) else {}
        if not info:
            return ''
        lines = [info.get('title') or Path(info.get('path', '')).stem]
        if info.get('artist'):
            lines.append(f'by {info["artist"]}')
        if info.get('album'):
            lines.append(f'on {info["album"]}')
        gy = ' · '.join(v for v in (info.get('genre'), info.get('year')) if v)
        if gy:
            lines.append(gy)
        if info.get('duration_s', 0.0) > 0.0:
            lines.append(_fmt_time(info['duration_s']))
        if info.get('bucket'):
            lines.append(f'crate: {info["bucket"]}')
        if fav:
            lines.append('♥ hearted')
        lines.append('\ndouble-click plays · right-click for more')
        return '\n'.join(lines)

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

    def _draw_side_show(self, d, lx0, lx1, cx, cyc, r, playing, levels,
                        spectrum) -> None:
        """Fill the flanks beside the round platter with something alive.

        A platter is a circle in a rectangular pane, which leaves two dead
        strips of flat panel colour beside it -- real space, visibly empty,
        and previously just background.  Each flank gets a small mirrored
        EQ (bars built from the app's own spectrum, closest to the platter
        = lowest band) and a slow drift of rising sparks for whimsy.  Skips
        entirely when a flank is too narrow to read as anything but a
        rendering glitch -- a small window's platter already fills the pane.
        """
        _bass, _mid, treble, beat = (tuple(levels) + (0.0,) * 4)[:4]
        spec = tuple(spectrum) if spectrum else ()
        t = time.monotonic()
        for side, x0, x1 in (('L', lx0, cx - r - 10), ('R', cx + r + 10, lx1)):
            avail = x1 - x0
            if avail < 28:
                continue
            n = max(3, min(7, avail // 12))
            gap3 = 3
            bar_w = max(3, (avail - (n - 1) * gap3) // n)
            band = (spec[:n] if side == 'L' else spec[-n:]) if spec else ()
            band = tuple(band) + (0.0,) * (n - len(band))
            base_y = cyc + r - 6
            top_y = cyc - r + 6
            span = max(10, base_y - top_y)
            for i in range(n):
                if playing:
                    v = float(band[i])
                else:
                    # Idle: a faint, slow breathing pair instead of a flat
                    # dead stack, so silence doesn't read as "broken".
                    v = 0.05 + 0.04 * math.sin(t * 0.6 + i * 0.8)
                bh = int(span * min(1.0, 0.05 + v * 0.95))
                bx = (x0 + i * (bar_w + gap3) if side == 'L'
                      else x1 - (i + 1) * bar_w - i * gap3)
                col = (_YELLOW if v > 0.85 else _PINK if v > 0.5
                       else _WAVE_HI)
                d.rectangle([bx, base_y - bh, bx + bar_w, base_y],
                           fill=col[:3] + (50 + int(150 * v),))
                if bh > 3:                  # a bright cap, like a peak LED
                    d.rectangle([bx, base_y - bh - 2, bx + bar_w,
                                base_y - bh], fill=(255, 255, 255,
                                                    80 + int(120 * v)))
            if not playing:
                continue
            # Rising sparks: a handful of pseudo-random glints drifting up
            # the flank and fading at the top, a little pop of size on beats.
            for k in range(5):
                seed = k * 13 + (0 if side == 'L' else 97)
                cycle = 3.4 + (seed % 5) * 0.7
                phase = ((t / cycle) + (seed * 0.37) % 1.0) % 1.0
                py = base_y - phase * span
                px = x0 + ((seed * 53) % max(1, avail - 4)) + 2
                alpha = int(150 * math.sin(phase * math.pi))
                if alpha <= 4:
                    continue
                spark_col = _WAVE_HI if seed % 2 == 0 else _PINK
                rad = 1 + int(1.5 * beat)
                d.ellipse([px - rad, py - rad, px + rad, py + rad],
                          fill=spark_col[:3] + (alpha,))

    def _draw_platter(self, d, cx, cyc, r, playing, pos, dur,
                      levels=(0.0, 0.0, 0.0, 0.0)) -> None:
        """The neon platter, media-deck flavour — and now it *hears* the room.

        Cyan glow breathing with a slow pulse while playing, deepened by live
        bass; a beat flash on the outer rim; a radial spectrum ring orbiting
        the record; grooves, strobe spokes (thicker with treble), orbiting
        sparkles, and a progress dot riding the rim.  Silence degrades to the
        old purely time-driven look, so a core without audio levels loses the
        reactivity and nothing else.
        """
        if r < 40:
            return
        bass, _mid, treble, beat = (tuple(levels) + (0.0,) * 4)[:4]
        ring = _EDGE if playing else _DIM
        pulse = 0.5 + 0.5 * abs((time.monotonic() * 0.9) % 2.0 - 1.0)
        glow = 3 if playing else 2
        for i in range(glow, 0, -1):
            a = (int((70 + 50 * pulse + 90 * bass) / i) if playing
                 else int(50 / i))
            d.ellipse([cx - r - 2 * i, cyc - r - 2 * i,
                       cx + r + 2 * i, cyc + r + 2 * i],
                      outline=ring[:3] + (min(255, a),))
        # Beat flash: one extra rim ring that spikes on the detector's beat
        # and decays with it -- the platter visibly *thumps*.
        if playing and beat > 0.05:
            d.ellipse([cx - r - 8, cyc - r - 8, cx + r + 8, cyc + r + 8],
                      outline=_WAVE_HI[:3] + (int(160 * beat),))
        d.ellipse([cx - r, cyc - r, cx + r, cyc + r], fill=(7, 8, 15, 255),
                  outline=ring)
        for gi in range(4, 10):
            gr = int(r * gi / 10)
            d.ellipse([cx - gr, cyc - gr, cx + gr, cyc + gr],
                      outline=(70, 88, 122, 36))
        import math
        ang = 2.0 * math.pi * pos * self._REV_PER_S
        # Vinyl sheen: a soft arc of light gliding around the surface, the
        # way a record catches the booth lamp.  Slow and translucent.
        if playing:
            sheen0 = (time.monotonic() * 24.0) % 360.0
            d.arc([cx - r + 3, cyc - r + 3, cx + r - 3, cyc + r - 3],
                  start=sheen0, end=sheen0 + 46,
                  fill=(200, 235, 255, 46), width=max(2, r // 9))
            d.arc([cx - r + 3, cyc - r + 3, cx + r - 3, cyc + r - 3],
                  start=sheen0 + 180, end=sheen0 + 210,
                  fill=_PINK[:3] + (26,), width=max(2, r // 12))
        # Radial spectrum: the app's live FFT as bars around the record,
        # counter-rotating slowly against the spin.  This is the drip.
        spec = getattr(self._c, 'audio_spectrum', ()) or ()
        if playing and spec:
            n = len(spec) * 2                       # mirrored: full circle
            base_a = -ang * 0.25
            for k in range(n):
                v = float(spec[k if k < len(spec) else n - 1 - k])
                if v <= 0.02:
                    continue
                sa = base_a + 2.0 * math.pi * k / n
                r0 = r + 6
                r1 = r + 6 + 4 + v * 12
                x0p = cx + r0 * math.cos(sa)
                y0p = cyc + r0 * math.sin(sa)
                x1p = cx + r1 * math.cos(sa)
                y1p = cyc + r1 * math.sin(sa)
                col = (_YELLOW if v > 0.8 else _PINK if v > 0.5
                       else _WAVE_HI)
                d.line([x0p, y0p, x1p, y1p],
                       fill=col[:3] + (60 + int(180 * v),), width=2)
        for spoke_a, col in ((ang, _TEXT), (ang + math.pi, (110, 130, 165, 140))):
            sx = cx + (r - 4) * math.cos(spoke_a)
            sy = cyc + (r - 4) * math.sin(spoke_a)
            lx = cx + (r * 0.42) * math.cos(spoke_a)
            ly = cyc + (r * 0.42) * math.sin(spoke_a)
            d.line([lx, ly, sx, sy], fill=col, width=2)
        if playing:
            phase = int(pos * 8)
            # Treble opens the sparkle gate: a busy top end fills the platter
            # with glints, a dark drop leaves only the steady pair.
            gate = 2 + int(2 * min(1.0, max(0.0, treble)))
            for i in range(12):
                if (i * 7 + phase) % 5 >= gate:
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
