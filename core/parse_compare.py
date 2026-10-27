"""«New since the previous run»: keep only people whose Telegram id was not in an earlier parsing run."""

from __future__ import annotations


def user_id_set(rows) -> set[int]:
    """Distinct Telegram ids of parsed rows (one person may appear once per source group)."""
    return {int(row["user_id"]) for row in rows if row.get("user_id")}


def only_new(rows, previous_ids) -> list:
    """Rows whose user_id is absent from `previous_ids` — the difference between the two runs.

    Applied to raw rows before they are merged into people, so every downstream view (table, export,
    «В аудиторию…») sees the same segment. Rows without an id are kept: nothing proves they were seen.
    """
    known = set(previous_ids)
    return [row for row in rows if not row.get("user_id") or int(row["user_id"]) not in known]


def describe_difference(current_ids, previous_ids) -> str:
    """Russian summary line for the UI."""
    current, previous = set(current_ids), set(previous_ids)
    fresh = len(current - previous)
    return f"Новых: {fresh} · было в прошлом запуске: {len(current & previous)} · ушло: {len(previous - current)}"
