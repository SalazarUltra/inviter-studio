import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator, ChatAdminRights, User

from core.export import write_parsed
from core.parser import audience_usernames, filter_members, member_row, unique_members


def row(user_id=1, source_id=-100101, **changes):
    result = member_row(User(id=user_id, username=f"person_{user_id}", first_name="Имя"), None, source_id)
    return {**result, "source_title": "Группа", **changes}


class ParsingFilterTests(unittest.TestCase):
    def test_member_row_stores_risk_flags(self):
        user = User(id=7, username="flagged", scam=True, fake=True, restricted=True, premium=True)
        result = member_row(user, None, -1)
        self.assertTrue(result["scam"] and result["fake"] and result["restricted"] and result["is_premium"])
        clean = member_row(User(id=8, username="clean"), None, -1)
        self.assertFalse(clean["scam"] or clean["fake"] or clean["restricted"] or clean["contact_require_premium"])

    def test_scam_fake_restricted_excluded_by_default_and_optional(self):
        rows = [row(1), row(2, scam=True), row(3, fake=True), row(4, restricted=True)]
        self.assertEqual([item["user_id"] for item in filter_members(rows)], [1])
        everyone = filter_members(rows, exclude_scam=False, exclude_fake=False, exclude_restricted=False)
        self.assertEqual(len(everyone), 4)
        self.assertEqual([item["user_id"] for item in filter_members(rows, exclude_scam=False)], [1, 2])

    def test_rows_without_flag_columns_are_not_excluded(self):
        old = row(1)
        for key in ("scam", "fake", "restricted"):
            old.pop(key)
        self.assertEqual(len(filter_members([old])), 1)

    def test_filter_returns_original_rows_without_copying(self):
        rows = [row(1)]
        self.assertIs(filter_members(rows)[0], rows[0])

    def test_empty_query_keeps_everyone_and_query_matches_id(self):
        rows = [row(1), row(22)]
        self.assertEqual(len(filter_members(rows, query="  ")), 2)
        self.assertEqual([item["user_id"] for item in filter_members(rows, query="22")], [22])

    def test_audience_skips_admins_creators_bots_deleted_and_nameless(self):
        rows = unique_members(
            [
                row(1),
                row(2, role="admin"),
                row(3, role="creator"),
                row(4, is_bot=True),
                row(5, is_deleted=True),
                row(6, username=""),
                row(1, source_id=-202, role="admin"),
                row(7),
            ]
        )
        names, skipped = audience_usernames(rows)
        self.assertEqual(names, ["person_7"])
        self.assertEqual(skipped["admins"], 3)
        self.assertEqual((skipped["bots"], skipped["deleted"], skipped["no_username"]), (1, 1, 1))
        names, _ = audience_usernames(rows, include_admins=True)
        self.assertEqual(names, ["person_1", "person_2", "person_3", "person_7"])

    def test_member_row_role_from_participant(self):
        user = User(id=9, username="boss")
        rights = ChatAdminRights()
        creator = ChannelParticipantCreator(user_id=9, admin_rights=rights)
        self.assertEqual(member_row(user, creator, -1)["role"], "creator")
        admin = ChannelParticipantAdmin(user_id=9, promoted_by=1, date=datetime.now(timezone.utc), admin_rights=rights)
        self.assertEqual(member_row(user, admin, -1)["role"], "admin")


class ParsedXlsxTests(unittest.TestCase):
    def test_xlsx_export_has_headers_flags_and_safe_text(self):
        people = unique_members([row(1, first_name="=SUM(1)\x07", scam=True), row(2, username="")])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "people.xlsx"
            write_parsed(path, people, "xlsx")
            with zipfile.ZipFile(path) as book:
                sheet = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
                workbook = book.read("xl/workbook.xml").decode("utf-8")
            self.assertIn("Участники", workbook)
            self.assertIn("Telegram ID", sheet)
            self.assertIn("@person_1", sheet)
            self.assertIn("=SUM(1)", sheet)  # stored as inline text, never as a formula
            self.assertNotIn("<f>", sheet)
            self.assertNotIn("\x07", sheet)
            self.assertIn("<cols", sheet)
            self.assertEqual(list(Path(folder).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
