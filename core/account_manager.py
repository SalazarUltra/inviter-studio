import asyncio
import json
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import socks
from telethon import TelegramClient, utils
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    InputUserDeactivatedError,
    PeerFloodError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    RPCError,
    SessionPasswordNeededError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
    UserBotError,
    UserChannelsTooMuchError,
    UserDeactivatedError,
    UserIdInvalidError,
    UserIsBlockedError,
    UserKickedError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserNotMutualContactError,
    UserNotParticipantError,
    UserPrivacyRestrictedError,
    YouBlockedUserError,
)
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest, ExportChatInviteRequest, SendMessageRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Channel, Chat, InputPeerUser, InputUser, PeerUser, UpdateMessageID, User

from core.paths import SESSIONS_DIR, session_path
from core.paths import session_name as normalize_session_name
from core.user_feedback import explain_error
from core.validation import (
    is_id_recipient,
    normalize_username,
    recipient_user_id,
    validate_message,
    validate_proxy,
)

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


# Allowance for local-vs-Telegram clock difference when matching history by send time.
VERIFY_CLOCK_SKEW_SECONDS = 120


def get_random_device() -> dict[str, str]:
    return random.choice(DEVICE_PROFILES)


# Markers are matched against "<RPC message> <class name>" with underscores removed and upper-cased.
_PRIVATE_RPC_MARKERS = (
    "PRIVACY",  # USER_PRIVACY_RESTRICTED, PRIVACY_PREMIUM_REQUIRED
    "PREMIUMREQUIRED",
    "USERISBLOCKED",
    "YOUBLOCKEDUSER",
    "CHATWRITEFORBIDDEN",
)
_SKIP_RPC_MARKERS = (
    "DEACTIVATED",  # USER_DEACTIVATED, INPUT_USER_DEACTIVATED
    "USERDELETED",
    "PEERIDINVALID",
    "INVALIDPEER",
    "USERIDINVALID",
    "USERNAMENOTOCCUPIED",
    "USERNAMEINVALID",
    "USERISBOT",
    "USERBOT",
)


