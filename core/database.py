import json
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from core.followup_store import FollowupStore
from core.followup_store import note as followup_note
from core.paths import DATA_DIR, session_name, session_path
from core.secrets import protect, reveal_row
from core.validation import normalize_username, validate_account, validate_message

DB_PATH = str(DATA_DIR / "inviter.db")


@asynccontextmanager
async def _connect():
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute("PRAGMA busy_timeout = 15000")
        await db.execute("PRAGMA foreign_keys = ON")
        yield db


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today_iso() -> str:
    return date.today().isoformat()


async def _require_active_campaign(connection, campaign_id):
    row = await (
        await connection.execute("SELECT id FROM campaigns WHERE id=? AND deleted_at IS NULL", (campaign_id,))
    ).fetchone()
    if row is None:
        raise ValueError("Кампания удалена или не найдена")


def _row_to_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return reveal_row(row)


CAMPAIGN_SETTINGS = {
    "target_chat",
    "interval_min",
    "interval_max",
    "schedule_enabled",
    "schedule_from",
    "schedule_to",
    "protection_consecutive",
    "protection_break_min",
    "protection_break_max",
    "task_status",
    "selected_accounts",
    "operation_mode",
    "message_text",
    "followup_enabled",
    "followup_text",
}


class Database(FollowupStore):
    def __init__(self, campaign_id: int = 1):
        self.campaign_id = int(campaign_id)

    def for_campaign(self, campaign_id: int):
        return Database(campaign_id)

    def _setting_key(self, key):
        return f"campaign:{self.campaign_id}:{key}" if key in CAMPAIGN_SETTINGS else key

    async def create_parse_run(self, account_id, sources, *, mode="members", since=None, until=None, scan_limit=None):
        async with _connect() as db:
            cursor = await db.execute(
                "INSERT INTO parse_runs(account_id,sources,status,started_at,mode,since,until,scan_limit) VALUES (?,?,'running',?,?,?,?,?)",
                (account_id, json.dumps(sources, ensure_ascii=False), _now_iso(), mode, since, until, scan_limit),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def save_parse_source(self, run_id, reference, info, status, detail, fetched=0):
        async with _connect() as db:
            await db.execute(
                "INSERT INTO parse_sources(run_id,reference,source_id,title,chat_type,membership,count_hint,status,detail,fetched) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id,reference) DO UPDATE SET source_id=COALESCE(excluded.source_id,parse_sources.source_id),title=COALESCE(excluded.title,parse_sources.title),chat_type=COALESCE(excluded.chat_type,parse_sources.chat_type),membership=COALESCE(excluded.membership,parse_sources.membership),count_hint=COALESCE(excluded.count_hint,parse_sources.count_hint),status=excluded.status,detail=excluded.detail,fetched=MAX(excluded.fetched,parse_sources.fetched)",
                (
                    run_id,
                    reference,
                    info.get("source_id"),
                    info.get("title"),
                    info.get("chat_type"),
                    info.get("membership"),
                    info.get("count_hint"),
                    status,
                    detail,
                    fetched,
                ),
            )
            await db.commit()

    async def save_parsed_authors(self, run_id, rows, reference, fetched):
        """Commit absolute author totals and the history cursor count together; retries do not inflate totals."""
        fields = (
            "source_id",
            "user_id",
            "username",
            "first_name",
            "last_name",
            "role",
            "is_bot",
            "is_deleted",
            "is_premium",
            "has_photo",
            "activity",
            "last_seen",
            "message_count",
            "first_message_at",
            "last_message_at",
        )
        updates = ",".join(
            f"{key}=excluded.{key}"
            for key in fields[2:]
            if key
            not in {"message_count", "first_message_at", "last_message_at", "username", "first_name", "last_name"}
        )
        updates += "," + ",".join(
            f"{key}=COALESCE(NULLIF(excluded.{key},''),parsed_members.{key})"
            for key in ("username", "first_name", "last_name")
        )
        updates += ",message_count=MAX(parsed_members.message_count,excluded.message_count),first_message_at=MIN(COALESCE(parsed_members.first_message_at,excluded.first_message_at),excluded.first_message_at),last_message_at=MAX(COALESCE(parsed_members.last_message_at,excluded.last_message_at),excluded.last_message_at)"
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.executemany(
                "INSERT INTO parsed_members(run_id,"
                + ",".join(fields)
                + ") VALUES ("
                + ",".join("?" for _ in range(len(fields) + 1))
                + ") ON CONFLICT(run_id,source_id,user_id) DO UPDATE SET "
                + updates,
                [(run_id, *(row.get(key) for key in fields)) for row in rows],
            )
            await db.execute(
                "UPDATE parse_sources SET fetched=MAX(fetched,?) WHERE run_id=? AND reference=?",
                (fetched, run_id, reference),
            )
            await db.commit()

    async def save_parsed_members(self, run_id, rows):
        fields = (
            "source_id",
            "user_id",
            "username",
            "first_name",
            "last_name",
            "role",
            "is_bot",
            "is_deleted",
            "is_premium",
            "has_photo",
            "activity",
            "last_seen",
        )
        async with _connect() as db:
            await db.executemany(
                "INSERT OR IGNORE INTO parsed_members(run_id,"
                + ",".join(fields)
                + ") VALUES ("
                + ",".join("?" for _ in range(len(fields) + 1))
                + ")",
                [(run_id, *(row.get(key) for key in fields)) for row in rows],
            )
            await db.commit()

    async def finish_parse_run(self, run_id, status, detail):
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE parse_runs SET status=?,detail=?,finished_at=? WHERE id=?", (status, detail, _now_iso(), run_id)
            )
            await db.execute(
                "UPDATE parse_sources SET status=?,detail=? WHERE run_id=? AND status='running'",
                ("stopped" if status == "stopped" else "partial", detail or "Получение было прервано", run_id),
            )
            await db.commit()

    async def get_parse_runs(self):
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT r.*, (SELECT COUNT(DISTINCT user_id) FROM parsed_members m WHERE m.run_id=r.id) AS people_count FROM parse_runs r ORDER BY id DESC LIMIT 50"
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_parse_results(self, run_id):
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM parse_sources WHERE run_id=? ORDER BY id", (run_id,))
            sources = [dict(row) for row in await cursor.fetchall()]
            cursor = await db.execute(
                "SELECT m.*,s.title AS source_title FROM parsed_members m LEFT JOIN parse_sources s ON s.run_id=m.run_id AND s.source_id=m.source_id WHERE m.run_id=? ORDER BY m.user_id,m.source_id",
                (run_id,),
            )
            return [dict(row) for row in await cursor.fetchall()], sources

    async def delete_parse_run(self, run_id):
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT status FROM parse_runs WHERE id=?", (run_id,))
            row = await cursor.fetchone()
            if row and row[0] == "running":
                raise ValueError("Сначала остановите получение участников")
            await db.execute("DELETE FROM parsed_members WHERE run_id=?", (run_id,))
            await db.execute("DELETE FROM parse_sources WHERE run_id=?", (run_id,))
            await db.execute("DELETE FROM parse_runs WHERE id=?", (run_id,))
            await db.commit()

    async def get_message_templates(self):
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM message_templates ORDER BY updated_at DESC,id DESC")
            return [dict(row) for row in await cursor.fetchall()]

    async def save_message_template(self, name, text, template_id=None):
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("Название шаблона: от 1 до 80 символов")
        validate_message(text)
        async with _connect() as db:
            try:
                if template_id is None:
                    cursor = await db.execute(
                        "INSERT INTO message_templates(name,text,updated_at) VALUES (?,?,?)",
                        (name, text, _now_iso()),
                    )
                    result = cursor.lastrowid
                else:
                    cursor = await db.execute(
                        "UPDATE message_templates SET name=?,text=?,updated_at=? WHERE id=?",
                        (name, text, _now_iso(), template_id),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("Шаблон удалён. Сохраните его как новый")
                    result = template_id
            except aiosqlite.IntegrityError:
                raise ValueError("Шаблон с таким названием уже существует. Измените название") from None
            await db.commit()
            return int(result)

    async def delete_message_template(self, template_id):
        async with _connect() as db:
            await db.execute("DELETE FROM message_templates WHERE id=?", (template_id,))
            await db.commit()

    async def rename_campaign(self, name):
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("Название кампании: от 1 до 80 символов")
        async with _connect() as db:
            cursor = await db.execute(
                "UPDATE campaigns SET name=? WHERE id=? AND deleted_at IS NULL", (name, self.campaign_id)
            )
            if cursor.rowcount != 1:
                raise ValueError("Кампания не найдена")
            await db.commit()

    async def copy_campaign_settings(self, name):
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("Название кампании: от 1 до 80 символов")
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT kind FROM campaigns WHERE id=? AND deleted_at IS NULL", (self.campaign_id,)
            )
            source = await cursor.fetchone()
            if source is None:
                raise ValueError("Кампания не найдена")
            cursor = await db.execute(
                "INSERT INTO campaigns(name,kind,created_at) VALUES (?,?,?)", (name, source[0], _now_iso())
            )
            campaign_id = cursor.lastrowid
            for key in CAMPAIGN_SETTINGS - {"task_status"}:
                cursor = await db.execute("SELECT value FROM settings WHERE key=?", (self._setting_key(key),))
                row = await cursor.fetchone()
                if row is not None:
                    await db.execute(
                        "INSERT INTO settings(key,value) VALUES (?,?)", (f"campaign:{campaign_id}:{key}", row[0])
                    )
            await db.commit()
            return int(campaign_id)

    async def save_campaign_settings(self, settings):
        if not set(settings) <= CAMPAIGN_SETTINGS:
            raise ValueError("Неизвестная настройка кампании")
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await _require_active_campaign(db, self.campaign_id)
            mode = settings.get("operation_mode")
            if mode is not None:
                if mode not in {"invite", "messages"}:
                    raise ValueError("Неизвестный режим кампании")
                cur = await db.execute("SELECT kind FROM campaigns WHERE id=?", (self.campaign_id,))
                row = await cur.fetchone()
                if not row:
                    raise ValueError("Кампания не найдена")
                if row[0] != mode:
                    cur = await db.execute(
                        "SELECT COUNT(*) FROM targets WHERE campaign_id=? AND status!='pending'", (self.campaign_id,)
                    )
                    outcomes = (await cur.fetchone())[0]
                    cur = await db.execute(
                        "SELECT COUNT(*) FROM campaign_runs WHERE campaign_id=?", (self.campaign_id,)
                    )
                    if outcomes or (await cur.fetchone())[0]:
                        raise ValueError("Для другого режима создайте новую кампанию: в этой уже есть результаты")
                    await db.execute("UPDATE campaigns SET kind=? WHERE id=?", (mode, self.campaign_id))
            await db.executemany(
                "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(self._setting_key(key), str(value)) for key, value in settings.items()],
            )
            await db.commit()

    async def get_campaigns(self):
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM campaigns WHERE deleted_at IS NULL ORDER BY id")
            return [dict(row) for row in await cur.fetchall()]

    async def delete_campaign(self) -> int:
        """Delete campaign data atomically; retain only identity and delivery deduplication."""
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute("SELECT id FROM campaigns WHERE id=? AND deleted_at IS NULL", (self.campaign_id,))
            ).fetchone()
            if row is None:
                raise ValueError("Кампания уже удалена или не найдена")
            status = await (
                await db.execute("SELECT value FROM settings WHERE key=?", (self._setting_key("task_status"),))
            ).fetchone()
            processing = await (
                await db.execute(
                    "SELECT 1 FROM targets WHERE campaign_id=? AND status='processing' LIMIT 1", (self.campaign_id,)
                )
            ).fetchone()
            running = await (
                await db.execute(
                    "SELECT 1 FROM campaign_runs WHERE campaign_id=? AND outcome='running' LIMIT 1", (self.campaign_id,)
                )
            ).fetchone()
            sending = await (
                await db.execute(
                    "SELECT 1 FROM message_followups WHERE campaign_id=? AND state IN ('armed','sending') LIMIT 1",
                    (self.campaign_id,),
                )
            ).fetchone()
            if status and status[0] in {"running", "paused"} or processing or running or sending:
                raise RuntimeError("Сначала остановите задачу и дождитесь завершения текущего действия")
            now = _now_iso()
            await db.execute(
                "UPDATE message_followups SET text='',state=CASE WHEN state IN ('waiting','ready') THEN 'cancelled' ELSE state END,completed_at=COALESCE(completed_at,?) WHERE campaign_id=?",
                (now, self.campaign_id),
            )
            for table in ("targets", "logs", "campaign_runs"):
                await db.execute(f"DELETE FROM {table} WHERE campaign_id=?", (self.campaign_id,))
            await db.execute("DELETE FROM settings WHERE key GLOB ?", (f"campaign:{self.campaign_id}:*",))
            # A tombstone prevents startup migrations from recreating campaign 1
            # and prevents reuse of IDs referenced by the global delivery ledger.
            await db.execute(
                "UPDATE campaigns SET name='Удалённая кампания',deleted_at=? WHERE id=?", (now, self.campaign_id)
            )
            remaining = await (
                await db.execute("SELECT id FROM campaigns WHERE deleted_at IS NULL ORDER BY id LIMIT 1")
            ).fetchone()
            if remaining is None:
                cursor = await db.execute(
                    "INSERT INTO campaigns(name,kind,created_at) VALUES ('Новая кампания','invite',?)", (now,)
                )
                fallback_id = int(cursor.lastrowid)
            else:
                fallback_id = int(remaining[0])
            await db.execute(
                "INSERT INTO settings(key,value) VALUES ('last_campaign_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(fallback_id),),
            )
            await db.commit()
            return fallback_id

    async def create_campaign(self, name: str, kind: str = "invite") -> int:
        if kind not in {"invite", "messages"}:
            raise ValueError("Неизвестный режим кампании")
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("Название кампании: от 1 до 80 символов")
        async with _connect() as db:
            cur = await db.execute(
                "INSERT INTO campaigns(name, created_at, kind) VALUES (?, ?, ?)", (name, _now_iso(), kind)
            )
            await db.commit()
            return int(cur.lastrowid)

    async def begin_run(self, chat: str) -> int:
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await _require_active_campaign(db, self.campaign_id)
            cur = await db.execute(
                "INSERT INTO campaign_runs(campaign_id, chat, started_at, outcome) VALUES (?, ?, ?, 'running')",
                (self.campaign_id, chat, _now_iso()),
            )
            await db.execute(
                "INSERT INTO settings(key,value) VALUES (?, 'running') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self._setting_key("task_status"),),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def finish_run(self, run_id: int, summary: dict) -> None:
        async with _connect() as db:
            await db.execute(
                "UPDATE campaign_runs SET ended_at=?, outcome=?, summary=? WHERE id=? AND campaign_id=?",
                (_now_iso(), summary["outcome"], json.dumps(summary, ensure_ascii=False), run_id, self.campaign_id),
            )
            await db.commit()

    async def get_runs(self):
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM campaign_runs WHERE campaign_id=? ORDER BY id DESC LIMIT 100", (self.campaign_id,)
            )
            return [dict(row) for row in await cur.fetchall()]

    async def get_campaign_report(self):
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT a.phone, t.status, COUNT(*) AS count FROM targets t "
                "LEFT JOIN accounts a ON a.id=t.added_by_account_id WHERE t.campaign_id=? "
                "GROUP BY a.phone, t.status ORDER BY a.phone, t.status",
                (self.campaign_id,),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def reset_daily_counters_if_needed(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        async with _connect() as db:
            await db.execute(
                """
                UPDATE accounts
                SET added_today = 0, last_reset_date = ?
                WHERE last_reset_date IS NULL OR last_reset_date != ?
                """,
                (today, today),
            )
            await db.commit()

    async def reset_message_counters(self):
        today = _today_iso()
        async with _connect() as db:
            await db.execute(
                "UPDATE accounts SET sent_today=0, last_message_reset_date=? WHERE last_message_reset_date IS NULL OR last_message_reset_date!=?",
                (today, today),
            )
            # Reply-triggered follow-ups keep their own daily counter with the same limits.
            await db.execute(
                "UPDATE accounts SET followups_today=0, followup_reset_date=? WHERE followup_reset_date IS NULL OR followup_reset_date!=?",
                (today, today),
            )
            await db.commit()

    async def get_campaign_kind(self):
        async with _connect() as db:
            cur = await db.execute("SELECT kind FROM campaigns WHERE id=? AND deleted_at IS NULL", (self.campaign_id,))
            row = await cur.fetchone()
            if not row:
                raise ValueError("Кампания не найдена")
            return row[0]

    async def delivery_exists(self, username):
        async with _connect() as db:
            cur = await db.execute(
                "SELECT state FROM message_deliveries WHERE username=?", (normalize_username(username),)
            )
            row = await cur.fetchone()
            return row[0] if row else None

    async def last_message_time(self):
        async with _connect() as db:
            cur = await db.execute(
                "SELECT MAX(completed_at) FROM (SELECT completed_at FROM message_deliveries WHERE state IN ('sent','uncertain') "
                "UNION ALL SELECT completed_at FROM message_followups WHERE state IN ('sent','uncertain'))"
            )
            return (await cur.fetchone())[0]

    async def reserve_delivery(self, target_id, account_id, user_id, *, followup_text=None, access_hash=None):
        if followup_text is not None:
            validate_message(followup_text)
            if access_hash is None:
                raise ValueError("Не удалось сохранить адрес получателя для второго сообщения")
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "SELECT username FROM targets WHERE id=? AND campaign_id=? AND status='processing'",
                (target_id, self.campaign_id),
            )
            row = await cur.fetchone()
            if not row:
                raise RuntimeError("Получатель больше не находится в обработке")
            random_id = secrets.randbits(63) or 1
            cur = await db.execute(
                "INSERT OR IGNORE INTO message_deliveries(username,user_id,campaign_id,target_id,account_id,random_id,state,created_at) VALUES (?,?,?,?,?,?,'reserved',?)",
                (row[0], user_id, self.campaign_id, target_id, account_id, random_id, _now_iso()),
            )
            if cur.rowcount == 1 and followup_text is not None:
                await db.execute(
                    "INSERT INTO message_followups(username,user_id,access_hash,campaign_id,target_id,account_id,text,state,created_at) "
                    "VALUES (?,?,?,?,?,?,?,'armed',?)",
                    (row[0], user_id, access_hash, self.campaign_id, target_id, account_id, followup_text, _now_iso()),
                )
            await db.commit()
            return random_id if cur.rowcount == 1 else None

    async def release_delivery(self, target_id):
        async with _connect() as db:
            await db.execute(
                "DELETE FROM message_followups WHERE target_id=? AND campaign_id=? AND state='armed'",
                (target_id, self.campaign_id),
            )
            await db.execute(
                "DELETE FROM message_deliveries WHERE target_id=? AND campaign_id=? AND state='reserved'",
                (target_id, self.campaign_id),
            )
            await db.commit()

    async def finish_delivery(self, target_id, status, message_id=None):
        if status not in {"sent", "uncertain", "private"}:
            raise ValueError("Неизвестный результат отправки")
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "SELECT account_id FROM message_deliveries WHERE target_id=? AND campaign_id=? AND state='reserved'",
                (target_id, self.campaign_id),
            )
            row = await cur.fetchone()
            if not row:
                raise RuntimeError("Отправка уже завершена или не была зарезервирована")
            state = {"private": "blocked"}.get(status, status)
            await db.execute(
                "UPDATE message_deliveries SET state=?,message_id=?,completed_at=? WHERE target_id=? AND campaign_id=? AND state='reserved'",
                (state, message_id, datetime.now().isoformat(), target_id, self.campaign_id),
            )
            detail = {
                "sent": None,
                "uncertain": "Результат неизвестен. Проверьте переписку вручную; повторная отправка отключена.",
                "private": "Пользователь не принимает сообщения",
            }[status]
            target_update = await db.execute(
                "UPDATE targets SET status=?, added_by_account_id=?, processed_at=?, error_message=? WHERE id=? AND campaign_id=?",
                (status, row[0], _now_iso(), detail, target_id, self.campaign_id),
            )
            if target_update.rowcount != 1:
                raise RuntimeError("Получатель удалён из очереди; результат требует ручной проверки")
            if status == "sent":
                await db.execute(
                    "UPDATE accounts SET sent_today=CASE WHEN last_message_reset_date=? THEN sent_today+1 ELSE 1 END,last_message_reset_date=? WHERE id=?",
                    (_today_iso(), _today_iso(), row[0]),
                )
            fstate = (
                "waiting"
                if status == "sent" and isinstance(message_id, int) and message_id > 0
                else ("blocked" if status == "private" else "uncertain")
            )
            db.row_factory = aiosqlite.Row
            followup = await (
                await db.execute(
                    "SELECT * FROM message_followups WHERE target_id=? AND campaign_id=? AND state='armed'",
                    (target_id, self.campaign_id),
                )
            ).fetchone()
            if followup:
                await db.execute(
                    "UPDATE message_followups SET state=?,first_message_id=?,checked_id=? "
                    "WHERE campaign_id=? AND username=?",
                    (fstate, message_id, message_id or 0, self.campaign_id, followup["username"]),
                )
                await followup_note(
                    db,
                    followup,
                    "FOLLOWUP_WAIT"
                    if fstate == "waiting"
                    else "FOLLOWUP_UNCERTAIN"
                    if fstate == "uncertain"
                    else "FOLLOWUP_BLOCKED",
                    "Первое сообщение отправлено. Ожидаем ответ для второго"
                    if fstate == "waiting"
                    else "Второе сообщение не разрешено: нет подтверждённого первого сообщения или переписка недоступна",
                )
            await db.commit()

    async def recover_message_deliveries(self):
        async with _connect() as db:
            await db.execute(
                "UPDATE targets SET status='uncertain',error_message='Результат неизвестен: проверьте переписку вручную' "
                "WHERE status IN ('pending','processing') AND campaign_id=? AND EXISTS "
                "(SELECT 1 FROM message_deliveries d WHERE d.state='reserved' AND d.target_id=targets.id "
                "AND d.campaign_id=targets.campaign_id AND d.username=targets.username)",
                (self.campaign_id,),
            )
            await db.execute(
                "UPDATE message_deliveries SET state='uncertain',completed_at=? WHERE state='reserved' AND campaign_id=?",
                (datetime.now().isoformat(), self.campaign_id),
            )
            await db.commit()

    async def add_account(self, payload: dict[str, Any]) -> int:
        now = _now_iso()
        today = _today_iso()
        payload = validate_account(payload)
        name = session_name(payload["phone"], payload.get("session_name"))
        async with _connect() as db:
            cur = await db.execute(
                """
                INSERT INTO accounts (
                    phone, api_id, api_hash, session_name,
                    proxy_type, proxy_host, proxy_port, proxy_login, proxy_password,
                    daily_limit, added_today, last_reset_date, status, flood_until, notes, created_at,
                    device_profile
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'active', NULL, ?, ?, ?)
                """,
                (
                    payload["phone"],
                    str(payload["api_id"]),
                    protect(payload["api_hash"]),
                    name,
                    payload.get("proxy_type"),
                    payload.get("proxy_host"),
                    payload.get("proxy_port"),
                    payload.get("proxy_login"),
                    protect(payload.get("proxy_password")),
                    int(payload.get("daily_limit", 40)),
                    today,
                    payload.get("notes"),
                    now,
                    payload.get("device_profile"),
                ),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def update_account(self, account_id: int, payload: dict[str, Any]) -> None:
        # Read and write under one immediate transaction: the engine may update
        # account status concurrently, and last-write-wins must at least be atomic.
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))).fetchone()
            if not row:
                raise ValueError("Аккаунт не найден")
            merged = validate_account({**reveal_row(row), **payload})
            changes = {key: merged[key] for key in payload}
            fields = []
            values: list[Any] = []
            for key in (
                "phone",
                "api_id",
                "api_hash",
                "proxy_type",
                "proxy_host",
                "proxy_port",
                "proxy_login",
                "proxy_password",
                "daily_limit",
                "notes",
                "status",
                "flood_until",
            ):
                if key in changes:
                    fields.append(f"{key} = ?")
                    values.append(protect(changes[key]) if key in {"api_hash", "proxy_password"} else changes[key])
            if not fields:
                await db.commit()
                return
            values.append(account_id)
            await db.execute(f"UPDATE accounts SET {', '.join(fields)} WHERE id = ?", values)
            await db.commit()

    async def update_account_status(self, account_id: int, status: str) -> None:
        async with _connect() as db:
            await db.execute("UPDATE accounts SET status = ? WHERE id = ?", (status, account_id))
            await db.commit()

    async def set_flood_wait(self, account_id: int, seconds: int) -> None:
        flood_until = (datetime.now() + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        async with _connect() as db:
            await db.execute(
                "UPDATE accounts SET status = 'flood', flood_until = ? WHERE id = ?",
                (flood_until, account_id),
            )
            await db.commit()

    async def increment_added_today(self, account_id: int) -> None:
        async with _connect() as db:
            await db.execute(
                "UPDATE accounts SET added_today = added_today + 1 WHERE id = ?",
                (account_id,),
            )
            await db.commit()

    async def increment_consecutive(self, account_id: int) -> None:
        async with _connect() as db:
            await db.execute(
                "UPDATE accounts SET consecutive_adds = COALESCE(consecutive_adds, 0) + 1 WHERE id = ?",
                (account_id,),
            )
            await db.commit()

    async def reset_consecutive(self, account_id: int) -> None:
        async with _connect() as db:
            await db.execute(
                "UPDATE accounts SET consecutive_adds = 0 WHERE id = ?",
                (account_id,),
            )
            await db.commit()

    async def set_rotation_break(self, account_id: int, break_until_iso: str) -> None:
        async with _connect() as db:
            await db.execute(
                "UPDATE accounts SET rotation_break_until = ? WHERE id = ?",
                (break_until_iso, account_id),
            )
            await db.commit()

    async def get_account(self, account_id: int) -> dict[str, Any] | None:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
            return _row_to_dict(await cur.fetchone())

    async def get_accounts(self) -> list[dict[str, Any]]:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM accounts ORDER BY id DESC")
            rows = await cur.fetchall()
            return [reveal_row(row) for row in rows]

    async def get_enabled_accounts(self) -> list[dict[str, Any]]:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM accounts WHERE is_enabled = 1 ORDER BY id DESC")
            rows = await cur.fetchall()
            return [reveal_row(row) for row in rows]

    async def get_accounts_by_ids(self, account_ids: list[int]) -> list[dict[str, Any]]:
        if not account_ids:
            return []
        placeholders = ",".join(["?"] * len(account_ids))
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT * FROM accounts WHERE id IN ({placeholders}) ORDER BY id",
                account_ids,
            )
            rows = await cur.fetchall()
            return [reveal_row(row) for row in rows]

    async def delete_account(self, account_id: int) -> None:
        session_name: str | None = None
        phone: str | None = None
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT session_name, phone FROM accounts WHERE id = ?",
                (account_id,),
            )
            row = await cur.fetchone()
            if row:
                session_name = row["session_name"]
                phone = row["phone"]
                session_path(phone, session_name)  # Validate before removing any data.
                from core.account_manager import AccountManager

                if AccountManager.is_session_busy(phone, session_name):
                    raise RuntimeError("Сначала остановите операции с этим аккаунтом")
            for table in (
                "warmup_sessions",
                "warmup_logs",
                "warmup_channels",
                "ai_memory",
                "ai_messages",
                "ai_send_events",
            ):
                await db.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
            await db.execute(
                "UPDATE targets SET added_by_account_id = NULL WHERE added_by_account_id = ?", (account_id,)
            )
            await db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            await db.commit()

        # Only the exact validated session belonging to this account is removed.
        # Re-check ownership: a task may have claimed the session while the row
        # deletion was committing; an orphaned file is safer than a live one.
        if phone:
            from core.account_manager import AccountManager

            if AccountManager.is_session_busy(phone, session_name):
                return
            path = session_path(phone, session_name)
            for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
                path.with_name(path.name + suffix).unlink(missing_ok=True)

    async def toggle_account_enabled(self, account_id: int, enabled: bool) -> None:
        async with _connect() as db:
            await db.execute(
                "UPDATE accounts SET is_enabled = ? WHERE id = ?",
                (1 if enabled else 0, account_id),
            )
            await db.commit()

    async def update_device_profile(self, account_id: int, device_json: str) -> None:
        async with _connect() as db:
            await db.execute(
                "UPDATE accounts SET device_profile = ? WHERE id = ?",
                (device_json, account_id),
            )
            await db.commit()

    async def bulk_insert_targets(self, usernames: list[str]) -> int:
        if not usernames:
            return 0
        now = _now_iso()
        data = [
            (username, now, self.campaign_id)
            for username in dict.fromkeys(normalize_username(item) for item in usernames)
        ]
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await _require_active_campaign(db, self.campaign_id)
            kind = await (await db.execute("SELECT kind FROM campaigns WHERE id=?", (self.campaign_id,))).fetchone()
            status = await (
                await db.execute("SELECT value FROM settings WHERE key=?", (self._setting_key("task_status"),))
            ).fetchone()
            if status and status[0] in {"running", "paused"}:
                raise RuntimeError("Сначала остановите задачу, затем добавьте получателей в список")
            before = db.total_changes
            if kind and kind[0] == "messages":
                await db.executemany(
                    "INSERT OR IGNORE INTO targets(username,status,error_message,created_at,campaign_id) "
                    "VALUES (?,CASE (SELECT state FROM message_deliveries WHERE username=?) "
                    "WHEN 'sent' THEN 'already_sent' WHEN 'uncertain' THEN 'uncertain' "
                    "WHEN 'reserved' THEN 'uncertain' WHEN 'blocked' THEN 'private' ELSE 'pending' END,"
                    "CASE WHEN EXISTS(SELECT 1 FROM message_deliveries WHERE username=?) "
                    "THEN 'Повтор отключён: получатель уже есть в общем журнале отправок' ELSE NULL END,?,?)",
                    [(name, name, name, created, campaign_id) for name, created, campaign_id in data],
                )
            else:
                await db.executemany(
                    "INSERT OR IGNORE INTO targets (username, status, created_at, campaign_id) VALUES (?, 'pending', ?, ?)",
                    data,
                )
            inserted = db.total_changes - before
            await db.commit()
            return inserted

    async def get_pending_list(self) -> list[dict]:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, username FROM targets WHERE campaign_id=? AND status='pending' ORDER BY sort_order, id",
                (self.campaign_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def replace_pending_targets(self, usernames: list[str], expected: list[dict]) -> dict[str, int]:
        """Edit only the waiting queue, leaving outcomes and the global delivery ledger intact."""
        usernames = list(dict.fromkeys(normalize_username(item) for item in usernames))
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await _require_active_campaign(db, self.campaign_id)
            cursor = await db.execute(
                "SELECT value FROM settings WHERE key=?",
                (self._setting_key("task_status"),),
            )
            status = await cursor.fetchone()
            cursor = await db.execute(
                "SELECT COUNT(*) FROM targets WHERE campaign_id=? AND status='processing'",
                (self.campaign_id,),
            )
            if (status and status[0] in {"running", "paused"}) or (await cursor.fetchone())[0]:
                raise RuntimeError("Сначала остановите задачу, затем измените список")
            cursor = await db.execute(
                "SELECT id, username FROM targets WHERE campaign_id=? AND status='pending' ORDER BY sort_order, id",
                (self.campaign_id,),
            )
            current = await cursor.fetchall()
            if [(row[0], row[1]) for row in current] != [(row["id"], row["username"]) for row in expected]:
                raise RuntimeError(
                    "Очередь изменилась. Закройте редактор и откройте его снова, чтобы загрузить актуальный список"
                )
            wanted = set(usernames)
            removed = [row[0] for row in current if row[1] not in wanted]
            await db.executemany(
                "DELETE FROM targets WHERE id=? AND campaign_id=? AND status='pending'",
                [(key, self.campaign_id) for key in removed],
            )
            kind = await (await db.execute("SELECT kind FROM campaigns WHERE id=?", (self.campaign_id,))).fetchone()
            before = db.total_changes
            now = _now_iso()
            if kind and kind[0] == "messages":
                await db.executemany(
                    "INSERT OR IGNORE INTO targets(username,status,error_message,created_at,campaign_id,sort_order) "
                    "VALUES (?,CASE (SELECT state FROM message_deliveries WHERE username=?) "
                    "WHEN 'sent' THEN 'already_sent' WHEN 'uncertain' THEN 'uncertain' "
                    "WHEN 'reserved' THEN 'uncertain' WHEN 'blocked' THEN 'private' ELSE 'pending' END,"
                    "CASE WHEN EXISTS(SELECT 1 FROM message_deliveries WHERE username=?) "
                    "THEN 'Повтор отключён: получатель уже есть в общем журнале отправок' ELSE NULL END,?,?,?)",
                    [(name, name, name, now, self.campaign_id, order) for order, name in enumerate(usernames)],
                )
            else:
                await db.executemany(
                    "INSERT OR IGNORE INTO targets(username,status,created_at,campaign_id,sort_order) "
                    "VALUES (?,'pending',?,?,?)",
                    [(name, now, self.campaign_id, order) for order, name in enumerate(usernames)],
                )
            inserted = db.total_changes - before
            await db.executemany(
                "UPDATE targets SET sort_order=? WHERE campaign_id=? AND username=? AND status='pending'",
                [(order, self.campaign_id, name) for order, name in enumerate(usernames)],
            )
            cursor = await db.execute(
                "SELECT COUNT(*) FROM targets WHERE campaign_id=? AND status='pending'", (self.campaign_id,)
            )
            pending = (await cursor.fetchone())[0]
            await db.execute(
                "INSERT INTO logs(action,message,created_at,campaign_id) VALUES ('AUDIENCE_EDIT',?,?,?)",
                (
                    f"Список сохранён: ожидает {pending}, добавлено {inserted}, удалено {len(removed)}",
                    now,
                    self.campaign_id,
                ),
            )
            await db.commit()
            return {
                "inserted": inserted,
                "removed": len(removed),
                "pending": pending,
                "processed": len(usernames) - pending,
            }

    async def claim_next_pending_target(self) -> dict[str, Any] | None:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                """
                SELECT * FROM targets
                WHERE status = 'pending' AND campaign_id = ?
                ORDER BY sort_order ASC, id ASC
                LIMIT 1
                """,
                (self.campaign_id,),
            )
            row = await cur.fetchone()
            if not row:
                await db.commit()
                return None
            await db.execute(
                "UPDATE targets SET status = 'processing' WHERE id = ?",
                (row["id"],),
            )
            await db.commit()
            result = dict(row)
            result["status"] = "processing"
            return result

    async def shuffle_pending_targets(self, *, allow_running: bool = False) -> None:
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await _require_active_campaign(db, self.campaign_id)
            status = await (
                await db.execute("SELECT value FROM settings WHERE key=?", (self._setting_key("task_status"),))
            ).fetchone()
            if not allow_running and status and status[0] in {"running", "paused"}:
                raise RuntimeError("Сначала остановите задачу, затем перемешайте список")
            cur = await db.execute(
                "SELECT id FROM targets WHERE status = 'pending' AND campaign_id=? ORDER BY RANDOM()",
                (self.campaign_id,),
            )
            rows = await cur.fetchall()
            ids = [int(r[0]) for r in rows]
            for i, target_id in enumerate(ids):
                await db.execute(
                    "UPDATE targets SET sort_order = ? WHERE id = ?",
                    (i, target_id),
                )
            await db.commit()

    async def mark_target(
        self,
        target_id: int,
        status: str,
        added_by_account_id: int | None = None,
        error_message: str | None = None,
    ) -> None:
        processed_at = _now_iso() if status not in {"pending", "processing"} else None
        async with _connect() as db:
            await db.execute(
                """
                UPDATE targets
                SET status = ?, added_by_account_id = ?, error_message = ?, processed_at = ?
                WHERE id = ? AND campaign_id = ?
                """,
                (status, added_by_account_id, error_message, processed_at, target_id, self.campaign_id),
            )
            await db.commit()

    async def record_success(self, target_id: int, account_id: int) -> None:
        """Commit queue outcome and counters together, including rollover at midnight."""
        today = _today_iso()
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE targets SET status = 'added', added_by_account_id = ?, "
                "processed_at = ?, error_message = NULL WHERE id = ? AND status = 'processing' AND campaign_id = ?",
                (account_id, _now_iso(), target_id, self.campaign_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Цель больше не находится в обработке")
            await db.execute(
                "UPDATE accounts SET added_today = CASE WHEN last_reset_date = ? "
                "THEN added_today + 1 ELSE 1 END, last_reset_date = ?, "
                "consecutive_adds = COALESCE(consecutive_adds, 0) + 1 WHERE id = ?",
                (today, today, account_id),
            )
            await db.commit()

    async def record_ai_send(self, account_id: int, chat_key: str) -> None:
        # No message text is retained here, even when conversation memory is off.
        async with _connect() as db:
            await db.execute(
                "INSERT INTO ai_send_events(account_id, chat_key, created_at) VALUES (?, ?, ?)",
                (account_id, chat_key, _now_iso()),
            )
            await db.execute(
                "DELETE FROM ai_send_events WHERE created_at < ?",
                ((datetime.now() - timedelta(days=7)).isoformat(timespec="seconds"),),
            )
            await db.commit()

    async def get_ai_send_limits(self, account_id: int, chat_key: str) -> tuple[int, str | None]:
        async with _connect() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM ai_send_events WHERE account_id = ? AND created_at >= ?",
                (account_id, (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")),
            )
            count = (await cursor.fetchone())[0]
            cursor = await db.execute(
                "SELECT MAX(created_at) FROM ai_send_events WHERE account_id = ? AND chat_key = ?",
                (account_id, chat_key),
            )
            last = (await cursor.fetchone())[0]
            return count, last

    async def reset_processing_to_pending(self) -> None:
        async with _connect() as db:
            await db.execute(
                """
                UPDATE targets
                SET status = 'pending'
                WHERE status = 'processing' AND campaign_id = ?
                """,
                (self.campaign_id,),
            )
            await db.commit()

    async def clear_targets(self) -> None:
        async with _connect() as db:
            cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (self._setting_key("task_status"),))
            row = await cursor.fetchone()
            if row and row[0] in {"running", "paused"}:
                raise RuntimeError("Сначала остановите задачу, затем очистите список")
            await db.execute("DELETE FROM targets WHERE campaign_id=?", (self.campaign_id,))
            await db.commit()

    async def reset_failed_targets(self) -> None:
        async with _connect() as db:
            await db.execute(
                """
                UPDATE targets
                SET status = 'pending',
                    added_by_account_id = NULL,
                    error_message = NULL,
                    processed_at = NULL
                WHERE status IN ('not_found', 'error', 'private') AND campaign_id=?
                """,
                (self.campaign_id,),
            )
            await db.commit()

    async def get_targets(
        self, status: str | None = None, limit: int | None = 500, search: str = "", offset: int = 0
    ) -> list[dict[str, Any]]:
        clause, params = self._target_filter(status, search)
        query = "SELECT * FROM targets WHERE " + clause + " ORDER BY id DESC"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend((max(1, int(limit)), max(0, int(offset))))
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(query, params)
            return [dict(row) for row in await cur.fetchall()]

    def _target_filter(self, status, search):
        clause, params = "campaign_id = ?", [self.campaign_id]
        if status and status != "all":
            clause += " AND status = ?"
            params.append(status)
        if search.strip():
            clause += " AND instr(lower(username), ?) > 0"
            params.append(search.strip().lstrip("@").lower())
        return clause, params

    async def count_targets(self, status=None, search=""):
        clause, params = self._target_filter(status, search)
        async with _connect() as db:
            cur = await db.execute("SELECT COUNT(*) FROM targets WHERE " + clause, params)
            return (await cur.fetchone())[0]

    async def get_targets_stats(self) -> dict[str, int]:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'added' THEN 1 ELSE 0 END) as added,
                    SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) as sent,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status = 'processing' THEN 1 ELSE 0 END) as processing,
                    SUM(CASE WHEN status IN ('error', 'uncertain') THEN 1 ELSE 0 END) as errors,
                    SUM(CASE WHEN status IN ('already_in', 'private', 'not_found', 'already_sent') THEN 1 ELSE 0 END) as skipped
                FROM targets WHERE campaign_id = ?
                """,
                (self.campaign_id,),
            )
            row = await cur.fetchone()
            if not row:
                return {
                    "total": 0,
                    "added": 0,
                    "sent": 0,
                    "pending": 0,
                    "processing": 0,
                    "errors": 0,
                    "skipped": 0,
                }
            return {
                "total": int(row["total"] or 0),
                "added": int(row["added"] or 0),
                "sent": int(row["sent"] or 0),
                "pending": int(row["pending"] or 0),
                "processing": int(row["processing"] or 0),
                "errors": int(row["errors"] or 0),
                "skipped": int(row["skipped"] or 0),
            }

    async def get_summary_stats(self) -> dict[str, int]:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'added' THEN 1 ELSE 0 END) as added,
                    SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) as sent,
                    SUM(CASE WHEN status IN ('already_in', 'not_found', 'private', 'already_sent') THEN 1 ELSE 0 END) as skipped,
                    SUM(CASE WHEN status IN ('error', 'uncertain') THEN 1 ELSE 0 END) as errors
                FROM targets WHERE campaign_id = ?
                """,
                (self.campaign_id,),
            )
            row = await cur.fetchone()
            return {
                "added": int(row["added"] or 0) if row else 0,
                "sent": int(row["sent"] or 0) if row else 0,
                "skipped": int(row["skipped"] or 0) if row else 0,
                "errors": int(row["errors"] or 0) if row else 0,
            }

    async def add_log(
        self,
        account_phone: str | None,
        username: str | None,
        action: str,
        message: str,
    ) -> None:
        async with _connect() as db:
            await db.execute(
                """
                INSERT INTO logs (account_phone, username, action, message, created_at, campaign_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (account_phone, username, action, message, _now_iso(), self.campaign_id),
            )
            await db.commit()

    async def get_logs(self, limit: int | None = 1000) -> list[dict[str, Any]]:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM logs WHERE campaign_id=? ORDER BY id DESC" + (" LIMIT ?" if limit is not None else ""),
                (self.campaign_id, limit) if limit is not None else (self.campaign_id,),
            )
            rows = await cur.fetchall()
            return [reveal_row(row) for row in rows]

    async def get_recent_logs(self, limit: int = 20) -> list[dict[str, Any]]:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT action, message, account_phone, created_at
                FROM logs WHERE campaign_id=?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (self.campaign_id, limit),
            )
            rows = await cur.fetchall()
            return [reveal_row(row) for row in rows]

    async def clear_logs(self) -> None:
        async with _connect() as db:
            await db.execute("DELETE FROM logs WHERE campaign_id=?", (self.campaign_id,))
            await db.commit()

    async def set_setting(self, key: str, value: str) -> None:
        scoped = key in CAMPAIGN_SETTINGS
        key = self._setting_key(key)
        async with _connect() as db:
            if scoped:
                await db.execute("BEGIN IMMEDIATE")
                await _require_active_campaign(db, self.campaign_id)
            await db.execute(
                """
                INSERT INTO settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            await db.commit()

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        key = self._setting_key(key)
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cur.fetchone()
            if row is None:
                return default
            return row["value"]

    async def get_settings_map(self, keys: list[str]) -> dict[str, str | None]:
        """Read several settings over one connection; hot loops call this per tick."""
        scoped = [self._setting_key(key) for key in keys]
        async with _connect() as db:
            rows = await (
                await db.execute(
                    "SELECT key, value FROM settings WHERE key IN (" + ",".join("?" * len(scoped)) + ")",
                    scoped,
                )
            ).fetchall()
        found = {row[0]: row[1] for row in rows}
        return {key: found.get(self._setting_key(key)) for key in keys}

    async def add_chat(self, username: str, title: str | None = None) -> None:
        chat_value = username.strip()
        if not chat_value:
            return
        if chat_value.startswith("@"):
            chat_value = chat_value[1:]
        now = _now_iso()
        display_title = title or chat_value
        async with _connect() as db:
            await db.execute(
                """
                INSERT INTO chats (username, title, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET title = excluded.title
                """,
                (chat_value, display_title, now),
            )
            await db.commit()

    async def get_chats(self) -> list[dict[str, Any]]:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM chats ORDER BY id DESC")
            rows = await cur.fetchall()
            return [reveal_row(row) for row in rows]

    async def delete_chat(self, username: str) -> None:
        chat_value = username.strip().lstrip("@")
        async with _connect() as db:
            await db.execute("DELETE FROM chats WHERE username = ?", (chat_value,))
            await db.commit()

    async def create_warmup_session(self, account_id: int, level: str, topic: str, days: int) -> int:
        now = _now_iso()
        async with _connect() as db:
            cur = await db.execute(
                """
                INSERT INTO warmup_sessions (
                    account_id, level, topic, status, day_current, day_total,
                    actions_today, actions_total, last_reset_date, started_at
                )
                VALUES (?, ?, ?, 'idle', 1, ?, 0, 0, ?, ?)
                """,
                (account_id, level, topic, int(days), _today_iso(), now),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def get_warmup_session(self, account_id: int) -> dict[str, Any] | None:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM warmup_sessions WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                (account_id,),
            )
            row = await cur.fetchone()
            return reveal_row(row) if row else None

    async def update_warmup_session(self, session_id: int, **kwargs) -> None:
        if not kwargs:
            return
        fields = []
        values: list[Any] = []
        allowed = {
            "daily_action_limit",
            "level",
            "topic",
            "status",
            "day_current",
            "day_total",
            "actions_today",
            "actions_total",
            "last_action_at",
            "last_reset_date",
            "started_at",
            "completed_at",
        }
        for key, value in kwargs.items():
            if key in allowed:
                fields.append(f"{key} = ?")
                values.append(value)
        if not fields:
            return
        values.append(session_id)
        async with _connect() as db:
            await db.execute(f"UPDATE warmup_sessions SET {', '.join(fields)} WHERE id = ?", values)
            await db.commit()

    async def add_warmup_log(self, account_id: int, action: str, detail: str, status: str) -> None:
        async with _connect() as db:
            await db.execute(
                """
                INSERT INTO warmup_logs (account_id, action, detail, status, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (account_id, action, detail, status, _now_iso()),
            )
            await db.commit()

    async def get_warmup_logs(self, account_id: int, limit: int = 50) -> list[dict[str, Any]]:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM warmup_logs WHERE account_id = ? ORDER BY id DESC LIMIT ?",
                (account_id, limit),
            )
            rows = await cur.fetchall()
            return [reveal_row(row) for row in rows]

    async def reset_warmup_actions_today(self, session_id: int) -> None:
        async with _connect() as db:
            await db.execute(
                """
                UPDATE warmup_sessions
                SET actions_today = 0, last_reset_date = ?
                WHERE id = ?
                """,
                (_today_iso(), session_id),
            )
            await db.commit()

    async def set_account_warmup_status(self, account_id: int, status: str) -> None:
        async with _connect() as db:
            await db.execute(
                "UPDATE accounts SET warmup_status = ? WHERE id = ?",
                (status, account_id),
            )
            await db.commit()

    async def save_account_channels(self, account_id: int, channels: list[dict[str, Any]]) -> None:
        synced_at = _now_iso()
        async with _connect() as db:
            selected_cursor = await db.execute(
                "SELECT channel_id FROM warmup_channels WHERE account_id = ? AND is_selected = 1", (account_id,)
            )
            selected = {row[0] for row in await selected_cursor.fetchall()}
            await db.execute("DELETE FROM warmup_channels WHERE account_id = ?", (account_id,))
            for ch in channels:
                await db.execute(
                    """
                    INSERT INTO warmup_channels (
                        account_id, channel_id, username, title, channel_type,
                        is_selected, members_count, synced_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        int(ch.get("id") or 0),
                        (ch.get("username") or "").strip(),
                        (ch.get("title") or "").strip(),
                        (ch.get("type") or "group").strip(),
                        int(int(ch.get("id") or 0) in selected),
                        int(ch.get("members_count") or 0),
                        synced_at,
                    ),
                )
            await db.commit()

    async def get_account_channels(self, account_id: int) -> list[dict[str, Any]]:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM warmup_channels WHERE account_id = ? ORDER BY title COLLATE NOCASE",
                (account_id,),
            )
            rows = await cur.fetchall()
            return [reveal_row(row) for row in rows]

    async def toggle_channel_selected(self, channel_id: int, selected: bool) -> None:
        async with _connect() as db:
            await db.execute(
                "UPDATE warmup_channels SET is_selected = ? WHERE id = ?",
                (1 if selected else 0, channel_id),
            )
            await db.commit()

    async def get_selected_channels(self, account_id: int) -> list[dict[str, Any]]:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT * FROM warmup_channels
                WHERE account_id = ? AND is_selected = 1
                ORDER BY title COLLATE NOCASE
                """,
                (account_id,),
            )
            rows = await cur.fetchall()
            return [reveal_row(row) for row in rows]

    async def add_ai_message(
        self,
        account_id: int,
        chat_key: str,
        role: str,
        content: str,
        peer_name: str | None = None,
    ) -> None:
        message_text = (content or "").strip()
        key = (chat_key or "").strip()
        if not message_text or not key:
            return
        async with _connect() as db:
            await db.execute(
                """
                INSERT INTO ai_messages (
                    account_id, chat_key, role, content, peer_name, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (int(account_id), key, str(role).strip(), message_text[:1000], (peer_name or "").strip(), _now_iso()),
            )
            # Prevent unbounded growth: keep last 200 events per account/chat.
            await db.execute(
                """
                DELETE FROM ai_messages
                WHERE account_id = ? AND chat_key = ?
                  AND id NOT IN (
                      SELECT id FROM ai_messages
                      WHERE account_id = ? AND chat_key = ?
                      ORDER BY id DESC
                      LIMIT 200
                  )
                """,
                (int(account_id), key, int(account_id), key),
            )
            # Keep memory table "alive" and timestamp fresh for this dialog.
            await db.execute(
                """
                INSERT INTO ai_memory (account_id, chat_key, summary, updated_at)
                VALUES (?, ?, '', ?)
                ON CONFLICT(account_id, chat_key)
                DO UPDATE SET updated_at = excluded.updated_at
                """,
                (int(account_id), key, _now_iso()),
            )
            await db.commit()

    async def get_ai_messages(self, account_id: int, chat_key: str, limit: int = 10) -> list[dict[str, Any]]:
        key = (chat_key or "").strip()
        if not key:
            return []
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT role, content, peer_name, created_at
                FROM ai_messages
                WHERE account_id = ? AND chat_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(account_id), key, int(limit)),
            )
            rows = await cur.fetchall()
            return [reveal_row(row) for row in rows]

    async def get_last_ai_message(
        self,
        account_id: int,
        chat_key: str,
        role: str = "assistant",
    ) -> dict[str, Any] | None:
        key = (chat_key or "").strip()
        if not key:
            return None
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT id, role, content, peer_name, created_at
                FROM ai_messages
                WHERE account_id = ? AND chat_key = ? AND role = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(account_id), key, str(role).strip()),
            )
            row = await cur.fetchone()
            return reveal_row(row) if row else None

    async def count_recent_ai_messages(
        self,
        account_id: int,
        role: str = "assistant",
        minutes: int = 60,
    ) -> int:
        if minutes <= 0:
            return 0
        threshold_dt = datetime.now() - timedelta(minutes=int(minutes))
        threshold = threshold_dt.isoformat(timespec="seconds")
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM ai_messages
                WHERE account_id = ? AND role = ? AND created_at >= ?
                """,
                (int(account_id), str(role).strip(), threshold),
            )
            row = await cur.fetchone()
            return int(row["cnt"] or 0) if row else 0

    async def get_ai_memory_summary(self, account_id: int, chat_key: str) -> str:
        key = (chat_key or "").strip()
        if not key:
            return ""
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT summary
                FROM ai_memory
                WHERE account_id = ? AND chat_key = ?
                LIMIT 1
                """,
                (int(account_id), key),
            )
            row = await cur.fetchone()
            return str(row["summary"] or "") if row else ""

    async def upsert_ai_memory_summary(self, account_id: int, chat_key: str, summary: str) -> None:
        key = (chat_key or "").strip()
        if not key:
            return
        async with _connect() as db:
            await db.execute(
                """
                INSERT INTO ai_memory (account_id, chat_key, summary, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id, chat_key)
                DO UPDATE SET summary = excluded.summary, updated_at = excluded.updated_at
                """,
                (int(account_id), key, (summary or "").strip()[:2000], _now_iso()),
            )
            await db.commit()

    async def clear_ai_memory_for_account(self, account_id: int) -> None:
        async with _connect() as db:
            await db.execute("DELETE FROM ai_messages WHERE account_id = ?", (int(account_id),))
            await db.execute("DELETE FROM ai_memory WHERE account_id = ?", (int(account_id),))
            await db.commit()

    async def clear_ai_memory_for_chat_keys(self, account_id: int, chat_keys: list[str]) -> int:
        keys = [str(k).strip() for k in chat_keys if str(k).strip()]
        if not keys:
            return 0
        placeholders = ",".join(["?"] * len(keys))
        params: list[Any] = [int(account_id), *keys]
        async with _connect() as db:
            cur = await db.execute(
                f"DELETE FROM ai_messages WHERE account_id = ? AND chat_key IN ({placeholders})",
                params,
            )
            deleted_messages = int(cur.rowcount or 0)
            await db.execute(
                f"DELETE FROM ai_memory WHERE account_id = ? AND chat_key IN ({placeholders})",
                params,
            )
            await db.commit()
            return deleted_messages

    async def add_ai_config(self, name: str, provider: str, api_key: str, model: str) -> int:
        from core.ai_providers import validate_connection

        provider = provider.strip()
        name, api_key, model = validate_connection(name, provider, api_key, model)
        async with _connect() as db:
            cur = await db.execute(
                """
                INSERT INTO ai_configs (
                    name, provider, api_key, model, is_active,
                    total_tokens, total_cost_usd, created_at
                )
                VALUES (?, ?, ?, ?, 0, 0, 0.0, ?)
                """,
                (name.strip(), provider.strip(), protect(api_key.strip()), model.strip(), _now_iso()),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def update_ai_config(self, config_id: int, name: str, provider: str, api_key: str, model: str) -> None:
        from core.ai_providers import validate_connection

        provider = provider.strip()
        name, api_key, model = validate_connection(name, provider, api_key, model)
        async with _connect() as db:
            cur = await db.execute(
                "UPDATE ai_configs SET name = ?, provider = ?, api_key = ?, model = ? WHERE id = ?",
                (name, provider, protect(api_key), model, config_id),
            )
            if cur.rowcount != 1:
                raise ValueError("Конфигурация AI не найдена")
            await db.commit()

    async def get_ai_configs(self) -> list[dict[str, Any]]:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM ai_configs ORDER BY is_active DESC, id DESC")
            rows = await cur.fetchall()
            return [reveal_row(row) for row in rows]

    async def get_active_ai_config(self) -> dict[str, Any] | None:
        async with _connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM ai_configs WHERE is_active = 1 ORDER BY id DESC LIMIT 1")
            row = await cur.fetchone()
            return reveal_row(row) if row else None

    async def set_active_ai_config(self, config_id: int) -> None:
        async with _connect() as db:
            cur = await db.execute("SELECT id FROM ai_configs WHERE id = ?", (config_id,))
            if not await cur.fetchone():
                raise ValueError("Конфигурация AI не найдена")
            await db.execute("UPDATE ai_configs SET is_active = 0")
            await db.execute("UPDATE ai_configs SET is_active = 1 WHERE id = ?", (config_id,))
            await db.commit()

    async def update_ai_usage(self, config_id: int, tokens: int, cost: float) -> None:
        async with _connect() as db:
            await db.execute(
                """
                UPDATE ai_configs
                SET total_tokens = COALESCE(total_tokens, 0) + ?,
                    total_cost_usd = COALESCE(total_cost_usd, 0) + ?
                WHERE id = ?
                """,
                (int(tokens), float(cost), config_id),
            )
            await db.commit()

    async def delete_ai_config(self, config_id: int) -> None:
        async with _connect() as db:
            await db.execute("DELETE FROM ai_configs WHERE id = ?", (config_id,))
            await db.commit()


async def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with _connect() as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY,
                phone TEXT UNIQUE,
                api_id TEXT,
                api_hash TEXT,
                session_name TEXT,
                proxy_type TEXT,
                proxy_host TEXT,
                proxy_port INTEGER,
                proxy_login TEXT,
                proxy_password TEXT,
                daily_limit INTEGER DEFAULT 40,
                added_today INTEGER DEFAULT 0,
                last_reset_date TEXT,
                status TEXT DEFAULT 'inactive',
                flood_until TEXT,
                notes TEXT,
                created_at TEXT,
                is_enabled INTEGER DEFAULT 1,
                consecutive_adds INTEGER DEFAULT 0,
                rotation_break_until TEXT,
                account_age_days INTEGER DEFAULT 0,
                device_profile TEXT,
                sort_order INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS targets (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE,
                status TEXT DEFAULT 'pending',
                added_by_account_id INTEGER,
                error_message TEXT,
                processed_at TEXT,
                created_at TEXT,
                sort_order INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY,
                account_phone TEXT,
                username TEXT,
                action TEXT,
                message TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS message_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                text TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS parse_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,account_id INTEGER,sources TEXT NOT NULL,
                status TEXT NOT NULL,started_at TEXT,finished_at TEXT,detail TEXT
            );
            CREATE TABLE IF NOT EXISTS parse_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,run_id INTEGER NOT NULL,reference TEXT NOT NULL,
                source_id INTEGER,title TEXT,chat_type TEXT,membership TEXT,count_hint INTEGER,
                status TEXT,detail TEXT,fetched INTEGER DEFAULT 0,UNIQUE(run_id,reference)
            );
            CREATE TABLE IF NOT EXISTS parsed_members (
                run_id INTEGER NOT NULL,source_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
                username TEXT,first_name TEXT,last_name TEXT,role TEXT,is_bot INTEGER,is_deleted INTEGER,
                is_premium INTEGER,has_photo INTEGER,activity TEXT,last_seen TEXT,
                PRIMARY KEY(run_id,source_id,user_id)
            );

            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                title TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS warmup_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                level TEXT DEFAULT 'medium',
                topic TEXT DEFAULT '',
                status TEXT DEFAULT 'idle',
                day_current INTEGER DEFAULT 0,
                day_total INTEGER DEFAULT 7,
                actions_today INTEGER DEFAULT 0,
                actions_total INTEGER DEFAULT 0,
                last_action_at TEXT,
                last_reset_date TEXT,
                started_at TEXT,
                completed_at TEXT,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );

            CREATE TABLE IF NOT EXISTS warmup_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                action TEXT,
                detail TEXT,
                status TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS warmup_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                channel_id INTEGER,
                username TEXT,
                title TEXT,
                channel_type TEXT,
                is_selected INTEGER DEFAULT 0,
                members_count INTEGER DEFAULT 0,
                synced_at TEXT,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );

            CREATE TABLE IF NOT EXISTS ai_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider TEXT NOT NULL,
                api_key TEXT NOT NULL,
                model TEXT NOT NULL,
                is_active INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                total_cost_usd REAL DEFAULT 0.0,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS ai_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                chat_key TEXT NOT NULL,
                summary TEXT DEFAULT '',
                updated_at TEXT,
                UNIQUE(account_id, chat_key)
            );

            CREATE TABLE IF NOT EXISTS ai_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                chat_key TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                peer_name TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS ai_send_events (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL,
                chat_key TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_send_events_account_created
                ON ai_send_events(account_id, created_at);

            """
        )
        migrations = {
            "parse_runs": {
                "mode": "TEXT NOT NULL DEFAULT 'members'",
                "since": "TEXT",
                "until": "TEXT",
                "scan_limit": "INTEGER",
            },
            "parsed_members": {
                "message_count": "INTEGER NOT NULL DEFAULT 0",
                "first_message_at": "TEXT",
                "last_message_at": "TEXT",
            },
            "accounts": {
                "sent_today": "INTEGER DEFAULT 0",
                "last_message_reset_date": "TEXT",
                "followups_today": "INTEGER DEFAULT 0",
                "followup_reset_date": "TEXT",
                "is_enabled": "INTEGER DEFAULT 1",
                "consecutive_adds": "INTEGER DEFAULT 0",
                "rotation_break_until": "TEXT",
                "account_age_days": "INTEGER DEFAULT 0",
                "device_profile": "TEXT",
                "sort_order": "INTEGER DEFAULT 0",
                "warmup_status": "TEXT DEFAULT 'cold'",
            },
            "targets": {"sort_order": "INTEGER DEFAULT 0"},
            "logs": {"campaign_id": "INTEGER NOT NULL DEFAULT 1"},
            "warmup_sessions": {"daily_action_limit": "INTEGER DEFAULT 0"},
            "warmup_channels": {"members_count": "INTEGER DEFAULT 0", "synced_at": "TEXT"},
            "ai_configs": {"total_tokens": "INTEGER DEFAULT 0", "total_cost_usd": "REAL DEFAULT 0.0"},
        }
        for table, columns in migrations.items():
            cursor = await db.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in await cursor.fetchall()}
            for name, definition in columns.items():
                if name not in existing:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS campaigns(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created_at TEXT)"
        )
        await db.execute(
            "INSERT OR IGNORE INTO campaigns(id, name, created_at) VALUES (1, 'Основная кампания', ?)", (_now_iso(),)
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS campaign_runs(id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL, chat TEXT, started_at TEXT, ended_at TEXT, outcome TEXT, summary TEXT)"
        )
        cur = await db.execute("PRAGMA table_info(targets)")
        if "campaign_id" not in {row[1] for row in await cur.fetchall()}:
            # Rebuild the legacy UNIQUE(username) constraint without changing row ids or outcomes.
            await db.execute(
                "CREATE TABLE targets_new(id INTEGER PRIMARY KEY, username TEXT, status TEXT DEFAULT 'pending', added_by_account_id INTEGER, error_message TEXT, processed_at TEXT, created_at TEXT, sort_order INTEGER DEFAULT 0, campaign_id INTEGER NOT NULL DEFAULT 1, UNIQUE(campaign_id, username))"
            )
            await db.execute(
                "INSERT INTO targets_new SELECT id, username, status, added_by_account_id, error_message, processed_at, created_at, sort_order, 1 FROM targets"
            )
            await db.execute("DROP TABLE targets")
            await db.execute("ALTER TABLE targets_new RENAME TO targets")
        cursor = await db.execute("PRAGMA table_info(campaigns)")
        campaign_columns = {row[1] for row in await cursor.fetchall()}
        if "kind" not in campaign_columns:
            await db.execute("ALTER TABLE campaigns ADD COLUMN kind TEXT NOT NULL DEFAULT 'invite'")
        if "deleted_at" not in campaign_columns:
            await db.execute("ALTER TABLE campaigns ADD COLUMN deleted_at TEXT")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS message_deliveries(username TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE, campaign_id INTEGER NOT NULL, target_id INTEGER NOT NULL, account_id INTEGER NOT NULL, random_id INTEGER NOT NULL, state TEXT NOT NULL, message_id INTEGER, created_at TEXT, completed_at TEXT)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS message_followups(username TEXT PRIMARY KEY,user_id INTEGER NOT NULL,access_hash INTEGER NOT NULL,"
            "campaign_id INTEGER NOT NULL,target_id INTEGER NOT NULL,account_id INTEGER NOT NULL,text TEXT NOT NULL,state TEXT NOT NULL,"
            "first_message_id INTEGER,reply_id INTEGER,random_id INTEGER,message_id INTEGER,checked_id INTEGER NOT NULL DEFAULT 0,"
            "checked_at TEXT,created_at TEXT,completed_at TEXT,UNIQUE(account_id,user_id))"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_followups_campaign_state ON message_followups(campaign_id,state)"
        )
        await db.execute("UPDATE message_followups SET state='uncertain' WHERE state IN ('armed','sending')")
        interrupted_followups = await (
            await db.execute(
                "SELECT campaign_id,username,account_id FROM message_followups WHERE state IN ('waiting','ready')"
            )
        ).fetchall()
        for campaign_id, username, account_id in interrupted_followups:
            await db.execute(
                "UPDATE message_followups SET state='cancelled',completed_at=? "
                "WHERE campaign_id=? AND username=? AND state IN ('waiting','ready')",
                (_now_iso(), campaign_id, username),
            )
            await db.execute(
                "INSERT INTO logs(account_phone,username,action,message,created_at,campaign_id) "
                "VALUES ((SELECT phone FROM accounts WHERE id=?),?,'FOLLOWUP_CANCELLED',?,?,?)",
                (
                    account_id,
                    username,
                    "Приложение закрывалось; отложенное второе сообщение отменено",
                    _now_iso(),
                    campaign_id,
                ),
            )
        await db.execute(
            "UPDATE targets SET status='uncertain',error_message='Результат неизвестен: проверьте переписку вручную' "
            "WHERE status IN ('pending','processing') AND EXISTS "
            "(SELECT 1 FROM message_deliveries d WHERE d.state='reserved' AND d.target_id=targets.id "
            "AND d.campaign_id=targets.campaign_id AND d.username=targets.username)"
        )
        await db.execute(
            "UPDATE message_deliveries SET state='uncertain',completed_at=? WHERE state='reserved'", (_now_iso(),)
        )
        for key in CAMPAIGN_SETTINGS:
            await db.execute(
                "INSERT OR IGNORE INTO settings(key, value) SELECT ?, value FROM settings WHERE key=? "
                "AND EXISTS (SELECT 1 FROM campaigns WHERE id=1 AND deleted_at IS NULL)",
                (f"campaign:1:{key}", key),
            )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_targets_campaign_status ON targets(campaign_id, status, sort_order, id)"
        )
        await db.execute(
            "UPDATE campaign_runs SET outcome='interrupted', ended_at=? WHERE outcome='running'", (_now_iso(),)
        )
        await db.execute("UPDATE settings SET value='stopped' WHERE key LIKE 'campaign:%:task_status'")
        await db.execute("UPDATE targets SET status='pending' WHERE status='processing'")
        await db.execute(
            "UPDATE parse_runs SET status='interrupted',finished_at=?,detail='Получение прервано при закрытии. Сохранённые записи доступны.' WHERE status='running'",
            (_now_iso(),),
        )
        await db.execute(
            "UPDATE parse_sources SET status='partial',detail='Получение прервано; сохранена доступная часть' WHERE status='running'"
        )
        await db.commit()
        await db.executescript("""
            CREATE INDEX IF NOT EXISTS idx_targets_status_sort
                ON targets(status, sort_order, id);
            CREATE INDEX IF NOT EXISTS idx_logs_created_at
                ON logs(created_at);
            CREATE INDEX IF NOT EXISTS idx_warmup_logs_account_created
                ON warmup_logs(account_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_warmup_channels_account_selected
                ON warmup_channels(account_id, is_selected);
            CREATE INDEX IF NOT EXISTS idx_ai_messages_acc_chat_id
                ON ai_messages(account_id, chat_key, id DESC);
            CREATE INDEX IF NOT EXISTS idx_accounts_enabled_status
                ON accounts(is_enabled, status);
            CREATE INDEX IF NOT EXISTS idx_ai_messages_account_created
                ON ai_messages(account_id, role, created_at);
        """)
        # Existing credentials are encrypted on startup; no secret is logged.
        for table, fields in (("accounts", ("api_hash", "proxy_password")), ("ai_configs", ("api_key",))):
            cursor = await db.execute(f"SELECT id, {', '.join(fields)} FROM {table}")
            for row in await cursor.fetchall():
                for field, value in zip(fields, row[1:], strict=True):
                    encoded = protect(value)
                    if encoded != value:
                        await db.execute(f"UPDATE {table} SET {field} = ? WHERE id = ?", (encoded, row[0]))
        await db.execute("UPDATE warmup_sessions SET status = 'paused' WHERE status = 'running'")
        await db.execute("UPDATE accounts SET warmup_status = 'paused' WHERE warmup_status = 'warming'")
        await db.execute(
            "INSERT INTO settings(key, value) VALUES ('task_status', 'stopped') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        await db.commit()
        await db.execute("PRAGMA journal_mode = WAL")

    db = Database()
    await db.reset_daily_counters_if_needed()
    await db.reset_processing_to_pending()
