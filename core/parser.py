"""Read only the participant lists Telegram makes available to the selected account."""

import asyncio
import json
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
    ChannelParticipantsSearch,
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
# Telegram caps the plain participant list on big groups; searching by first letters/digits reaches the rest.
SEARCH_QUERIES = tuple("abcdefghijklmnopqrstuvwxyz") + tuple("абвгдеёжзийклмнопрстуфхцчшщыэюя") + tuple("0123456789")
PAGE_DELAY = 0.5
SEARCH_DELAY = 1.5
SEARCH_STALL_PAGES = 3
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


def pending_sources(run, source_rows):
    """References of a saved member run that still need requesting (not completed, not exhausted)."""
    try:
        references = json.loads(run.get("sources") or "[]")
    except (TypeError, ValueError):
        return []
    limit = run.get("limit_per_source")
    saved = {str(row["reference"]): row for row in source_rows}
    result = []
    for reference in references:
        row = saved.get(str(reference))
        if row is not None:
            if row["status"] == "completed":
                continue
            exhausted = (limit and (row.get("fetched") or 0) >= limit) or (row.get("search_pos") or 0) >= len(
                SEARCH_QUERIES
            )
            if row["status"] == "partial" and exhausted:
                continue
        result.append(reference)
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
        "scam": bool(getattr(user, "scam", False)),
        "fake": bool(getattr(user, "fake", False)),
        "restricted": bool(getattr(user, "restricted", False)),
        "contact_require_premium": bool(getattr(user, "contact_require_premium", False)),
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
    exclude_scam=True,
    exclude_fake=True,
    exclude_restricted=True,
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
            or exclude_scam
            and row.get("scam")
            or exclude_fake
            and row.get("fake")
            or exclude_restricted
            and row.get("restricted")
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
        if (
            query
            and query not in f"{row['username']} {row['first_name']} {row['last_name']} {row['user_id']}".casefold()
        ):
            continue
        result.append(row)  # no per-row copy: callers build new dicts via unique_members
    return result


def audience_usernames(rows, *, include_admins=False):
    """Usernames safe to import as an audience; returns (names, skipped counts by reason)."""
    names, skipped = [], {"admins": 0, "bots": 0, "deleted": 0, "no_username": 0, "invalid": 0}
    for row in rows:
        roles = set(row.get("roles") or ()) | {row.get("role")}
        if not include_admins and roles & {"creator", "admin"}:
            skipped["admins"] += 1
        elif row.get("is_bot"):
            skipped["bots"] += 1
        elif row.get("is_deleted"):
            skipped["deleted"] += 1
        elif not row.get("username"):
            skipped["no_username"] += 1
        else:
            try:
                names.append(normalize_username(row["username"]))
            except ValueError:
                skipped["invalid"] += 1
    return list(dict.fromkeys(names)), skipped


