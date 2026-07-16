"""Platter ambience — a mini now-spinning platter on the main output.

While the media player plays, a small neon platter (glow, grooves, strobe
spoke, rim progress dot) with the track title floats in a corner of the
audience output — display-only, projectM-overlay style.  Rendered as a small
PIL image (rebuilt ~12 fps, cached between), uploaded to a moderngl texture
and alpha-blit on the app's GL context inside the subsystem
``render_overlay`` hook (the banner-01 mechanism).

Toggle: the "corner platter ambience" checkbox in the Media Deck, config
default ``[media] ambience = true``; corner via ``ambience_corner``
('br'/'bl'/'tr'/'tl').
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

log = logging.getLogger(__name__)

try:
    import moderngl
    import numpy as np
    from PIL import Image, ImageDraw
    _GL_OK = True
except Exception:  # pragma: no cover - headless guard
    _GL_OK = False

_EDGE = (0, 180, 255, 255)
_TEXT = (210, 225, 245, 255)
_DIM = (110, 130, 165, 255)
_YELLOW = (255, 210, 70, 255)
_WAVE_HI = (0, 200, 255, 255)

_W, _H = 300, 120          # canvas of the ambience card
_R = 46                    # platter radius
_REV_PER_S = 0.45
_REBUILD_S = 1.0 / 12.0    # raster refresh rate (blit still runs per frame)


class MediaAmbience:
    """Builds + blits the corner platter card on the app's GL context."""

    def __init__(self, ctx: 'moderngl.Context', font_b, font_s,
                 corner: str = 'br') -> None:
        self._ctx = ctx
        self._font_b = font_b
        self._font_s = font_s
        self._corner = corner if corner in ('br', 'bl', 'tr', 'tl') else 'br'
        self._tex = None
        self._next_build = 0.0
        self._program = ctx.program(
            vertex_shader='''
#version 330
// Vertex shader — fullscreen-quad slice in pixel-mapped clip space.
in vec2 in_pos;
in vec2 in_uv;
out vec2 v_uv;
void main() {
    v_uv = in_uv;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
''',
            fragment_shader='''
#version 330
// Fragment shader — textured quad with premultiplied-style alpha tint.
uniform sampler2D tex;
in vec2 v_uv;
out vec4 fragColor;
void main() {
    fragColor = texture(tex, v_uv);
}
''',
        )
        self._vbo = ctx.buffer(reserve=6 * 4 * 4)
        self._vao = ctx.vertex_array(
            self._program, [(self._vbo, '2f 2f', 'in_pos', 'in_uv')])

    def release(self) -> None:
        for obj in (self._tex, self._vbo, self._vao, self._program):
            try:
                if obj is not None:
                    obj.release()
            except Exception:
                pass
        self._tex = None

    # -- card raster ---------------------------------------------------------

    def _build_card(self, snap: dict[str, Any]) -> 'Image.Image':
        img = Image.new('RGBA', (_W, _H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img, 'RGBA')
        pos = float(snap.get('position_s', 0.0) or 0.0)
        dur = float(snap.get('duration_s', 0.0) or 0.0)
        cx, cy = 10 + _R + 6, _H // 2
        pulse = 0.5 + 0.5 * abs((time.monotonic() * 0.9) % 2.0 - 1.0)
        # Soft dark backing so it reads over bright visuals.
        d.rounded_rectangle([0, 8, _W - 1, _H - 8], radius=14,
                            fill=(6, 8, 14, 150))
        for i in (3, 2, 1):
            a = int((60 + 60 * pulse) / i)
            d.ellipse([cx - _R - 2 * i, cy - _R - 2 * i,
                       cx + _R + 2 * i, cy + _R + 2 * i],
                      outline=_EDGE[:3] + (a,))
        d.ellipse([cx - _R, cy - _R, cx + _R, cy + _R], fill=(7, 8, 15, 235),
                  outline=_EDGE)
        for gi in range(4, 9):
            gr = int(_R * gi / 9)
            d.ellipse([cx - gr, cy - gr, cx + gr, cy + gr],
                      outline=(70, 88, 122, 60))
        ang = 2.0 * math.pi * pos * _REV_PER_S
        d.line([cx + (_R * 0.35) * math.cos(ang), cy + (_R * 0.35) * math.sin(ang),
                cx + (_R - 4) * math.cos(ang), cy + (_R - 4) * math.sin(ang)],
               fill=_TEXT, width=2)
        phase = int(pos * 8)
        for i in range(7):
            if (i * 7 + phase) % 5 >= 2:
                continue
            sa = i * 2.399963 + ang * 0.5
            sr = _R * (0.45 + 0.45 * ((i * 37 % 97) / 97.0))
            px, py = cx + sr * math.cos(sa), cy + sr * math.sin(sa)
            col = (_WAVE_HI, _TEXT, _YELLOW)[i % 3]
            d.line([px - 2, py, px + 2, py], fill=col)
            d.line([px, py - 2, px, py + 2], fill=col)
        if dur > 0:                       # rim progress dot
            pa = -math.pi / 2 + 2.0 * math.pi * min(1.0, pos / dur)
            px, py = cx + (_R + 4) * math.cos(pa), cy + (_R + 4) * math.sin(pa)
            d.ellipse([px - 3, py - 3, px + 3, py + 3], fill=_YELLOW)
        lr = int(_R * 0.3)
        d.ellipse([cx - lr, cy - lr, cx + lr, cy + lr],
                  fill=(0, 60, 90, 255), outline=_EDGE)
        # Title / artist to the right of the disc.
        tx = cx + _R + 14
        title = str(snap.get('title', '') or '')[:22]
        artist = str(snap.get('artist', '') or '')[:24]
        d.text((tx, cy - 20), 'NOW SPINNING', font=self._font_s, fill=_DIM)
        d.text((tx, cy - 6), title, font=self._font_b, fill=_WAVE_HI)
        d.text((tx, cy + 12), artist, font=self._font_s, fill=_TEXT)
        return img

    # -- per-frame blit --------------------------------------------------------

    def render(self, width: int, height: int, snap: dict[str, Any]) -> None:
        if not _GL_OK or width <= 0 or height <= 0:
            return
        now = time.monotonic()
        if now >= self._next_build or self._tex is None:
            self._next_build = now + _REBUILD_S
            arr = np.asarray(self._build_card(snap), dtype=np.uint8)
            size = (arr.shape[1], arr.shape[0])
            if self._tex is None or self._tex.size != size:
                if self._tex is not None:
                    self._tex.release()
                self._tex = self._ctx.texture(size, 4)
                self._tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            self._tex.write(arr.tobytes())
        m = 18
        x = m if 'l' in self._corner else width - _W - m
        y = m if 't' in self._corner else height - _H - m

        def px(v: float) -> float:
            return (v / float(width)) * 2.0 - 1.0

        def py(v: float) -> float:
            return 1.0 - (v / float(height)) * 2.0

        x0, x1 = px(x), px(x + _W)
        y0, y1 = py(y), py(y + _H)
        verts = np.array([x0, y0, 0.0, 0.0, x1, y0, 1.0, 0.0, x0, y1, 0.0, 1.0,
                          x1, y0, 1.0, 0.0, x1, y1, 1.0, 1.0, x0, y1, 0.0, 1.0],
                         dtype=np.float32)
        self._vbo.write(verts.tobytes())
        self._tex.use(location=0)
        self._program['tex'].value = 0
        self._ctx.enable(moderngl.BLEND)
        self._vao.render(moderngl.TRIANGLES)
