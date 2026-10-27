"""Light/dark theme: palette, persistence key, listeners and independence from the density presets."""

import json
import unittest
from pathlib import Path

import customtkinter as ctk

from tests.support import DatabaseCase
from ui import theme
from ui.icons import icon

HEX = "#"
ROOT = Path(__file__).resolve().parent.parent


def luminance(color):
    channels = []
    for index in (1, 3, 5):
        value = int(color[index : index + 2], 16) / 255
        channels.append(value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(first, second):
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class ThemeTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(theme.set_theme, theme.DEFAULT_THEME)
        theme.set_theme(theme.DEFAULT_THEME)


class ThemeStateTests(ThemeTestCase):
    def test_normalize_accepts_only_known_themes(self):
        self.assertEqual(theme.normalize_theme("LIGHT "), "light")
        self.assertEqual(theme.normalize_theme("dark"), "dark")
        for junk in (None, "", "blue", 5):
            self.assertEqual(theme.normalize_theme(junk), "dark")

    def test_set_theme_switches_the_appearance_mode_and_resolves_pairs(self):
        self.assertEqual(theme.set_theme("light"), "light")
        self.assertEqual(ctk.get_appearance_mode(), "Light")
        self.assertEqual(theme.resolve(theme.COLORS["bg"]), theme.COLORS["bg"][0])
        self.assertEqual(theme.set_theme("dark"), "dark")
        self.assertEqual(ctk.get_appearance_mode(), "Dark")
        self.assertEqual(theme.resolve(theme.COLORS["bg"]), theme.COLORS["bg"][1])
        self.assertEqual(theme.resolve("#123456"), "#123456")  # plain strings pass through
        self.assertEqual(theme.set_theme("nonsense"), "dark")

    def test_listeners_are_called_and_can_unsubscribe_and_a_broken_one_does_not_block_others(self):
        seen = []
        unsubscribe = theme.on_theme_change(seen.append)

        def broken(_theme):
            raise RuntimeError("repaint failed")

        theme.on_theme_change(broken)
        self.addCleanup(theme.on_theme_change(lambda _theme: None))
        with self.assertLogs("ui.theme", level="ERROR"):
            theme.set_theme("light")
        self.assertEqual(seen, ["light"])
        unsubscribe()
        with self.assertLogs("ui.theme", level="ERROR"):
            theme.set_theme("dark")
        self.assertEqual(seen, ["light"])
        theme._theme_listeners[:] = [item for item in theme._theme_listeners if item is not broken]

    def test_same_color_compares_tuples_with_lists_from_the_json_theme(self):
        self.assertTrue(theme.same_color(theme.COLORS["accent"], list(theme.COLORS["accent"])))
        self.assertFalse(theme.same_color(theme.COLORS["accent"], theme.COLORS["bg"]))

    def test_density_presets_do_not_know_about_the_theme(self):
        for preset in theme.DENSITY_PRESETS.values():
            self.assertNotIn("theme", " ".join(preset))
        for profile in theme.APP_PROFILES.values():
            if profile:
                self.assertNotIn(theme.THEME_SETTING_KEY, profile)  # a profile never flips light/dark
        theme.set_theme("light")
        self.assertEqual(theme.get_density_preset("compact"), theme.DENSITY_PRESETS["compact"])


class PaletteTests(unittest.TestCase):
    def test_every_color_is_a_light_dark_hex_pair(self):
        for name, pair in theme.COLORS.items():
            with self.subTest(name=name):
                self.assertEqual(len(pair), 2)
                for value in pair:
                    self.assertRegex(value, r"^#[0-9A-F]{6}$")

    def test_light_and_dark_surfaces_really_differ(self):
        for name in ("bg", "surface", "text", "accent"):
            self.assertNotEqual(*theme.COLORS[name], name)
        self.assertGreater(luminance(theme.COLORS["bg"][0]), 0.5)
        self.assertLess(luminance(theme.COLORS["bg"][1]), 0.1)

    def test_text_stays_readable_in_both_themes(self):
        for index, mode in ((0, "light"), (1, "dark")):
            for surface in ("bg", "surface", "input", "elevated"):
                background = theme.COLORS[surface][index]
                for foreground, minimum in (("text", 7), ("muted", 4.5), ("accent", 4.5), ("error", 4.5)):
                    with self.subTest(mode=mode, surface=surface, foreground=foreground):
                        self.assertGreaterEqual(contrast(theme.COLORS[foreground][index], background), minimum)
            with self.subTest(mode=mode, button="primary"):  # the primary button: «bg» text on the accent fill
                self.assertGreaterEqual(contrast(theme.COLORS["bg"][index], theme.COLORS["accent"][index]), 4.5)

    def test_json_theme_has_light_dark_pairs_for_the_studio_colors(self):
        data = json.loads((ROOT / "ui" / "assets" / "studio-theme.json").read_text(encoding="utf-8"))
        self.assertEqual(data["CTk"]["fg_color"], [theme.COLORS["bg"][0], theme.COLORS["bg"][1]])
        self.assertEqual(data["CTkButton"]["fg_color"], list(theme.COLORS["accent"]))
        for widget, options in data.items():
            for option, value in options.items() if widget != "CTkFont" else ():
                if isinstance(value, str) and value.startswith(HEX):
                    self.fail(f"{widget}.{option} is a single color and would not follow the theme: {value}")


class IconTests(unittest.TestCase):
    def test_icons_have_a_variant_for_each_appearance_mode(self):
        image = icon("support", theme.COLORS["accent"], 16)
        self.assertIsInstance(image, ctk.CTkImage)
        light, dark = image.cget("light_image"), image.cget("dark_image")

        def ink(picture):  # the color of the first drawn (non-transparent) pixel
            width, height = picture.size
            return next(
                picture.getpixel((x, y))[:3]
                for y in range(height)
                for x in range(width)
                if picture.getpixel((x, y))[3] == 255
            )

        self.assertEqual(ink(light), (0x3F, 0x5A, 0x00))
        self.assertEqual(ink(dark), (0xD4, 0xFF, 0x00))
        self.assertEqual(icon("support", "#112233").cget("light_image").size, (96, 96))


class ThemeSettingTests(DatabaseCase):
    async def test_theme_setting_is_global_and_independent_of_density(self):
        await self.db.set_setting("ui_density", "compact")
        self.assertIsNone(await self.db.get_setting(theme.THEME_SETTING_KEY))
        await self.db.set_setting(theme.THEME_SETTING_KEY, "light")
        other_campaign = self.db.for_campaign(await self.db.create_campaign("Другая", "invite"))
        self.assertEqual(await other_campaign.get_setting(theme.THEME_SETTING_KEY), "light")
        self.assertEqual(await self.db.get_setting("ui_density"), "compact")
        await self.db.set_setting("ui_density", "normal")
        self.assertEqual(await self.db.get_setting(theme.THEME_SETTING_KEY), "light")


if __name__ == "__main__":
    unittest.main()
