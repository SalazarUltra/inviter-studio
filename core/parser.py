"""Read only the participant lists Telegram makes available to the selected account."""

import asyncio
import json
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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
from core.validation import id_recipient, normalize_username

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


def audience_members(rows, *, include_admins=False, include_id_only=False):
    """Like audience_usernames, but keeps Telegram identity (id, activity, source) for the audience table.

    With `include_id_only`, people without a @username but with a known Telegram id are kept too (their
    «username» is the stored key «id:<number>»); bots and deleted accounts are still skipped.
    """
    names, skipped = audience_usernames(rows, include_admins=include_admins)
    allowed = set(names)
    members, seen = [], set()
    id_only_added = 0
    for row in rows:
        try:
            username = normalize_username(row["username"]) if row.get("username") else ""
        except ValueError:
            username = ""
            if not include_id_only:
                continue
        if not username:
            user_id = row.get("user_id")
            roles = set(row.get("roles") or ()) | {row.get("role")}
            if not (
                include_id_only
                and user_id
                and not row.get("is_bot")
                and not row.get("is_deleted")
                and (include_admins or not roles & {"creator", "admin"})
            ):
                continue
            username = id_recipient(int(user_id))
            if username not in seen:
                id_only_added += 1
        elif username not in allowed:
            continue
        if username in seen:
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
    if id_only_added:
        skipped = dict(skipped)
        skipped["id_only"] = id_only_added
        skipped["no_username"] = max(0, skipped["no_username"] - id_only_added)
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
        """Collect members. `account` is one account dict, or a list: sources are then shared between accounts."""
        if not 1 <= limit <= MAX_PER_SOURCE or not 1 <= len(sources) <= MAX_SOURCES:
            raise ValueError("Некорректное число групп или лимит участников")
        return await self._run(_account_list(account), sources, limit)

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
        return await self._run(_account_list(account), pending, limit, run_id=run_id)

    async def _run(self, accounts, sources, limit, run_id=None):
        self._task = asyncio.current_task()
        self.run_id = None
        ctx = SimpleNamespace(
            limit=limit,
            outcome="completed",
            detail="",
            budget=MAX_RUN_RECORDS,  # records not yet reserved by a source
            outstanding=0,  # records reserved by sources that are being read right now
            successful=0,
            collected=0,
            seen_sources=set(),
            saved_sources={},
            resumed=run_id is not None,
            stop=False,
            alive={account["id"] for account in accounts},
            orphans=deque(),  # sources handed over by an account that hit a limit
            handed_over=set(),  # references that an account started and left half-read
            notes=[],
        )
        try:
            if ctx.resumed:
                await self.db.reopen_parse_run(run_id, accounts[0]["id"])
                self.run_id = run_id
                ctx.budget = max(0, MAX_RUN_RECORDS - await self.db.count_parsed_members(run_id))
                ctx.saved_sources = {str(row["reference"]): row for row in await self.db.get_parse_sources(run_id)}
                ctx.seen_sources = {
                    row["source_id"]
                    for row in ctx.saved_sources.values()
                    if row["source_id"] is not None and row["status"] == "completed"
                }
            else:
                self.run_id = await self.db.create_parse_run(accounts[0]["id"], sources, limit_per_source=limit)
            queues = {
                account_id: deque(items)
                for account_id, items in split_sources_round_robin(sources, [a["id"] for a in accounts]).items()
            }
            await self._drain(accounts, queues, ctx)
            while ctx.orphans and ctx.alive and not ctx.stop:
                # Sources handed over after the other workers had already finished: a second pass.
                survivors = [account for account in accounts if account["id"] in ctx.alive]
                await self._drain(survivors, {}, ctx)
            if ctx.orphans and not ctx.stop:
                ctx.outcome = "partial"
                ctx.detail = ctx.detail or "Не все источники запрошены: аккаунты достигли ограничений Telegram."
            if not ctx.successful and not ctx.collected and ctx.outcome != "stopped":
                ctx.outcome = "partial" if ctx.resumed else "failed"
            if ctx.resumed and ctx.outcome == "completed":
                rows = await self.db.get_parse_sources(self.run_id)
                if any(row["status"] != "completed" for row in rows):
                    ctx.outcome = "partial"
            if ctx.notes:
                ctx.detail = (ctx.detail + " " if ctx.detail else "") + "Аккаунты: " + "; ".join(ctx.notes)
        except asyncio.CancelledError:
            ctx.outcome, ctx.detail = "stopped", "Остановлено. Уже полученные записи сохранены."
        except Exception as exc:
            ctx.outcome = "partial" if ctx.collected or ctx.resumed else "failed"
            ctx.detail = access_error(exc)
        finally:
            if self.run_id is not None:
                await self.db.finish_parse_run(self.run_id, ctx.outcome, ctx.detail)
            self._task = None
        return {"run_id": self.run_id, "status": ctx.outcome, "detail": ctx.detail}

    async def _drain(self, accounts, queues, ctx):
        """Run one worker per account until its own list and the shared hand-over list are empty."""
        if len(accounts) == 1:  # single account: exactly the old sequential behaviour, in the same task
            await self._worker(accounts[0], queues.setdefault(accounts[0]["id"], deque()), ctx)
            return
        tasks = [
            asyncio.ensure_future(self._worker(account, queues.setdefault(account["id"], deque()), ctx))
            for account in accounts
        ]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _worker(self, account, own, ctx):
        if (not own and not ctx.orphans) or ctx.stop:
            return
        manager = AccountManager(account, self.db)
        try:
            try:
                await manager.connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._hand_over(account, own, None, exc, ctx):
                    return
                raise
            while not ctx.stop and ctx.alive:
                if own:
                    reference = own.popleft()
                elif ctx.orphans:
                    reference = ctx.orphans.popleft()
                else:
                    break
                if await self._process_source(manager, account, reference, own, ctx) != "ok":
                    break
        finally:
            await manager.disconnect()

    def _hand_over(self, account, own, reference, exc, ctx) -> bool:
        """Give this account's unfinished sources to the other accounts (False when none is left)."""
        if not ctx.alive - {account["id"]}:
            return False
        ctx.alive.discard(account["id"])
        phone = account.get("phone") or account["id"]
        detail = access_error(exc)
        ctx.notes.append(f"{phone}: {detail}")
        pending = ([reference] if reference is not None else []) + list(own)
        own.clear()
        ctx.handed_over.update(item for item in pending if item is not None)
        ctx.orphans.extendleft(reversed(pending))  # they run before sources nobody has started
        self.progress(f"{phone}: {detail} Источники переданы другим аккаунтам: {len(pending)}")
        return True

    async def _process_source(self, manager, account, reference, own, ctx):
        """Read one source. Returns «ok» to continue, anything else to stop this account's loop."""
        source_id = None
        initial, seen, reserved = 0, set(), 0
        try:
            # Several accounts reserve from one record budget: wait while it is fully reserved elsewhere.
            while ctx.budget <= 0 and ctx.outstanding > 0:
                await asyncio.sleep(0.2)
            entity, info, basic = await self.open_source(manager.client, reference)
            source_id = info["source_id"]
            if source_id in ctx.seen_sources:
                return "ok"
            ctx.seen_sources.add(source_id)
            continuing = ctx.resumed or reference in ctx.handed_over
            seen = await self.db.get_parse_resume_state(self.run_id, source_id) if continuing else set()
            initial = len(seen)
            saved = ctx.saved_sources.get(str(reference))
            if reference in ctx.handed_over:
                saved = {str(row["reference"]): row for row in await self.db.get_parse_sources(self.run_id)}.get(
                    str(reference)
                )
            search_pos = int((saved or {}).get("search_pos") or 0)
            await self.db.save_parse_source(self.run_id, str(reference), info, "running", "", len(seen))
            self.progress(f"Получение участников: {info['title']}")
            cap = min(ctx.limit, ctx.budget + len(seen))
            reserved = max(0, cap - len(seen))
            ctx.budget -= reserved
            ctx.outstanding += reserved

            async def save(batch, new_count, reference=reference, info=info, seen=seen):
                ctx.collected += len(batch)
                await self.db.save_parsed_members(self.run_id, batch)
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
            ctx.successful += 1
            if partial:
                ctx.outcome = "partial"
        except FloodWaitError as exc:
            await self.db.set_flood_wait(account["id"], exc.seconds)
            return await self._account_limited(account, reference, source_id, own, exc, ctx)
        except ACCOUNT_ERRORS as exc:
            return await self._account_limited(account, reference, source_id, own, exc, ctx)
        except (ValueError, RPCError, asyncio.TimeoutError, ConnectionError, OSError) as exc:
            ctx.detail = access_error(exc)
            await self.db.save_parse_source(self.run_id, str(reference), {"source_id": source_id}, "failed", ctx.detail)
            ctx.outcome = "partial"
            self.progress(ctx.detail)
        finally:
            # Unused part of the reservation goes back to the shared budget.
            ctx.outstanding -= reserved
            ctx.budget += max(0, reserved - max(0, len(seen) - initial))
        if ctx.budget <= 0 and ctx.outstanding <= 0:
            ctx.outcome = "partial"
            ctx.detail = "Достигнут общий предел 20000 записей; оставшиеся группы не запрашивались."
            ctx.stop = True
            return "budget"
        return "ok"

    async def _account_limited(self, account, reference, source_id, own, exc, ctx):
        """FloodWait / revoked session on one account: hand its sources to the others, or stop the run."""
        if self._hand_over(account, own, reference, exc, ctx):
            # The next account reads this source from scratch (rows already saved are skipped via resume state).
            ctx.seen_sources.discard(source_id)
            return "handed_over"
        ctx.detail = access_error(exc)
        await self.db.save_parse_source(self.run_id, str(reference), {"source_id": source_id}, "failed", ctx.detail)
        ctx.outcome = "partial" if ctx.successful or ctx.collected else "failed"
        ctx.stop = True
        return "account_limited"


def _account_list(account) -> list[dict]:
    """One account dict or an iterable of them -> unique, non-empty list."""
    accounts = [account] if isinstance(account, dict) else list(account or ())
    unique = list({item["id"]: item for item in accounts}.values())
    if not unique:
        raise ValueError("Выберите хотя бы один Telegram-аккаунт")
    return unique


def split_sources_round_robin(sources, account_ids) -> dict:
    """Share sources between accounts in turn: the 1st source to the 1st account, the 2nd to the 2nd, ..."""
    ids = list(dict.fromkeys(account_ids))
    shares = {account_id: [] for account_id in ids}
    for index, source in enumerate(sources):
        if ids:
            shares[ids[index % len(ids)]].append(source)
    return shares
