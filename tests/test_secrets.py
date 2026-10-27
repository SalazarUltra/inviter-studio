import unittest
from unittest.mock import patch

from core import secrets


class SecretsGuardTests(unittest.TestCase):
    def test_protect_never_returns_plaintext_off_windows(self):
        with patch("core.secrets.os.name", "posix"):
            with self.assertRaises(RuntimeError):
                secrets.protect("super-secret")

    def test_frozen_non_windows_fails_with_clear_message(self):
        with patch("core.secrets.os.name", "posix"), patch.object(secrets.sys, "frozen", True, create=True):
            with self.assertRaises(RuntimeError) as ctx:
                secrets.protect("super-secret")
        self.assertIn("только Windows", str(ctx.exception))

    def test_empty_and_already_protected_values_pass_through(self):
        with patch("core.secrets.os.name", "posix"):
            self.assertEqual(secrets.protect(""), "")
            self.assertIsNone(secrets.protect(None))
            self.assertEqual(secrets.protect(secrets.PREFIX + "abc"), secrets.PREFIX + "abc")


if __name__ == "__main__":
    unittest.main()
