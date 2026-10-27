"""Inviter Studio design system: graphite surfaces and electric lime."""

import logging
from collections.abc import Callable
from pathlib import Path

import customtkinter as ctk

THEME_SETTING_KEY = "ui_theme"
THEMES = ("dark", "light")
DEFAULT_THEME = "dark"

# Every color is a CustomTkinter (light, dark) pair: CTk widgets repaint themselves when the appearance mode changes.
# Plain Tk/ttk widgets, PIL images and Text tags need one string: use `resolve()` and `on_theme_change()` for them.
_PALETTE = {
    #                 light      dark
    "bg": ("#F4F5F7", "#101113"),
    "surface": ("#FFFFFF", "#191B1F"),
    "elevated": ("#ECEEF1", "#23262B"),
    "border": ("#D5D8DE", "#2D3036"),
    "accent": ("#526F00", "#D4FF00"),
    "accent_hover": ("#445C00", "#BEDF12"),
    "accent_soft": ("#E3EEC2", "#2B3417"),
    "success": ("#1E8E4E", "#6FCF97"),
    "warning": ("#9A6B00", "#E8C76B"),
    "error": ("#C62F4B", "#FF7D92"),
    "danger_bg": ("#FBE4E8", "#352129"),
    "text": ("#16181C", "#F5F6F7"),
    "muted": ("#5B616C", "#9A9EA8"),
    "subtle": ("#8A909B", "#676D78"),
    "input": ("#FFFFFF", "#131519"),
    "sidebar": ("#E9EBEF", "#141619"),
    "hover": ("#DDE0E6", "#30343B"),
    "hover_strong": ("#D2D6DD", "#333840"),
    "danger_hover": ("#F6D0D7", "#492B36"),
    "danger_border": ("#EDB7C2", "#50303D"),
    "hero_bg": ("#F0F4E3", "#191F19"),
    "hero_border": ("#D3DDB0", "#343C26"),
    "hero_text": ("#5E6652", "#BAC1B1"),
}
COLORS: dict[str, tuple[str, str]] = dict(_PALETTE)
_theme = DEFAULT_THEME
_theme_listeners: list[Callable[[str], None]] = []


