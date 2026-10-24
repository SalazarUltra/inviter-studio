import asyncio
import json
import random
from typing import Any

import socks
from telethon import TelegramClient, utils
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerFloodError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    RPCError,
    SessionPasswordNeededError,
    UserAlreadyParticipantError,
    UserIsBlockedError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserNotParticipantError,
    UserPrivacyRestrictedError,
    YouBlockedUserError,
)
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest, SendMessageRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Channel, Chat, InputPeerUser, InputUser, PeerUser, UpdateMessageID, User

from core.paths import SESSIONS_DIR, session_path
from core.paths import session_name as normalize_session_name
from core.user_feedback import explain_error
from core.validation import normalize_username, validate_message, validate_proxy

DEVICE_PROFILES = [
    {"device_model": "Samsung Galaxy S21", "system_version": "Android 12", "app_version": "9.3.3"},
    {"device_model": "Samsung Galaxy S22", "system_version": "Android 13", "app_version": "10.1.2"},
    {"device_model": "Xiaomi 12 Pro", "system_version": "Android 12", "app_version": "9.6.7"},
    {"device_model": "Redmi Note 11", "system_version": "Android 11", "app_version": "9.2.1"},
    {"device_model": "HUAWEI P50", "system_version": "Android 11", "app_version": "9.1.0"},
    {"device_model": "OnePlus 10 Pro", "system_version": "Android 13", "app_version": "10.0.1"},
    {"device_model": "Google Pixel 6", "system_version": "Android 13", "app_version": "9.8.4"},
    {"device_model": "iPhone 13", "system_version": "16.0", "app_version": "9.3.3"},
    {"device_model": "iPhone 14 Pro", "system_version": "16.2", "app_version": "9.5.0"},
    {"device_model": "iPhone 12", "system_version": "15.4", "app_version": "9.1.5"},
]


def get_random_device() -> dict[str, str]:
    return random.choice(DEVICE_PROFILES)


def is_followup_reply(message, user_id, first_id):
    """Only a new incoming private user message counts; edits/reactions/services do not."""
    return (
        isinstance(getattr(message, "peer_id", None), PeerUser)
        and message.peer_id.user_id == user_id
        and getattr(message, "sender_id", None) == user_id
        and not getattr(message, "out", True)
        and not getattr(message, "action", None)
        and isinstance(getattr(message, "id", None), int)
        and message.id > first_id > 0
    )


