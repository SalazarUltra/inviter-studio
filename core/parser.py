"""Read only the participant lists Telegram makes available to the selected account."""

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone

from telethon import utils
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    PeerFloodError,
    RPCError,
    SessionRevokedError,
    UserDeactivatedBanError,
)
from telethon.tl.functions.channels import GetFullChannelRequest, GetParticipantsRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.types import (
    Channel,
    ChannelParticipantAdmin,
    ChannelParticipantCreator,
    ChannelParticipantsRecent,
    Chat,
    ChatParticipantAdmin,
    ChatParticipantCreator,
    ChatParticipants,
    User,
    UserProfilePhoto,
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)

from core.account_manager import AccountManager
from core.user_feedback import explain_error
from core.validation import normalize_username

MAX_SOURCES = 10
MAX_PER_SOURCE = 5000
MAX_RUN_RECORDS = 20000
ACTIVITY_LABELS = {
    "online": "Онлайн",
    "recently": "Недавно",
    "week": "На этой неделе",
    "month": "В этом месяце",
    "offline": "Есть точное время",
    "unknown": "Не видно",
}
ROLE_LABELS = {"creator": "Создатель", "admin": "Администратор", "member": "Участник", "unknown": "Роль не проверялась"}
ACCOUNT_ERRORS = (AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedBanError, PeerFloodError)


def parse_sources(text):
    result = []
    for line_number, line in enumerate(text.splitlines(), 1):
        value = line.strip()
        if not value:
            continue
        try:
            if value.startswith("-") and value[1:].isdigit():
                reference = int(value)
                if not -(2**63) < reference < 0:
                    raise ValueError("Некорректный ID чата")
            else:
                reference = normalize_username(value)
        except ValueError:
            raise ValueError(
                f"Строка {line_number}: укажите @адрес публичной группы или выберите приватную в «Моих группах»"
            ) from None
        if reference not in result:
            result.append(reference)
    if not 1 <= len(result) <= MAX_SOURCES:
        raise ValueError(f"Укажите от 1 до {MAX_SOURCES} групп, по одной на строку")
    return result


def member_row(user, participant, source_id):
    if not isinstance(user, User) or user.is_self:
        return None
    activity, last_seen = "unknown", None
    status = user.status
    if isinstance(status, UserStatusOnline):
        activity = "online"
    elif isinstance(status, UserStatusRecently):
        activity = "recently"
    elif isinstance(status, UserStatusLastWeek):
        activity = "week"
    elif isinstance(status, UserStatusLastMonth):
        activity = "month"
    elif isinstance(status, UserStatusOffline):
        activity = "offline"
        if status.was_online is not None:
            last_seen = (
                status.was_online.replace(tzinfo=timezone.utc).isoformat()
                if status.was_online.tzinfo is None
                else status.was_online.astimezone(timezone.utc).isoformat()
            )
    role = (
        "creator"
        if isinstance(participant, (ChatParticipantCreator, ChannelParticipantCreator))
        else "admin"
        if isinstance(participant, (ChatParticipantAdmin, ChannelParticipantAdmin))
        else "member"
    )
    return {
        "source_id": source_id,
        "user_id": int(user.id),
        "username": (user.username or "").lower(),
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "role": role,
        "is_bot": bool(user.bot),
        "is_deleted": bool(user.deleted),
        "is_premium": bool(user.premium),
        "has_photo": isinstance(user.photo, UserProfilePhoto),
        "activity": activity,
        "last_seen": last_seen,
    }


def filter_members(
    rows,
    *,
    query="",
    only_username=True,
    exclude_bots=True,
    exclude_deleted=True,
    premium=False,
    photo=False,
    role="all",
    activity="all",
    source_id=None,
):
    result = []
    query = query.strip().casefold()
    now = datetime.now(timezone.utc)
    for row in rows:
        if source_id is not None and row["source_id"] != source_id:
            continue
        if (
            only_username
            and not row["username"]
            or exclude_bots
            and row["is_bot"]
            or exclude_deleted
            and row["is_deleted"]
        ):
            continue
        if premium and not row["is_premium"] or photo and not row["has_photo"]:
            continue
        if (
            role == "admins"
            and row["role"] not in {"creator", "admin"}
            or role == "members"
            and row["role"] != "member"
        ):
            continue
        if activity != "all":
            exact_recent = False
            if row.get("last_seen"):
                try:
                    seen = datetime.fromisoformat(row["last_seen"])
                    exact_recent = now - timedelta(days=7) <= seen <= now
                except (ValueError, TypeError):
                    pass
            if activity == "exact_week":
                if not exact_recent:
                    continue
            elif activity == "recent":
                if row["activity"] not in {"online", "recently", "week"} and not exact_recent:
                    continue
            elif row["activity"] != activity:
                continue
        if query not in f"{row['username']} {row['first_name']} {row['last_name']} {row['user_id']}".casefold():
            continue
        result.append(dict(row))
    return result


def source_label(row):
    return f"{row['source_title']} ({row['source_id']})" if row.get("source_title") else str(row["source_id"])


