"""Prepare the minimal, self-contained installer handoff for an end user."""

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
        'set "INVITER_HOME=%~dp0profile"\n'
        'if not exist "%~dp0InviterStudio.exe" (\n'
        "    echo InviterStudio.exe не найден. Распакуйте весь ZIP целиком.\n"
        "    pause\n"
        "    exit /b 1\n"
        ")\n"
        'start "" "%~dp0InviterStudio.exe"\n'
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
        'if not exist "%TMP%\\stage\\InviterStudioPortable\\InviterStudio.exe" goto archive_error\n'
        'tasklist /fi "IMAGENAME eq InviterStudio.exe" | find /i "InviterStudio.exe" >nul\n'
        "if not errorlevel 1 goto running_error\n"
        'robocopy "%TMP%\\stage\\InviterStudioPortable" "%~dp0." /E /PURGE /R:1 /W:1 /XD "profile"\n'
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
        (staging / "start.bat").write_text(_portable_launcher(), encoding="utf-8")
        (staging / "update.bat").write_text(_portable_updater(), encoding="utf-8")
        (staging / "ПРОЧТИ_ПЕРЕД_ЗАПУСКОМ.txt").write_text(
            "INVITER STUDIO — PORTABLE\n\n"
            "1. Распакуйте весь ZIP в обычную папку, например C:\\Apps\\InviterStudio.\n"
            "2. Не запускайте InviterStudio.exe отдельно: используйте start.bat.\n"
            "3. Папку profile рядом с приложением удалять не нужно — в ней хранятся\n"
            "   локальная база, аккаунты и Telegram-сессии portable-версии.\n"
            "4. Python и библиотеки уже встроены. Интернет нужен для Telegram и AI-функций.\n\n"
            "Для обновления закройте программу и запустите update.bat. Архив берётся\n"
            "только из официального GitHub-релиза и проверяется по SHA-256.\n\n"
            "Portable-версия не обходит Smart App Control. Если Windows блокирует\n"
            "неподписанный InviterStudio.exe, нужен доверенный сертификат издателя.\n"
            "Не отключайте защиту Windows ради запуска неизвестного файла.\n",
            encoding="utf-8-sig",
        )
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staging).as_posix())
    os.replace(temporary, target)
    return target


def prepare_handoff(root: Path, installer: Path, portable: Path | None = None) -> Path:
    if not installer.is_file():
        raise FileNotFoundError("Сначала соберите InviterStudioSetup.exe")
    folder = root / HANDOFF_NAME
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / (".setup-" + uuid.uuid4().hex + ".tmp")
    target = folder / "InviterStudioSetup.exe"
    portable_digest = None
    if portable is not None:
        if not portable.is_file():
            raise FileNotFoundError("Сначала создайте InviterStudioPortable.zip")
        portable_digest = hashlib.sha256(portable.read_bytes()).hexdigest()
    try:
        shutil.copy2(installer, temporary)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        if digest != hashlib.sha256(installer.read_bytes()).hexdigest():
            raise RuntimeError("Не совпала контрольная сумма копии установщика")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    (folder / "СНАЧАЛА_ПРОЧТИ.txt").write_text(
        f"INVITER STUDIO {VERSION} — УСТАНОВКА\n\n"
        "РЕКОМЕНДУЕМЫЙ ВАРИАНТ: скачайте InviterStudioPortable.zip, распакуйте\n"
        "всю папку и запустите start.bat. Установка не требуется.\n"
        "АЛЬТЕРНАТИВА: InviterStudioSetup.exe устанавливает ярлыки и профиль\n"
        "в %LOCALAPPDATA%\\InviterStudio. Инструкция и SHA256.txt приложены для проверки.\n"
        "Архив проекта, исходники, venv, папки data и sessions передавать не нужно.\n\n"
        "1. Нужен компьютер с Windows 10 или 11, 64 бита.\n"
        "2. Для установочной версии закройте Inviter Studio и откройте InviterStudioSetup.exe.\n"
        "3. Выберите папку установки и нужные ярлыки, завершите мастер.\n"
        "4. Запустите программу. Python и библиотеки включены — отдельно устанавливать их не нужно.\n"
        "5. Для portable-версии профиль хранится в папке profile рядом с start.bat.\n"
        "6. Для работы подключите интернет. В разделе «Аккаунты» добавьте СВОЙ номер Telegram,\n"
        "   API ID и API Hash, подтвердите вход кодом и при необходимости паролем 2FA.\n"
        "   Кнопки получения данных и пошаговая инструкция находятся прямо в форме.\n"
        "7. AI-ключ не требуется для отправки вашего готового текста. Для AI-функций\n"
        "   подключите своего провайдера и проверьте его API-баланс и тариф. Донат добровольный.\n\n"
        "Установка не загружает Python или библиотеки из интернета. Интернет нужен для Telegram\n"
        "и дополнительных AI-функций. Антивирус и правила организации могут ограничивать запуск.\n"
        "Если Windows показывает неизвестного издателя: этот выпуск не имеет цифровой подписи.\n"
        "Проверяйте источник файла; отключать защиту Windows или антивирус не нужно.\n\n"
        "Данные установочной версии: %LOCALAPPDATA%\\InviterStudio.\n"
        "Обновление и удаление программы сохраняют этот профиль. Для полной копии при закрытой\n"
        "программе сохраняйте data и sessions; ключи Windows привязаны к пользователю этого ПК.\n"
        "Если ранее запускали программу из папки проекта через start-exe.bat, у неё другой профиль.\n"
        "При первой установке может понадобиться заново подключить аккаунт.\n\n"
        "В portable-архиве не удаляйте _internal и запускайте приложение через start.bat.\n"
        "Архив проекта, исходники, venv, папки data и sessions передавать не нужно.\n",
        encoding="utf-8-sig",
    )
    checksums = [f"{digest}  InviterStudioSetup.exe"]
    if portable_digest:
        checksums.append(f"{portable_digest}  InviterStudioPortable.zip")
    (folder / "SHA256.txt").write_text("\n".join(checksums) + "\n", encoding="ascii")
    return folder
