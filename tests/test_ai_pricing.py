import unittest

from core.ai_pricing import (
    estimate_cost_usd,
    estimate_from_total_tokens,
    format_usd,
    lookup_rates,
    resolve_request_cost,
    resolve_stored_cost,
    ticks_to_usd,
)


class AiPricingTests(unittest.TestCase):
    def test_ticks_to_usd(self):
        self.assertAlmostEqual(ticks_to_usd(37_756_000), 0.0037756)
        self.assertEqual(ticks_to_usd(None), None)

    def test_grok_catalog_rates(self):
        self.assertEqual(lookup_rates("xai", "grok-4.7"), (2.00, 6.00))
        self.assertEqual(lookup_rates("xai", "grok-4-fast-reasoning"), (1.25, 2.50))

    def test_estimate_prefers_provider_ticks(self):
        cost, source = resolve_request_cost(
            provider="xai",
            model="grok-4.7",
            input_tokens=100,
            output_tokens=50,
            usage={"cost_in_usd_ticks": 10_000_000},  # $0.001
        )
        self.assertEqual(source, "provider")
        self.assertAlmostEqual(cost, 0.001)

    def test_estimate_from_public_rates(self):
        cost, source = resolve_request_cost(
            provider="xai",
            model="grok-4.7",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        self.assertEqual(source, "estimate")
        self.assertAlmostEqual(cost, 8.0)

    def test_stored_total_falls_back_to_token_estimate(self):
        amount, source = resolve_stored_cost(
            {"provider": "xai", "model": "grok-4.7", "total_tokens": 1_000_000, "total_cost_usd": 0}
        )
        self.assertEqual(source, "estimate")
        self.assertGreater(amount, 0)
        # Direct stored cost wins over token guess.
        stored, stored_source = resolve_stored_cost(
            {"provider": "xai", "model": "grok-4.7", "total_tokens": 1_000_000, "total_cost_usd": 0.42}
        )
        self.assertEqual((stored, stored_source), (0.42, "stored"))

    def test_format_and_helpers(self):
        self.assertTrue(format_usd(0.0123, estimated=True).startswith("~$"))
        self.assertAlmostEqual(estimate_cost_usd(1_000_000, 0, input_per_million=2, output_per_million=6), 2.0)
        self.assertIsNotNone(estimate_from_total_tokens("xai", "grok-4-fast-reasoning", 1462))


if __name__ == "__main__":
    unittest.main()
