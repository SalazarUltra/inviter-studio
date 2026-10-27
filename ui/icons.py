"""Small native vector-style icons rendered at 3x resolution."""

from functools import lru_cache

import customtkinter as ctk
from PIL import Image, ImageDraw

from ui.theme import COLORS


@lru_cache(maxsize=64)
def icon(name, color=COLORS["muted"], size=20):
    """A CTkImage with a light and a dark variant (it follows the appearance mode); `color` is a (light, dark) pair or a single color string."""
    light, dark = (color[0], color[1]) if isinstance(color, (tuple, list)) else (color, color)
    return ctk.CTkImage(light_image=_draw(name, light), dark_image=_draw(name, dark), size=(size, size))


@lru_cache(maxsize=128)
def _draw(name, color):
    scale = 4
    canvas = Image.new("RGBA", (24 * scale, 24 * scale))
    draw = ImageDraw.Draw(canvas)

    def line(points):
        draw.line([(x * scale, y * scale) for x, y in points], fill=color, width=2 * scale, joint="curve")

    def box(bounds, radius=2):
        draw.rounded_rectangle(
            tuple(value * scale for value in bounds), radius=radius * scale, outline=color, width=2 * scale
        )

    def circle(bounds):
        draw.ellipse(tuple(value * scale for value in bounds), outline=color, width=2 * scale)

    if name == "dashboard":
        for x, y in ((3, 3), (14, 3), (3, 14), (14, 14)):
            box((x, y, x + 7, y + 7))
    elif name == "accounts":
        circle((7, 3, 15, 11))
        draw.arc((3 * scale, 12 * scale, 19 * scale, 25 * scale), 180, 360, fill=color, width=2 * scale)
        line(((19, 8), (22, 8)))
        line(((20.5, 6.5), (20.5, 9.5)))
    elif name == "targets":
        circle((3, 3, 21, 21))
        circle((8, 8, 16, 16))
        line(((15, 9), (22, 2)))
    elif name == "task":
        line(((7, 4), (20, 12), (7, 20), (7, 4)))
    elif name == "warmup":
        line(((13, 2), (4, 13), (11, 13), (10, 22), (20, 9), (13, 9), (13, 2)))
    elif name == "ai":
        line(((12, 2), (15, 9), (22, 12), (15, 15), (12, 22), (9, 15), (2, 12), (9, 9), (12, 2)))
    elif name == "logs":
        box((4, 3, 20, 21))
        line(((8, 8), (16, 8)))
        line(((8, 12), (16, 12)))
        line(((8, 16), (13, 16)))
    elif name == "parsing":
        circle((3, 3, 16, 16))
        line(((15, 15), (22, 22)))
        line(((7, 9), (12, 9)))
        line(((9, 7), (9, 12)))
    elif name == "support":
        line(
            (
                (12, 21),
                (3, 12),
                (2, 8),
                (3, 5),
                (6, 3),
                (9, 3),
                (12, 6),
                (15, 3),
                (18, 3),
                (21, 5),
                (22, 8),
                (21, 12),
                (12, 21),
            )
        )
    elif name == "arrow":
        line(((5, 19), (19, 5)))
        line(((8, 5), (19, 5), (19, 16)))
    elif name == "settings":
        line(((5, 3), (5, 21)))
        line(((12, 3), (12, 21)))
        line(((19, 3), (19, 21)))
        box((3, 6, 7, 10), 1)
        box((10, 13, 14, 17), 1)
        box((17, 5, 21, 9), 1)
    else:
        line(((3, 8), (12, 3), (21, 8), (12, 13), (3, 8)))
        line(((3, 13), (12, 18), (21, 13)))
        line(((3, 18), (12, 23), (21, 18)))
    return canvas
