import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.export import write_targets_csv
from core.paths import session_name, session_path
from core.validation import csv_cell, normalize_username, time_minutes, validate_account, validate_proxy
from tests.support import account_payload
from ui.dispatch import UIDispatcher
from ui.tab_targets import parse_txt


class ValidationTests(unittest.TestCase):
    def test_session_paths_reject_traversal_and_absolute_paths(self):
        for name in ("../../secret", r"..\secret", "C:/outside", "session_..", "session_a/b"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                session_path("+380500000001", name)
        self.assertEqual(session_name("+380500000001", "session_test.session"), "session_test")

    def test_usernames_and_links_are_normalized(self):
        self.assertEqual(normalize_username(" @Alice "), "alice")
        self.assertEqual(normalize_username("https://t.me/Alice?foo=bar"), "alice")
        for invalid in ("", "a b c", "../../bad", "https://evil.test/alice", "https://t.me/+invite", "123abc"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_username(invalid)

    def test_account_and_proxy_validation(self):
        validate_account(account_payload())
        for changes in ({"phone": "not-a-number"}, {"api_id": 0}, {"api_hash": "bad"}, {"daily_limit": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_account({**account_payload(), **changes})
        with self.assertRaises(ValueError):
            validate_proxy({"proxy_type": "UNKNOWN", "proxy_host": "host", "proxy_port": 1080})
        with self.assertRaises(ValueError):
            validate_proxy({"proxy_type": "SOCKS5", "proxy_host": "host", "proxy_port": 0})

    def test_time_validation(self):
        self.assertEqual(time_minutes("23:59"), 1439)
        for invalid in ("24:00", "12:60", "abc", "9:30"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                time_minutes(invalid)

    def test_bom_import_is_ordered_and_reports_bad_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.txt"
            path.write_text("@Alice\n# comment\nalice\nhttps://t.me/Bobby\n", encoding="utf-8-sig")
            self.assertEqual(parse_txt(str(path)), ["alice", "bobby"])
            path.write_text("alice\ninvalid name\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "2"):
                parse_txt(str(path))

    def test_csv_neutralizes_formulas(self):
        for formula in ("=1+1", "+SUM(A1)", "@SUM(A1)", " -1+1", "\t=1"):
            self.assertTrue(csv_cell(formula).startswith("'"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.csv"
            write_targets_csv(str(path), [{"username": "alice", "error_message": "=DANGEROUS()"}])
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[1][-1], "'=DANGEROUS()")

    def test_failed_export_keeps_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.csv"
            path.write_text("original", encoding="utf-8")
            with patch("core.export.os.replace", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    write_targets_csv(str(path), [])
            self.assertEqual(path.read_text(encoding="utf-8"), "original")
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_ui_callbacks_only_run_when_drained(self):
        dispatcher = UIDispatcher()
        calls = []
        dispatcher.post(lambda: calls.append("done"))
        self.assertEqual(calls, [])
        dispatcher.drain(self.fail)
        self.assertEqual(calls, ["done"])
        dispatcher.closed = True
        dispatcher.post(lambda: calls.append("late"))
        dispatcher.drain(self.fail)
        self.assertEqual(calls, ["done"])

    def test_ui_dispatch_recovers_from_callback_failure(self):
        dispatcher = UIDispatcher()
        errors = []
        calls = []

        def fail():
            raise RuntimeError("test")

        dispatcher.post(fail)
        dispatcher.post(lambda: calls.append("done"))
        dispatcher.drain(errors.append)
        self.assertEqual(len(errors), 1)
        self.assertEqual(calls, ["done"])