def audience_members(rows, *, include_admins=False):
    """Like audience_usernames, but keeps Telegram identity (id, activity, source) for the audience table."""
    names, skipped = audience_usernames(rows, include_admins=include_admins)
    allowed = set(names)
    members, seen = [], set()
    for row in rows:
        try:
            username = normalize_username(row["username"]) if row.get("username") else ""
        except ValueError:
            continue
        if username not in allowed or username in seen:
            continue
        seen.add(username)
        members.append(
            {
                "username": username,
                "user_id": row.get("user_id"),
                "activity": row.get("activity"),
                "source": " | ".join(row.get("sources") or [])[:200] or None,
                "last_seen": row.get("last_seen"),
                "last_message_at": row.get("last_message_at"),
                "parse_seq": row.get("parse_seq"),
            }
        )
    return members, skipped


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
        if row.get("parse_seq") is not None:
            person["parse_seq"] = min(person.get("parse_seq", row["parse_seq"]), row["parse_seq"])
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

    @staticmethod
    def _rows(response, participants, seen, source_id):
        users = {user.id: user for user in response.users if isinstance(user, User)}
        batch = []
        for participant in participants:
            user_id = getattr(participant, "user_id", None)
            if user_id is None or user_id in seen:
                continue
            seen.add(user_id)
            row = member_row(users.get(user_id), participant, source_id)
            if row is not None:
                batch.append(row)
        return batch

    async def _read_pass(
        self,
        client,
        entity,
        participants_filter,
        cap,
        seen,
        source_id,
        save,
        *,
        offset=0,
        stall_pages=1,
        stop_short=False,
    ):
        """One paginated participants query. A pass stops on an empty page, after `stall_pages` pages with
        nothing new, or (for searches) on a short page. `save(batch, new_count)` commits every page."""
        stalled = 0
        while len(seen) < cap:
            limit = min(200, cap - len(seen))
            response = await asyncio.wait_for(
                client(GetParticipantsRequest(entity, participants_filter, offset, limit, 0)), 30
            )
            participants = response.participants
            if not participants:
                break
            previous = len(seen)
            batch = self._rows(response, participants, seen, source_id)
            await save(batch, len(seen) - previous)
            offset += len(participants)
            if len(seen) == previous:
                stalled += 1
                if stalled >= stall_pages:
                    break
            else:
                stalled = 0
            if stop_short and len(participants) < limit:
                break
            await asyncio.sleep(PAGE_DELAY)

    async def _read_source(
        self, client, entity, info, basic, *, cap, seen, source_id, save, search_pos=0, on_search=None
    ):
        """Recent list first; when it stalls below the advertised size, sweep search queries (letters, digits)."""
        if basic is not None:
            participants = basic.full_chat.participants.participants[:cap]
            if len(seen) < cap and participants:
                previous = len(seen)
                batch = self._rows(basic, participants, seen, source_id)
                await save(batch, len(seen) - previous)
            return
        await self._read_pass(client, entity, ChannelParticipantsRecent(), cap, seen, source_id, save, offset=len(seen))
        hint = info["count_hint"]
        if not hint:
            return
        for position in range(search_pos, len(SEARCH_QUERIES)):
            if len(seen) >= cap or len(seen) >= hint:
                break
            query = SEARCH_QUERIES[position]
            await asyncio.sleep(SEARCH_DELAY)
            self.progress(f"{info['title']}: поиск «{query}», получено {len(seen)} из ~{hint}")
            await self._read_pass(
                client,
                entity,
                ChannelParticipantsSearch(query),
                cap,
                seen,
                source_id,
                save,
                stall_pages=SEARCH_STALL_PAGES,
                stop_short=True,
            )
            if on_search is not None:
                await on_search(position + 1)

    async def collect_member_keys(self, account, reference, limit=MAX_RUN_RECORDS):
        """Read-only participant scan of one chat (nothing is stored) for excluding people already inside."""
        if isinstance(reference, str) and reference.strip().lstrip("-").isdigit():
            reference = int(reference.strip())
        manager = AccountManager(account, self.db)
        try:
            await manager.connect()
            entity, info, basic = await self.open_source(manager.client, reference)
            seen, usernames = set(), set()

            async def save(batch, new_count):
                usernames.update(row["username"] for row in batch if row["username"])

            await self._read_source(
                manager.client,
                entity,
                info,
                basic,
                cap=limit,
                seen=seen,
                source_id=info["source_id"],
                save=save,
            )
            hint = info["count_hint"]
            return {
                "title": info["title"],
                "user_ids": seen,
                "usernames": usernames,
                "count": len(seen),
                "partial": bool(hint and len(seen) < hint) or len(seen) >= limit,
            }
        except FloodWaitError as exc:
            await self.db.set_flood_wait(account["id"], exc.seconds)
            raise
        finally:
            await manager.disconnect()

    async def start(self, account, sources, limit):
        if not 1 <= limit <= MAX_PER_SOURCE or not 1 <= len(sources) <= MAX_SOURCES:
            raise ValueError("Некорректное число групп или лимит участников")
        return await self._run(account, sources, limit)

    async def resume(self, account, run_id):
        """Continue an interrupted member run: only sources that are not complete are requested again."""
        run = await self.db.get_parse_run(run_id)
        if run is None or run["mode"] != "members":
            raise ValueError("Продолжить можно только сохранённое получение участников")
        if run["status"] == "running":
            raise ValueError("Этот запуск уже выполняется")
        pending = pending_sources(run, await self.db.get_parse_sources(run_id))
        if not pending:
            raise ValueError("Все источники этого запуска уже получены")
        limit = min(max(int(run.get("limit_per_source") or 500), 1), MAX_PER_SOURCE)
        return await self._run(account, pending, limit, run_id=run_id)

    async def _run(self, account, sources, limit, run_id=None):
        self._task = asyncio.current_task()
        self.run_id = None
        manager = AccountManager(account, self.db)
        outcome, detail, budget, successful, collected = "completed", "", MAX_RUN_RECORDS, 0, 0
        seen_sources = set()
        resumed = run_id is not None
        try:
            if resumed:
                await self.db.reopen_parse_run(run_id, account["id"])
                self.run_id = run_id
                budget = max(0, MAX_RUN_RECORDS - await self.db.count_parsed_members(run_id))
                saved_sources = {str(row["reference"]): row for row in await self.db.get_parse_sources(run_id)}
                seen_sources = {
                    row["source_id"]
                    for row in saved_sources.values()
                    if row["source_id"] is not None and row["status"] == "completed"
                }
            else:
                saved_sources = {}
                self.run_id = await self.db.create_parse_run(account["id"], sources, limit_per_source=limit)
            await manager.connect()
            for reference in sources:
                source_id = None
                try:
                    entity, info, basic = await self.open_source(manager.client, reference)
                    source_id = info["source_id"]
                    if source_id in seen_sources:
                        continue
                    seen_sources.add(source_id)
                    seen = await self.db.get_parse_resume_state(self.run_id, source_id) if resumed else set()
                    search_pos = int((saved_sources.get(str(reference)) or {}).get("search_pos") or 0)
                    await self.db.save_parse_source(self.run_id, str(reference), info, "running", "", len(seen))
                    self.progress(f"Получение участников: {info['title']}")
                    cap = min(limit, budget + len(seen))

                    async def save(batch, new_count, reference=reference, info=info, seen=seen):
                        nonlocal collected, budget
                        await self.db.save_parsed_members(self.run_id, batch)
                        collected += len(batch)
                        budget -= new_count
                        await self.db.save_parse_source(
                            self.run_id, str(reference), info, "running", "Получение доступного списка", len(seen)
                        )
                        self.progress(f"{info['title']}: получено {len(seen)}; результаты сохраняются")

                    async def remember_search(position, reference=reference):
                        await self.db.set_parse_search_pos(self.run_id, str(reference), position)

                    await self._read_source(
                        manager.client,
                        entity,
                        info,
                        basic,
                        cap=cap,
                        seen=seen,
                        source_id=source_id,
                        save=save,
                        search_pos=search_pos,
                        on_search=remember_search,
                    )
                    count = len(seen)
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
                outcome = "partial" if resumed else "failed"
            if resumed and outcome == "completed":
                rows = await self.db.get_parse_sources(self.run_id)
                if any(row["status"] != "completed" for row in rows):
                    outcome = "partial"
        except asyncio.CancelledError:
            outcome, detail = "stopped", "Остановлено. Уже полученные записи сохранены."
        except Exception as exc:
            outcome, detail = "partial" if collected or resumed else "failed", access_error(exc)
        finally:
            try:
                await manager.disconnect()
            finally:
                if self.run_id is not None:
                    await self.db.finish_parse_run(self.run_id, outcome, detail)
                self._task = None
        return {"run_id": self.run_id, "status": outcome, "detail": detail}
