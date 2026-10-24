import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.errors import AuthKeyUnregisteredError, FloodWaitError, PeerFloodError, UserNotParticipantError
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.types import Channel, Chat, User

from core.account_manager import AccountManager
from tests.support import account_payload


class AccountManagerTests(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        manager = AccountManager(account_payload())
        manager.client = AsyncMock()
        manager.client.get_entity.side_effect = [User(id=1), Channel(id=2, title="test", photo=None, date=None)]
        manager.client.return_value = SimpleNamespace(missing_invitees=[])
        manager.client.get_permissions.side_effect = UserNotParticipantError(request=None)
        return manager

    async def test_missing_invitee_is_not_success(self):
        manager = self.manager()
        manager.client.return_value = SimpleNamespace(missing_invitees=[SimpleNamespace(user_id=1)])
        result = await manager.add_user_to_chat("group", "alice")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "private")

    async def test_basic_group_uses_basic_chat_request(self):
        manager = self.manager()
        manager.client.get_entity.side_effect = [
            User(id=1),
            Chat(id=2, title="test", photo=None, participants_count=1, date=None, version=1),
        ]
        self.assertTrue((await manager.add_user_to_chat("group", "alice"))["success"])
        self.assertIsInstance(manager.client.call_args.args[0], AddChatUserRequest)

    async def test_flood_during_username_resolution_is_preserved(self):
        manager = self.manager()
        manager.client.get_entity.side_effect = FloodWaitError(request=None, capture=42)
        result = await manager.add_user_to_chat("group", "alice")
        self.assertEqual(result["flood_seconds"], 42)
        self.assertEqual(result["error"], "flood")

    async def test_revoked_auth_is_not_missing_username(self):
        manager = self.manager()
        manager.client.get_entity.side_effect = AuthKeyUnregisteredError(request=None)
        self.assertEqual((await manager.add_user_to_chat("group", "alice"))["error"], "unauthorized")

    async def test_peer_flood_is_account_restriction(self):
        manager = self.manager()
        manager.client.side_effect = PeerFloodError(request=None)
        result = await manager.add_user_to_chat("group", "alice")
        self.assertEqual(result["error"], "peer_flood")
        self.assertFalse(result.get("flood_seconds"))

    async def test_channel_username_cannot_be_invited_as_user(self):
        manager = self.manager()
        manager.client.get_entity.side_effect = [Channel(id=1, title="test", photo=None, date=None)]
        self.assertFalse((await manager.add_user_to_chat("group", "alice"))["success"])
        manager.client.assert_not_called()

    async def test_failed_connection_releases_session(self):
        fake = AsyncMock()
        fake.connect.side_effect = OSError("offline")
        manager = AccountManager(account_payload())
        with patch("core.account_manager.TelegramClient", return_value=fake):
            with self.assertRaises(OSError):
                await manager.connect()
        fake.disconnect.assert_awaited_once()
        self.assertIsNone(manager.client)
        self.assertFalse(AccountManager.is_session_busy(account_payload()["phone"]))

    async def test_same_session_cannot_connect_twice(self):
        fake = AsyncMock()
        first = AccountManager(account_payload())
        second = AccountManager(account_payload())
        with patch("core.account_manager.TelegramClient", return_value=fake):
            try:
                await first.connect()
                with self.assertRaises(RuntimeError):
                    await second.connect()
            finally:
                await first.disconnect()
        self.assertFalse(AccountManager.is_session_busy(account_payload()["phone"]))

    async def test_unauthorized_connect_releases_client(self):
        fake = AsyncMock()
        fake.is_user_authorized.return_value = False
        with patch("core.account_manager.TelegramClient", return_value=fake):
            manager = AccountManager(account_payload())
            with self.assertRaises(RuntimeError):
                await manager.connect()
        fake.disconnect.assert_awaited_once()

    async def test_existing_member_is_not_counted_as_new_invitee(self):
        manager = self.manager()
        manager.client.get_permissions.side_effect = None
        manager.client.get_permissions.return_value = SimpleNamespace(has_left=False, is_banned=False)
        self.assertEqual((await manager.add_user_to_chat("group", "alice"))["error"], "already_in")
        manager.client.assert_not_called()

    async def test_missing_chat_is_not_missing_user(self):
        manager = self.manager()
        manager.client.get_entity.side_effect = [User(id=1), ValueError("chat missing")]
        self.assertEqual((await manager.add_user_to_chat("group", "alice"))["error"], "need_admin")
