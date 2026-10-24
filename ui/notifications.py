"""Local Windows notifications. Call only on the Tk thread; never include secrets.

Uses Shell_NotifyIconW without a new dependency, process or network connection.
Windows controls visibility (including Do Not Disturb). The application retains
its ordinary result dialogs and journal if the shell cannot show a notification.
"""

import ctypes
import logging
import sys
from ctypes import wintypes


class NotifyIconData(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


def utf16_clip(text, limit):
    """Win32 limits count UTF-16 units; do not split surrogate pairs."""
    return text.replace("\0", "").encode("utf-16-le")[: limit * 2].decode("utf-16-le", errors="ignore")


class WindowsNotifications:
    def __init__(self, root):
        self.root = root
        self.enabled = True
        self._data = None
        self._timer = None
        self._shell = None

    def _prepare(self):
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NotifyIconData)]
        shell.Shell_NotifyIconW.restype = wintypes.BOOL
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        user.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user.LoadIconW.restype = wintypes.HICON
        data = NotifyIconData()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd = user.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT
        data.uID = 1
        data.uFlags = 0x02 | 0x04  # NIF_ICON | NIF_TIP
        data.hIcon = user.LoadIconW(None, 32516)  # shared IDI_INFORMATION; no DestroyIcon needed
        data.szTip = "Inviter Studio"
        self._shell = shell.Shell_NotifyIconW
        if not self._shell(0, ctypes.byref(data)):  # NIM_ADD
            raise OSError("Windows не приняла значок уведомления")
        self._data = data

    def show(self, title, text, *, warning=False):
        if not self.enabled or sys.platform != "win32":
            return False
        try:
            # Remove a previous temporary icon, including after Explorer restarts.
            self.close()
            self._prepare()
            self._data.uFlags = 0x10  # NIF_INFO
            self._data.szInfoTitle = utf16_clip(title, 63)
            self._data.szInfo = utf16_clip(text, 255)
            self._data.dwInfoFlags = (2 if warning else 1) | 0x80  # respect quiet time
            shown = bool(self._shell(1, ctypes.byref(self._data)))  # NIM_MODIFY
            self._timer = self.root.after(30000, self.close)
            return shown
        except (OSError, AttributeError, RuntimeError, ValueError, ctypes.ArgumentError):
            logging.getLogger(__name__).warning("Уведомление Windows недоступно; результат доступен в программе")
            self.close()
            return False

    def close(self):
        if self._timer is not None:
            self.root.after_cancel(self._timer)
            self._timer = None
        if self._data is not None:
            self._shell(2, ctypes.byref(self._data))  # NIM_DELETE
            self._data = None