def normalize_theme(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in THEMES else DEFAULT_THEME


def current_theme() -> str:
    return _theme


def resolve(color):
    """The single color string a pair has in the active theme (plain strings pass through)."""
    if isinstance(color, (tuple, list)) and len(color) == 2:
        return color[0] if _theme == "light" else color[1]
    return color


def same_color(first, second) -> bool:
    """Compare colors that may be a tuple in one place and a list (loaded from the JSON theme) in another."""
    as_key = lambda value: tuple(value) if isinstance(value, (tuple, list)) else value  # noqa: E731
    return as_key(first) == as_key(second)


def on_theme_change(listener: Callable[[str], None]) -> Callable[[], None]:
    """Register a callback fired after `set_theme`; returns an unsubscribe function."""
    _theme_listeners.append(listener)

    def unsubscribe() -> None:
        if listener in _theme_listeners:
            _theme_listeners.remove(listener)

    return unsubscribe


def set_theme(value: object) -> str:
    """Switch light/dark without a restart (independent of the density preset). Returns the active theme."""
    global _theme
    _theme = normalize_theme(value)
    ctk.set_appearance_mode(_theme)
    for listener in list(_theme_listeners):
        try:
            listener(_theme)
        except Exception:
            logging.getLogger(__name__).exception("Не удалось обновить цвета после смены темы")
    return _theme


def bind_text_tags(text_widget, tags: dict) -> None:
    """Colour the tags of a Tk Text widget now and again whenever the theme changes."""

    def apply(_theme_name=None) -> None:
        for tag, color in tags.items():
            text_widget.tag_config(tag, foreground=resolve(color))

    apply()

    def refresh(theme_name) -> None:
        try:
            if not text_widget.winfo_exists():
                unsubscribe()
                return
            apply(theme_name)
        except Exception:  # the widget was destroyed (TclError): nothing to repaint
            unsubscribe()

    unsubscribe = on_theme_change(refresh)


ctk.set_appearance_mode(DEFAULT_THEME)
ctk.set_default_color_theme(str(Path(__file__).parent / "assets" / "studio-theme.json"))
FONTS: dict[str, ctk.CTkFont] = {}
UI = {
    "space_1": 4,
    "space_2": 8,
    "space_3": 12,
    "space_4": 16,
    "space_5": 24,
    "input_h": 40,
    "btn_h": 40,
    "btn_h_lg": 44,
    "panel_border": 1,
    "radius": 14,
    "control_radius": 10,
}
DENSITY_PRESETS = {
    "normal": {"row_pad_y": 5, "card_pad_y": 12, "list_item_h": 34},
    "compact": {"row_pad_y": 3, "card_pad_y": 8, "list_item_h": 30},
}
APP_PROFILES = {
    "Кастом": None,
    "Оператор": {
        "ui_density": "normal",
        "ai_memory_enabled": "1",
        "ai_style_tone": "friendly",
        "ai_hourly_limit": "6",
        "ai_chat_cooldown_min": "10",
    },
    "Аналитик": {
        "ui_density": "normal",
        "ai_memory_enabled": "1",
        "ai_style_tone": "expert",
        "ai_hourly_limit": "4",
        "ai_chat_cooldown_min": "20",
    },
    "Compact Ops": {
        "ui_density": "compact",
        "ai_memory_enabled": "1",
        "ai_style_tone": "neutral",
        "ai_hourly_limit": "5",
        "ai_chat_cooldown_min": "12",
    },
}


def init_fonts() -> None:
    if FONTS:
        return
    for key, size, weight in (
        ("title", 26, "bold"),
        ("hero", 34, "bold"),
        ("heading", 24, "bold"),
        ("subhead", 14, "bold"),
        ("body", 13, "normal"),
        ("button", 13, "bold"),
        ("small", 12, "normal"),
        ("label", 11, "bold"),
        ("stat", 34, "bold"),
        ("brand", 18, "bold"),
    ):
        FONTS[key] = ctk.CTkFont(family="Segoe UI", size=size, weight=weight)
    FONTS["mono"] = ctk.CTkFont(family="Consolas", size=12)


def style_card(parent, color: str | None = None, size=12, thickness=1) -> None:
    """A single rounded surface, without decorative corner widgets."""
    parent.configure(corner_radius=14, border_width=1, border_color=COLORS["border"])


def get_density_preset(mode: str | None) -> dict:
    return DENSITY_PRESETS.get((mode or "normal").strip().lower(), DENSITY_PRESETS["normal"])


def apply_button_role(button, role="secondary") -> None:
    button._studio_role = role
    base = {
        "corner_radius": 10,
        "border_width": 1,
        "border_color": COLORS["border"],
        "font": FONTS.get("button", ("Segoe UI", 13, "bold")),
    }
    roles = {
        "primary": {
            "fg_color": COLORS["accent"],
            "hover_color": COLORS["accent_hover"],
            "text_color": COLORS["bg"],
            "border_width": 0,
        },
        "secondary": {"fg_color": COLORS["elevated"], "hover_color": COLORS["hover"], "text_color": COLORS["text"]},
        "accent-line": {
            "fg_color": COLORS["surface"],
            "hover_color": COLORS["accent_soft"],
            "text_color": COLORS["text"],
        },
        "danger": {
            "fg_color": COLORS["danger_bg"],
            "hover_color": COLORS["danger_hover"],
            "text_color": COLORS["error"],
            "border_color": COLORS["danger_border"],
        },
        "ghost": {
            "fg_color": "transparent",
            "hover_color": COLORS["elevated"],
            "text_color": COLORS["muted"],
            "border_width": 0,
        },
    }
    button.configure(**{**base, **roles.get(role, roles["secondary"])})
    button._studio_styled = True


def style_tree(widget) -> None:
    """Style newly built controls, including asynchronously rendered cards."""
    if not getattr(widget, "_studio_styled", False):
        if isinstance(widget, ctk.CTkButton):
            role = getattr(widget, "_studio_role", None)
            if role is None:
                foreground = widget.cget("fg_color")
                color = widget.cget("text_color")
                role = (
                    "primary"
                    if same_color(foreground, COLORS["accent"])
                    else ("danger" if same_color(color, COLORS["error"]) else "secondary")
                )
            apply_button_role(widget, role)
        elif isinstance(widget, (ctk.CTkEntry, ctk.CTkComboBox)):
            options = dict(
                corner_radius=10,
                border_width=1,
                fg_color=COLORS["input"],
                border_color=COLORS["border"],
                text_color=COLORS["text"],
            )
            if isinstance(widget, ctk.CTkComboBox):
                options.update(button_color=COLORS["elevated"], button_hover_color=COLORS["hover_strong"])
            else:
                options["placeholder_text_color"] = COLORS["subtle"]
            widget.configure(**options)
        elif isinstance(widget, ctk.CTkOptionMenu):
            widget.configure(
                corner_radius=10,
                fg_color=COLORS["elevated"],
                button_color=COLORS["elevated"],
                button_hover_color=COLORS["hover_strong"],
                text_color=COLORS["text"],
                dropdown_fg_color=COLORS["elevated"],
                dropdown_hover_color=COLORS["hover_strong"],
            )
        elif isinstance(widget, ctk.CTkSwitch):
            widget.configure(progress_color=COLORS["accent"], button_color=COLORS["text"], fg_color=COLORS["border"])
        elif isinstance(widget, ctk.CTkCheckBox):
            widget.configure(
                corner_radius=6,
                border_width=2,
                fg_color=COLORS["accent"],
                checkmark_color=COLORS["bg"],
                border_color=COLORS["subtle"],
                hover_color=COLORS["accent_soft"],
            )
        elif isinstance(widget, ctk.CTkRadioButton):
            widget.configure(
                fg_color=COLORS["accent"], border_color=COLORS["subtle"], hover_color=COLORS["accent_hover"]
            )
        elif isinstance(widget, ctk.CTkProgressBar):
            widget.configure(corner_radius=4, fg_color=COLORS["border"], progress_color=COLORS["accent"])
        elif isinstance(widget, ctk.CTkTextbox):
            widget.configure(corner_radius=12, border_color=COLORS["border"], border_width=1, fg_color=COLORS["input"])
        elif (
            isinstance(widget, ctk.CTkFrame)
            and widget.cget("fg_color") != "transparent"
            and not same_color(widget.cget("fg_color"), COLORS["bg"])
        ):
            if widget.cget("height") > 2:
                widget.configure(corner_radius=14)
        widget._studio_styled = True
    for child in widget.winfo_children():
        style_tree(child)
