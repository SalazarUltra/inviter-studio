"""Durable, one-time second messages bound to a confirmed first delivery."""

import secrets
from contextlib import asynccontextmanager
from datetime import datetime

import aiosqlite


@asynccontextmanager
async def connection():
    from core.database import _connect

    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        yield db


async def note(db, row, action, text):
    now = datetime.now().isoformat(timespec="seconds")
    await db.execute(
        "UPDATE targets SET error_message=? WHERE id=? AND campaign_id=? AND username=? AND status='sent'",
        (text, row["target_id"], row["campaign_id"], row["username"]),
    )
    await db.execute(
        "INSERT INTO logs(account_phone,username,action,message,created_at,campaign_id) "
        "VALUES ((SELECT phone FROM accounts WHERE id=?),?,?,?,?,?)",
        (row["account_id"], row["username"], action, text, now, row["campaign_id"]),
    )


class FollowupStore:
    async def get_followups(self, states=("waiting", "ready"), account_ids=None, *, limit=None, per_account=False):
        if not states or account_ids is not None and not account_ids:
            return []
        sql = "SELECT * FROM message_followups WHERE campaign_id=? AND state IN (" + ",".join("?" for _ in states) + ")"
        if per_account:
            sql = sql.replace(
                "SELECT *",
                "SELECT *,ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY COALESCE(checked_at,''),username) AS queue_rank",
                1,
            )
        params = [self.campaign_id, *states]
        if account_ids is not None:
            sql += " AND account_id IN (" + ",".join("?" for _ in account_ids) + ")"
            params.extend(account_ids)
        if per_account:
            sql = "SELECT * FROM (" + sql + ") WHERE queue_rank=1"
        sql += " ORDER BY COALESCE(checked_at,''),username"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        async with connection() as db:
            return [dict(row) for row in await (await db.execute(sql, params)).fetchall()]

    async def get_followup_stats(self, account_ids=None):
        if account_ids is not None and not account_ids:
            return {}
        async with connection() as db:
            sql = "SELECT state,COUNT(*) AS count FROM message_followups WHERE campaign_id=?"
            params = [self.campaign_id]
            if account_ids is not None:
                sql += " AND account_id IN (" + ",".join("?" for _ in account_ids) + ")"
                params.extend(account_ids)
            sql += " GROUP BY state"
            rows = await (await db.execute(sql, params)).fetchall()
            return {row["state"]: row["count"] for row in rows}

    async def receive_followup_reply(self, account_id, user_id, message_id):
        if not isinstance(message_id, int) or message_id <= 0:
            return False
        async with connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            campaign_status = await (
                await db.execute(
                    "SELECT value FROM settings WHERE key=?",
                    (self._setting_key("task_status"),),
                )
            ).fetchone()
            if not campaign_status or campaign_status[0] not in {"running", "paused"}:
                await db.commit()
                return False
            row = await (
                await db.execute(
                    "SELECT * FROM message_followups WHERE campaign_id=? AND account_id=? AND user_id=? "
                    "AND state='waiting' AND first_message_id>0 AND first_message_id<?",
                    (self.campaign_id, account_id, user_id, message_id),
                )
            ).fetchone()
            if not row:
                await db.commit()
                return False
            await db.execute(
                "UPDATE message_followups SET state='ready',reply_id=? "
                "WHERE campaign_id=? AND username=? AND state='waiting'",
                (message_id, self.campaign_id, row["username"]),
            )
            await note(db, row, "FOLLOWUP_REPLY", "Получен ответ. Второе сообщение ожидает отправки")
            await db.commit()
            return True

    async def followup_checked(self, username, last_id):
        async with connection() as db:
            await db.execute(
                "UPDATE message_followups SET checked_at=?,checked_id=MAX(checked_id,?) "
                "WHERE campaign_id=? AND username=? AND state='waiting'",
                (datetime.now().isoformat(timespec="microseconds"), last_id, self.campaign_id, username),
            )
            await db.commit()

    async def claim_followup(self, username):
        async with connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT * FROM message_followups WHERE username=? AND campaign_id=? AND state='ready'",
                    (username, self.campaign_id),
                )
            ).fetchone()
            if not row:
                await db.commit()
                return None
            random_id = secrets.randbits(63) or 1
            await db.execute(
                "UPDATE message_followups SET state='sending',random_id=? "
                "WHERE campaign_id=? AND username=? AND state='ready'",
                (random_id, self.campaign_id, username),
            )
            await db.commit()
            return dict(row) | {"state": "sending", "random_id": random_id}

    async def finish_followup(self, username, state, message_id=None):
        if state not in {"sent", "blocked", "uncertain", "ready"}:
            raise ValueError("Неизвестный результат второго сообщения")
        async with connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT * FROM message_followups WHERE username=? AND campaign_id=? AND state='sending'",
                    (username, self.campaign_id),
                )
            ).fetchone()
            if not row:
                raise RuntimeError("Второе сообщение уже обработано")
            await db.execute(
                "UPDATE message_followups SET state=?,message_id=?,completed_at=? WHERE campaign_id=? AND username=?",
                (state, message_id, datetime.now().isoformat(), self.campaign_id, username),
            )
            if state == "sent":
                # A delivered follow-up consumes its own daily quota, never the first-message one.
                today = datetime.now().date().isoformat()
                await db.execute(
                    "UPDATE accounts SET followups_today=CASE WHEN followup_reset_date=? "
                    "THEN followups_today+1 ELSE 1 END,followup_reset_date=? WHERE id=?",
                    (today, today, row["account_id"]),
                )
            labels = {
                "sent": ("FOLLOWUP_SENT", "Второе сообщение отправлено после ответа"),
                "blocked": ("FOLLOWUP_BLOCKED", "Второе сообщение не отправлено: личные сообщения недоступны"),
                "uncertain": (
                    "FOLLOWUP_UNCERTAIN",
                    "Исход второго сообщения неизвестен. Проверьте переписку; повтора не будет",
                ),
                "ready": ("FOLLOWUP_WAIT", "Второе сообщение отложено из-за ограничений Telegram"),
            }
            await note(db, row, *labels[state])
            await db.commit()

    async def block_followup(self, username):
        async with connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT * FROM message_followups WHERE campaign_id=? AND username=? AND state IN ('waiting','ready')",
                    (self.campaign_id, username),
                )
            ).fetchone()
            if row:
                await db.execute(
                    "UPDATE message_followups SET state='blocked' WHERE campaign_id=? AND username=?",
                    (self.campaign_id, username),
                )
                await note(db, row, "FOLLOWUP_BLOCKED", "Второе сообщение не отправлено: переписка недоступна")
            await db.commit()

    async def recover_followups(self):
        async with connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await (
                await db.execute(
                    "SELECT * FROM message_followups WHERE campaign_id=? AND state IN ('armed','sending')",
                    (self.campaign_id,),
                )
            ).fetchall()
            for row in rows:
                await db.execute(
                    "UPDATE message_followups SET state='uncertain',completed_at=? WHERE campaign_id=? AND username=?",
                    (datetime.now().isoformat(), self.campaign_id, row["username"]),
                )
                await note(
                    db,
                    row,
                    "FOLLOWUP_UNCERTAIN",
                    "Отправка прервана. Второе сообщение требует ручной проверки; повтора не будет",
                )
            await db.commit()

    async def cancel_followups(self, reason="Ожидание второго сообщения отменено пользователем"):
        async with connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            status = await (
                await db.execute("SELECT value FROM settings WHERE key=?", (self._setting_key("task_status"),))
            ).fetchone()
            if status and status[0] in {"running", "paused"}:
                raise RuntimeError("Сначала остановите задачу")
            rows = await (
                await db.execute(
                    "SELECT * FROM message_followups WHERE campaign_id=? AND state IN ('waiting','ready')",
                    (self.campaign_id,),
                )
            ).fetchall()
            for row in rows:
                await db.execute(
                    "UPDATE message_followups SET state='cancelled',completed_at=? "
                    "WHERE campaign_id=? AND username=? AND state IN ('waiting','ready')",
                    (datetime.now().isoformat(), self.campaign_id, row["username"]),
                )
                await note(db, row, "FOLLOWUP_CANCELLED", reason)
            await db.commit()
            return len(rows)
