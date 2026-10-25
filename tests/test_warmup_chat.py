"""Warmup chat behavior: live replies, own-message awareness and per-account memory."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.tl.types import Message, PeerChannel, PeerUser, User

from core.warmup_actions import WarmupActions, select_chat_context


def text_message(id, text, user_id, *, out=False):
    return Message(
        id=id,
        peer_id=PeerUser(user_id) if out else PeerChannel(-100100),
        out=out,
        message=text,
        date=None,
        from_id=PeerUser(user_id),
    )


def users_map(*ids):
    return {uid: User(id=uid, first_name=f"U{uid}", access_hash=1) for uid in ids}


class SelectChatContextTests(unittest.TestCase):
    def test_entries_are_oldest_first_with_names_and_own_flags(self):
        history = SimpleNamespace(
            messages=[
                text_message(3, "второе сообщение", 456),
                text_message(2, "первое сообщение", 123, out=True),
                SimpleNamespace(id=1, service="chat_created"),  # service message ignored
            ]
        )
        snapshot = select_chat_context(history, users_map(123, 456))
        self.assertEqual([entry["name"] for entry in snapshot["entries"]], ["Вы", "U456"])
        self.assertFalse(snapshot["newest_own"])
        self.assertEqual(snapshot["reply"]["message"].id, 3)

    def test_newest_own_message_blocks_reply_candidate(self):
        history = SimpleNamespace(
            messages=[
                text_message(2, "наше последнее слово", 123, out=True),
                text_message(1, "реплика соседа", 456),
            ]
        )
        snapshot = select_chat_context(history, users_map(123, 456))
        # newest_own short-circuits the send in send_message_to_chat, so the
        # engine never reaches the (unused) reply candidate here.
        self.assertTrue(snapshot["newest_own"])

    def test_reply_candidate_prefers_recent_peer_message(self):
        history = SimpleNamespace(
            messages=[
                text_message(4, "короткий вопрос по теме?", 456),
                text_message(3, "x", 456),  # too short to answer
                text_message(2, "наше earlier", 123, out=True),
            ]
        )
        snapshot = select_chat_context(history, users_map(123, 456))
        self.assertEqual(snapshot["reply"]["message"].id, 4)
        self.assertEqual(snapshot["own_texts"], ["наше earlier"])


class SendMessageToChatTests(unittest.TestCase):
    def actions(self, *, memory_rows=None, chat_key="group_one"):
        client = AsyncMock()
        client.get_entity.return_value = SimpleNamespace(
            id=-100100, username="group_one", title="Группа", broadcast=False
        )
        history = SimpleNamespace(
            messages=[
                text_message(7, "Что думаете про обновление?", 456),
                text_message(6, "Наше прошлое сообщение", 123, out=True),
            ],
            users=[User(id=456, first_name="Петр", access_hash=1)],
        )
        client.return_value = history
        db = SimpleNamespace(
            get_ai_send_limits=AsyncMock(return_value=(0, None)),
            get_ai_messages=AsyncMock(return_value=memory_rows or []),
            record_ai_send=AsyncMock(),
        )
        ai = SimpleNamespace(
            db=db,
            generate_message=AsyncMock(return_value="Согласен, обновление полезное."),
            remember_sent_message=AsyncMock(),
        )
        actions = WarmupActions(client, ai, lambda *_: None, account_id=1)
        actions.client.get_entity.side_effect = None
        return actions, client, ai, db

    def test_replies_to_peer_message_with_reply_to_and_remembers(self):
        actions, client, ai, db = self.actions()
        import asyncio

        result = asyncio.run(actions.send_message_to_chat("group_one"))
        self.assertTrue(result["success"], result)
        self.assertIn("Ответ", result["detail"])
        kwargs = client.send_message.call_args.kwargs
        self.assertEqual(kwargs.get("reply_to"), 7)
        ai.generate_message.assert_awaited_once()
        self.assertEqual(ai.generate_message.await_args.kwargs["reply_to"]["text"], "Что думаете про обновление?")
        self.assertFalse(ai.generate_message.await_args.kwargs["first_in_chat"])
        db.record_ai_send.assert_awaited_once()
        ai.remember_sent_message.assert_awaited_once()

    def test_skips_when_newest_message_is_own(self):
        actions, client, ai, _ = self.actions(memory_rows=[{"role": "assistant", "content": "Наше прошлое сообщение"}])
        client.return_value = SimpleNamespace(
            messages=[text_message(7, "Наше последнее сообщение", 123, out=True)],
            users=[User(id=456, first_name="Петр", access_hash=1)],
        )
        import asyncio

        result = asyncio.run(actions.send_message_to_chat("group_one"))
        self.assertFalse(result["success"])
        self.assertIn("последнее сообщение в чате наше", result["detail"])
        client.send_message.assert_not_awaited()

    def test_first_time_in_chat_is_reported_to_prompt(self):
        actions, client, ai, _ = self.actions()
        client.return_value = SimpleNamespace(
            messages=[text_message(5, "Всем привет в этой группе", 456)],
            users=[User(id=456, first_name="Олег", access_hash=1)],
        )
        import asyncio

        result = asyncio.run(actions.send_message_to_chat("group_one"))
        self.assertTrue(result["success"], result)
        self.assertTrue(ai.generate_message.await_args.kwargs["first_in_chat"])

    def test_memory_assistant_texts_passed_for_antirepeat(self):
        rows = [{"role": "assistant", "content": "Ранее написанная мысль"}]
        actions, client, ai, _ = self.actions(memory_rows=rows)
        import asyncio

        asyncio.run(actions.send_message_to_chat("group_one"))
        own_recent = ai.generate_message.await_args.kwargs["own_recent"]
        self.assertIn("Ранее написанная мысль", own_recent)


if __name__ == "__main__":
    unittest.main()
