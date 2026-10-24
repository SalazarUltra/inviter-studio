"""Install/update/uninstall in a disposable workspace with isolated registry and shortcuts."""

import ctypes
import hashlib
import json
import os
import sqlite3
import subprocess
import uuid
import winreg
from contextlib import closing
from pathlib import Path

from core.version import APP_MUTEX, VERSION
from tools.build_windows import ROOT, find_compiler


def main():
    audit = ROOT / ".audit/installer"
    base = audit / ("scenario-" + uuid.uuid4().hex[:8])
    base.mkdir(parents=True)
    app = base / "Приложение с пробелами"
    desktop, menu, profile = base / "Desktop", base / "StartMenu", base / "profile"
    for path in (desktop, menu, profile / "data", profile / "sessions"):
        path.mkdir(parents=True, exist_ok=True)
    assert app.resolve().is_relative_to(audit.resolve())
    app_id = "InviterStudio.Test." + uuid.uuid4().hex[:12]
    registry_path = "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" + app_id + "_is1"
    manifest = json.loads((ROOT / "dist/release.json").read_text())
    package = ROOT / manifest["package"]
    subprocess.run(
        [
            str(find_compiler()),
            "/Q",
            f"/DSourceRoot={ROOT}",
            f"/DSourcePackage={package}",
            f"/DAppVersion={VERSION}",
            f"/DInstallerAppId={app_id}",
            f"/DDefaultInstallDir={base / 'fallback'}",
            f"/DDesktopRoot={desktop}",
            f"/DStartMenuRoot={menu}",
            f"/O{base}",
            "/FSetupUnderTest",
            str(ROOT / "tools/installer/inviter.iss"),
        ],
        check=True,
        timeout=120,
    )
    setup = base / "SetupUnderTest.exe"
    env = {**os.environ, "INVITER_HOME": str(profile), "SSLKEYLOGFILE": ""}
    # The installed executable must use its bundled runtime and Tk resources.
    for name in ("PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY", "VIRTUAL_ENV"):
        env.pop(name, None)
    env["PATH"] = os.pathsep.join((str(Path(os.environ["WINDIR"]) / "System32"), os.environ["WINDIR"]))
    common = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART", "/NOCLOSEAPPLICATIONS", "/LANG=russian"]

    def run(command, timeout=45):
        return subprocess.run(
            [str(item) for item in command],
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=timeout,
            capture_output=True,
            text=True,
        )

    # A user-data directory must not be accepted even for a silent installation.
    blocked = run([setup, *common, f"/DIR={profile}", f"/LOG={base / 'invalid-folder.log'}"])
    assert blocked.returncode != 0 and not (profile / "InviterStudio.exe").exists()
    assert "Нельзя устанавливать программу" in (base / "invalid-folder.log").read_text(encoding="utf-8-sig")
    # The real product mutex prevents updates while the app is running.
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, APP_MUTEX)
    assert handle
    try:
        blocked = run([setup, *common, f"/DIR={app}", f"/LOG={base / 'running-app.log'}"])
        assert blocked.returncode != 0 and not (app / "InviterStudio.exe").exists()
    finally:
        kernel.CloseHandle(handle)
    # Also exercise update from an older unpacked build without installer metadata.
    app.mkdir()
    (app / "InviterStudio.exe").write_bytes(b"legacy executable fixture")
    (app / "_internal").mkdir()
    (app / "_internal/base_library.zip").write_bytes(b"legacy standard library fixture")
    (app / "user-note.txt").write_text("Do not remove user-created files", encoding="utf-8")
    installed = run([setup, *common, f"/DIR={app}", "/TASKS=desktopicon,startmenuicon", f"/LOG={base / 'install.log'}"])
    assert installed.returncode == 0, (installed.returncode, installed.stderr)
    assert (app / "InviterStudio.exe").read_bytes() == (package / "InviterStudio.exe").read_bytes()
    standard_library = app / "_internal/base_library.zip"
    assert standard_library.read_bytes() == (package / "_internal/base_library.zip").read_bytes()
    assert (desktop / "Inviter Studio.lnk").is_file()
    assert (menu / "Inviter Studio/Inviter Studio.lnk").is_file()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        assert winreg.QueryValueEx(key, "DisplayVersion")[0] == VERSION
        assert Path(winreg.QueryValueEx(key, "InstallLocation")[0]).resolve() == app.resolve()
    report = base / "exe-smoke.json"
    smoked = run([app / "InviterStudio.exe", "--smoke-test", report])
    result = json.loads(report.read_text(encoding="utf-8"))
    assert smoked.returncode == 0 and not result["errors"] and result["screens"] == 8 and result["frozen"]
    database = profile / "data/inviter.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('installer-test','keep-user-settings')")
    session = profile / "sessions/keep.session"
    session.write_text("Disposable session fixture", encoding="utf-8")
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (database, session)}
    # No /DIR: remembered directory must win over the isolated default folder.
    # A reader may retain the ZIP after app exit. Identical contents must not be
    # overwritten; a different legacy archive above must still be replaced.
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    archive_handle = kernel.CreateFileW(str(standard_library), 0x80000000, 1, None, 3, 0, None)
    assert archive_handle != wintypes.HANDLE(-1).value
    try:
        upgrade_attempts = 1
        upgrade_log = base / "upgrade-1.log"
        upgraded = run([setup, *common, "/TASKS=!desktopicon,!startmenuicon", f"/LOG={upgrade_log}"])
    finally:
        kernel.CloseHandle(archive_handle)
    assert upgraded.returncode == 0, upgraded.returncode
    assert "замена не требуется" in upgrade_log.read_text(encoding="utf-8-sig")
    assert standard_library.read_bytes() == (package / "_internal/base_library.zip").read_bytes()
    assert not (base / "fallback").exists()
    assert not (desktop / "Inviter Studio.lnk").exists()
    assert not (menu / "Inviter Studio/Inviter Studio.lnk").exists()
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in hashes.items())
    uninstaller = app / "unins000.exe"
    assert uninstaller.is_file() and uninstaller.resolve().is_relative_to(base.resolve())
    removed = run([uninstaller, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={base / 'uninstall.log'}"])
    assert removed.returncode == 0, removed.returncode
    assert not (app / "InviterStudio.exe").exists()
    assert (app / "user-note.txt").read_text() == "Do not remove user-created files"
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in hashes.items())
    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        raise AssertionError("Uninstall registration was left behind")
    except FileNotFoundError:
        pass
    (audit / "integration.json").write_text(
        json.dumps(
            {
                "errors": [],
                "custom_unicode_folder": True,
                "desktop_and_start_menu_options": True,
                "remembered_upgrade_folder": True,
                "upgrade_attempts": upgrade_attempts,
                "unchanged_archive_can_be_read_locked": True,
                "changed_archive_replaced": True,
                "running_app_blocked": True,
                "data_folder_rejected": True,
                "legacy_update": True,
                "uninstall_registration": True,
                "profile_and_sessions_preserved": True,
                "user_created_files_preserved": True,
                "installed_app_smoke": result,
                "external_python_removed_from_path": True,
                "test_registry_id": app_id,
                "scenario": str(base.relative_to(ROOT)),
                "live_telegram_calls": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        "Installer passed: custom folder, shortcuts, update, active-app guard, installed app and uninstall; user data preserved."
    )


if __name__ == "__main__":
    main()
