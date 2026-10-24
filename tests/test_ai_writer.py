import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core.ai_writer import AIWriter


class AIWriterTests(unittest.IsolatedAsyncioTestCase):
    def writer(self, provider):
        return AIWriter({"provider": provider, "model": "test-model", "api_key": "fake-key"})

    async def test_xai_parses_nested_raw_response(self):
        writer = self.writer("xai")
        writer._post_json = AsyncMock(
            return_value={
                "output": [
                    {"type": "reasoning", "summary": []},
                    {"type": "message", "content": [{"type": "output_text", "text": "Спасибо за информацию."}]},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )
        text = await writer.generate_message()
        self.assertEqual(text, "Спасибо за информацию.")
        self.assertEqual(writer.session_tokens, 15)
        self.assertIsNone(writer.get_session_stats()["cost_usd"])

    async def test_gemini_usage_comes_from_api_and_key_is_not_in_url(self):
        writer = self.writer("gemini")
        writer._post_json = AsyncMock(
            return_value={
                "candidates": [{"content": {"parts": [{"text": "Полезная информация."}]}}],
                "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7, "thoughtsTokenCount": 3},
            }
        )
        await writer.generate_message()
        self.assertEqual(writer.session_tokens, 21)
        args = writer._post_json.call_args.args
        self.assertNotIn("fake-key", args[0])
        self.assertEqual(args[1]["x-goog-api-key"], "fake-key")

    async def test_anthropic_skips_thinking_blocks(self):
        writer = self.writer("anthropic")
        writer._post_json = AsyncMock(
            return_value={
                "content": [{"type": "thinking", "thinking": "internal"}, {"type": "text", "text": "Спасибо за ответ."}]
            }
        )
        self.assertEqual(await writer.generate_message(), "Спасибо за ответ.")

    async def test_provider_failure_is_visible_and_does_not_send_template(self):
        writer = self.writer("openai")
        writer._post_json = AsyncMock(side_effect=RuntimeError("HTTP 401"))
        with self.assertRaises(RuntimeError):
            await writer.generate_message()
        self.assertIn("401", writer.last_error)

    async def test_empty_model_response_is_failure(self):
        writer = self.writer("openai")
        writer._post_json = AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})
        with self.assertRaises(RuntimeError):
            await writer.generate_message()

    async def test_openai_request_has_supported_token_parameter(self):
        writer = self.writer("openai")
        writer._post_json = AsyncMock(return_value={"choices": [{"message": {"content": "Спасибо."}}]})
        await writer.generate_message()
        body = writer._post_json.call_args.args[2]
        self.assertIn("max_completion_tokens", body)
        self.assertNotIn("max_tokens", body)

    async def test_current_openai_models_use_compatible_low_latency_effort(self):
        for model, effort in (("gpt-6-luna", "none"), ("gpt-6.1-sol", "low"), ("gpt-6-astra", "low")):
            writer = self.writer("openai")
            writer.config["model"] = model
            writer._post_json = AsyncMock(return_value={"choices": [{"message": {"content": "Спасибо."}}]})
            await writer.generate_message()
            body = writer._post_json.call_args.args[2]
            self.assertEqual(body["reasoning_effort"], effort)
            self.assertEqual(body["max_completion_tokens"], 2048)
            self.assertNotIn("temperature", body)

    async def test_grok_47_uses_low_effort_without_storing_response(self):
        writer = self.writer("xai")
        writer.config["model"] = "grok-4.7"
        writer._post_json = AsyncMock(return_value={"output_text": "Спасибо."})
        await writer.generate_message()
        body = writer._post_json.call_args.args[2]
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertFalse(body["store"])

    async def test_gemini_resource_name_does_not_duplicate_models_prefix(self):
        writer = self.writer("gemini")
        writer.config["model"] = "models/gemini-3.5-flash-lite"
        writer._post_json = AsyncMock(return_value={"candidates": [{"content": {"parts": [{"text": "Спасибо."}]}}]})
        await writer.generate_message()
        self.assertTrue(writer._post_json.call_args.args[0].endswith("/models/gemini-3.5-flash-lite:generateContent"))

    async def test_http_errors_are_actionable_and_do_not_expose_provider_body_or_key(self):
        for status, expected in ((401, "Ключ"), (403, "доступ"), (404, "Модель"), (429, "квота"), (503, "позже")):
            writer = self.writer("openai")
            response = MagicMock(status=status)
            response.json = AsyncMock(return_value={"error": "fake-key"})
            response_context = MagicMock()
            response_context.__aenter__ = AsyncMock(return_value=response)
            session = MagicMock()
            session.post.return_value = response_context
            session_context = MagicMock()
            session_context.__aenter__ = AsyncMock(return_value=session)
            with (
                patch("aiohttp.ClientSession", return_value=session_context),
                self.assertRaises(RuntimeError) as caught,
            ):
                await writer.generate_message()
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn("fake-key", str(caught.exception))
            response.json.assert_not_called()
            self.assertFalse(session.post.call_args.kwargs["allow_redirects"])

    async def test_invalid_json_does_not_expose_raw_provider_response(self):
        writer = self.writer("openai")
        response = MagicMock(status=200)
        response.json = AsyncMock(side_effect=ValueError("fake-key"))
        response_context = MagicMock()
        response_context.__aenter__ = AsyncMock(return_value=response)
        session = MagicMock()
        session.post.return_value = response_context
        session_context = MagicMock()
        session_context.__aenter__ = AsyncMock(return_value=session)
        with patch("aiohttp.ClientSession", return_value=session_context), self.assertRaises(RuntimeError) as caught:
            await writer.generate_message()
        self.assertIn("JSON", str(caught.exception))
        self.assertNotIn("fake-key", str(caught.exception))

    async def test_no_ai_config_uses_short_local_template(self):
        text = await AIWriter().generate_message()
        self.assertTrue(text)
        self.assertLessEqual(len(text.split()), 12)

    async def test_explicit_prices_are_estimated(self):
        writer = self.writer("openai")
        writer.config.update(input_cost_per_million=2, output_cost_per_million=4)
        await writer._track_usage(100, 50)
        self.assertAlmostEqual(writer.session_cost, 0.0004)
        self.assertTrue(writer.get_session_stats()["cost_formatted"].startswith("~"))
