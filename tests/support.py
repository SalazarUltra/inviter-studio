import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.database import Database, init_db


def account_payload(index=1):
    return {
        "phone": f"+38050000{index:04d}",
        "api_id": 12345,
        "api_hash": "a" * 32,
        "daily_limit": 40,
        "account_age_days": 45,
    }


class DatabaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path_patch = patch("core.database.DB_PATH", str(self.root / "inviter.db"))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.addCleanup(self.temp.cleanup)
        self.sessions_patch = patch("core.paths.SESSIONS_DIR", self.root / "sessions")
        self.sessions_patch.start()
        self.addCleanup(self.sessions_patch.stop)
        await init_db()
        self.db = Database()

    async def add_account(self, index=1):
        key = await self.db.add_account(account_payload(index))
        return await self.db.get_account(key)
