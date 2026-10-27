"""How an account is shown in lists and menus (phone + Telegram name)."""

from __future__ import annotations

from typing import Any


def profile_from_user(user: Any) -> dict[str, str]:
    """Extract display fields from a Telethon ``User`` / ``get_me()`` result."""
    return {
        "tg_first_name": str(getattr(user, "first_name", None) or "").strip(),
        "tg_last_name": str(getattr(user, "last_name", None) or "").strip(),
        "tg_username": str(getattr(user, "username", None) or "").strip().lstrip("@"),
    }


def account_person_name(account: dict[str, Any] | None) -> str:
    """First + last name from the last successful Telegram check; empty when unknown."""
    if not account:
        return ""
    first = str(account.get("tg_first_name") or "").strip()
    last = str(account.get("tg_last_name") or "").strip()
    return " ".join(part for part in (first, last) if part)


def account_headline(account: dict[str, Any] | None) -> str:
    """Card title: ``Name · @user · +phone`` (missing parts are omitted)."""
    if not account:
        return ""
    phone = str(account.get("phone") or "").strip()
    name = account_person_name(account)
    username = str(account.get("tg_username") or "").strip().lstrip("@")
    parts = []
    if name:
        parts.append(name)
    if username:
        parts.append("@" + username)
    if phone:
        parts.append(phone)
    return " · ".join(parts) if parts else phone


def account_menu_label(account: dict[str, Any] | None, *, used: int | None = None, limit: int | None = None) -> str:
    """Shorter line for checkboxes: ``Name (+phone)`` or phone alone; optional daily counters."""
    if not account:
        return ""
    phone = str(account.get("phone") or "").strip()
    name = account_person_name(account)
    base = f"{name} ({phone})" if name and phone else (name or phone)
    if used is None or limit is None:
        return base
    return f"{base} ({used}/{limit})"


def account_owner_tag(account: dict[str, Any] | None) -> str:
    """Compact owner mark for group lists: ``Name (@user)``, ``@user``, or phone."""
    if not account:
        return "?"
    name = account_person_name(account)
    username = str(account.get("tg_username") or "").strip().lstrip("@")
    phone = str(account.get("phone") or "").strip()
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    if username:
        return "@" + username
    return phone or "?"
