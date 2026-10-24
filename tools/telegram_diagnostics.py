"""Explicit manual diagnostics; no embedded credentials or session deletion."""

import argparse
import asyncio
import getpass
import os

from core.account_manager import AccountManager
from core.validation import validate_account


async def run(mode: str, usernames: list[str]) -> None:
    payload = validate_account(
        {
            "phone": os.environ.get("TG_PHONE") or input("Телефон: "),
            "api_id": os.environ.get("TG_API_ID") or input("API ID: "),
            "api_hash": os.environ.get("TG_API_HASH") or getpass.getpass("API Hash: "),
        }
    )
    manager = AccountManager(payload)
    try:
        if mode == "auth":
            await manager.send_code(payload["phone"], payload["api_id"], payload["api_hash"])
            result = await manager.authorize(payload["phone"], code=input("Код Telegram: ").strip())
            if result["status"] == "password_required":
                result = await manager.authorize(payload["phone"], password=getpass.getpass("Пароль 2FA: "))
            print("Статус авторизации:", result["status"])
        else:
            await manager.connect()
            for username in usernames:
                try:
                    await manager.client.get_entity(username)
                    print(f"{username}: найден")
                except Exception as exc:
                    print(f"{username}: {type(exc).__name__}")
    finally:
        await manager.disconnect()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Ручная диагностика Telegram")
    parser.add_argument("mode", choices=("auth", "resolve"))
    parser.add_argument("usernames", nargs="*")
    args = parser.parse_args(argv)
    if args.mode == "resolve" and not args.usernames:
        parser.error("Укажите username для проверки")
    asyncio.run(run(args.mode, args.usernames))


if __name__ == "__main__":
    main()
