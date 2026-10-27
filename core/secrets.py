"""Protect stored credentials with Windows DPAPI (current Windows user)."""

import base64
import ctypes
import os
import sys
from ctypes import wintypes

PREFIX = "dpapi:v1:"
SECRET_FIELDS = {"api_hash", "proxy_password", "api_key"}


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _require_windows() -> None:
    """Fail loudly when DPAPI is unavailable; secrets are never stored in plaintext."""
    if os.name == "nt":
        return
    if getattr(sys, "frozen", False):
        raise RuntimeError(
            "Собранная версия BotInvityer поддерживает только Windows: защита ключей через DPAPI "
            "недоступна на этой системе. Запустите программу в Windows."
        )
    raise RuntimeError("DPAPI доступен только в Windows: ключи нельзя сохранить без шифрования")


def _crypt(data: bytes, decrypt: bool) -> bytes:
    _require_windows()
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _Blob(len(data), buffer)
    target = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(target.data)


def protect(value):
    if not value or str(value).startswith(PREFIX):
        return value
    # Never fall back to plaintext: raises on non-Windows systems.
    _require_windows()
    return PREFIX + base64.b64encode(_crypt(str(value).encode("utf-8"), False)).decode("ascii")


def reveal(value):
    if not value or not str(value).startswith(PREFIX):
        return value
    try:
        data = base64.b64decode(str(value)[len(PREFIX) :], validate=True)
        return _crypt(data, True).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("Не удалось расшифровать ключ: нужен исходный пользователь Windows") from exc


def reveal_row(row):
    if row is None:
        return None
    result = dict(row)
    for field in SECRET_FIELDS & result.keys():
        result[field] = reveal(result[field])
    return result
