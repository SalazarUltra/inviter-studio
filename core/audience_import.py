"""Read recipient lists (TXT / CSV) that may contain usernames, numeric Telegram ids or both."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from core.validation import id_recipient, normalize_recipient, normalize_username, parse_user_id

_ID_HEADERS = {"user_id", "userid", "id", "telegram id", "telegram_id", "tg_id", "id пользователя"}
_NAME_HEADERS = {"username", "user_name", "никнейм", "юзернейм", "логин", "login", "nick"}


def audience_item(username: str | None, user_id: int | None) -> dict | None:
    """One import row: a real @username (with the id when known) or an id-only recipient."""
    name = ""
    if username:
        try:
            name = normalize_username(str(username))
        except ValueError:
            name = ""
    if name:
        return {"username": name, "user_id": user_id}
    if user_id:
        return {"username": id_recipient(user_id), "user_id": int(user_id)}
    return None


def parse_audience_text(text: str) -> list:
    """TXT lines -> items for ``Database.import_audience``.

    Accepted lines: ``@name``, ``t.me/name``, ``123456789``, ``id:123456789``, and ``@name;123456789`` /
    ``name,123456789`` (username plus id). Empty lines and ``#`` comments are ignored. The whole file is
    validated first, so one bad line aborts the import with its number.
    """
    items: dict[str, dict] = {}
    for line_number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = _line_item(line)
        except ValueError as exc:
            raise ValueError(f"Строка {line_number}: {exc}") from exc
        known = items.get(item["username"])
        if known is None:
            items[item["username"]] = item
        elif not known.get("user_id") and item.get("user_id"):
            known["user_id"] = item["user_id"]
    return list(items.values())


def _line_item(line: str) -> dict:
    for separator in (";", ",", "\t"):
        if separator in line:
            parts = [part.strip() for part in line.split(separator) if part.strip()]
            if len(parts) == 2:
                first, second = parts
                number = parse_user_id(second)
                if number is not None:
                    item = audience_item(first, number)
                    if item is None:
                        raise ValueError("Никнейм: 4–32 символа, латинские буквы, цифры и _")
                    return item
            break
    key = normalize_recipient(line)
    number = parse_user_id(key) if key.startswith("id:") else None
    return {"username": key, "user_id": number}


def parse_audience_csv(text: str) -> list:
    """CSV (a parsing export or a plain list) -> items.

    With a header row the ``user_id`` / ``username`` columns are used (extra columns are ignored); without
    one, the first column of each row is read like a TXT line.
    """
    text = text.lstrip("\ufeff")
    if not text.strip():
        return []
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [row for row in csv.reader(io.StringIO(text), dialect) if any(cell.strip() for cell in row)]
    if not rows:
        return []
    header = [cell.strip().lower() for cell in rows[0]]
    id_col = next((i for i, cell in enumerate(header) if cell in _ID_HEADERS), None)
    name_col = next((i for i, cell in enumerate(header) if cell in _NAME_HEADERS), None)
    if id_col is None and name_col is None:
        return parse_audience_text("\n".join(row[0] for row in rows if row and row[0].strip()))
    items: dict[str, dict] = {}
    for offset, row in enumerate(rows[1:], 2):
        name = row[name_col].strip() if name_col is not None and name_col < len(row) else ""
        number = parse_user_id(row[id_col]) if id_col is not None and id_col < len(row) else None
        if not name and number is None:
            if id_col is not None and id_col < len(row) and row[id_col].strip():
                raise ValueError(f"Строка {offset}: «{row[id_col].strip()}» не похоже на ID пользователя Telegram")
            continue
        item = audience_item(name, number)
        if item is None:
            raise ValueError(f"Строка {offset}: Никнейм: 4–32 символа, латинские буквы, цифры и _")
        known = items.get(item["username"])
        if known is None:
            items[item["username"]] = item
        elif not known.get("user_id") and item.get("user_id"):
            known["user_id"] = item["user_id"]
    return list(items.values())


def load_audience_file(path: str) -> list:
    """Read a UTF-8 .txt or .csv recipient file."""
    text = Path(path).read_text(encoding="utf-8-sig")
    if Path(path).suffix.lower() == ".csv":
        return parse_audience_csv(text)
    return parse_audience_text(text)
