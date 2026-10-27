"""
Конкретные действия для прогрева аккаунта.
Каждое действие возвращает {"success": bool, "detail": str}
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import Any

from telethon import TelegramClient, utils
from telethon.errors import AuthKeyUnregisteredError, FloodWaitError, PeerFloodError
from telethon.tl.functions.messages import GetHistoryRequest, GetRepliesRequest, SendReactionRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Channel, InputUser, Message, PeerUser, ReactionEmoji, User

from core.user_feedback import explain_error

REACTIONS = ["👍", "❤️", "🔥", "👏", "😍", "🤔", "💯"]
REPLY_REACTIONS = ["👍", "🔥", "❤️", "🤔", "💯"]

CONTEXT_MESSAGES = 20
CONTEXT_ENTRIES = 6
REPLY_MIN_LEN = 5
REPLY_MAX_LEN = 300


def author_name(user) -> str:
    if user is None:
        return "Участник"
    if getattr(user, "username", None):
        return "@" + user.username
    if getattr(user, "first_name", None):
        name = user.first_name
        if getattr(user, "last_name", None):
            name += " " + user.last_name
        return name
    return "Участник"


def select_chat_context(history, users: dict, *, history_limit: int = CONTEXT_MESSAGES):
    """Turn a GetHistory page into a conversation snapshot for the AI.

    Returns entries oldest→newest with author labels, flags whether the newest
    text message is our own (nothing to add until someone replies), the best
    candidate worth answering, and our recent own texts for anti-repeat.
    """
    entries = []
    for message in reversed(list(history.messages[:history_limit])):
        if not isinstance(message, Message):
            continue
        text = (getattr(message, "message", "") or "").strip()
        if not text:
            continue
        sender_id = getattr(message.from_id, "user_id", None) if isinstance(message.from_id, PeerUser) else None
        own = bool(getattr(message, "out", False))
        sender = users.get(sender_id) if sender_id is not None else None
        entries.append({"name": "Вы" if own else author_name(sender), "text": text, "own": own, "message": message})
    newest_own = bool(entries and entries[-1]["own"])
    reply = None
    for entry in reversed(entries):
        sender_id = getattr(entry["message"].from_id, "user_id", None)
        sender = users.get(sender_id)
        if entry["own"] or (sender is not None and getattr(sender, "bot", False)):
            continue
        if not REPLY_MIN_LEN <= len(entry["text"]) <= REPLY_MAX_LEN:
            continue
        reply = {"name": entry["name"], "text": entry["text"], "message": entry["message"]}
        break
    own_texts = [entry["text"] for entry in entries if entry["own"]][-3:]
    return {"entries": entries, "newest_own": newest_own, "reply": reply, "own_texts": own_texts}


class WarmupActions:
    def __init__(
        self,
        client: TelegramClient,
        ai_writer: Any,
        log_callback,
        account_id: int | None = None,
        memory_enabled: bool = True,
        tone: str = "neutral",
        hourly_limit: int = 6,
        chat_cooldown_min: int = 12,
        reply_chance: int = 100,
    ):
        self.client = client
        self.ai = ai_writer
        self.log_callback = log_callback
        self.account_id = account_id
        self.memory_enabled = memory_enabled
        self.tone = (tone or "neutral").strip().lower() or "neutral"
        self.hourly_limit = max(1, int(hourly_limit or 6))
        self.chat_cooldown_min = max(1, int(chat_cooldown_min or 12))
        self.reply_chance = max(0, min(100, int(reply_chance if reply_chance is not None else 100)))
        # Messages we already reacted to in this run; re-sending the same
        # reaction on Telegram would toggle it off.
        self._reacted: set[tuple[str, int]] = set()

    async def _effective_tone(self) -> str:
        """Prefer the latest saved Warm-up tone so the dropdown steers live writing."""
        if getattr(self.ai, "db", None):
            try:
                saved = str(await self.ai.db.get_setting("ai_style_tone", self.tone) or self.tone)
                key = saved.strip().lower()
                if key in {"neutral", "friendly", "expert"}:
                    self.tone = key
                    return key
            except Exception:
                pass
        key = (self.tone or "neutral").strip().lower()
        return key if key in {"neutral", "friendly", "expert"} else "neutral"

    async def go_online(self) -> dict[str, Any]:
        try:
            from telethon.tl.functions.account import UpdateStatusRequest

            await self.client(UpdateStatusRequest(offline=False))
            await asyncio.sleep(random.uniform(30, 120))
            await self.client(UpdateStatusRequest(offline=True))
            return {"success": True, "detail": "Статус онлайн/оффлайн"}
        except (FloodWaitError, PeerFloodError, AuthKeyUnregisteredError):
            raise
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "detail": explain_error(exc)}

    async def read_channel(self, channel: str) -> dict[str, Any]:
        try:
            entity = await self.client.get_entity(channel)
            messages = await self.client(
                GetHistoryRequest(
                    peer=entity,
                    limit=random.randint(5, 15),
                    offset_date=None,
                    offset_id=0,
                    max_id=0,
                    min_id=0,
                    add_offset=0,
                    hash=0,
                )
            )

            for _ in messages.messages:
                await asyncio.sleep(random.uniform(3, 15))

            return {"success": True, "detail": f"Прочитано {len(messages.messages)} сообщений в @{channel}"}
        except (FloodWaitError, PeerFloodError, AuthKeyUnregisteredError):
            raise
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "detail": explain_error(exc)}

    async def react_to_post(self, channel: str) -> dict[str, Any]:
        try:
            entity = await self.client.get_entity(channel)
            messages = await self.client(
                GetHistoryRequest(
                    peer=entity,
                    limit=20,
                    offset_date=None,
                    offset_id=0,
                    max_id=0,
                    min_id=0,
                    add_offset=0,
                    hash=0,
                )
            )
            if not messages.messages:
                return {"success": False, "detail": "Нет сообщений"}

            msg = random.choice(messages.messages[:10])
            reaction = random.choice(REACTIONS)
            await asyncio.sleep(random.uniform(2, 5))
            await self.client(
                SendReactionRequest(
                    peer=entity,
                    msg_id=msg.id,
                    reaction=[ReactionEmoji(emoticon=reaction)],
                )
            )
            return {"success": True, "detail": f"Реакция {reaction} на пост в @{channel}"}
        except (FloodWaitError, PeerFloodError, AuthKeyUnregisteredError):
            raise
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "detail": explain_error(exc)}

    async def view_profile(self, channel: str) -> dict[str, Any]:
        """Open a real participant profile via GetFullUser, not just a cached channel lookup."""
        try:
            entity = await self.client.get_entity(channel)
            if isinstance(entity, Channel) and entity.broadcast:
                # Broadcast channels expose no member dialogue to sample from.
                await asyncio.sleep(random.uniform(3, 10))
                title = getattr(entity, "username", None) or channel
                return {"success": True, "detail": f"Обновлены данные канала @{title}"}
            history = await self.client(
                GetHistoryRequest(
                    peer=entity,
                    limit=10,
                    offset_date=None,
                    offset_id=0,
                    max_id=0,
                    min_id=0,
                    add_offset=0,
                    hash=0,
                )
            )
            candidates = [
                user
                for user in history.users
                if isinstance(user, User)
                and not user.is_self
                and not user.bot
                and not user.deleted
                and getattr(user, "access_hash", None)
            ]
            if not candidates:
                await asyncio.sleep(random.uniform(3, 10))
                title = getattr(entity, "title", None) or channel
                return {"success": True, "detail": f"Обновлены данные чата {title}"}
            person = random.choice(candidates)
            await self.client(GetFullUserRequest(InputUser(person.id, person.access_hash)))
            await asyncio.sleep(random.uniform(5, 20))
            name = f"@{person.username}" if person.username else (person.first_name or str(person.id))
            return {"success": True, "detail": f"Просмотр профиля {name}"}
        except (FloodWaitError, PeerFloodError, AuthKeyUnregisteredError):
            raise
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "detail": explain_error(exc)}

    async def _mark_read(self, entity, messages: list) -> None:
        """Leave read receipts: a participant who replies has read the room."""
        if not messages:
            return
        try:
            await self.client.send_read_acknowledge(entity, messages)
        except Exception:  # noqa: BLE001 - receipts are a nicety, never block the send
            pass

    async def _read_thread(self, entity, reply_message) -> dict | None:
        """Fetch the discussion thread under a post before answering it.

        Works only for channel discussion threads (messages.getReplies);
        returns the thread snapshot or None when there is nothing to read.
        """
        if not isinstance(entity, Channel):
            return None
        if getattr(reply_message, "replies", None) is None:
            return None
        try:
            response = await asyncio.wait_for(
                self.client(
                    GetRepliesRequest(
                        peer=entity,
                        msg_id=reply_message.id,
                        offset_id=0,
                        offset_date=None,
                        add_offset=0,
                        limit=CONTEXT_MESSAGES,
                        max_id=0,
                        min_id=0,
                        hash=0,
                    )
                ),
                20,
            )
        except (FloodWaitError, PeerFloodError, AuthKeyUnregisteredError):
            raise
        except Exception:  # noqa: BLE001 - not every group supports threads
            return None
        users = {user.id: user for user in response.users if isinstance(user, User)}
        snapshot = select_chat_context(response, users)
        if snapshot["entries"]:
            await self._mark_read(entity, [entry["message"] for entry in snapshot["entries"]])
            return snapshot
        return None

    async def _react_to_reply(self, entity, chat_key: str, reply_message) -> str | None:
        """Add a small reaction to the message we are about to answer."""
        if (chat_key, reply_message.id) in self._reacted:
            return None
        reaction = random.choice(REPLY_REACTIONS)
        try:
            await self.client(
                SendReactionRequest(peer=entity, msg_id=reply_message.id, reaction=[ReactionEmoji(emoticon=reaction)])
            )
        except (FloodWaitError, PeerFloodError, AuthKeyUnregisteredError):
            raise
        except Exception as exc:  # noqa: BLE001 - reaction must not block the reply
            self.log_callback("INFO", f"Реакция не удалась, отвечаю текстом: {explain_error(exc)}")
            return None
        self._reacted.add((chat_key, reply_message.id))
        return reaction

    async def send_message_to_chat(self, channel: str) -> dict[str, Any]:
        try:
            entity = await self.client.get_entity(channel)
            chat_key = str(getattr(entity, "username", "") or utils.get_peer_id(entity))
            peer_name = str(getattr(entity, "title", "") or getattr(entity, "username", "") or channel)

            if isinstance(entity, Channel) and entity.broadcast:
                return {"success": False, "detail": "Пропуск: публикация в вещательном канале отключена"}
            if self.account_id and getattr(self.ai, "db", None):
                hourly_count, last_sent = await self.ai.db.get_ai_send_limits(self.account_id, chat_key)
                if hourly_count >= self.hourly_limit:
                    return {"success": False, "detail": f"Лимит AI-сообщений в час достигнут ({self.hourly_limit}/ч)"}
                if last_sent and datetime.now() - datetime.fromisoformat(last_sent) < timedelta(
                    minutes=self.chat_cooldown_min
                ):
                    return {"success": False, "detail": "Пропуск: слишком рано писать повторно в этот чат"}

            history = await self.client(
                GetHistoryRequest(
                    peer=entity,
                    limit=CONTEXT_MESSAGES,
                    offset_date=None,
                    offset_id=0,
                    max_id=0,
                    min_id=0,
                    add_offset=0,
                    hash=0,
                )
            )
            users = {user.id: user for user in history.users if isinstance(user, User)}
            snapshot = select_chat_context(history, users)
            if not snapshot["entries"]:
                return {"success": False, "detail": "Пропуск: в чате пока нет сообщений для контекста"}
            if snapshot["newest_own"]:
                # Writing again would put two of our messages in a row with nobody
                # answering in between; a live participant waits for the room.
                return {
                    "success": False,
                    "detail": "Пропуск: последнее сообщение в чате наше; ждём реакции собеседников",
                }

            memory_own: list[str] = []
            memory_count = 0
            if self.account_id and getattr(self.ai, "db", None) and self.memory_enabled:
                recent_memory = await self.ai.db.get_ai_messages(self.account_id, chat_key, limit=40)
                memory_own = [
                    (item.get("content") or "").strip()
                    for item in recent_memory
                    if (item.get("role") or "") == "assistant" and (item.get("content") or "").strip()
                ]
                memory_count = len(memory_own)
            own_recent = (memory_own[-3:] or []) + snapshot["own_texts"]
            first_in_chat = memory_count == 0 and not any(entry["own"] for entry in snapshot["entries"])

            reply = snapshot["reply"]
            # Not every action must answer someone: the reply-chance setting
            # mixes replies with standalone on-topic messages.
            if reply is not None and random.randint(1, 100) > self.reply_chance:
                reply = None

            context_entries = snapshot["entries"]
            thread = None
            if reply is not None:
                thread = await self._read_thread(entity, reply["message"])
                if thread is not None and thread["newest_own"]:
                    return {
                        "success": False,
                        "detail": "Пропуск: последнее сообщение в ветке наше; ждём ответа собеседников",
                    }
                if thread is not None and thread["entries"]:
                    context_entries = thread["entries"]
            await self._mark_read(entity, [entry["message"] for entry in context_entries[-CONTEXT_ENTRIES:]])

            reaction = None
            if reply is not None:
                reaction = await self._react_to_reply(entity, chat_key, reply["message"])
                if reaction is not None:
                    await asyncio.sleep(random.uniform(1.5, 4))

            context = " | ".join(
                f"{entry['name']}: {entry['text'][:200]}" for entry in context_entries[-CONTEXT_ENTRIES:]
            )
            text = await self.ai.generate_message(
                context=context,
                account_id=self.account_id,
                chat_key=chat_key,
                peer_name=peer_name,
                memory_enabled=self.memory_enabled,
                tone=await self._effective_tone(),
                reply_to=reply,
                chat_title=peer_name,
                own_recent=own_recent,
                first_in_chat=first_in_chat,
            )
            if not text:
                return {"success": False, "detail": "AI не сгенерировал текст"}

            await asyncio.sleep(random.uniform(10, 30))
            if reply is not None:
                await self.client.send_message(entity, text, reply_to=reply["message"].id)
            else:
                await self.client.send_message(entity, text)
            if self.account_id and getattr(self.ai, "db", None):
                await self.ai.db.record_ai_send(self.account_id, chat_key)
            if hasattr(self.ai, "remember_sent_message"):
                await self.ai.remember_sent_message(
                    account_id=self.account_id,
                    chat_key=chat_key,
                    text=text,
                    peer_name=peer_name,
                    memory_enabled=self.memory_enabled,
                )
            if reply is not None:
                reacted = f" с реакцией {reaction}" if reaction else ""
                return {"success": True, "detail": f"Ответ {reply['name']}{reacted} в {peer_name}: {text[:60]}"}
            return {"success": True, "detail": f"Сообщение отправлено в {peer_name}: {text[:60]}"}
        except (FloodWaitError, PeerFloodError, AuthKeyUnregisteredError):
            raise
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "detail": explain_error(exc)}
