"""Prepare the portable distribution handoff for end users."""

import hashlib
import os
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

from core.version import VERSION

HANDOFF_NAME = "ДЛЯ_УСТАНОВКИ"


def _portable_launcher() -> str:
    return (
        "@echo off\n"
        "setlocal\n"
        'cd /d "%~dp0"\n'
        "chcp 65001 >nul\n"
        'set "INVITER_HOME=%~dp0profile"\n'
        'if not exist "%~dp0InviterStudio.exe" (\n'
        "    echo InviterStudio.exe не найден. Распакуйте весь ZIP целиком в одну папку.\n"
        "    pause\n"
        "    exit /b 1\n"
        ")\n"
        'if not exist "%~dp0_internal" (\n'
        "    echo Папка _internal не найдена. Распакуйте весь ZIP целиком в одну папку.\n"
        "    pause\n"
        "    exit /b 1\n"
        ")\n"
        "rem Снимаем пометку «скачано из интернета», которую Windows расставляет при\n"
        "rem распаковке ZIP: после этого SmartScreen не останавливает программу.\n"
        "powershell -NoProfile -ExecutionPolicy Bypass -Command \"Get-ChildItem -LiteralPath '%~dp0' -Recurse | Unblock-File\" >nul 2>&1\n"
        'start "" "%~dp0InviterStudio.exe"\n'
        "timeout /t 4 /nobreak >nul\n"
        'tasklist /fi "IMAGENAME eq InviterStudio.exe" | find /i "InviterStudio.exe" >nul\n'
        "if errorlevel 1 (\n"
        "    echo.\n"
        "    echo Программа не запустилась. Самая частая причина - блокировка Windows или антивируса:\n"
        "    echo 1. Щёлкните правой кнопкой по InviterStudio.exe, выберите Свойства,\n"
        "    echo    внизу вкладки Общие отметьте Разблокировать и нажмите ОК, затем снова start.bat.\n"
        "    echo 2. Если антивирус удалил или блокирует файл - добавьте папку в исключения.\n"
        "    echo 3. Убедитесь, что распакованы все файлы архива, включая папку _internal.\n"
        "    echo.\n"
        "    pause\n"
        ")\n"
        "exit /b 0\n"
    )


def _portable_updater() -> str:
    return (
        "@echo off\n"
        "setlocal\n"
        'cd /d "%~dp0"\n'
        "chcp 65001 >nul\n"
        "echo Проверяю последнюю portable-версию Inviter Studio...\n"
        'set "TMP=%TEMP%\\InviterStudio-update-%RANDOM%"\n'
        'mkdir "%TMP%" >nul 2>&1\n'
        'curl.exe -fL --retry 2 "https://github.com/SalazarUltra/inviter-studio/releases/latest/download/InviterStudioPortable.zip" -o "%TMP%\\InviterStudioPortable.zip"\n'
        "if errorlevel 1 goto download_error\n"
        'curl.exe -fL --retry 2 "https://github.com/SalazarUltra/inviter-studio/releases/latest/download/SHA256.txt" -o "%TMP%\\SHA256.txt"\n'
        "if errorlevel 1 goto download_error\n"
        'for /f "tokens=1" %%H in (\'findstr /i "InviterStudioPortable.zip" "%TMP%\\SHA256.txt"\') do if not defined EXPECTED set "EXPECTED=%%H"\n'
        'for /f "skip=1 tokens=1" %%H in (\'certutil -hashfile "%TMP%\\InviterStudioPortable.zip" SHA256 ^| findstr /r /v "CertUtil"\') do if not defined ACTUAL set "ACTUAL=%%H"\n'
        'if /i not "%EXPECTED%"=="%ACTUAL%" goto checksum_error\n'
        'mkdir "%TMP%\\stage" >nul 2>&1\n'
        'tar.exe -xf "%TMP%\\InviterStudioPortable.zip" -C "%TMP%\\stage"\n'
        "if errorlevel 1 goto archive_error\n"
        'if not exist "%TMP%\\stage\\InviterStudio.exe" goto archive_error\n'
        'if not exist "%TMP%\\stage\\_internal" goto archive_error\n'
        'tasklist /fi "IMAGENAME eq InviterStudio.exe" | find /i "InviterStudio.exe" >nul\n'
        "if not errorlevel 1 goto running_error\n"
        'robocopy "%TMP%\\stage" "%~dp0." /E /PURGE /R:1 /W:1 /XD "profile"\n'
        "if errorlevel 8 goto copy_error\n"
        'rmdir /s /q "%TMP%" >nul 2>&1\n'
        "echo Обновление установлено. Запускаю программу.\n"
        'start "" "%~dp0start.bat"\n'
        "exit /b 0\n"
        ":download_error\n"
        "echo Не удалось скачать официальный релиз. Проверьте интернет.\n"
        "goto failed\n"
        ":checksum_error\n"
        "echo Проверка SHA-256 не пройдена. Файлы не заменены.\n"
        "goto failed\n"
        ":archive_error\n"
        "echo Архив повреждён или имеет неверную структуру.\n"
        "goto failed\n"
        ":running_error\n"
        "echo Сначала закройте Inviter Studio. Профиль не изменён.\n"
        "goto failed\n"
        ":copy_error\n"
        "echo Не удалось заменить файлы приложения. Профиль сохранён.\n"
        "goto failed\n"
        ":failed\n"
        'rmdir /s /q "%TMP%" >nul 2>&1\n'
        "pause\n"
        "exit /b 1\n"
    )


