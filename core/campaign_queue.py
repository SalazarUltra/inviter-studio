"""Campaign queue: «after this campaign finishes, offer to start that one».

Each campaign stores at most one follower (`queue_next_campaign_id`) and a trigger (`queue_next_on`):
`success` — only when the run completed normally, `always` — also after an error. A run that the user
stopped never continues on its own. Nothing starts here: `plan_next` only describes what should be
offered, the UI asks the user and then starts the task.
"""

from __future__ import annotations

from typing import Any

NEXT_KEY = "queue_next_campaign_id"
ON_KEY = "queue_next_on"
ON_SUCCESS = "success"
ON_ALWAYS = "always"
TRIGGERS = (ON_SUCCESS, ON_ALWAYS)
# Run outcomes (summary["outcome"]) that may continue the queue for each trigger.
CONTINUE_OUTCOMES = {ON_SUCCESS: {"completed"}, ON_ALWAYS: {"completed", "failed"}}
MAX_CHAIN = 25
# Global (not per campaign) switch: start the next campaign without asking each time. Stop, an empty next campaign,
# a deleted one and loops still end the queue; only the confirmation dialog is skipped.
AUTO_KEY = "queue_auto_continue"


def parse_auto_continue(value) -> bool:
    return str(value if value is not None else "").strip().lower() in {"1", "true", "yes", "on"}


def needs_confirmation(auto_continue: bool, session_confirmed: bool) -> bool:
    """Ask the user before starting the next campaign unless they already agreed (setting or this session)."""
    return not (auto_continue or session_confirmed)


async def get_auto_continue(db) -> bool:
    return parse_auto_continue(await db.get_setting(AUTO_KEY, "0"))


async def set_auto_continue(db, enabled: bool) -> None:
    await db.set_setting(AUTO_KEY, "1" if enabled else "0")


def normalize_trigger(value) -> str:
    return value if value in TRIGGERS else ON_SUCCESS


def parse_campaign_id(value) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def outcome_continues(trigger, outcome) -> bool:
    return outcome in CONTINUE_OUTCOMES[normalize_trigger(trigger)]


async def get_link(db) -> dict[str, Any]:
    """The follower of `db`'s campaign: {"next_id": int | None, "on": «success»|«always»}."""
    values = await db.get_settings_map([NEXT_KEY, ON_KEY])
    return {"next_id": parse_campaign_id(values.get(NEXT_KEY)), "on": normalize_trigger(values.get(ON_KEY))}


async def _chain_reaches(db, start_id, target_id) -> bool:
    """True when following the followers from `start_id` leads to `target_id` (a loop)."""
    seen, current = set(), start_id
    while current is not None and current not in seen and len(seen) < 1000:
        if current == target_id:
            return True
        seen.add(current)
        current = (await get_link(db.for_campaign(current)))["next_id"]
    return False


async def set_link(db, next_id, trigger=ON_SUCCESS) -> None:
    """Save the follower of `db`'s campaign (None clears it). Rejects itself, missing campaigns and loops."""
    trigger = trigger if trigger in TRIGGERS else None
    if trigger is None:
        raise ValueError("Выберите, когда запускать следующую кампанию")
    next_id = parse_campaign_id(next_id) if next_id not in (None, "") else None
    if next_id is not None:
        if next_id == db.campaign_id:
            raise ValueError("Нельзя запускать эту же кампанию после себя")
        if next_id not in {row["id"] for row in await db.get_campaigns()}:
            raise ValueError("Выбранная кампания удалена или не найдена")
        if await _chain_reaches(db, next_id, db.campaign_id):
            raise ValueError("Так очередь зациклится: эта кампания уже стоит в цепочке после выбранной")
    await db.save_campaign_settings({NEXT_KEY: "" if next_id is None else str(next_id), ON_KEY: trigger})


async def plan_next(db, summary: dict, chain=()) -> dict[str, Any]:
    """Decide what to offer after `db`'s campaign finished with `summary`.

    Returns {"action": "none" | "blocked" | "offer", "next_id", "name", "pending", "message"}. `chain` lists the
    campaign ids already run in this queue so a loop can never restart a campaign.
    """
    plan: dict[str, Any] = {"action": "none", "next_id": None, "name": "", "pending": 0, "message": ""}
    link = await get_link(db)
    if link["next_id"] is None or not outcome_continues(link["on"], (summary or {}).get("outcome", "completed")):
        return plan
    plan["next_id"] = link["next_id"]
    current = {row["id"]: row["name"] for row in await db.get_campaigns()}
    if link["next_id"] not in current:
        return {**plan, "action": "blocked", "message": "Следующая кампания в очереди удалена. Очередь остановлена."}
    plan["name"] = current[link["next_id"]]
    if link["next_id"] in set(chain) or len(set(chain)) >= MAX_CHAIN:
        return {
            **plan,
            "action": "blocked",
            "message": f"Кампания «{plan['name']}» уже запускалась в этой очереди. Остановлено, чтобы не зациклиться.",
        }
    stats = await db.for_campaign(link["next_id"]).get_targets_stats()
    plan["pending"] = stats["pending"]
    if not stats["pending"]:
        return {
            **plan,
            "action": "blocked",
            "message": f"В кампании «{plan['name']}» нет ожидающих получателей. Очередь остановлена.",
        }
    done = {"completed": "завершена", "failed": "завершилась с ошибкой"}.get(
        (summary or {}).get("outcome"), "завершена"
    )
    plan["action"] = "offer"
    plan["message"] = (
        f"Кампания {done}. Следующая в очереди: «{plan['name']}» (ожидает получателей: {stats['pending']})."
    )
    return plan
