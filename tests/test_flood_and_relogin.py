from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from core.database import is_flood_blocking
from core.warmup_engine import WarmupEngine
from tests.support import DatabaseCase, account_payload


def past():
    return (datetime.now() - timedelta(seconds=5)).isoformat()


class FloodHelperTests(DatabaseCase):
    def test_is_flood_blocking_only_while_deadline_in_future(self):
        now = datetime(2026, 1, 1, 12, 0)
        self.assertFalse(is_flood_blocking({}, now))
        self.assertFalse(is_flood_blocking({"flood_until": None}, now))
        self.assertFalse(is_flood_blocking({"flood_until": "garbage"}, now))
        self.assertFalse(is_flood_blocking({"flood_until": "2026-01-01T11:59:59"}, now))
        self.assertFalse(is_flood_blocking({"flood_until": "2026-01-01T12:00:00"}, now))
        self.assertTrue(is_flood_blocking({"flood_until": "2026-01-01T12:00:01"}, now))

    async def test_clear_expired_flood_resets_only_after_deadline(self):
        account = await self.add_account()
        await self.db.set_flood_wait(account["id"], 60)
        blocked = await self.db.clear_expired_flood(await self.db.get_account(account["id"]))
        self.assertEqual(blocked["status"], "flood")
        self.assertEqual((await self.db.get_account(account["id"]))["status"], "flood")

        await self.db.update_account(account["id"], {"flood_until": past()})
        stale = await self.db.get_account(account["id"])
        cleared = await self.db.clear_expired_flood(stale)
        self.assertEqual(cleared["status"], "active")
        self.assertIs(cleared, stale)
        stored = await self.db.get_account(account["id"])
        self.assertEqual((stored["status"], stored["flood_until"]), ("active", None))

    async def test_clear_expired_flood_keeps_other_statuses_and_newer_pause(self):
        account = await self.add_account()
        await self.db.update_account_status(account["id"], "restricted")
        await self.db.clear_expired_flood(await self.db.get_account(account["id"]))
        self.assertEqual((await self.db.get_account(account["id"]))["status"], "restricted")

        await self.db.update_account(account["id"], {"status": "flood", "flood_until": past()})
        stale = await self.db.get_account(account["id"])
        await self.db.set_flood_wait(account["id"], 300)  # a new pause lands before the reset
        result = await self.db.clear_expired_flood(stale)
        self.assertEqual(result["status"], "flood")
        self.assertTrue(is_flood_blocking(await self.db.get_account(account["id"])))

    async def test_warmup_start_respects_active_pause_and_ignores_expired_one(self):
        account = await self.add_account()
        await self.db.set_flood_wait(account["id"], 300)
        engine = WarmupEngine(self.db, lambda *_: None)
        with self.assertRaisesRegex(ValueError, "Аккаунт выключен"):
            await engine.start(await self.db.get_account(account["id"]), "medium", "test")

        await self.db.update_account(account["id"], {"flood_until": past()})
        with patch("core.warmup_engine.AccountManager", return_value=AsyncMock()):
            # Passes the status gate; fails later only because no channels are selected.
            with self.assertRaisesRegex(ValueError, "Выберите каналы"):
                await engine.start(await self.db.get_account(account["id"]), "medium", "test")
        self.assertEqual((await self.db.get_account(account["id"]))["status"], "active")


class ReloginTests(DatabaseCase):
    async def test_new_phone_is_inserted(self):
        account_id, created = await self.db.save_authorized_account(account_payload(1))
        self.assertTrue(created)
        self.assertEqual((await self.db.get_account(account_id))["status"], "active")

    async def test_existing_phone_is_updated_not_duplicated(self):
        account = await self.add_account()
        await self.db.update_account(account["id"], {"daily_limit": 7, "status": "unauthorized"})
        await self.db.set_flood_wait(account["id"], 600)
        await self.db.update_device_profile(account["id"], '{"device_model": "Old"}')

        payload = {
            **account_payload(1),
            "api_id": 999,
            "api_hash": "b" * 32,
            "daily_limit": 40,
            "device_profile": '{"device_model": "New"}',
        }
        account_id, created = await self.db.save_authorized_account(payload)

        self.assertFalse(created)
        self.assertEqual(account_id, account["id"])
        accounts = await self.db.get_accounts()
        self.assertEqual(len(accounts), 1)
        row = accounts[0]
        self.assertEqual((row["status"], row["flood_until"]), ("active", None))
        self.assertEqual((row["api_id"], row["api_hash"]), ("999", "b" * 32))
        self.assertEqual(row["device_profile"], '{"device_model": "New"}')
        self.assertEqual(row["daily_limit"], 7)  # user's existing limit is preserved
        self.assertEqual((await self.db.get_account_by_phone(account["phone"]))["id"], account["id"])

    async def test_relogin_without_device_keeps_profile_and_updates_proxy_only_when_given(self):
        account = await self.add_account()
        await self.db.update_device_profile(account["id"], '{"device_model": "Keep"}')
        await self.db.save_authorized_account(account_payload(1))
        self.assertEqual((await self.db.get_account(account["id"]))["device_profile"], '{"device_model": "Keep"}')

        proxy = {"proxy_type": "SOCKS5", "proxy_host": "127.0.0.1", "proxy_port": 1080}
        await self.db.save_authorized_account({**account_payload(1), **proxy})
        row = await self.db.get_account(account["id"])
        self.assertEqual((row["proxy_type"], row["proxy_host"], row["proxy_port"]), ("SOCKS5", "127.0.0.1", 1080))
        await self.db.save_authorized_account(account_payload(1))
        self.assertEqual((await self.db.get_account(account["id"]))["proxy_host"], "127.0.0.1")
