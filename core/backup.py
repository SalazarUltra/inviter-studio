"""SQLite online snapshots and validated restore at the next startup."""

import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from core import database
from core.secrets import protect, reveal_row


def validate_backup(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Резервная копия повреждена")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"accounts", "targets", "settings", "logs", "ai_configs"}
        if not required <= tables:
            raise ValueError("Это не резервная копия TG Inviter")
        columns = {
            "accounts": {"id", "phone", "api_id", "api_hash", "session_name"},
            "targets": {"id", "username", "status", "processed_at", "error_message", "added_by_account_id"},
            "settings": {"key", "value"},
            "logs": {"id", "action", "message", "created_at"},
            "ai_configs": {"id", "name", "provider", "api_key", "model"},
        }
        for table, required_columns in columns.items():
            if not required_columns <= {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
                raise ValueError("Резервная копия имеет неподдерживаемую структуру")
        conn.row_factory = sqlite3.Row
        # DPAPI secrets must be readable by this Windows user before replacing data.
        for table in ("accounts", "ai_configs"):
            for row in conn.execute(f"SELECT * FROM {table}"):
                reveal_row(row)


def _protect_snapshot(path):
    # A first backup of a legacy DB must not preserve plaintext API credentials.
    with closing(sqlite3.connect(path)) as conn:
        for table, fields in (("accounts", ("api_hash", "proxy_password")), ("ai_configs", ("api_key",))):
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for field in fields:
                if field not in existing:
                    continue
                for key, value in conn.execute(f"SELECT id, {field} FROM {table}").fetchall():
                    conn.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (protect(value), key))
        conn.commit()


def create_backup(destination=None):
    source = Path(database.DB_PATH).resolve()
    folder = source.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    destination = Path(destination) if destination else folder / f"inviter-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
    destination = destination.resolve()
    if destination == source:
        raise ValueError("Копию нельзя записать поверх рабочей базы")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, suffix=".db")
    os.close(fd)
    try:
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src,
            closing(sqlite3.connect(temporary)) as dst,
        ):
            src.backup(dst)
        validate_backup(temporary)
        _protect_snapshot(temporary)
        validate_backup(temporary)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination


def automatic_backup():
    source = Path(database.DB_PATH)
    if not source.exists():
        return
    folder = source.parent / "backups"
    today = datetime.now().strftime("%Y%m%d")
    if not list(folder.glob(f"auto-{today}-*.db")):
        create_backup(folder / f"auto-{datetime.now():%Y%m%d-%H%M%S-%f}.db")
    for old in sorted(folder.glob("auto-*.db"), reverse=True)[7:]:
        old.unlink()


def stage_restore(path):
    """Copy external selection now; the running DB is untouched until restart."""
    validate_backup(path)
    staged = Path(database.DB_PATH).parent / "restore-pending.db"
    if Path(path).resolve() == Path(database.DB_PATH).resolve():
        raise ValueError("Выберите резервную копию, а не рабочую базу")
    fd, temporary = tempfile.mkstemp(dir=staged.parent, suffix=".db")
    os.close(fd)
    try:
        with (
            closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as src,
            closing(sqlite3.connect(temporary)) as dst,
        ):
            src.backup(dst)
        validate_backup(temporary)
        _protect_snapshot(temporary)
        validate_backup(temporary)
        os.replace(temporary, staged)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply_pending_restore():
    source = Path(database.DB_PATH)
    staged = source.parent / "restore-pending.db"
    if not staged.exists():
        return
    validate_backup(staged)
    if source.exists():
        create_backup()  # Always keep a recovery point before restoring.
        # Checkpoint the old WAL before replacing the main database.
        with closing(sqlite3.connect(source)) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for suffix in ("-wal", "-shm"):
        Path(str(source) + suffix).unlink(missing_ok=True)
    os.replace(staged, source)
