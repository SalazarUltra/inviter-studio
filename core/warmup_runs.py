"""In-memory registry of concurrent warm-up runs (one engine per account)."""

from __future__ import annotations

from typing import Any


class WarmupRunRegistry:
    """Tracks active WarmupEngine futures keyed by account id."""

    def __init__(self) -> None:
        self._runs: dict[int, dict[str, Any]] = {}

    def is_warming(self, account_id: int | None) -> bool:
        if account_id is None:
            return False
        run = self._runs.get(int(account_id))
        if not run:
            return False
        future = run.get("future")
        return bool(future is not None and not future.done())

    def any_warming(self) -> bool:
        return any(self.is_warming(account_id) for account_id in list(self._runs))

    def active_ids(self) -> list[int]:
        return [account_id for account_id in list(self._runs) if self.is_warming(account_id)]

    def get(self, account_id: int | None) -> dict[str, Any] | None:
        if account_id is None:
            return None
        run = self._runs.get(int(account_id))
        if not run or not self.is_warming(int(account_id)):
            return None
        return run

    def put(
        self,
        account_id: int,
        *,
        engine: Any,
        future: Any,
        phone: str = "",
        label: str = "",
    ) -> None:
        self._runs[int(account_id)] = {
            "engine": engine,
            "future": future,
            "phone": phone,
            "label": label or phone or str(account_id),
        }

    def discard(self, account_id: int | None) -> None:
        if account_id is None:
            return
        self._runs.pop(int(account_id), None)

    def prune(self) -> None:
        for account_id in list(self._runs):
            if not self.is_warming(account_id):
                self._runs.pop(account_id, None)

    def engines(self) -> list[Any]:
        self.prune()
        return [run["engine"] for run in self._runs.values() if run.get("engine") is not None]

    def futures(self) -> list[Any]:
        self.prune()
        return [run["future"] for run in self._runs.values() if run.get("future") is not None]

    def items(self) -> list[tuple[int, dict[str, Any]]]:
        self.prune()
        return [(account_id, run) for account_id, run in self._runs.items() if self.is_warming(account_id)]
