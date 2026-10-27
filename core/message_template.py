"""Personalization of direct-message text.

Supported placeholders:
* ``{имя}`` / ``{name}`` -> the recipient's first name (``FALLBACK_NAME`` when unknown);
* ``{вариант1|вариант2|вариант3}`` -> one option picked at random for every send.

``{ссылка}`` and any other braces without ``|`` are left untouched.
"""

from __future__ import annotations

import random
import re

FALLBACK_NAME = "друг"
SAMPLE_NAME = "Алексей"

_NAME_RE = re.compile(r"\{\s*(?:имя|name)\s*\}", re.IGNORECASE)
_SPIN_RE = re.compile(r"\{([^{}]*\|[^{}]*)\}")
# A recipient name must never be interpreted as a template: it is substituted last.
_NAME_TOKEN = "\x00NAME\x00"


def clean_name(value) -> str:
    """Single-line first name without control characters, or '' when unusable."""
    text = " ".join(str(value or "").replace("\x00", " ").split())
    return text[:64]


def has_placeholders(text: str) -> bool:
    return bool(_NAME_RE.search(text or "") or _SPIN_RE.search(text or ""))


def render_message(text: str, first_name=None, *, rng=None, fallback: str = FALLBACK_NAME) -> str:
    """Return ``text`` with ``{имя}``/``{name}`` filled and every ``{a|b|c}`` resolved."""
    picker = rng or random
    name = clean_name(first_name) or fallback
    result = _NAME_RE.sub(_NAME_TOKEN, text or "")
    result = _SPIN_RE.sub(lambda match: picker.choice(match.group(1).split("|")), result)
    return result.replace(_NAME_TOKEN, name)


def preview_message(text: str, name: str = SAMPLE_NAME, *, rng=None) -> str:
    """Sample rendering shown in the UI with a fake recipient name."""
    return render_message(text, name, rng=rng)
