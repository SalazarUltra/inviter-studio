"""Procedural studio artwork: local, deterministic and independent of network."""

import math
from functools import lru_cache

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFilter


@lru_cache(maxsize=4)
def studio_art(width=390, height=220):
    scale = 2
    w, h = width * scale, height * scale
    image = Image.new("RGB", (w, h), "#171C17")
    pixels = image.load()
    for y in range(h):
        for x in range(w):
            distance = (x / w - 0.57) ** 2 / 0.12 + (y / h - 0.48) ** 2 / 0.35
            glow = math.exp(-distance * 2.4)
            pixels[x, y] = (int(22 + 35 * glow), int(26 + 50 * glow), int(21 + 4 * glow))
    glow_layer = Image.new("RGBA", (w, h))
    glow_draw = ImageDraw.Draw(glow_layer)
    draw = ImageDraw.Draw(image)
    for row in range(14, height, 22):
        for col in range(12, width, 22):
            draw.ellipse((col * scale, row * scale, col * scale + 2, row * scale + 2), fill="#41472A")
    paths = []
    for ring in range(26):
        points = []
        radius = 49 + ring * 2.6
        for step in range(241):
            angle = step / 240 * 2 * math.pi
            ripple = 1 + 0.14 * math.cos(3 * angle + ring * 0.075)
            x = width * 0.53 + radius * math.cos(angle) * ripple
            y = height * 0.51 + radius * 0.61 * math.sin(angle) * ripple
            points.append((x * scale, y * scale))
        paths.append(points)
        glow_draw.line(points, fill=(204, 255, 0, 160), width=4)
    image = Image.alpha_composite(image.convert("RGBA"), glow_layer.filter(ImageFilter.GaussianBlur(8)))
    draw = ImageDraw.Draw(image)
    for i, points in enumerate(paths):
        draw.line(points, fill=(162 + i * 2, 213 + i, 26, 230), width=2)
    cx, cy = width * 0.53 * scale, height * 0.51 * scale
    plane = [
        (cx - 38 * scale, cy - 12 * scale),
        (cx + 43 * scale, cy - 37 * scale),
        (cx + 14 * scale, cy + 40 * scale),
        (cx - 2 * scale, cy + 9 * scale),
        (cx - 38 * scale, cy - 12 * scale),
    ]
    draw.polygon(plane, fill="#D4FF00")
    draw.line([(cx - 2 * scale, cy + 9 * scale), (cx + 29 * scale, cy - 23 * scale)], fill="#4B6316", width=3 * scale)
    image = image.resize((width * 2, height * 2), Image.Resampling.LANCZOS)
    return ctk.CTkImage(light_image=image, dark_image=image, size=(width, height))
