import copy
import json
import tempfile
import unittest
from pathlib import Path

from core.campaign_transfer import (
    EXPORT_SETTING_KEYS,
    FORBIDDEN_KEYS,
    MAX_FILE_BYTES,
    build_campaign_export,
    export_campaign,
    find_forbidden_keys,
    import_campaign,
    parse_campaign_import,
    read_campaign_file,
    write_campaign_file,
)
from tests.support import DatabaseCase, account_payload

SECRET_HASH = "deadbeef" * 4
PROXY_PASSWORD = "Pr0xy-P@ssw0rd-XYZ"
AI_KEY = "sk-live-VERY-SECRET-KEY-123456"
SESSION_NAME = "sess-private-file"


def valid_payload(**changes):
    payload = {
        "format": "botinviter.campaign",
        "version": 1,
        "campaign": {"name": "Spring", "kind": "messages"},
        "settings": {"message_text": "Hello {имя}", "interval_min": "60", "interval_max": "120"},
        "targets": [{"username": "alice"}],
    }
    payload.update(changes)
    return payload


class ImportValidationTests(unittest.TestCase):
    def test_minimal_file_is_accepted(self):
        parsed = parse_campaign_import(valid_payload())
        self.assertEqual(parsed["kind"], "messages")
        self.assertEqual(parsed["targets"], [{"username": "alice"}])
        self.assertTrue(parsed["name"].startswith("Spring"))

    def test_rejects_foreign_or_broken_files(self):
        for bad in (
            [],
            {},
            valid_payload(format="other"),
            valid_payload(version=99),
            valid_payload(version="1"),
            valid_payload(campaign={"name": "x", "kind": "sms"}),
            valid_payload(campaign="nope"),
            valid_payload(settings=[]),
            valid_payload(targets={}),
        ):
            with self.assertRaises(ValueError, msg=str(bad)[:60]):
                parse_campaign_import(bad)

    def test_rejects_invalid_settings_before_anything_is_created(self):
        cases = [
            {"interval_min": "5", "interval_max": "40"},
            {"interval_min": "100", "interval_max": "50"},
            {"interval_min": "20", "interval_max": "40"},  # below the direct-message minimum
            {"ab_enabled": "1", "message_text_b": ""},
            {"ab_enabled": "1", "message_text_b": "B", "ab_split_a": "100"},
            {"ab_enabled": "yes"},
            {"followup_enabled": "1", "followup_text": ""},
            {"schedule_from": "25:00"},
            {"message_text": "x" * 5000},
            {"invite_order": "random"},
            {"message_text": ["not", "text"]},
        ]
        for extra in cases:
            payload = valid_payload()
            payload["settings"] = {**payload["settings"], **extra}
            with self.assertRaises(ValueError, msg=str(extra)):
                parse_campaign_import(payload)

    def test_rejects_invalid_targets_with_row_number(self):
        with self.assertRaises(ValueError) as caught:
            parse_campaign_import(valid_payload(targets=[{"username": "alice"}, {"username": "x"}]))
        self.assertIn("№2", str(caught.exception))
        for bad in ([{}], [5.5], [{"user_id": "abc"}], [{"username": "alice", "user_id": "zzz"}]):
            with self.assertRaises(ValueError, msg=str(bad)):
                parse_campaign_import(valid_payload(targets=bad))

    def test_targets_accept_strings_ids_and_deduplicate(self):
        parsed = parse_campaign_import(
            valid_payload(
                campaign={"name": "Inv", "kind": "invite"},
                settings={},
                targets=[
                    "@Alice",
                    {"username": "alice"},
                    {"user_id": 777},
                    "id:777",
                    {"username": "bob_1", "user_id": "42"},
                ],
            )
        )
        self.assertEqual(
            parsed["targets"],
            [
                {"username": "alice"},
                {"username": "", "user_id": 777},
                {"username": "bob_1", "user_id": 42},
            ],
        )

    def test_secrets_and_unknown_fields_in_a_file_are_dropped_and_reported(self):
        payload = valid_payload(accounts=[{"phone": "+1", "api_hash": SECRET_HASH}], session="x.session")
        payload["settings"] = {
            **payload["settings"],
            "api_hash": SECRET_HASH,
            "proxy_password": PROXY_PASSWORD,
            "selected_accounts": "[1,2]",
            "task_status": "running",
        }
        parsed = parse_campaign_import(payload)
        for key in ("api_hash", "proxy_password", "selected_accounts", "task_status"):
            self.assertNotIn(key, parsed["settings"])
            self.assertIn(key, parsed["ignored"])
        self.assertTrue(set(parsed["settings"]) <= set(EXPORT_SETTING_KEYS))
        self.assertIn("accounts", parsed["secrets_dropped"])
        self.assertIn("settings.api_hash", parsed["secrets_dropped"])
        self.assertNotIn(SECRET_HASH, json.dumps(parsed, ensure_ascii=False))

    def test_name_is_limited_and_marked_as_import(self):
        parsed = parse_campaign_import(valid_payload(campaign={"name": "N" * 200, "kind": "invite"}, settings={}))
        self.assertLessEqual(len(parsed["name"]), 80)
        self.assertTrue(parsed["name"].endswith("(импорт)"))
        same = parse_campaign_import(valid_payload(), rename=False)
        self.assertEqual(same["name"], "Spring")

    def test_forbidden_key_scan_is_recursive(self):
        self.assertEqual(find_forbidden_keys({"a": [{"b": {"API_HASH": 1}}]}), ["a[0].b.API_HASH"])
        self.assertEqual(find_forbidden_keys({"message_text": "api_hash in text is fine"}), [])
        self.assertIn("proxy_password", FORBIDDEN_KEYS)


