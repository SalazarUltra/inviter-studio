"""English help, UI key coverage and the portable package pruning."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.build_windows import prune_package
from ui import help_content, help_content_en, i18n

ROOT = Path(__file__).resolve().parent.parent


class EnglishHelpTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("ru")

    def test_every_guide_is_translated_with_the_same_block_layout(self):
        self.assertEqual(set(help_content_en.GUIDES_EN), set(help_content.GUIDES))
        for key, source in help_content.GUIDES.items():
            translated = help_content_en.GUIDES_EN[key]
            self.assertTrue(translated["title"].strip() and translated["intro"].strip(), key)
            self.assertEqual(len(translated["blocks"]), len(source["blocks"]), key)
            for heading, text in translated["blocks"]:
                self.assertTrue(heading.strip() and text.strip(), key)
                self.assertFalse(re.search("[А-Яа-яЁё]{4,}", heading), f"untranslated heading in {key}: {heading}")

    def test_get_guides_follows_the_language_and_defaults_to_russian(self):
        self.assertIs(help_content.get_guides(), help_content.GUIDES)
        i18n.set_language("en")
        self.assertEqual(help_content.get_guides()["start"]["title"], "Getting started")
        self.assertEqual(help_content.get_guides("ru")["start"]["title"], "С чего начать")
        self.assertEqual(help_content.get_guides("de")["start"]["title"], "С чего начать")

    def test_missing_translation_falls_back_to_russian(self):
        partial = {"start": {"title": "Getting started", "intro": "", "blocks": []}}
        with patch.object(help_content_en, "GUIDES_EN", partial):
            guides = help_content.get_guides("en")
        self.assertEqual(guides["start"]["title"], "Getting started")
        self.assertEqual(guides["start"]["intro"], help_content.GUIDES["start"]["intro"])
        self.assertEqual(guides["start"]["blocks"], help_content.GUIDES["start"]["blocks"])
        self.assertEqual(guides["task"], help_content.GUIDES["task"])

    def test_search_uses_the_active_language(self):
        self.assertTrue(help_content.search_guides("личку"))
        self.assertFalse(help_content.search_guides("interval", lang="ru"))
        self.assertTrue(help_content.search_guides("interval", lang="en"))
        i18n.set_language("en")
        self.assertTrue(help_content.search_guides("PeerFlood"))
        self.assertFalse(help_content.search_guides("личку"))


class UiKeyCoverageTests(unittest.TestCase):
    def test_literal_keys_used_in_ui_exist_in_both_languages(self):
        pattern = re.compile(r"""\bt\(\s*["']([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)["']""")
        missing = []
        for path in sorted((ROOT / "ui").glob("*.py")):
            for key in pattern.findall(path.read_text(encoding="utf-8")):
                for lang in i18n.LANGS:
                    if key not in i18n.STRINGS[lang]:
                        missing.append(f"{path.name}:{lang}:{key}")
        self.assertEqual(missing, [])

    def test_new_ui_areas_have_english_text(self):
        for prefix in ("conv.", "queue.", "ab.", "transfer.", "help."):
            keys = [key for key in i18n.STRINGS["ru"] if key.startswith(prefix)]
            self.assertTrue(keys, prefix)
            for key in keys:
                self.assertNotEqual(i18n.t(key, lang="en"), i18n.t(key, lang="ru"), key)

    def test_period_titles_are_localized(self):
        from ui.conversion_panel import period_titles

        i18n.set_language("en")
        try:
            self.assertEqual(period_titles()["7d"], "7 days")
            self.assertEqual(period_titles()["all"], "All time")
        finally:
            i18n.set_language("ru")
        self.assertEqual(period_titles()["all"], "Всё время")


class PrunePackageTests(unittest.TestCase):
    def test_prune_removes_only_unused_files(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            internal = package / "_internal"
            for relative, size in (
                ("PIL/_avif.cp314-win_amd64.pyd", 100),
                ("PIL/_webp.cp314-win_amd64.pyd", 10),
                ("PIL/_imaging.cp314-win_amd64.pyd", 10),
                ("customtkinter/__pycache__/x.pyc", 5),
                ("_tcl_data/msgs/de.msg", 3),
                ("_tcl_data/msgs/en.msg", 3),
                ("_tcl_data/msgs/en_gb.msg", 3),
                ("_tcl_data/msgs/ru.msg", 3),
                ("_tcl_data/msgs/ru_ua.msg", 3),
                ("_tk_data/msgs/fr.msg", 3),
            ):
                target = internal / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"x" * size)
            (package / "InviterStudio.exe").write_bytes(b"MZ")
            freed = prune_package(package)
            self.assertEqual(freed, 100 + 10 + 5 + 3 + 3)
            remaining = {path.relative_to(internal).as_posix() for path in internal.rglob("*") if path.is_file()}
            self.assertEqual(
                remaining,
                {
                    "PIL/_imaging.cp314-win_amd64.pyd",
                    "_tcl_data/msgs/en.msg",
                    "_tcl_data/msgs/en_gb.msg",
                    "_tcl_data/msgs/ru.msg",
                    "_tcl_data/msgs/ru_ua.msg",
                },
            )
            self.assertTrue((package / "InviterStudio.exe").is_file())
            self.assertEqual(prune_package(package), 0)  # idempotent


if __name__ == "__main__":
    unittest.main()
