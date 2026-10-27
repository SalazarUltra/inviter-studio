import ctypes
import hashlib
import os
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from core.instance_lock import InstanceLock
from tools.build_assets import create_assets
from tools.build_windows import validate_package, version_resource
from tools.distribution import prepare_portable_handoff


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
                "profile/data/inviter.db",
                "backups/auto.db",
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

    def test_portable_handoff_contains_runtime_launcher_checksum_and_guide(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            package = root / "dist/releases/test/InviterStudio"
            package.mkdir(parents=True)
            (package / "InviterStudio.exe").write_bytes(b"app fixture")
            (package / "_internal").mkdir()
            (package / "_internal/runtime.dll").write_bytes(b"runtime fixture")
            portable = prepare_portable_handoff(root, package)
            self.assertTrue(portable.is_file())
            with zipfile.ZipFile(portable) as archive:
                names = set(archive.namelist())
                self.assertIn("InviterStudio.exe", names)
                self.assertIn("_internal/runtime.dll", names)
                self.assertIn("start.bat", names)
                self.assertIn("update.bat", names)
                self.assertNotIn("update.ps1", names)
                self.assertIn("ПРОЧТИ_ПЕРЕД_ЗАПУСКОМ.txt", names)
                self.assertTrue(all("profile" not in name and "sessions" not in name for name in names))
                guide = archive.read("ПРОЧТИ_ПЕРЕД_ЗАПУСКОМ.txt").decode("utf-8-sig")
                self.assertIn("profile", guide)
                self.assertIn("никому не отправляйте", guide)
                launcher = archive.read("start.bat").decode("utf-8")
                self.assertIn("_internal", launcher)
                self.assertIn("Unblock-File", launcher)
                self.assertIn("Разблокировать", launcher)
                updater = archive.read("update.bat").decode("utf-8")
                self.assertIn("certutil -hashfile", updater)
                self.assertIn("curl.exe", updater)
                self.assertNotIn("powershell.exe", updater)
                self.assertIn("\\stage\\InviterStudio.exe", updater)
                self.assertNotIn("InviterStudioPortable\\InviterStudio.exe", updater)
            handoff = portable.parent
            self.assertEqual(
                {path.name for path in handoff.iterdir()},
                {"InviterStudioPortable.zip", "SHA256.txt", "ПЕРЕДАЙТЕ_ПОЛЬЗОВАТЕЛЮ.txt"},
            )
            checksums = (handoff / "SHA256.txt").read_text(encoding="ascii")
            self.assertIn(hashlib.sha256(portable.read_bytes()).hexdigest(), checksums)

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
