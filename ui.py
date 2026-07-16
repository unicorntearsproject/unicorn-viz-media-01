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
import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

try:
    import sdl2
    from unicornviz.secondary_gl_window import SecondaryGLWindow
    _SDL_OK = True
except Exception:  # pragma: no cover - headless import guard
    _SDL_OK = False

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except Exception:  # pragma: no cover
    _PIL_OK = False

# Palette — the dj-mixer-01 board language (kept in sync by eye, not import:
# drop-ins stay independent).
_BG      = (8, 10, 18, 255)
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

_FONT_CANDIDATES = (
    '/usr/share/fonts/liberation-mono-fonts/LiberationMono-Bold.ttf',  # Fedora
    '/usr/share/fonts/liberation-mono/LiberationMono-Bold.ttf',
    '/usr/share/fonts/liberation/LiberationMono-Bold.ttf',             # Arch
    '/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf',    # Debian
    '/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf',
)


def _font(size: int):
    if _PIL_OK:
        for path in _FONT_CANDIDATES:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        try:
            return ImageFont.load_default()
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
        self._mouse_scale_x = 1.0
        self._mouse_scale_y = 1.0
        self._sel = 0                     # library selection (view row)
        self._query = ''                  # search filter
        self._search_focused = False
        self._rows_visible = 10
        self._close_confirm_deadline = 0.0
        self._menu: dict | None = None      # right-click context menu
        self._menu_rects: list = []
        self._pl_open = False               # playlist picker flyout
        self._sort_open = False             # sort flyout
        self._frame_id = 0
        self._presented_id = -1
        self._raw: bytes | None = None

        self._font_title = _font(22)
        self._font_h = _font(16)
        self._font_b = _font(12)
        self._font_s = _font(10)

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
            if self._menu is not None:
                self._click_menu(bx, by)
                return
            self._click(bx, by, int(event.button.clicks))
            return

    def _on_key(self, sym: int, mod: int, repeat: bool) -> None:  # noqa: ARG002
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

    def _click(self, x: int, y: int, clicks: int) -> None:
        with self._frame_lock:
            hit = next((r for r in self._regions
                        if r[0] <= x <= r[2] and r[1] <= y <= r[3]), None)
            row = next((r for r in self._rows
                        if r[0] <= x <= r[2] and r[1] <= y <= r[3]), None)
        self._search_focused = False
        if row is not None:
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
        self._menu = {'x': x, 'y': y, 'name': name, 'items': items}

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

        # Keep-playing-on-close checkbox (the window is a remote control —
        # by default the music continues when you dismiss it).
        keep = bool(snap.get('keep_playing_on_close', True))
        kb_y = h - gap - 22
        d.rectangle([lx0 + 6, kb_y, lx0 + 18, kb_y + 12],
                    fill=_HOT if keep else (0, 0, 0, 90),
                    outline=_HOT if keep else _DIM)
        if keep:
            d.line([lx0 + 8, kb_y + 6, lx0 + 11, kb_y + 9], fill=(4, 18, 26, 255))
            d.line([lx0 + 11, kb_y + 9, lx0 + 16, kb_y + 3], fill=(4, 18, 26, 255))
        d.text((lx0 + 24, kb_y + 1), 'keep playing when window is closed',
               font=self._font_s, fill=_TEXT if keep else _DIM)
        regions.append((lx0 + 6, kb_y - 3, lx0 + 240, kb_y + 15,
                        'keep_toggle', None))
        # Crossfade checkbox (VLC backend: true overlapping fade; dim when
        # the subprocess fallback can't do it).
        can_xf = bool(snap.get('can_crossfade', False))
        xf = bool(snap.get('crossfade', False)) and can_xf
        xf_y = kb_y - 40
        xcol = (_HOT if xf else _DIM) if can_xf else _FAINT
        d.rectangle([lx0 + 6, xf_y, lx0 + 18, xf_y + 12],
                    fill=_HOT if xf else (0, 0, 0, 90), outline=xcol)
        if xf:
            d.line([lx0 + 8, xf_y + 6, lx0 + 11, xf_y + 9], fill=(4, 18, 26, 255))
            d.line([lx0 + 11, xf_y + 9, lx0 + 16, xf_y + 3], fill=(4, 18, 26, 255))
        d.text((lx0 + 24, xf_y + 1),
               'crossfade between tracks' + ('' if can_xf else ' (needs VLC)'),
               font=self._font_s, fill=_TEXT if xf else xcol)
        if can_xf:
            regions.append((lx0 + 6, xf_y - 3, lx0 + 240, xf_y + 15,
                            'crossfade_toggle', None))

        # Corner-platter ambience checkbox (main-output overlay).
        amb = bool(snap.get('ambience', True))
        ab_y = kb_y - 20
        d.rectangle([lx0 + 6, ab_y, lx0 + 18, ab_y + 12],
                    fill=_HOT if amb else (0, 0, 0, 90),
                    outline=_HOT if amb else _DIM)
        if amb:
            d.line([lx0 + 8, ab_y + 6, lx0 + 11, ab_y + 9], fill=(4, 18, 26, 255))
            d.line([lx0 + 11, ab_y + 9, lx0 + 16, ab_y + 3], fill=(4, 18, 26, 255))
        d.text((lx0 + 24, ab_y + 1), 'corner platter on the visualizer',
               font=self._font_s, fill=_TEXT if amb else _DIM)
        regions.append((lx0 + 6, ab_y - 3, lx0 + 240, ab_y + 15,
                        'ambience_toggle', None))

        # Library pane (right column): LIBRARY | PLAYLISTS tabs, accordion
        # crates with per-crate exclude toggles, sort flyout, playlist picker.
        d.rectangle([rx0 - 4, top, rx1 + 4, h - gap],
                    fill=(13, 16, 28, 255), outline=(40, 52, 78, 255))
        mode = getattr(self._c, 'mode', 'library')
        pls = getattr(self._c, 'playlists', None)
        ty = top + 6
        tabx = rx0 + 4
        for label, m in (('LIBRARY', 'library'), ('PLAYLISTS', 'playlists')):
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
        # Sort flyout icon at the right edge of the tab row (library mode).
        if mode == 'library':
            sox = rx1 - 26
            skey = getattr(self._c, 'sort', (None, True))[0]
            scol = _HOT if (self._sort_open or skey) else _DIM
            d.rectangle([sox, ty, sox + 22, ty + 18], outline=scol)
            d.text((sox + 4, ty + 4), '↑↓', font=self._font_s, fill=scol)
            regions.append((sox, ty, sox + 22, ty + 18, 'toggle_sort', None))
        sy = ty + 24
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
        # Accordion crates (library mode with real subfolders).
        buckets = (self._c.buckets()
                   if mode == 'library' and hasattr(self._c, 'buckets') else [])
        if len(buckets) > 1:
            hh = 18
            open_b = getattr(self._c, 'bucket', None)
            open_pos = next((i for i, b in enumerate(buckets)
                             if b[0] == open_b), 0)
            for i, (bkey, blabel, bcount, excluded) in enumerate(buckets):
                is_open = i == open_pos
                hy = (list_top + i * hh if i <= open_pos
                      else list_bot - (len(buckets) - i) * hh)
                if is_open:
                    d.rectangle([rx0, hy, rx1, hy + hh - 2],
                                fill=(0, 45, 90, 255), outline=_EDGE)
                else:
                    d.rectangle([rx0, hy, rx1, hy + hh - 2],
                                fill=(16, 20, 34, 255),
                                outline=(40, 52, 78, 255))
                lcol = (_WARN if excluded else
                        (_TEXT if is_open else _DIM))
                tag = '× ' if excluded else ''
                d.text((rx0 + 6, hy + 3),
                       f'{"▼" if is_open else "►"} {tag}{blabel[:16]} ({bcount})',
                       font=self._font_s, fill=lcol)
                regions.append((rx0, hy, rx1 - 30, hy + hh - 2,
                                'set_bucket', bkey))
                if bkey is not None:       # exclude eye (not on ALL)
                    ex = rx1 - 24
                    d.rectangle([ex, hy + 2, ex + 18, hy + hh - 4],
                                outline=_WARN if excluded else _DIM)
                    d.text((ex + 5, hy + 3), '×' if excluded else 'o',
                           font=self._font_s,
                           fill=_WARN if excluded else _DIM)
                    regions.append((ex, hy + 2, ex + 18, hy + hh - 4,
                                    'exclude_bucket', bkey))
            list_top = list_top + (open_pos + 1) * hh + 2
            list_bot = list_bot - (len(buckets) - open_pos - 1) * hh
        # Rows.
        view = self._view()
        self._sel = min(self._sel, max(0, len(view) - 1))
        d.text((rx0 + 4, top + 6 - 0), '', font=self._font_s, fill=_FAINT)
        row_h = 20
        self._rows_visible = max(1, (list_bot - list_top) // row_h)
        offset = max(0, min(max(0, len(view) - self._rows_visible),
                            self._sel - self._rows_visible // 2))
        playing_name = (f'{artist} - {title}' if artist else title)
        for i in range(offset, min(len(view), offset + self._rows_visible)):
            idx, name = view[i]
            ry = list_top + (i - offset) * row_h
            is_sel = i == self._sel
            is_live = playing and name == playing_name
            if is_sel:
                d.rectangle([rx0, ry - 1, rx1, ry + row_h - 3],
                            fill=(0, 45, 90, 255))
                d.text((rx0 + 2, ry), '►', font=self._font_s, fill=_HOT)
            ncol = _WAVE_HI if is_live else (_TEXT if is_sel else _DIM)
            maxr = max(8, (rx1 - rx0 - 20) // 6)
            label = name if len(name) <= maxr else name[:maxr - 1] + '…'
            d.text((rx0 + 14, ry), label, font=self._font_s, fill=ncol)
            rows.append((rx0, ry - 1, rx1, ry + row_h - 3, i))
        if len(view) > self._rows_visible:
            track_h = list_bot - list_top
            bar_h = max(12, int(track_h * self._rows_visible / len(view)))
            bar_y = list_top + int(track_h * offset / len(view))
            d.rectangle([rx1 - 2, list_top, rx1, list_bot],
                        fill=(30, 40, 60, 255))
            d.rectangle([rx1 - 2, bar_y, rx1, bar_y + bar_h], fill=_DIM)
        # Flyouts + context menu paint over the rows.
        menu_rects: list = []
        if self._sort_open and mode == 'library':
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

        with self._frame_lock:
            self._regions = regions
            self._rows = rows
            self._menu_rects = menu_rects
        return img

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
