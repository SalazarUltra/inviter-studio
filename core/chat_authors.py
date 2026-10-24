"""Collect visible user senders from accessible history without storing message contents."""

import asyncio
from datetime import datetime, timedelta, timezone

from telethon import utils
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.messages import GetHistoryRequest
from telethon.tl.types import Channel, Chat, Message, PeerUser, User

from core.account_manager import AccountManager
from core.parser import ACCOUNT_ERRORS, MAX_RUN_RECORDS, MAX_SOURCES, ParserEngine, access_error, member_row

MAX_HISTORY_MESSAGES = 10000
PERIOD_CHOICES = {
    "Последние 7 дней": 7,
    "Последние 30 дней": 30,
    "Последние 90 дней": 90,
    "Свои даты": "custom",
    "Вся доступная история": "all",
}


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def history_period(choice, start="", end="", now=None):
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        raise ValueError("Время должно содержать часовой пояс")
    selected = PERIOD_CHOICES.get(choice)
    if selected == "custom":
        try:
            begin_date, end_date = (
                datetime.strptime(start.strip(), "%Y-%m-%d"),
                datetime.strptime(end.strip(), "%Y-%m-%d"),
            )
        except ValueError:
            raise ValueError("Введите обе даты в формате ГГГГ-ММ-ДД") from None
        if begin_date > end_date:
            raise ValueError("Начало периода должно быть не позже конца")
        # astimezone() uses the computer's local offset at each boundary, including DST.
        return utc(begin_date.astimezone()), utc((end_date + timedelta(days=1)).astimezone())
    if selected == "all":
        return None, utc(now)
    if isinstance(selected, int):
        return utc(now) - timedelta(days=selected), utc(now)
    raise ValueError("Выберите период истории")


def filter_author_stats(people, *, min_messages=0, last_days=None, now=None):
    now = utc(now or datetime.now(timezone.utc))
    result = []
    for person in people:
        if (person.get("message_count") or 0) < min_messages:
            continue
        if last_days is not None:
            try:
                posted = datetime.fromisoformat(person.get("last_message_at") or "")
            except (ValueError, TypeError):
                continue
            if not now - timedelta(days=last_days) <= utc(posted) <= now:
                continue
        result.append(person)
    return result


def history_request(entity, offset, limit, until=None):
    return GetHistoryRequest(
        peer=entity,
        offset_id=offset,
        offset_date=until if not offset else None,
        add_offset=0,
        limit=limit,
        max_id=0,
        min_id=0,
        hash=0,
    )


