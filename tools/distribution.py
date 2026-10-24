"""Prepare the minimal, self-contained installer handoff for an end user."""

import hashlib
import os
import shutil
import uuid
from pathlib import Path

from core.version import VERSION

HANDOFF_NAME = "ДЛЯ_УСТАНОВКИ"


def prepare_handoff(root: Path, installer: Path) -> Path:
    if not installer.is_file():
        raise FileNotFoundError("Сначала соберите InviterStudioSetup.exe")
    folder = root / HANDOFF_NAME
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / (".setup-" + uuid.uuid4().hex + ".tmp")
    target = folder / "InviterStudioSetup.exe"
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
        "ДЛЯ УСТАНОВКИ ДОСТАТОЧНО ОДНОГО ФАЙЛА: InviterStudioSetup.exe.\n"
        "Можно передать всю эту папку: инструкция и SHA256.txt приложены для удобства.\n"
        "Архив проекта, исходники, venv, папки data и sessions передавать не нужно.\n\n"
        "1. Нужен компьютер с Windows 10 или 11, 64 бита.\n"
        "2. Закройте Inviter Studio, если она запущена. Откройте InviterStudioSetup.exe.\n"
        "3. Выберите папку установки и нужные ярлыки, завершите мастер.\n"
        "4. Запустите программу. Python и библиотеки включены — отдельно устанавливать их не нужно.\n"
        "5. Для работы подключите интернет. В разделе «Аккаунты» добавьте СВОЙ номер Telegram,\n"
        "   API ID и API Hash, подтвердите вход кодом и при необходимости паролем 2FA.\n"
        "   Кнопки получения данных и пошаговая инструкция находятся прямо в форме.\n"
        "6. AI-ключ не требуется для отправки вашего готового текста. Для AI-функций\n"
        "   подключите своего провайдера и проверьте его API-баланс и тариф. Донат добровольный.\n\n"
        "Установка не загружает Python или библиотеки из интернета. Интернет нужен для Telegram\n"
        "и дополнительных AI-функций. Антивирус и правила организации могут ограничивать запуск.\n"
        "Если Windows показывает неизвестного издателя: этот выпуск не имеет цифровой подписи.\n"
        "Проверяйте источник файла; отключать защиту Windows или антивирус не нужно.\n\n"
        "Данные установленной программы: %LOCALAPPDATA%\\InviterStudio.\n"
        "Обновление и удаление программы сохраняют этот профиль. Для полной копии при закрытой\n"
        "программе сохраняйте data и sessions; ключи Windows привязаны к пользователю этого ПК.\n"
        "Если ранее запускали программу из папки проекта через start-exe.bat, у неё другой профиль.\n"
        "При первой установке может понадобиться заново подключить аккаунт.\n\n"
        "InviterStudio.exe внутри установленной папки — файл запуска; его нельзя передавать\n"
        "отдельно от runtime-папки _internal вместо установщика. Передавайте InviterStudioSetup.exe.\n",
        encoding="utf-8-sig",
    )
    (folder / "SHA256.txt").write_text(f"{digest}  InviterStudioSetup.exe\n", encoding="ascii")
    return folder
