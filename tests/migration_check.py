"""Verify a copy of the user's DB without modifying the original or calling APIs."""

import asyncio
import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from core.database import init_db
from core.paths import DATA_DIR, PROJECT_ROOT, SESSIONS_DIR
from core.secrets import reveal


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    original = DATA_DIR / "inviter.db"
    original_hash = fingerprint(original)
    sessions = {str(path): fingerprint(path) for path in SESSIONS_DIR.glob("*.session")}
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / ".audit") as directory:
        target = Path(directory) / "migration.db"
        with closing(sqlite3.connect(f"file:{original.as_posix()}?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(target)) as destination:
                source.backup(destination)
                tables = [
                    row[0]
                    for row in destination.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'"
                    )
                ]
                counts = {name: destination.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables}
                accounts = list(destination.execute("SELECT id, api_hash, proxy_password FROM accounts"))
                configs = list(destination.execute("SELECT id, api_key FROM ai_configs"))
                original_settings = dict(destination.execute("SELECT key, value FROM settings"))
        with patch("core.database.DB_PATH", str(target)):
            asyncio.run(init_db())
            with closing(sqlite3.connect(target)) as check:
                migrated_settings_count = check.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
            asyncio.run(init_db())
        with closing(sqlite3.connect(target)) as destination:
            assert destination.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            for name, count in counts.items():
                actual = destination.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                assert actual == (migrated_settings_count if name == "settings" else count), name
            current_settings = dict(destination.execute("SELECT key, value FROM settings"))
            for key, value in original_settings.items():
                if key != "task_status":
                    assert current_settings[key] == value, key
            assert (
                destination.execute("SELECT COUNT(*) FROM targets WHERE campaign_id=1").fetchone()[0]
                == counts["targets"]
            )
            for key, old_hash, old_password in accounts:
                values = destination.execute(
                    "SELECT api_hash, proxy_password FROM accounts WHERE id=?", (key,)
                ).fetchone()
                assert reveal(values[0]) == reveal(old_hash)
                assert reveal(values[1]) == reveal(old_password)
            for key, old_key in configs:
                value = destination.execute("SELECT api_key FROM ai_configs WHERE id=?", (key,)).fetchone()[0]
                assert reveal(value) == reveal(old_key)
    assert fingerprint(original) == original_hash
    assert all(fingerprint(Path(path)) == digest for path, digest in sessions.items())
    result = {
        "integrity": "ok",
        "row_counts_preserved": True,
        "credentials_round_trip": True,
        "idempotent_migration": True,
        "original_database_unchanged": True,
        "sessions_unchanged": True,
    }
    (PROJECT_ROOT / ".audit" / "migration-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("Migration verified on a copy: integrity, row counts, credentials, repeatability; originals unchanged.")


if __name__ == "__main__":
    main()
