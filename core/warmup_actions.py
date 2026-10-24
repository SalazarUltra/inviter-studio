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
from telethon.tl.functions.messages import GetHistoryRequest, SendReactionRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Channel, InputUser, ReactionEmoji, User

from core.user_feedback import explain_error

REACTIONS = ["👍", "❤️", "🔥", "👏", "😍", "🤔", "💯"]


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
    ):
        self.client = client
        self.ai = ai_writer
        self.log_callback = log_callback
        self.account_id = account_id
        self.memory_enabled = memory_enabled
        self.tone = tone
        self.hourly_limit = max(1, int(hourly_limit or 6))
        self.chat_cooldown_min = max(1, int(chat_cooldown_min or 12))

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

            messages = await self.client(
                GetHistoryRequest(
                    peer=entity,
                    limit=5,
                    offset_date=None,
                    offset_id=0,
                    max_id=0,
                    min_id=0,
                    add_offset=0,
                    hash=0,
                )
            )
            context = " | ".join([m.message for m in messages.messages if hasattr(m, "message") and m.message][:3])
            text = await self.ai.generate_message(
                context=context,
                account_id=self.account_id,
                chat_key=chat_key,
                peer_name=peer_name,
                memory_enabled=self.memory_enabled,
                tone=self.tone,
            )
            if not text:
                return {"success": False, "detail": "AI не сгенерировал текст"}

            await asyncio.sleep(random.uniform(10, 30))
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
            return {"success": True, "detail": f"Сообщение отправлено в {channel}"}
        except (FloodWaitError, PeerFloodError, AuthKeyUnregisteredError):
            raise
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "detail": explain_error(exc)}