class AccountManager:
    _session_owners: dict[str, "AccountManager"] = {}

    @classmethod
    def is_session_busy(cls, phone: str, name: str | None = None) -> bool:
        return str(session_path(phone, name)) in cls._session_owners

    def __init__(self, account_data: dict[str, Any], db=None):
        self.account = account_data
        self.client: TelegramClient | None = None
        self.db = db
        self._owned_path: str | None = None

    def _build_proxy(self) -> tuple[Any, ...] | None:
        validate_proxy(self.account)
        proxy_type = (self.account.get("proxy_type") or "").upper()
        if not proxy_type:
            return None

        proxy_kind = socks.SOCKS5 if proxy_type == "SOCKS5" else socks.HTTP
        return (
            proxy_kind,
            self.account["proxy_host"],
            int(self.account["proxy_port"]),
            True,
            self.account.get("proxy_login"),
            self.account.get("proxy_password"),
        )

    def _build_session_name(self, phone: str, session_name: str | None) -> str:
        return normalize_session_name(phone, session_name)

    async def connect(self, require_authorized: bool = True) -> None:
        if self.client is not None:
            raise RuntimeError("Клиент уже подключён")
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        path = str(session_path(self.account["phone"], self.account.get("session_name")))
        if path in self._session_owners:
            raise RuntimeError("Аккаунт занят другой операцией; дождитесь её завершения")
        self._session_owners[path] = self
        self._owned_path = path
        try:
            proxy = self._build_proxy()
            device = None
            raw_device = self.account.get("device_profile")
            if raw_device:
                try:
                    candidate = json.loads(raw_device)
                    if isinstance(candidate, dict):
                        device = candidate
                except (ValueError, TypeError):
                    pass
            if device is None:
                device = get_random_device().copy()
                self.account["device_profile"] = json.dumps(device)
                if self.db and self.account.get("id"):
                    await self.db.update_device_profile(self.account["id"], json.dumps(device))
            self.client = TelegramClient(
                path,
                int(self.account["api_id"]),
                self.account["api_hash"],
                proxy=proxy,
                device_model=device.get("device_model", "Desktop"),
                system_version=device.get("system_version", "Windows"),
                app_version=device.get("app_version", "1.0"),
                lang_code="ru",
                system_lang_code="ru-RU",
                flood_sleep_threshold=0,
                request_retries=2,
                connection_retries=2,
            )
            await asyncio.wait_for(self.client.connect(), timeout=30)
            if require_authorized and not await asyncio.wait_for(self.client.is_user_authorized(), timeout=15):
                raise RuntimeError("Сессия не авторизована. Войдите в аккаунт заново")
        except BaseException:
            await self.disconnect()
            raise

    async def request_code(self, phone: str) -> None:
        if not self.client:
            raise RuntimeError("Нет соединения с Telegram. Повторите вход в аккаунт.")
        await self.client.send_code_request(phone)

    async def send_code(
        self,
        phone: str,
        api_id: str | int,
        api_hash: str,
        proxy: tuple[Any, ...] | dict[str, Any] | None = None,
    ) -> Any:
        if self.client is not None:
            await self.disconnect()
        self.account.update(phone=phone, api_id=api_id, api_hash=api_hash)
        if isinstance(proxy, dict):
            # Proxy is applied before connect(); set_proxy after connect would not
            # affect the already established connection.
            self.account.update(proxy)
        try:
            await self.connect(require_authorized=False)
            return await asyncio.wait_for(self.client.send_code_request(phone), timeout=30)
        except BaseException:
            await self.disconnect()
            raise

    async def authorize(
        self,
        phone: str,
        code: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        if not self.client:
            raise RuntimeError("Нет соединения с Telegram. Повторите вход в аккаунт.")

        try:
            if password:
                await asyncio.wait_for(self.client.sign_in(password=password), timeout=30)
            elif code:
                await asyncio.wait_for(self.client.sign_in(phone=phone, code=code), timeout=30)
            else:
                return {"status": "code_required"}

            if await asyncio.wait_for(self.client.is_user_authorized(), timeout=15):
                return {"status": "ok"}
            return {"status": "unauthorized"}
        except SessionPasswordNeededError:
            return {"status": "password_required"}
        except PhoneCodeInvalidError:
            return {"status": "invalid_code"}
        except PhoneCodeExpiredError:
            return {"status": "code_expired"}

    async def add_user_to_chat(self, chat: str, username: str) -> dict[str, Any]:
        if not self.client:
            return {"success": False, "error": "not_connected", "flood_seconds": None}

        resolving_user = True
        try:
            clean_username = normalize_username(username)
            user_entity = await asyncio.wait_for(self.client.get_entity(clean_username), timeout=30)
            if not isinstance(user_entity, User):
                return {"success": False, "error": "not_found", "message": "Никнейм принадлежит каналу или группе"}
            resolving_user = False
            chat_entity = await asyncio.wait_for(self.client.get_entity(chat), timeout=30)
            try:
                permissions = await asyncio.wait_for(
                    self.client.get_permissions(chat_entity, user_entity), timeout=30
                )
                if permissions and not permissions.has_left and not permissions.is_banned:
                    return {"success": False, "error": "already_in"}
            except UserNotParticipantError:
                pass
            if isinstance(chat_entity, Chat):
                request = AddChatUserRequest(chat_id=chat_entity.id, user_id=user_entity, fwd_limit=0)
            elif isinstance(chat_entity, Channel):
                request = InviteToChannelRequest(channel=chat_entity, users=[user_entity])
            else:
                return {"success": False, "error": "need_admin", "message": "Цель должна быть группой или каналом"}
            response = await asyncio.wait_for(self.client(request), timeout=30)
            if getattr(response, "missing_invitees", None):
                return {"success": False, "error": "private", "message": "Telegram не разрешил добавить пользователя"}
            return {"success": True, "error": None, "flood_seconds": None}
        except FloodWaitError as exc:
            seconds = max(1, int(exc.seconds))
            return {
                "success": False,
                "error": "flood",
                "flood_seconds": seconds,
                "message": f"Telegram требует ожидание {seconds} сек",
            }
        except PeerFloodError:
            # An account restriction is not a fixed server-specified waiting period.
            return {
                "success": False,
                "error": "peer_flood",
                "message": "Telegram отклонил действие с этого аккаунта; проверьте возможность отправки",
            }
        except AuthKeyUnregisteredError:
            return {"success": False, "error": "unauthorized", "message": "Сессия отозвана"}
        except (UsernameNotOccupiedError, UsernameInvalidError, ValueError) as exc:
            return {
                "success": False,
                "error": "not_found" if resolving_user else "need_admin",
                "message": explain_error(exc),
            }
        except UserPrivacyRestrictedError:
            return {"success": False, "error": "private"}
        except UserAlreadyParticipantError:
            return {"success": False, "error": "already_in"}
        except ChatAdminRequiredError:
            return {"success": False, "error": "need_admin"}
        except asyncio.TimeoutError:
            # The invite request may have reached Telegram; the outcome is unknown.
            return {
                "success": False,
                "error": "uncertain",
                "message": "Превышено время ожидания Telegram; результат неизвестен, проверьте вручную",
            }
        except Exception as exc:
            return {"success": False, "error": "unknown", "message": explain_error(exc)}

    async def resolve_recipient(self, username: str):
        if not self.client:
            raise RuntimeError("Клиент не подключён")
        user = await asyncio.wait_for(self.client.get_entity(normalize_username(username)), timeout=20)
        if not isinstance(user, User) or user.bot or user.deleted or user.is_self:
            raise ValueError("Получатель должен быть другим действующим пользователем")
        return user

    async def send_direct_message(self, user, text: str, random_id: int) -> dict:
        if not self.client:
            return {"success": False, "error": "unauthorized"}
        validate_message(text)
        try:
            response = await asyncio.wait_for(
                self.client(SendMessageRequest(peer=user, message=text, random_id=random_id, no_webpage=True)),
                timeout=30,
            )
            if response is None:
                return {"success": False, "error": "uncertain"}
            message_id = getattr(response, "id", None)
            if not isinstance(message_id, int):
                message_id = next(
                    (
                        update.id
                        for update in getattr(response, "updates", [])
                        if isinstance(update, UpdateMessageID) and update.random_id == random_id
                    ),
                    None,
                )
            return {"success": True, "message_id": message_id}
        except FloodWaitError as exc:
            return {"success": False, "error": "flood", "flood_seconds": max(1, int(exc.seconds))}
        except PeerFloodError:
            return {"success": False, "error": "peer_flood"}
        except AuthKeyUnregisteredError:
            return {"success": False, "error": "unauthorized"}
        except (UserPrivacyRestrictedError, UserIsBlockedError, YouBlockedUserError, ChatWriteForbiddenError):
            return {"success": False, "error": "private"}
        except RPCError:
            # Other RPC failures are conservatively treated as an unknown outcome.
            return {"success": False, "error": "uncertain"}
        except (asyncio.TimeoutError, OSError):
            return {"success": False, "error": "uncertain"}

    async def followup_history(self, row):
        peer = InputPeerUser(row["user_id"], row["access_hash"])
        last_id = max(row["first_message_id"], row["checked_id"])

        async def scan():
            nonlocal last_id
            async for message in self.client.iter_messages(peer, min_id=last_id, reverse=True, limit=30):
                last_id = max(last_id, getattr(message, "id", 0) or 0)
                if is_followup_reply(message, row["user_id"], row["first_message_id"]):
                    return {"last_id": last_id, "reply_id": message.id}
            return {"last_id": last_id, "reply_id": None}

        return await asyncio.wait_for(scan(), timeout=20)

    async def followup_peer(self, row):
        peer = InputUser(row["user_id"], row["access_hash"])
        result = await asyncio.wait_for(self.client(GetFullUserRequest(peer)), timeout=20)
        if getattr(result, "full_user", None) is None:
            raise RuntimeError("Не удалось проверить переписку")
        if getattr(result.full_user, "blocked", False) or getattr(result.full_user, "contact_require_premium", False):
            return None
        return InputPeerUser(row["user_id"], row["access_hash"])

    async def disconnect(self) -> None:
        try:
            if self.client:
                await self.client.disconnect()
        finally:
            self.client = None
            if self._owned_path and self._session_owners.get(self._owned_path) is self:
                del self._session_owners[self._owned_path]
            self._owned_path = None

    async def get_joined_dialogs(self) -> list[dict[str, Any]]:
        """
        Получить список всех групп и каналов аккаунта.
        """
        if not self.client:
            return []

        async def fetch() -> list[dict[str, Any]]:
            dialogs: list[dict[str, Any]] = []
            async for dialog in self.client.iter_dialogs():
                entity = dialog.entity
                if not isinstance(entity, (Channel, Chat)):
                    continue
                dialogs.append(
                    {
                        "id": utils.get_peer_id(entity),
                        "username": getattr(entity, "username", None) or "",
                        "title": dialog.name or "",
                        "type": "channel" if getattr(entity, "broadcast", False) else "group",
                        "members_count": getattr(entity, "participants_count", 0) or 0,
                    }
                )
            return dialogs

        try:
            return await asyncio.wait_for(fetch(), timeout=120)
        except asyncio.TimeoutError:
            raise RuntimeError("Не удалось получить список диалогов за отведённое время. Проверьте подключение") from None

    async def check_admin_rights(self, chat: str) -> dict[str, Any]:
        if not self.client:
            return {"ok": False, "reason": "Клиент не подключён"}
        try:
            entity = await self.client.get_entity(chat)
            permissions = await self.client.get_permissions(entity, "me")
            if permissions and permissions.is_creator:
                return {"ok": True, "reason": "Создатель чата"}
            if permissions and permissions.is_admin and permissions.invite_users:
                return {"ok": True, "reason": "Администратор с правом приглашения"}
            return {"ok": False, "reason": "Нет прав администратора с правом приглашения"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": "Проверка не удалась: " + explain_error(exc)}
