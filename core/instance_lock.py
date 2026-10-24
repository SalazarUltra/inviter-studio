"""Prevent two desktop instances from sharing the same SQLite/session files."""

import os

from core.paths import DATA_DIR
from core.version import APP_MUTEX


class InstanceLock:
    def __enter__(self):
        self._mutex = None
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.file = (DATA_DIR / "app.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("TG Inviter уже запущен. Закройте другое окно приложения") from exc
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            kernel.CreateMutexW.restype = wintypes.HANDLE
            self._mutex = kernel.CreateMutexW(None, False, APP_MUTEX)
            if not self._mutex:
                self.file.close()
                raise ctypes.WinError(ctypes.get_last_error())
        return self

    def __exit__(self, *_):
        self.file.close()
        if self._mutex:
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            kernel.CloseHandle(self._mutex)
            self._mutex = None
