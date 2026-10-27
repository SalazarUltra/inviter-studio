import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta

from core.conversion import conversion_report, format_rate, period_bounds, rate
from tests.support import DatabaseCase

NOW = datetime(2026, 10, 27, 12, 0, 0)


def stamp(days=0):
    return (NOW - timedelta(days=days)).isoformat(timespec="seconds")


class ConversionHelpersTests(unittest.TestCase):
    def test_rate_and_format(self):
        self.assertEqual(rate(1, 3), 33.3)
        self.assertIsNone(rate(1, 0))
        self.assertEqual((format_rate(None), format_rate(50.0), format_rate(33.3)), ("—", "50%", "33.3%"))

    def test_period_bounds(self):
        self.assertEqual(period_bounds("today", NOW), ("2026-10-27T00:00:00", None))
        self.assertEqual(period_bounds("7d", NOW), (stamp(7), None))
        self.assertEqual(period_bounds("all", NOW), (None, None))
        with self.assertRaises(ValueError):
            period_bounds("year", NOW)


class ConversionReportTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.first, self.second = await self.add_account(1), await self.add_account(2)
        self.invite = (await self.db.get_campaigns())[0]["id"]
        self.messages = await self.db.create_campaign("Сообщения", "messages")
        self.connection = sqlite3.connect(self.root / "inviter.db")
        self.addCleanup(self.connection.close)

    def sql(self, query, params=()):
        with closing(self.connection.cursor()) as cursor:
            cursor.execute(query, params)
        self.connection.commit()

    def target(self, campaign, name, status, account=None, days=0):
        self.sql(
            "INSERT INTO targets(username,status,added_by_account_id,processed_at,campaign_id,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (name, status, account, stamp(days), campaign, stamp(days)),
        )

    def log(self, phone, action, message, campaign=None, days=0):
        self.sql(
            "INSERT INTO logs(account_phone,username,action,message,created_at,campaign_id) VALUES (?,?,?,?,?,?)",
            (phone, "x", action, message, stamp(days), campaign or self.invite),
        )

    def delivery(self, number, state, account, campaign=None, days=0):
        self.sql(
            "INSERT INTO message_deliveries(username,user_id,campaign_id,target_id,account_id,random_id,state,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"user{number}", number, campaign or self.messages, number, account, number, state, stamp(days)),
        )

    def followup(self, number, state, account, reply=None, days=0):
        self.sql(
            "INSERT INTO message_followups(username,user_id,access_hash,campaign_id,target_id,account_id,text,state,"
            "reply_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"user{number}", number, 1, self.messages, number, account, "t", state, reply, stamp(days)),
        )

    def seed(self):
        a, b = self.first["id"], self.second["id"]
        for index, (status, account) in enumerate(
            [("added", a), ("added", a), ("added", b), ("private", None), ("error", None), ("already_in", None)]
        ):
            self.target(self.invite, f"p{index}", status, account)
        self.target(self.invite, "old", "added", a, days=40)
        self.log(self.first["phone"], "SKIP", "private")
        self.log(self.second["phone"], "SKIP", "already_in")
        self.log(self.second["phone"], "ADD_ERROR", "boom")
        self.log(self.first["phone"], "FLOOD_WAIT", "wait")
        self.log(self.first["phone"], "ADD_SUCCESS", "ok")  # successes come from targets, never counted twice
        # Messages: 3 sent (2 from a), 1 private, 1 uncertain; 2 replies of which one second message sent.
        for number, (state, account) in enumerate(
            [("sent", a), ("sent", a), ("sent", b), ("blocked", a), ("uncertain", b)], 1
        ):
            self.delivery(number, state, account)
        self.delivery(99, "reserved", a)
        self.delivery(100, "sent", a, days=40)
        self.followup(1, "ready", a, reply=11)
        self.followup(2, "sent", a, reply=12)
        self.followup(3, "waiting", b)
        self.followup(4, "cancelled", b)

    async def test_invite_rates_for_the_selected_campaign_and_period(self):
        self.seed()
        report = await conversion_report(self.db, self.invite, "all", now=NOW)
        invites = report["invites"]
        self.assertEqual((report["kind"], report["campaign_name"]), ("invite", "Основная кампания"))
        self.assertEqual(
            {key: invites[key] for key in ("success", "skipped", "errors", "already_in", "total")},
            {"success": 4, "skipped": 1, "errors": 1, "already_in": 1, "total": 7},
        )
        self.assertEqual(invites["rates"]["success"], 57.1)
        self.assertEqual(invites["rates"]["already_in"], 14.3)
        recent = await conversion_report(self.db, self.invite, "30d", now=NOW)
        self.assertEqual((recent["invites"]["success"], recent["invites"]["total"]), (3, 6))
        self.assertEqual(report["messages"]["attempts"], 0)  # a messages campaign is not counted as invites

    async def test_message_counts_replies_and_second_messages(self):
        self.seed()
        report = await conversion_report(self.db, self.messages, "30d", now=NOW)
        messages = report["messages"]
        self.assertEqual(
            {key: messages[key] for key in ("sent", "private", "uncertain", "attempts", "replies")},
            {"sent": 3, "private": 1, "uncertain": 1, "attempts": 5, "replies": 2},
        )
        self.assertEqual((messages["followup_ready"], messages["followup_sent"]), (1, 1))
        self.assertEqual(
            (messages["rates"]["sent"], messages["rates"]["replies"], messages["rates"]["followup_sent"]),
            (60.0, 66.7, 50.0),
        )
        self.assertEqual(report["invites"]["total"], 0)
        everything = await conversion_report(self.db, self.messages, "all", now=NOW)
        self.assertEqual(everything["messages"]["sent"], 4)

    async def test_per_account_breakdown(self):
        self.seed()
        invite = {
            row["phone"]: row for row in (await conversion_report(self.db, self.invite, "30d", now=NOW))["accounts"]
        }
        first, second = invite[self.first["phone"]]["invites"], invite[self.second["phone"]]["invites"]
        self.assertEqual((first["success"], first["skipped"], first["floods"]), (2, 1, 1))
        self.assertEqual((second["success"], second["already_in"], second["errors"]), (1, 1, 1))
        messages = {
            row["phone"]: row["messages"]
            for row in (await conversion_report(self.db, self.messages, "30d", now=NOW))["accounts"]
        }
        first, second = messages[self.first["phone"]], messages[self.second["phone"]]
        self.assertEqual((first["sent"], first["private"], first["replies"], first["followup_sent"]), (2, 1, 2, 1))
        self.assertEqual((second["sent"], second["uncertain"], second["replies"]), (1, 1, 0))

    async def test_all_campaigns_and_empty_campaign(self):
        self.seed()
        report = await conversion_report(self.db, None, "30d", now=NOW)
        self.assertEqual((report["invites"]["success"], report["messages"]["sent"]), (3, 3))
        empty = await conversion_report(self.db, await self.db.create_campaign("Пустая", "invite"), "all", now=NOW)
        self.assertEqual((empty["invites"]["total"], empty["accounts"]), (0, []))
        self.assertIsNone(empty["invites"]["rates"]["success"])
        with self.assertRaises(ValueError):
            await conversion_report(self.db, 999, "all", now=NOW)

    async def test_ledger_counts_people_not_keys_and_follows_the_campaign_chat(self):
        await self.db.set_setting("target_chat", "@my_chat")
        for key, when in (("id:1", 0), ("u:alice", 0), ("u:bob", 0), ("u:carol", 40)):
            self.sql(
                "INSERT INTO invited_ledger(chat_key,user_key,invited_at,account_id) VALUES (?,?,?,?)",
                ("my_chat", key, stamp(when), self.first["id"]),
            )
        self.sql(
            "INSERT INTO invited_ledger(chat_key,user_key,invited_at,account_id) VALUES ('other','id:9',?,?)",
            (stamp(0), self.first["id"]),
        )
        report = await conversion_report(self.db, self.invite, "30d", now=NOW)
        self.assertEqual(report["invites"]["ledger"], 2)  # id:1 + (alice, bob) -> the larger key family
        self.assertEqual(report["accounts"][0]["invites"]["ledger"], 2)
        self.assertEqual((await conversion_report(self.db, self.invite, "all", now=NOW))["invites"]["ledger"], 3)
        self.assertEqual((await conversion_report(self.db, self.messages, "all", now=NOW))["invites"]["ledger"], 0)


if __name__ == "__main__":
    unittest.main()