class ChatAuthorsEngine(ParserEngine):
    async def open_source(self, client, reference):
        entity = await asyncio.wait_for(client.get_entity(reference), 20)
        if not isinstance(entity, (Chat, Channel)):
            raise ValueError("Адрес относится к пользователю, а не чату или каналу")
        if isinstance(entity, Chat) and (entity.left or entity.deactivated):
            raise ValueError("Аккаунт не имеет доступа к этой обычной группе")
        if isinstance(entity, Channel) and entity.left and not entity.username:
            raise ValueError("Приватный чат недоступен без вступления. Вступите вручную и обновите «Мои группы»")
        return (
            entity,
            {
                "source_id": utils.get_peer_id(entity),
                "title": entity.title,
                "chat_type": "channel" if getattr(entity, "broadcast", False) else "group",
                "membership": "not_joined" if entity.left else "joined",
                "count_hint": None,
            },
            None,
        )

    async def check(self, account, reference):
        manager = AccountManager(account, self.db)
        try:
            await manager.connect()
            entity, info, _ = await self.open_source(manager.client, reference)
            await asyncio.wait_for(manager.client(history_request(entity, 0, 1)), 25)
            return info
        except FloodWaitError as exc:
            await self.db.set_flood_wait(account["id"], exc.seconds)
            raise
        finally:
            await manager.disconnect()

    async def start(self, account, sources, limit, *, since=None, until=None):
        if not 1 <= limit <= MAX_HISTORY_MESSAGES or not 1 <= len(sources) <= MAX_SOURCES:
            raise ValueError("Укажите до 10 источников и от 1 до 10000 сообщений на источник")
        if any(
            value is not None and (not isinstance(value, datetime) or value.tzinfo is None) for value in (since, until)
        ):
            raise ValueError("Даты периода должны содержать часовой пояс")
        since, until = utc(since) if since else None, utc(until) if until else None
        if since and until and since >= until:
            raise ValueError("Начало периода должно быть раньше конца")
        self._task, self.run_id = asyncio.current_task(), None
        manager = AccountManager(account, self.db)
        outcome, detail, budget, successful, saved = "completed", "", MAX_RUN_RECORDS, 0, 0
        seen_sources = set()
        try:
            self.run_id = await self.db.create_parse_run(
                account["id"],
                sources,
                mode="authors",
                since=since.isoformat() if since else None,
                until=until.isoformat() if until else None,
                scan_limit=limit,
            )
            await manager.connect()
            for reference in sources:
                info, count = {}, 0
                try:
                    entity, info, _ = await self.open_source(manager.client, reference)
                    if info["source_id"] in seen_sources:
                        continue
                    seen_sources.add(info["source_id"])
                    await self.db.save_parse_source(
                        self.run_id, str(reference), info, "running", "Чтение доступной истории"
                    )
                    cap, cursor, messages_seen, people = min(limit, budget), 0, set(), {}
                    reason, excluded, found = "", 0, 0
                    while count < cap:
                        response = await asyncio.wait_for(
                            manager.client(history_request(entity, cursor, min(100, cap - count), until)), 30
                        )
                        messages = response.messages
                        if not messages:
                            reason = "Достигнут конец доступной истории."
                            break
                        users = {user.id: user for user in response.users if isinstance(user, User)}
                        changed, reached_start, fresh = set(), False, 0
                        for message in messages[: cap - count]:
                            message_id = getattr(message, "id", 0)
                            if message_id <= 0 or message_id in messages_seen or cursor and message_id >= cursor:
                                continue
                            messages_seen.add(message_id)
                            fresh += 1
                            stamp = utc(message.date) if getattr(message, "date", None) else None
                            if stamp and since and stamp < since:
                                reached_start = True
                            if not stamp or since and stamp < since or until and stamp >= until:
                                continue
                            if not isinstance(message, Message) or not isinstance(message.from_id, PeerUser):
                                excluded += 1
                                continue
                            user_id = message.from_id.user_id
                            profile = member_row(users.get(user_id) or User(id=user_id), None, info["source_id"])
                            if profile is None:
                                excluded += 1
                                continue
                            profile["role"] = "unknown"
                            person = people.setdefault(
                                user_id,
                                {
                                    **profile,
                                    "message_count": 0,
                                    "first_message_at": stamp.isoformat(),
                                    "last_message_at": stamp.isoformat(),
                                },
                            )
                            # Upgrade missing profile information if a later page includes it.
                            for field in ("username", "first_name", "last_name"):
                                if not person[field]:
                                    person[field] = profile[field]
                            for field in ("is_bot", "is_deleted", "is_premium", "has_photo"):
                                person[field] = person[field] or profile[field]
                            if profile["activity"] != "unknown":
                                person["activity"], person["last_seen"] = profile["activity"], profile["last_seen"]
                            person["message_count"] += 1
                            person["first_message_at"] = min(person["first_message_at"], stamp.isoformat())
                            person["last_message_at"] = max(person["last_message_at"], stamp.isoformat())
                            changed.add(user_id)
                            found += 1
                        count += fresh
                        budget -= fresh
                        await self.db.save_parsed_authors(
                            self.run_id, [people[key] for key in changed], str(reference), count
                        )
                        saved += len(changed)
                        self.progress(f"{info['title']}: просмотрено {count} сообщений, найдено {len(people)} авторов")
                        if reached_start:
                            reason = "Достигнуто начало выбранного периода."
                            break
                        next_cursor = min(
                            (getattr(message, "id", 0) for message in messages if getattr(message, "id", 0) > 0),
                            default=0,
                        )
                        if not fresh or not next_cursor or cursor and next_cursor >= cursor:
                            reason = "Telegram повторил страницу истории; полученная часть сохранена."
                            outcome = "partial"
                            break
                        cursor = next_cursor
                        if count < cap:
                            await asyncio.sleep(0.5)
                    partial = not reason or "повторил" in reason
                    reason = reason or "Достигнут лимит сообщений; период мог быть просмотрен не полностью."
                    source_detail = f"{reason} Сообщений от найденных людей: {found}; служебных/анонимных/своих пропущено: {excluded}. Это авторы доступных сообщений, не список текущих участников."
                    await self.db.save_parse_source(
                        self.run_id, str(reference), info, "partial" if partial else "completed", source_detail, count
                    )
                    successful += 1
                    if partial:
                        outcome = "partial"
                except (FloodWaitError, *ACCOUNT_ERRORS) as exc:
                    if isinstance(exc, FloodWaitError):
                        await self.db.set_flood_wait(account["id"], exc.seconds)
                    detail = access_error(exc)
                    await self.db.save_parse_source(self.run_id, str(reference), info, "failed", detail, count)
                    outcome = "partial" if successful or saved else "failed"
                    break
                except (ValueError, RPCError, asyncio.TimeoutError, ConnectionError, OSError) as exc:
                    detail = access_error(exc)
                    await self.db.save_parse_source(self.run_id, str(reference), info, "failed", detail, count)
                    outcome = "partial"
                    self.progress(detail)
                if budget <= 0:
                    outcome, detail = (
                        "partial",
                        "Достигнут общий предел 20000 просмотренных сообщений; остальные источники не запрашивались.",
                    )
                    break
            if not successful and not saved:
                outcome = "failed"
        except asyncio.CancelledError:
            outcome, detail = "stopped", "Остановлено. Уже найденные авторы и их счётчики сохранены."
        except Exception as exc:
            outcome, detail = "partial" if saved else "failed", access_error(exc)
        finally:
            try:
                await manager.disconnect()
            finally:
                if self.run_id is not None:
                    await self.db.finish_parse_run(self.run_id, outcome, detail)
                self._task = None
        return {"run_id": self.run_id, "status": outcome, "detail": detail}