def prepare_portable_handoff(root: Path, package: Path) -> Path:
    """Create a self-contained ZIP with the onedir runtime and a launcher."""

    if not package.is_dir() or not (package / "InviterStudio.exe").is_file():
        raise FileNotFoundError("Сначала соберите полную папку InviterStudio")
    folder = root / HANDOFF_NAME
    folder.mkdir(parents=True, exist_ok=True)
    build_root = root / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    portable_name = "InviterStudioPortable"
    target = folder / f"{portable_name}.zip"
    temporary = folder / (".portable-" + uuid.uuid4().hex + ".tmp")
    with tempfile.TemporaryDirectory(prefix="portable-", dir=build_root) as temporary_root:
        staging = Path(temporary_root) / portable_name
        shutil.copytree(package, staging)
        # Never ship a developer/user profile inside the public portable ZIP.
        for forbidden in ("profile", "data", "sessions", "backups"):
            leftover = staging / forbidden
            if leftover.exists():
                raise RuntimeError(f"В portable-сборке нельзя оставлять «{forbidden}»")
        (staging / "start.bat").write_text(_portable_launcher(), encoding="utf-8")
        (staging / "update.bat").write_text(_portable_updater(), encoding="utf-8")
        (staging / "ПРОЧТИ_ПЕРЕД_ЗАПУСКОМ.txt").write_text(
            "INVITER STUDIO — PORTABLE\n\n"
            "1. Распакуйте весь ZIP в обычную папку, например C:\\Apps\\InviterStudio.\n"
            "2. Запустите start.bat — он сам снимает блокировку «скачано из интернета»\n"
            "   и запускает программу. Отдельно InviterStudio.exe запускать не нужно.\n"
            "3. Если программа всё же не запустилась: щёлкните правой кнопкой по\n"
            "   InviterStudio.exe, выберите Свойства, внизу вкладки «Общие» отметьте\n"
            "   «Разблокировать», нажмите ОК и снова start.bat.\n"
            "4. После первого запуска появится папка profile — это ВАШИ личные данные\n"
            "   (база, аккаунты, Telegram-сессии). Не удаляйте её и никому не отправляйте.\n"
            "   В ZIP из GitHub этой папки нет: у каждого пользователя свой пустой профиль.\n"
            "5. Python и библиотеки уже встроены. Интернет нужен для Telegram и AI-функций.\n\n"
            "Для обновления закройте программу и запустите update.bat. Архив берётся\n"
            "только из официального GitHub-релиза и проверяется по SHA-256.\n"
            "update.bat не трогает папку profile — аккаунты и история сохраняются.\n\n"
            "Portable-версия не обходит Smart App Control. Если Windows блокирует\n"
            "неподписанный InviterStudio.exe, нужен доверенный сертификат издателя.\n"
            "Не отключайте защиту Windows ради запуска неизвестного файла.\n",
            encoding="utf-8-sig",
        )
        # Level 9 = smallest portable ZIP. Deflate stays the method: Explorer, tar.exe (update.bat) and the
        # release check all read it natively, unlike LZMA/Zstd ZIPs. See "Portable size" in tools/build_windows.py.
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    if any(part.lower() in {"profile", "data", "sessions", "backups"} for part in path.parts):
                        raise RuntimeError(f"Попытка упаковать пользовательские данные: {path}")
                    if ".session" in path.name.lower() or path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                        raise RuntimeError(f"Попытка упаковать секретный файл: {path}")
                    archive.write(path, path.relative_to(staging).as_posix())
    os.replace(temporary, target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (folder / "SHA256.txt").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    (folder / "ПЕРЕДАЙТЕ_ПОЛЬЗОВАТЕЛЮ.txt").write_text(
        f"INVITER STUDIO {VERSION} — ПЕРЕДАЧА ПОЛЬЗОВАТЕЛЮ\n\n"
        "Публикуйте только InviterStudioPortable.zip и SHA256.txt из этой папки.\n"
        "Не публикуйте и не пересылайте: profile, data, sessions, backups, *.session,\n"
        "inviter.db, исходники с вашим профилем, venv и содержимое репозитория с\n"
        "локальными данными — в них доступы к вашим Telegram-аккаунтам.\n\n"
        "Получатель: распакуйте ZIP целиком в обычную папку и запустите start.bat.\n"
        "Если Windows блокирует запуск — Свойства InviterStudio.exe → «Разблокировать».\n"
        "Обновление: закрыть программу и запустить update.bat рядом с start.bat.\n",
        encoding="utf-8-sig",
    )
    return target
