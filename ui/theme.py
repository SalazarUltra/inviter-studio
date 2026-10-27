"""Inviter Studio design system: graphite surfaces and electric lime."""

from pathlib import Path

import customtkinter as ctk

COLORS = {
    "bg": "#101113",
    "surface": "#191B1F",
    "elevated": "#23262B",
    "border": "#2D3036",
    "accent": "#D4FF00",
    "accent_hover": "#BEDF12",
    "accent_soft": "#2B3417",
    "success": "#6FCF97",
    "warning": "#E8C76B",
    "error": "#FF7D92",
    "danger_bg": "#352129",
    "text": "#F5F6F7",
    "muted": "#9A9EA8",
    "subtle": "#676D78",
    "input": "#131519",
}
ctk.set_appearance_mode("dark")
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
        "secondary": {"fg_color": COLORS["elevated"], "hover_color": "#30343B", "text_color": COLORS["text"]},
        "accent-line": {
            "fg_color": COLORS["surface"],
            "hover_color": COLORS["accent_soft"],
            "text_color": COLORS["text"],
        },
        "danger": {
            "fg_color": COLORS["danger_bg"],
            "hover_color": "#492B36",
            "text_color": COLORS["error"],
            "border_color": "#50303D",
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
                    if foreground == COLORS["accent"]
                    else ("danger" if color == COLORS["error"] else "secondary")
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
                options.update(button_color=COLORS["elevated"], button_hover_color="#333840")
            else:
                options["placeholder_text_color"] = COLORS["subtle"]
            widget.configure(**options)
        elif isinstance(widget, ctk.CTkOptionMenu):
            widget.configure(
                corner_radius=10,
                fg_color=COLORS["elevated"],
                button_color=COLORS["elevated"],
                button_hover_color="#333840",
                text_color=COLORS["text"],
                dropdown_fg_color=COLORS["elevated"],
                dropdown_hover_color="#333840",
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
        elif isinstance(widget, ctk.CTkFrame) and widget.cget("fg_color") not in ("transparent", COLORS["bg"]):
            if widget.cget("height") > 2:
                widget.configure(corner_radius=14)
        widget._studio_styled = True
    for child in widget.winfo_children():
        style_tree(child)
