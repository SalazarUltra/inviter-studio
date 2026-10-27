"""First-run wizard rules (pure): who sees it and which steps it has."""

from __future__ import annotations

WIZARD_DONE_KEY = "first_run_wizard_done"
API_SITE_URL = "https://my.telegram.org/apps"
# (step id, tab to open or None). Texts live in ui.i18n as wizard.<id>.title/text/button.
WIZARD_STEPS = (
    ("s1", None),
    ("s2", "accounts"),
    ("s3", "accounts"),
    ("s4", "targets"),
    ("s5", "task"),
)


def should_show_wizard(account_count: int, done_flag: str | None) -> bool:
    """Only a brand-new install: no accounts and the wizard was never finished or skipped."""
    return account_count <= 0 and done_flag != "1"