def unique_members(rows):
    people = {}
    for row in rows:
        person = people.setdefault(
            row["user_id"],
            {
                **row,
                "sources": [],
                "source_ids": [],
                "memberships": [],
                "roles": [],
                "message_count": 0,
                "first_message_at": None,
                "last_message_at": None,
            },
        )
        for field in ("username", "first_name", "last_name"):
            if not person.get(field):
                person[field] = row.get(field, "")
        if row["source_id"] not in person["source_ids"]:
            person["source_ids"].append(row["source_id"])
            person["sources"].append(source_label(row))
            person["memberships"].append(
                {
                    "source_id": row["source_id"],
                    "title": row.get("source_title", ""),
                    "role": row["role"],
                    "message_count": row.get("message_count", 0),
                    "first_message_at": row.get("first_message_at"),
                    "last_message_at": row.get("last_message_at"),
                }
            )
            person["message_count"] += row.get("message_count", 0) or 0
            for field, choose in (("first_message_at", min), ("last_message_at", max)):
                if row.get(field):
                    person[field] = choose(person[field], row[field]) if person[field] else row[field]
        if row["role"] not in person["roles"]:
            person["roles"].append(row["role"])
    return list(people.values())


def group_members(rows, by):
    people = unique_members(rows)
    if by == "source":
        return dict(Counter(label for person in people for label in person["sources"]))
    if by == "role":
        return dict(Counter(ROLE_LABELS.get(role, role) for person in people for role in person["roles"]))
    if by == "activity":
        return dict(Counter(ACTIVITY_LABELS.get(row["activity"], row["activity"]) for row in people))
    if by == "username":
        return dict(Counter("С никнеймом" if row["username"] else "Без никнейма" for row in people))
    if by == "premium":
        return dict(Counter("Premium отмечен" if row["is_premium"] else "Premium не отмечен" for row in people))
    if by == "messages":
        return dict(
            Counter(
                "Не считались"
                if not row["message_count"]
                else "1 сообщение"
                if row["message_count"] == 1
                else "2–4 сообщения"
                if row["message_count"] < 5
                else "5–19 сообщений"
                if row["message_count"] < 20
                else "20+ сообщений"
                for row in people
            )
        )
    return {"Уникальных людей": len(people)}


def access_error(exc):
    if isinstance(exc, ACCOUNT_ERRORS):
        return "Аккаунт ограничен или сессия отозвана. Получение остановлено; проверьте аккаунт и его авторизацию."
    if isinstance(exc, ChannelPrivateError):
        return "Группа приватная, аккаунт не состоит в ней или доступ отозван. Вступите вручную и выберите её из «Моих групп»."
    if isinstance(exc, ChatAdminRequiredError):
        return (
            "Telegram требует права администратора для списка участников. Обычное вступление может не дать этот доступ."
        )
    if isinstance(exc, FloodWaitError):
        return f"Telegram требует паузу {exc.seconds} сек. Получение остановлено; подождите перед новым запуском."
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return "Соединение прервано или истекло время ожидания. Полученные записи сохранены; проверьте подключение."
    if isinstance(exc, RPCError):
        return "Telegram отклонил запрос. Проверьте доступ аккаунта."
    if isinstance(exc, (ValueError, RuntimeError)):
        return explain_error(exc)
    return "Не удалось получить участников: " + explain_error(exc)


