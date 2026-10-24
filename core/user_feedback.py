"""Short Russian explanations for errors displayed outside developer logs."""

import asyncio

from telethon.errors import (
    AuthKeyUnregisteredError,
    ChatAdminRequiredError,
    FloodWaitError,
    PeerFloodError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    RPCError,
    SessionPasswordNeededError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserPrivacyRestrictedError,
)


def explain_error(exc: BaseException) -> str:
    if isinstance(exc, FloodWaitError):
        return f"Telegram просит подождать {max(1, int(exc.seconds))} сек. Повторите действие после паузы."
    if isinstance(exc, PeerFloodError):
        return (
            "Telegram отклонил действие с этого аккаунта. Аккаунт сохранён в программе. "
            "Проверьте его состояние в Telegram перед повторной попыткой."
        )
    if isinstance(exc, AuthKeyUnregisteredError):
        return "Telegram отозвал сохранённый вход. Подключите аккаунт заново."
    if isinstance(exc, (UsernameNotOccupiedError, UsernameInvalidError)):
        return "Никнейм не найден. Проверьте написание."
    if isinstance(exc, UserPrivacyRestrictedError):
        return "Настройки получателя не разрешают это действие."
    if isinstance(exc, ChatAdminRequiredError):
        return "Для этого действия нужны права администратора в чате."
    if isinstance(exc, PhoneCodeInvalidError):
        return "Неверный код входа. Проверьте код в Telegram."
    if isinstance(exc, PhoneCodeExpiredError):
        return "Срок действия кода истёк. Запросите новый код."
    if isinstance(exc, SessionPasswordNeededError):
        return "Введите пароль двухэтапной защиты Telegram."
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        return "Не удалось дождаться ответа. Проверьте интернет и подключение."
    if isinstance(exc, FileNotFoundError):
        return "Файл не найден. Проверьте выбранный путь."
    if isinstance(exc, PermissionError):
        return "Нет доступа к файлу или папке. Проверьте разрешения Windows."
    if isinstance(exc, OSError):
        return "Ошибка соединения или работы с файлом. Проверьте интернет и доступ к файлу."
    if isinstance(exc, RPCError):
        return "Telegram отклонил запрос. Проверьте доступ аккаунта и настройки получателя."
    if isinstance(exc, (ValueError, RuntimeError)):
        message = str(exc).strip()
        if any("А" <= char <= "Я" or "а" <= char <= "я" or char in "Ёё" for char in message):
            return message.replace("PeerFlood", "отказ Telegram").replace("FloodWait", "пауза Telegram")
        return "Проверьте введённые данные и состояние аккаунта."
    return "Не удалось выполнить действие. Проверьте подключение и настройки."