class FileTests(unittest.TestCase):
    def test_write_read_roundtrip_and_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "c.json"
            write_campaign_file(str(path), {"format": "x", "text": "Привет"})
            self.assertEqual(read_campaign_file(str(path)), {"format": "x", "text": "Привет"})
            self.assertEqual([item.name for item in Path(folder).iterdir()], ["c.json"])  # no temp leftovers
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_campaign_file(str(path))
            path.write_bytes(b"\xff\xfe\x00\x00garbage")
            with self.assertRaises(ValueError):
                read_campaign_file(str(path))
            with self.assertRaises(ValueError):
                read_campaign_file(str(Path(folder) / "missing.json"))
            path.write_bytes(b"{" + b" " * MAX_FILE_BYTES + b"}")
            with self.assertRaises(ValueError):
                read_campaign_file(str(path))


class RoundtripTests(DatabaseCase):
    async def seed_secrets(self):
        """Everything sensitive the app stores: none of it may reach a campaign file."""
        await self.db.add_account(
            {
                **account_payload(1),
                "api_hash": SECRET_HASH,
                "proxy_type": "SOCKS5",
                "proxy_host": "10.1.2.3",
                "proxy_port": 1080,
                "proxy_login": "proxy-user",
                "proxy_password": PROXY_PASSWORD,
                "session_name": SESSION_NAME,
            }
        )
        await self.db.add_ai_config("Main", "openai", AI_KEY, "gpt-4o")
        await self.db.set_setting("selected_accounts", "[1]")

    async def make_messages_campaign(self):
        campaign_id = await self.db.create_campaign("Весна", "messages")
        self.db = self.db.for_campaign(campaign_id)
        await self.db.save_campaign_settings(
            {
                "operation_mode": "messages",
                "message_text": "Привет, {имя}! {Как дела|Что нового}?",
                "ab_enabled": "1",
                "message_text_b": "Здравствуйте, {имя}",
                "ab_split_a": "30",
                "followup_enabled": "1",
                "followup_text": "Второе",
                "followup_wait_hours": "48",
                "interval_min": "75",
                "interval_max": "130",
                "schedule_enabled": "1",
                "schedule_from": "10:00",
                "schedule_to": "18:30",
                "schedule_days": "0,1,2",
                "protection_consecutive": "4",
                "protection_break_min": "6",
                "protection_break_max": "9",
                "campaign_daily_cap": "25",
                "require_warmup_ready": "1",
                "selected_accounts": "[1]",
                "task_status": "stopped",
            }
        )
        await self.db.bulk_insert_targets(["alice", "bobby", "carol_x", "dave_01"])
        target = await self.db.claim_next_pending_target()
        await self.db.mark_target(target["id"], "error", error_message="boom")
        return campaign_id

    async def test_messages_campaign_roundtrip_without_secrets(self):
        await self.seed_secrets()
        source_id = await self.make_messages_campaign()
        source_settings = await self.db.get_settings_map(list(EXPORT_SETTING_KEYS))
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "campaign.json")
            payload = await export_campaign(self.db, path, include_statuses=True)
            raw = Path(path).read_text(encoding="utf-8")
            data = json.loads(raw)

            # 1) no secret value or secret key anywhere in the file
            for secret in (SECRET_HASH, PROXY_PASSWORD, AI_KEY, SESSION_NAME, "10.1.2.3", "proxy-user", "+38050000"):
                self.assertNotIn(secret, raw)
            self.assertEqual(find_forbidden_keys(data), [])
            self.assertEqual(data, payload)
            self.assertTrue(set(data["settings"]) <= set(EXPORT_SETTING_KEYS))
            self.assertNotIn("selected_accounts", data["settings"])
            self.assertNotIn("task_status", data["settings"])

            # 2) content is what the user configured
            self.assertEqual(data["campaign"], {"name": "Весна", "kind": "messages"})
            self.assertEqual([t["username"] for t in data["targets"]], ["alice", "bobby", "carol_x", "dave_01"])
            self.assertEqual(sorted(t["status"] for t in data["targets"]), ["error", "pending", "pending", "pending"])
            self.assertEqual(data["settings"]["message_text_b"], "Здравствуйте, {имя}")

            # 3) import creates a NEW campaign with identical settings
            parsed = parse_campaign_import(read_campaign_file(path))
            self.assertEqual(parsed["ignored"], [])
            self.assertEqual(parsed["secrets_dropped"], [])
            result = await import_campaign(self.db, parsed)
        new_id = result["campaign_id"]
        self.assertNotEqual(new_id, source_id)
        self.assertEqual(result["stats"]["inserted"], 4)
        copy_db = self.db.for_campaign(new_id)
        self.assertEqual(await copy_db.get_campaign_kind(), "messages")
        imported_settings = await copy_db.get_settings_map(list(EXPORT_SETTING_KEYS))
        self.assertEqual(
            {k: v for k, v in imported_settings.items() if v is not None},
            {k: v for k, v in source_settings.items() if v is not None},
        )
        self.assertEqual(await copy_db.get_setting("operation_mode"), "messages")
        # accounts and run state are NOT carried over
        self.assertIsNone(await copy_db.get_setting("selected_accounts"))
        self.assertIsNone(await copy_db.get_setting("task_status"))
        # targets come back in order and ALL pending (the old error status is informational only)
        rows = list(reversed(await copy_db.get_targets(status="all", limit=None)))
        self.assertEqual([r["username"] for r in rows], ["alice", "bobby", "carol_x", "dave_01"])
        self.assertEqual({r["status"] for r in rows}, {"pending"})
        # the source campaign is untouched
        self.assertEqual((await self.db.get_targets_stats())["errors"], 1)
        self.assertEqual(await self.db.get_setting("message_text_b"), "Здравствуйте, {имя}")

    async def test_export_without_statuses_omits_them(self):
        await self.make_messages_campaign()
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "c.json")
            await export_campaign(self.db, path)
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertFalse(data["includes_statuses"])
        self.assertTrue(all(set(t) == {"username"} for t in data["targets"]))

    async def test_roundtrip_is_idempotent(self):
        await self.make_messages_campaign()
        with tempfile.TemporaryDirectory() as folder:
            first, second = str(Path(folder) / "1.json"), str(Path(folder) / "2.json")
            payload_one = await export_campaign(self.db, first)
            result = await import_campaign(self.db, parse_campaign_import(read_campaign_file(first), rename=False))
            payload_two = await export_campaign(self.db.for_campaign(result["campaign_id"]), second)
        for payload in (payload_one, payload_two):
            payload.pop("exported_at")
        self.assertEqual(payload_one, payload_two)

    async def test_invite_campaign_with_id_only_recipients_roundtrip(self):
        campaign_id = await self.db.create_campaign("Chat", "invite")
        self.db = self.db.for_campaign(campaign_id)
        await self.db.save_campaign_settings(
            {"operation_mode": "invite", "target_chat": "mygroup", "invite_order": "as_is", "interval_min": "20"}
        )
        await self.db.bulk_insert_targets(
            ["alice", "id:555", {"username": "bobby", "user_id": 42}], preserve_order=True
        )
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "c.json")
            payload = await export_campaign(self.db, path, include_statuses=True)
            self.assertNotIn("ab_enabled", payload["settings"])
            self.assertIn({"user_id": 555, "status": "pending"}, payload["targets"])
            result = await import_campaign(self.db, parse_campaign_import(read_campaign_file(path)))
        copy_db = self.db.for_campaign(result["campaign_id"])
        self.assertEqual(await copy_db.get_campaign_kind(), "invite")
        self.assertEqual(await copy_db.get_setting("target_chat"), "mygroup")
        rows = {r["username"]: r for r in await copy_db.get_targets(status="all", limit=None)}
        self.assertEqual(set(rows), {"alice", "id:555", "bobby"})
        self.assertEqual(rows["id:555"]["user_id"], 555)
        self.assertEqual(rows["bobby"]["user_id"], 42)

    async def test_imported_messages_campaign_respects_shared_delivery_ledger(self):
        campaign_id = await self.db.create_campaign("Old", "messages")
        self.db = self.db.for_campaign(campaign_id)
        await self.db.bulk_insert_targets(["alice"])
        account = await self.add_account()
        target = await self.db.claim_next_pending_target()
        await self.db.reserve_delivery(target["id"], account["id"], 11)
        await self.db.finish_delivery(target["id"], "sent", 5)
        result = await import_campaign(
            self.db, parse_campaign_import(valid_payload(targets=[{"username": "alice"}, {"username": "bobby"}]))
        )
        rows = {r["username"]: r["status"] for r in await self.db.for_campaign(result["campaign_id"]).get_targets()}
        self.assertEqual(rows, {"alice": "already_sent", "bobby": "pending"})

    async def test_hand_edited_file_with_secrets_never_stores_them(self):
        payload = valid_payload()
        payload["settings"] = {**payload["settings"], "api_hash": SECRET_HASH, "proxy_password": PROXY_PASSWORD}
        payload["accounts"] = [{"api_hash": SECRET_HASH}]
        result = await import_campaign(self.db, parse_campaign_import(copy.deepcopy(payload)))
        scoped = self.db.for_campaign(result["campaign_id"])
        self.assertIsNone(await scoped.get_setting("api_hash"))
        import aiosqlite

        from core.database import DB_PATH

        async with aiosqlite.connect(DB_PATH) as connection:
            rows = await (await connection.execute("SELECT key,value FROM settings")).fetchall()
        self.assertFalse([row for row in rows if SECRET_HASH in str(row) or PROXY_PASSWORD in str(row)])

    async def test_export_builder_refuses_to_leak_even_if_data_contains_secrets(self):
        data = {
            "name": "X",
            "kind": "messages",
            "settings": {"message_text": "hi", "api_hash": SECRET_HASH, "proxy_password": PROXY_PASSWORD},
            "targets": [{"username": "alice", "user_id": None, "status": "pending", "api_hash": SECRET_HASH}],
        }
        payload = build_campaign_export(data)
        self.assertNotIn(SECRET_HASH, json.dumps(payload))
        self.assertNotIn(PROXY_PASSWORD, json.dumps(payload))

    async def test_failed_target_import_removes_the_half_created_campaign(self):
        before = {row["id"] for row in await self.db.get_campaigns()}
        parsed = parse_campaign_import(valid_payload())
        scoped_class = type(self.db)
        original = scoped_class.import_audience

        async def boom(self, *args, **kwargs):
            raise RuntimeError("disk full")

        scoped_class.import_audience = boom
        try:
            with self.assertRaises(RuntimeError):
                await import_campaign(self.db, parsed)
        finally:
            scoped_class.import_audience = original
        after = {row["id"] for row in await self.db.get_campaigns()}
        self.assertEqual(before, after)