def rpc_error_text(exc: BaseException) -> str:
    """Raw Telegram RPC text (e.g. PEER_ID_INVALID) for logs; falls back to the class name."""
    text = str(getattr(exc, "message", None) or exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def classify_recipient_rpc(exc: BaseException) -> str | None:
    """'private' / 'skip' for a recipient-level RPC rejection, None when it is not one."""
    haystack = f"{getattr(exc, 'message', None) or ''} {exc} {type(exc).__name__}".upper().replace("_", "")
    if any(marker in haystack for marker in _PRIVATE_RPC_MARKERS):
        return "private"
    if any(marker in haystack for marker in _SKIP_RPC_MARKERS):
        return "skip"
    return None


_SERVER_RPC_MARKERS = (
    "INTERNAL",
    "RPCCALLFAIL",
    "RPCMCGETFAIL",
    "INTERDC",
    "SERVERERROR",
    "TIMEOUT",
)


def is_server_rpc_error(exc: BaseException) -> bool:
    """True for a 5xx / server-side RPC failure, where the message may or may not have been stored.

    Client errors (4xx) are definite rejections and are NOT matched here.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        if 500 <= code < 600:
            return True
        if 400 <= code < 500:
            return False
    haystack = f"{getattr(exc, 'message', None) or ''} {exc} {type(exc).__name__}".upper().replace("_", "")
    return any(marker in haystack for marker in _SERVER_RPC_MARKERS)


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
        # Chat entities resolved during one run; avoids get_entity(chat) per invite.
        self._chat_cache: dict[str, Any] = {}

    async def get_chat_entity(self, chat: str, *, refresh: bool = False):
        """Resolve the target chat once per connection and reuse it for later invites."""
        key = str(chat)
        if not refresh and key in self._chat_cache:
            return self._chat_cache[key]
        entity = await asyncio.wait_for(self.client.get_entity(chat), timeout=30)
        self._chat_cache[key] = entity
        return entity

    def invalidate_chat_cache(self, chat: str | None = None) -> None:
        if chat is None:
            self._chat_cache.clear()
        else:
            self._chat_cache.pop(str(chat), None)

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
                me = await asyncio.wait_for(self.client.get_me(), timeout=15)
                return {
                    "status": "ok",
                    "tg_first_name": str(getattr(me, "first_name", None) or "").strip(),
                    "tg_last_name": str(getattr(me, "last_name", None) or "").strip(),
                    "tg_username": str(getattr(me, "username", None) or "").strip().lstrip("@"),
                }
            return {"status": "unauthorized"}
        except SessionPasswordNeededError:
            return {"status": "password_required"}
        except PhoneCodeInvalidError:
            return {"status": "invalid_code"}
        except PhoneCodeExpiredError:
            return {"status": "code_expired"}

    async def add_user_to_chat(self, chat: str, username: str, user_id: int | None = None) -> dict[str, Any]:
        if not self.client:
            return {"success": False, "error": "not_connected", "flood_seconds": None}

        resolving_user = True
        try:
            # «id:<number>» recipients have no username: they are resolved by Telegram id only.
            user_id = user_id or recipient_user_id(username)
            clean_username = None if is_id_recipient(username) else normalize_username(username)
            user_entity = None
            if user_id:
                # A parsed Telegram id is exact; it only resolves when this account has seen the user,
                # so fall back to the username otherwise.
                try:
                    user_entity = await asyncio.wait_for(self.client.get_entity(PeerUser(int(user_id))), timeout=30)
                except (ValueError, TypeError):
                    user_entity = None
            if user_entity is None and clean_username is None:
                raise ValueError(
                    "Telegram не знает этот ID для аккаунта (нужна общая группа или контакт); "
                    "получатель без никнейма пропущен"
                )
            if user_entity is None:
                user_entity = await asyncio.wait_for(self.client.get_entity(clean_username), timeout=30)
            if not isinstance(user_entity, User):
                return {"success": False, "error": "not_found", "message": "Никнейм принадлежит каналу или группе"}
            resolving_user = False
            chat_entity = await self.get_chat_entity(chat)
            try:
                permissions = await asyncio.wait_for(self.client.get_permissions(chat_entity, user_entity), timeout=30)
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
            return {
                "success": False,
                "error": "private",
                "message": "Настройки приватности пользователя не разрешают добавление в чат",
            }
        except UserNotMutualContactError:
            return {
                "success": False,
                "error": "private",
                "message": "Пользователя можно добавить только из взаимных контактов",
            }
        except UserAlreadyParticipantError:
            return {"success": False, "error": "already_in"}
        except UserChannelsTooMuchError:
            return {
                "success": False,
                "error": "skip",
                "message": "Пользователь состоит в слишком большом числе чатов и каналов",
            }
        except UserKickedError:
            return {
                "success": False,
                "error": "skip",
                "message": "Пользователь был удалён из этого чата; повторное добавление запрещено",
            }
        except UserBotError:
            return {
                "success": False,
                "error": "skip",
                "message": "Это бот: ботов нельзя добавлять приглашением",
            }
        except (InputUserDeactivatedError, UserDeactivatedError, UserIdInvalidError):
            return {
                "success": False,
                "error": "skip",
                "message": "Аккаунт получателя удалён или недоступен",
            }
        except UserBannedInChannelError:
            # Telegram reports a restriction of the *inviting* account, not of the recipient.
            return {
                "success": False,
                "error": "account_banned",
                "message": "Аккаунту запрещено писать и приглашать в группы; проверьте ограничения в @SpamBot",
            }
        except ChatWriteForbiddenError:
            return {
                "success": False,
                "error": "need_admin",
                "message": "Аккаунту запрещено действовать в целевом чате",
            }
        except ChatAdminRequiredError:
            return {
                "success": False,
                "error": "need_admin",
                "message": "Для приглашения нужны права администратора в чате",
            }
        except asyncio.TimeoutError:
            # The invite request may have reached Telegram; the outcome is unknown.
            return {
                "success": False,
                "error": "uncertain",
                "message": "Превышено время ожидания Telegram; результат неизвестен, проверьте вручную",
            }
        except (ConnectionError, OSError):
            return {
                "success": False,
                "error": "network",
                "message": "Сбой соединения с Telegram; попытка будет повторена",
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

    async def send_saved_message(self, text: str) -> int | None:
        """Send ``text`` to this account's own «Избранное» (test send; nobody else receives it)."""
        if not self.client:
            raise RuntimeError("Клиент не подключён")
        validate_message(text)
        sent = await asyncio.wait_for(self.client.send_message("me", text, link_preview=False), timeout=30)
        message_id = getattr(sent, "id", None)
        return message_id if isinstance(message_id, int) else None

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
                return {"success": False, "error": "uncertain", "reason": "empty"}
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
        except RPCError as exc:
            # Telegram answered with a definite rejection: the message was not sent.
            # Recipient-level rejections are final; anything else is a failed attempt
            # for this recipient only. Neither must stop the whole campaign.
            detail = rpc_error_text(exc)
            kind = classify_recipient_rpc(exc)
            if kind == "private":
                return {"success": False, "error": "private", "message": detail}
            if kind == "skip":
                return {"success": False, "error": "skip", "message": detail}
            if is_server_rpc_error(exc):
                # 5xx / server timeout: Telegram may have stored the message anyway, so a blind
                # retry could duplicate it. Report an unknown outcome instead of a rejection.
                return {"success": False, "error": "uncertain", "reason": "server", "message": detail}
            return {"success": False, "error": "error", "message": detail}
        except asyncio.TimeoutError:
            return {"success": False, "error": "uncertain", "reason": "timeout"}
        except OSError:
            return {"success": False, "error": "uncertain", "reason": "network"}

    async def verify_sent_message(self, user, text: str, random_id: int | None, since) -> dict:
        """Look at the last few messages of the chat for our outgoing ``text`` sent after ``since``.

        Returns ``{"found": True, "message_id": id}``, ``{"found": False}`` when the history was
        read and the message is clearly absent, or ``{"found": None}`` when it could not be read.
        ``since`` is an aware datetime (send start); a small clock-skew allowance is applied.
        """
        if not self.client:
            return {"found": None}
        wanted = (text or "").strip()
        threshold = since - timedelta(seconds=VERIFY_CLOCK_SKEW_SECONDS)

        async def scan():
            async for message in self.client.iter_messages(user, limit=5):
                if not getattr(message, "out", False) or not isinstance(getattr(message, "id", None), int):
                    continue
                date = getattr(message, "date", None)
                if isinstance(date, datetime):
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    if date < threshold:
                        continue
                known_random = getattr(message, "random_id", None)
                if random_id is not None and isinstance(known_random, int) and known_random == random_id:
                    return {"found": True, "message_id": message.id}
                if (getattr(message, "message", None) or "").strip() == wanted:
                    return {"found": True, "message_id": message.id}
            return {"found": False}

        try:
            return await asyncio.wait_for(scan(), timeout=15)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - verification is best effort; failure means "unknown"
            return {"found": None}

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

    async def followup_catchup(
        self,
        rows,
        *,
        dialog_limit: int = 100,
        max_history: int = 15,
        history_delay: float = 0.5,
        since=None,
    ) -> dict:
        """Find replies to first messages that arrived while the app was offline.

        Walks the most recent dialogs (newest first, ``dialog_limit`` at most) and only looks at
        private chats with a person who still has a waiting follow-up. A reply that is already the
        top message of the dialog needs no extra request; otherwise the chat history after the last
        checked message is read (at most ``max_history`` such reads per pass, ``history_delay`` apart).
        Nothing is sent and nothing is marked read.

        Returns ``{"replies": {user_id: reply_id}, "checked": {user_id: last_id}, "dialogs": int,
        "history": int}``. Telegram limits (``FloodWaitError`` etc.) are raised to the caller.
        """
        result = {"replies": {}, "checked": {}, "dialogs": 0, "history": 0}
        if not self.client or not rows:
            return result
        wanted = {row["user_id"]: row for row in rows}
        threshold = None
        if isinstance(since, datetime):
            threshold = since if since.tzinfo else since.astimezone()
        handled: set[int] = set()

        async def scan():
            async for dialog in self.client.iter_dialogs(limit=dialog_limit):
                if len(handled) >= len(wanted):
                    break
                dialog_date = getattr(dialog, "date", None)
                if isinstance(dialog_date, datetime) and threshold is not None:
                    if (dialog_date if dialog_date.tzinfo else dialog_date.replace(tzinfo=timezone.utc)) < threshold:
                        break  # newest first: everything below is older than the first messages
                result["dialogs"] += 1
                entity = getattr(dialog, "entity", None)
                row = wanted.get(entity.id) if isinstance(entity, User) else None
                if row is None or row["user_id"] in handled:
                    continue
                user_id, first_id = row["user_id"], row["first_message_id"]
                checked = max(first_id, row.get("checked_id") or 0)
                top = getattr(dialog, "message", None)
                top_id = top.id if isinstance(getattr(top, "id", None), int) else 0
                if top_id <= checked:
                    handled.add(user_id)  # nothing new in this chat since the last check
                    continue
                if is_followup_reply(top, user_id, first_id):
                    handled.add(user_id)
                    result["replies"][user_id] = top_id
                    result["checked"][user_id] = top_id
                    continue
                if result["history"] >= max_history:
                    continue
                if result["history"]:
                    await asyncio.sleep(history_delay)
                result["history"] += 1
                handled.add(user_id)
                last_id = checked
                peer = InputPeerUser(user_id, row["access_hash"])
                async for message in self.client.iter_messages(peer, min_id=checked, reverse=True, limit=30):
                    last_id = max(last_id, getattr(message, "id", 0) or 0)
                    if is_followup_reply(message, user_id, first_id):
                        result["replies"][user_id] = message.id
                        break
                result["checked"][user_id] = last_id

        await asyncio.wait_for(scan(), timeout=90)
        return result

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
            self._chat_cache.clear()
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
            raise RuntimeError(
                "Не удалось получить список диалогов за отведённое время. Проверьте подключение"
            ) from None

    async def check_admin_rights(self, chat: str) -> dict[str, Any]:
        if not self.client:
            return {"ok": False, "reason": "Клиент не подключён"}
        try:
            entity = await self.client.get_entity(chat)
            # The invite loop reuses this entity instead of resolving the chat per user.
            self._chat_cache[str(chat)] = entity
            permissions = await self.client.get_permissions(entity, "me")
            if permissions and permissions.is_creator:
                return {"ok": True, "reason": "Создатель чата"}
            if permissions and permissions.is_admin and permissions.invite_users:
                return {"ok": True, "reason": "Администратор с правом приглашения"}
            return {"ok": False, "reason": "Нет прав администратора с правом приглашения"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": "Проверка не удалась: " + explain_error(exc)}

    async def export_invite_link(self, chat: str) -> str:
        """Create/export an invite link for the chat (needs the invite-users admin right)."""
        if not self.client:
            raise RuntimeError("Клиент не подключён")
        entity = await self.get_chat_entity(chat)
        exported = await asyncio.wait_for(self.client(ExportChatInviteRequest(peer=entity)), timeout=30)
        link = getattr(exported, "link", None)
        if not link:
            raise RuntimeError("Telegram не вернул ссылку-приглашение")
        return str(link)
