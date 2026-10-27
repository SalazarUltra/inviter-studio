"""UI translations: key fallback, language normalization and dictionary parity."""

import unittest

from core.first_run import WIZARD_STEPS, should_show_wizard
from ui import i18n


class I18nTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("ru")

    def test_default_is_russian_and_unknown_language_normalizes(self):
        self.assertEqual(i18n.get_language(), "ru")
        for value in (None, "", "de", "EN-us", 5):
            self.assertEqual(i18n.normalize_lang(value), "ru")
        self.assertEqual(i18n.normalize_lang(" EN "), "en")

    def test_translation_and_switch(self):
        self.assertEqual(i18n.t("btn.start"), "Запустить задачу")
        i18n.set_language("en")
        self.assertEqual(i18n.t("btn.start"), "Start task")
        self.assertEqual(i18n.t("btn.start", lang="ru"), "Запустить задачу")
        self.assertEqual(i18n.t("pace.safe"), "Safe")

    def test_missing_key_falls_back_to_russian_then_to_key(self):
        i18n.STRINGS["ru"]["test.only_ru"] = "Только русский"
        self.addCleanup(i18n.STRINGS["ru"].pop, "test.only_ru", None)
        i18n.set_language("en")
        self.assertEqual(i18n.t("test.only_ru"), "Только русский")
        self.assertEqual(i18n.t("no.such.key"), "no.such.key")

    def test_format_errors_never_raise(self):
        self.assertEqual(i18n.t("task.summary.added", n=3), "Добавлено: 3")
        self.assertEqual(i18n.t("task.summary.added"), "Добавлено: {n}")
        self.assertEqual(i18n.t("task.summary.added", wrong=1), "Добавлено: {n}")

    def test_both_languages_define_the_same_keys(self):
        self.assertEqual(set(i18n.STRINGS["ru"]), set(i18n.STRINGS["en"]))
        for lang, table in i18n.STRINGS.items():
            for key, text in table.items():
                self.assertTrue(text.strip(), f"{lang}:{key} is empty")

    def test_placeholders_match_between_languages(self):
        import string

        def fields(text):
            return {name for _, name, _, _ in string.Formatter().parse(text) if name}

        for key, text in i18n.STRINGS["ru"].items():
            self.assertEqual(fields(text), fields(i18n.STRINGS["en"][key]), key)

    def test_listeners_notified_and_failures_isolated(self):
        seen = []

        def broken(_lang):
            raise RuntimeError("view gone")

        unsubscribe_broken = i18n.subscribe(broken)
        unsubscribe = i18n.subscribe(seen.append)
        self.assertEqual(i18n.set_language("en"), "en")
        unsubscribe()
        unsubscribe_broken()
        i18n.set_language("ru")
        self.assertEqual(seen, ["en"])

    def test_wizard_steps_have_texts_in_every_language(self):
        for lang in i18n.LANGS:
            for step_id, _tab in WIZARD_STEPS:
                for part in ("title", "text", "button"):
                    key = f"wizard.{step_id}.{part}"
                    self.assertNotEqual(i18n.t(key, lang=lang), key)

    def test_nav_titles_exist_for_all_tabs(self):
        for lang in i18n.LANGS:
            for tab in ("dashboard", "accounts", "targets", "task", "warmup", "ai", "logs", "parsing"):
                self.assertNotEqual(i18n.t(f"nav.{tab}", lang=lang), f"nav.{tab}")


class FirstRunRuleTests(unittest.TestCase):
    def test_wizard_only_for_new_installs(self):
        self.assertTrue(should_show_wizard(0, None))
        self.assertTrue(should_show_wizard(0, "0"))
        self.assertFalse(should_show_wizard(0, "1"))
        self.assertFalse(should_show_wizard(1, None))
        self.assertFalse(should_show_wizard(3, "0"))


if __name__ == "__main__":
    unittest.main()
