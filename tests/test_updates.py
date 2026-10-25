import hashlib
import tempfile
import unittest
from pathlib import Path

from core import updates


class VersionCompareTests(unittest.TestCase):
    def test_parse_version_accepts_plain_and_prefixed_tags(self):
        self.assertEqual(updates.parse_version("1.2.15"), (1, 2, 15))
        self.assertEqual(updates.parse_version("v1.2.16"), (1, 2, 16))
        self.assertIsNone(updates.parse_version("nightly"))
        self.assertIsNone(updates.parse_version("1.2"))
        self.assertIsNone(updates.parse_version(""))

    def test_is_newer_compares_numerically(self):
        self.assertTrue(updates.is_newer("v1.2.16", "1.2.15"))
        self.assertTrue(updates.is_newer("v1.3.0", "1.2.99"))
        self.assertTrue(updates.is_newer("v2.0.0", "1.99.99"))
        self.assertFalse(updates.is_newer("v1.2.15", "1.2.15"))
        self.assertFalse(updates.is_newer("v1.2.14", "1.2.15"))
        self.assertFalse(updates.is_newer("latest", "1.2.15"))


class ChecksumTests(unittest.TestCase):
    def test_expected_checksum_matches_setup_asset_only(self):
        digest = "a" * 64
        text = f"{'b' * 64}  other.txt\n{digest}  InviterStudioSetup.exe\n"
        self.assertEqual(updates.expected_checksum(text), digest)

    def test_expected_checksum_rejects_malformed_digests(self):
        self.assertIsNone(updates.expected_checksum("zzz  InviterStudioSetup.exe"))
        self.assertIsNone(updates.expected_checksum("abc  readme.txt"))
        self.assertIsNone(updates.expected_checksum(""))


class DownloadTests(unittest.TestCase):
    def test_hash_file_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "payload.bin"
            path.write_bytes(b"inviter")
            self.assertEqual(updates.hash_file(path), hashlib.sha256(b"inviter").hexdigest())

    def test_failed_download_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "setup.exe"
            with self.assertRaises(RuntimeError):
                import asyncio

                asyncio.run(updates.download_installer("not a url", destination, "0" * 64))
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_name(destination.name + ".part").exists())


if __name__ == "__main__":
    unittest.main()
