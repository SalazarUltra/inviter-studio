import ctypes
import hashlib
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from core.instance_lock import InstanceLock
from tools.build_assets import create_assets
from tools.build_windows import validate_package, version_resource
from tools.distribution import prepare_handoff


class InstallerBuildTests(unittest.TestCase):
    def test_package_requires_runtime_and_rejects_user_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(RuntimeError):
                validate_package(root)
            (root / "InviterStudio.exe").write_bytes(b"fixture")
            (root / "_internal").mkdir()
            validate_package(root)
            for name in (
                "data/users.txt",
                "sessions/session_1.session",
                "backup.db",
                ".env.local",
                "_internal/data.sqlite3",
                "test.db-wal",
            ):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"private fixture")
                with self.subTest(path=name), self.assertRaises(RuntimeError):
                    validate_package(root)
                path.unlink()
                if path.parent != root and not list(path.parent.iterdir()) and path.parent.name != "_internal":
                    path.parent.rmdir()

    def test_brand_assets_and_windows_version_resource_generated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            create_assets(root)
            self.assertTrue(all((root / name).stat().st_size > 100 for name in ("app.ico", "wizard.bmp", "small.bmp")))
            version_resource(root / "version.txt")
            self.assertIn("ProductName','Inviter Studio", (root / "version.txt").read_text())
            self.assertIn("LegalCopyright", (root / "version.txt").read_text())

    def test_handoff_has_only_installer_instructions_and_matching_checksum(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "dist/InviterStudioSetup.exe"
            source.parent.mkdir()
            source.write_bytes(b"installer fixture")
            target = prepare_handoff(root, source)
            self.assertEqual(
                {path.name for path in target.iterdir()}, {"InviterStudioSetup.exe", "СНАЧАЛА_ПРОЧТИ.txt", "SHA256.txt"}
            )
            self.assertEqual((target / "InviterStudioSetup.exe").read_bytes(), source.read_bytes())
            self.assertIn(hashlib.sha256(source.read_bytes()).hexdigest(), (target / "SHA256.txt").read_text())
            self.assertIn("ДОСТАТОЧНО ОДНОГО ФАЙЛА", (target / "СНАЧАЛА_ПРОЧТИ.txt").read_text(encoding="utf-8-sig"))
            source.write_bytes(b"updated installer")
            self.assertEqual(prepare_handoff(root, source), target)
            self.assertEqual((target / "InviterStudioSetup.exe").read_bytes(), b"updated installer")

    @unittest.skipUnless(os.name == "nt", "Windows named mutex")
    def test_named_mutex_lifetime_and_independent_profile_locks(self):
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.OpenMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        name = "Local\\InviterStudio.Tests." + uuid.uuid4().hex

        def exists():
            handle = kernel.OpenMutexW(0x100000, False, name)
            if handle:
                kernel.CloseHandle(handle)
            return bool(handle)

        with tempfile.TemporaryDirectory() as folder, patch("core.instance_lock.APP_MUTEX", name):
            self.assertFalse(exists())
            with patch("core.instance_lock.DATA_DIR", Path(folder) / "one"), InstanceLock():
                self.assertTrue(exists())
                with self.assertRaises(RuntimeError):
                    with InstanceLock():
                        pass
                with patch("core.instance_lock.DATA_DIR", Path(folder) / "two"), InstanceLock():
                    self.assertTrue(exists())
                self.assertTrue(exists())
            self.assertFalse(exists())