class ParserEngine:
    def __init__(self, db, progress=None):
        self.db = db
        self.progress = progress or (lambda text: None)
        self._task = None
        self.run_id = None

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()

    async def open_source(self, client, reference):
        entity = await asyncio.wait_for(client.get_entity(reference), 20)
        if not isinstance(entity, (Chat, Channel)):
            raise ValueError("Адрес относится к пользователю, а не группе или каналу")
        if isinstance(entity, Chat) and (entity.left or entity.deactivated):
            raise ValueError("Выбранный аккаунт не имеет доступа к этой обычной группе")
        if isinstance(entity, Channel) and entity.left and not entity.username:
            raise ValueError(
                "Приватный чат недоступен без вступления выбранного аккаунта. Вступите вручную и обновите «Мои группы»"
            )
        request = GetFullChannelRequest(entity) if isinstance(entity, Channel) else GetFullChatRequest(entity.id)
        response = await asyncio.wait_for(client(request), 25)
        full = response.full_chat
        if isinstance(entity, Channel):
            admin = bool(entity.creator or entity.admin_rights)
            if full.participants_hidden and not admin:
                raise ValueError(
                    "Список участников скрыт администратором. Для выбранного аккаунта обычный список недоступен"
                )
            if not full.can_view_participants:
                raise ValueError(
                    "Telegram не разрешает этому аккаунту просмотр участников. Для канала обычно нужны права администратора"
                )
            total = full.participants_count
            basic = None
        else:
            if not isinstance(full.participants, ChatParticipants):
                raise ValueError("Telegram не открыл список участников обычной группы")
            total = len(full.participants.participants)
            basic = response
        return (
            entity,
            {
                "source_id": utils.get_peer_id(entity),
                "title": entity.title,
                "chat_type": "channel" if getattr(entity, "broadcast", False) else "group",
                "membership": "not_joined" if entity.left else "joined",
                "count_hint": total,
            },
            basic,
        )

    async def check(self, account, reference):
        manager = AccountManager(account, self.db)
        try:
            await manager.connect()
            entity, info, basic = await self.open_source(manager.client, reference)
            if basic is None:
                await asyncio.wait_for(
                    manager.client(GetParticipantsRequest(entity, ChannelParticipantsRecent(), 0, 1, 0)), 25
                )
            return info
        except FloodWaitError as exc:
            await self.db.set_flood_wait(account["id"], exc.seconds)
            raise
        finally:
            await manager.disconnect()

    async def start(self, account, sources, limit):
        if not 1 <= limit <= MAX_PER_SOURCE or not 1 <= len(sources) <= MAX_SOURCES:
            raise ValueError("Некорректное число групп или лимит участников")
        self._task = asyncio.current_task()
        manager = AccountManager(account, self.db)
        outcome, detail, budget, successful, collected = "completed", "", MAX_RUN_RECORDS, 0, 0
        seen_sources = set()
        try:
            self.run_id = await self.db.create_parse_run(account["id"], sources)
            await manager.connect()
            for reference in sources:
                source_id = None
                try:
                    entity, info, basic = await self.open_source(manager.client, reference)
                    source_id = info["source_id"]
                    if source_id in seen_sources:
                        continue
                    seen_sources.add(source_id)
                    await self.db.save_parse_source(self.run_id, str(reference), info, "running", "")
                    self.progress(f"Получение участников: {info['title']}")
                    count, offset, seen = 0, 0, set()
                    cap = min(limit, budget)
                    while count < cap:
                        if basic is not None:
                            response, participants = basic, basic.full_chat.participants.participants[:cap]
                        else:
                            response = await asyncio.wait_for(
                                manager.client(
                                    GetParticipantsRequest(
                                        entity, ChannelParticipantsRecent(), offset, min(200, cap - count), 0
                                    )
                                ),
                                30,
                            )
                            participants = response.participants
                        if not participants:
                            break
                        users = {user.id: user for user in response.users if isinstance(user, User)}
                        batch = []
                        previous = len(seen)
                        for participant in participants:
                            user_id = getattr(participant, "user_id", None)
                            if user_id is None or user_id in seen:
                                continue
                            seen.add(user_id)
                            row = member_row(users.get(user_id), participant, source_id)
                            if row is not None:
                                batch.append(row)
                        await self.db.save_parsed_members(self.run_id, batch)
                        collected += len(batch)
                        count = len(seen)
                        budget -= count - previous
                        await self.db.save_parse_source(
                            self.run_id, str(reference), info, "running", "Получение доступного списка", count
                        )
                        offset += len(participants)
                        self.progress(f"{info['title']}: получено {count}; результаты сохраняются")
                        if basic is not None or len(seen) == previous:
                            break
                        await asyncio.sleep(0.5)
                    partial = info["count_hint"] is None or count < info["count_hint"] or basic is None and count >= cap
                    source_detail = (
                        "Получена доступная часть списка. Полнота не гарантируется."
                        if partial
                        else "Получен доступный Telegram список на момент запроса."
                    )
                    await self.db.save_parse_source(
                        self.run_id, str(reference), info, "partial" if partial else "completed", source_detail, count
                    )
                    successful += 1
                    if partial:
                        outcome = "partial"
                    if budget <= 0:
                        detail = "Достигнут общий предел 20000 записей; оставшиеся группы не запрашивались."
                        outcome = "partial"
                        break
                except FloodWaitError as exc:
                    await self.db.set_flood_wait(account["id"], exc.seconds)
                    detail = access_error(exc)
                    await self.db.save_parse_source(
                        self.run_id, str(reference), {"source_id": source_id}, "failed", detail
                    )
                    outcome = "partial" if successful or collected else "failed"
                    break
                except ACCOUNT_ERRORS as exc:
                    detail = access_error(exc)
                    await self.db.save_parse_source(
                        self.run_id, str(reference), {"source_id": source_id}, "failed", detail
                    )
                    outcome = "partial" if successful or collected else "failed"
                    break
                except (ValueError, RPCError, asyncio.TimeoutError, ConnectionError, OSError) as exc:
                    detail = access_error(exc)
                    await self.db.save_parse_source(
                        self.run_id, str(reference), {"source_id": source_id}, "failed", detail
                    )
                    outcome = "partial"
                    self.progress(detail)
                if budget <= 0:
                    outcome, detail = (
                        "partial",
                        "Достигнут общий предел 20000 записей; оставшиеся группы не запрашивались.",
                    )
                    break
            if not successful and not collected and outcome != "stopped":
                outcome = "failed"
        except asyncio.CancelledError:
            outcome, detail = "stopped", "Остановлено. Уже полученные записи сохранены."
        except Exception as exc:
            outcome, detail = "partial" if collected else "failed", access_error(exc)
        finally:
            try:
                await manager.disconnect()
            finally:
                if self.run_id is not None:
                    await self.db.finish_parse_run(self.run_id, outcome, detail)
                self._task = None
        return {"run_id": self.run_id, "status": outcome, "detail": detail}
