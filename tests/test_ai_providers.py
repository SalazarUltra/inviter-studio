import unittest

from core.ai_providers import PROVIDERS, validate_connection
from tests.support import DatabaseCase


class ProviderInputTests(unittest.TestCase):
    def test_unknown_models_are_allowed_and_gemini_resource_prefix_is_normalized(self):
        self.assertEqual(
            validate_connection(" Main ", "gemini", " fake-key ", " models/custom-model "),
            ("Main", "fake-key", "custom-model"),
        )
        self.assertEqual(validate_connection("Old", "openai", "fake-key", "gpt-4o-mini")[2], "gpt-4o-mini")

    def test_mistakes_have_clear_errors_without_disclosing_key(self):
        for provider, key, model in (
            ("unknown", "secret", "test"),
            ("openai", "sk-ant-secret", "test"),
            ("openai", "secret\nkey", "test"),
            ("openai", "12345", "test"),
            ("gemini", "a" * 32, "test"),
            ("openai", "secret", "https://example.org"),
            ("openai", "secret", "Model Name"),
        ):
            with self.subTest(provider=provider, model=model), self.assertRaises(ValueError) as caught:
                validate_connection("Main", provider, key, model)
            self.assertNotIn(key, str(caught.exception))

    def test_curated_models_are_text_only_starter_choices(self):
        self.assertEqual(PROVIDERS["openai"]["models"][0], "gpt-6-luna")
        self.assertEqual(PROVIDERS["xai"]["models"], ["grok-4.7"])
        for provider in PROVIDERS.values():
            self.assertTrue(provider["keys_url"].startswith("https://"))
            self.assertTrue(provider["docs_url"].startswith("https://"))


class ProviderStorageTests(DatabaseCase):
    async def test_edit_keeps_usage_identity_and_activation(self):
        identifier = await self.db.add_ai_config("Original", "openai", "fake-key", "gpt-4o-mini")
        await self.db.set_active_ai_config(identifier)
        await self.db.update_ai_usage(identifier, 42, 0.1)
        await self.db.update_ai_config(identifier, "Updated", "gemini", "fake-new-key", "models/gemini-3.5-flash-lite")
        config = await self.db.get_active_ai_config()
        self.assertEqual(
            (config["id"], config["name"], config["model"], config["total_tokens"], config["api_key"]),
            (identifier, "Updated", "gemini-3.5-flash-lite", 42, "fake-new-key"),
        )
        self.assertEqual(len(await self.db.get_ai_configs()), 1)

    async def test_edit_missing_or_invalid_connection_does_not_create_record(self):
        with self.assertRaises(ValueError):
            await self.db.update_ai_config(123, "Main", "openai", "fake-key", "gpt-6-luna")
        with self.assertRaises(ValueError):
            await self.db.add_ai_config("Main", "openai", "12345", "gpt-6-luna")
        self.assertEqual(await self.db.get_ai_configs(), [])
