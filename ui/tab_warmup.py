"""
Вкладка управления прогревом аккаунтов.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError
from datetime import datetime

import customtkinter as ctk

from core.account_display import account_headline
from core.account_manager import AccountManager
from core.ai_writer import preview_warmup_prompt
from core.user_feedback import explain_error
from core.warmup_engine import WARMUP_LEVELS, WarmupEngine
from core.warmup_runs import WarmupRunRegistry
from ui.components import empty_state, page_intro, set_page_intro
from ui.i18n import t
from ui.dispatch import post_ui
from ui.hints import tip
from ui.theme import COLORS, FONTS, bind_text_tags, init_fonts, style_card, style_tree


def _action_full_label(action: str) -> str:
    key = f"warmup.action_full.{action}"
    label = t(key)
    return label if label != key else t("warmup.action_full.generic")


class TabWarmup(ctk.CTkFrame):
    MAX_LOG_LINES = 600

    def __init__(self, parent, db, run_coro, async_loop):
        super().__init__(parent, fg_color=COLORS["bg"])
        init_fonts()
        self.db = db
        self.run_coro = run_coro
        self.async_loop = async_loop
        self._runs = WarmupRunRegistry()
        self.accounts_map: dict[str, dict] = {}
        self.channel_vars: dict[int, ctk.BooleanVar] = {}
        self.channel_rows: dict[int, dict] = {}
        self.current_account_id: int | None = None
        self.ai_memory_enabled = ctk.BooleanVar(value=True)
        self.ai_tone_var = ctk.StringVar(value=t("warmup.tone.neutral"))
        self._status_refresh_job = None
        self._ai_refresh_job = None
        self._build_ui()
        self._load_ai_memory_setting()
        self._load_ai_behavior_settings()
        self._load_accounts()
        self._refresh_scenario_summary()

    @property
    def warmup_engine(self) -> WarmupEngine | None:
        """Engine for the focused account, else the first active run (compat for app/parsing)."""
        run = self._runs.get(self.current_account_id)
        if run:
            return run.get("engine")
        items = self._runs.items()
        return items[0][1].get("engine") if items else None

    @property
    def warmup_future(self):
        run = self._runs.get(self.current_account_id)
        if run:
            return run.get("future")
        items = self._runs.items()
        return items[0][1].get("future") if items else None

    def is_warming(self, account_id: int | None = None) -> bool:
        if account_id is None:
            return self._runs.any_warming()
        return self._runs.is_warming(account_id)

    def any_warming(self) -> bool:
        return self._runs.any_warming()

    def stop_account(self, account_id: int | None) -> bool:
        run = self._runs.get(account_id)
        if not run:
            return False
        engine = run.get("engine")
        if engine:
            self.async_loop.call_soon_threadsafe(engine.stop)
        return True

    def stop_all(self) -> int:
        stopped = 0
        for account_id, run in self._runs.items():
            engine = run.get("engine")
            if engine:
                self.async_loop.call_soon_threadsafe(engine.stop)
                stopped += 1
        return stopped

    @staticmethod
    def _tone_labels() -> dict[str, str]:
        return {
            "neutral": t("warmup.tone.neutral"),
            "friendly": t("warmup.tone.friendly"),
            "expert": t("warmup.tone.expert"),
        }

    def _tone_key(self) -> str:
        labels = self._tone_labels()
        reverse = {label: key for key, label in labels.items()}
        return reverse.get(self.ai_tone_var.get(), "neutral")

    def _safe_after(self, callback) -> None:
        post_ui(self, callback)

    def _build_ui(self):
        self.intro = page_intro(self, t("warmup.title"), t("warmup.desc"), t("warmup.kicker"))
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=16, pady=(4, 12))
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        main.grid_columnconfigure(0, weight=1)
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(
            main, fg_color=COLORS["surface"], border_color=COLORS["border"], border_width=1, corner_radius=12
        )
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        style_card(left, color=COLORS["accent"])

        self.settings_label = ctk.CTkLabel(
            left, text=t("warmup.settings"), font=FONTS["label"], text_color=COLORS["muted"]
        )
        self.settings_label.pack(anchor="w", padx=16, pady=(16, 8))
        self.account_label = ctk.CTkLabel(
            left, text=t("warmup.account"), font=FONTS["body"], text_color=COLORS["muted"]
        )
        self.account_label.pack(anchor="w", padx=16, pady=(0, 4))
        self.account_combo = ctk.CTkComboBox(
            left,
            values=[t("warmup.loading")],
            height=36,
            font=FONTS["body"],
            fg_color=COLORS["bg"],
            border_color=COLORS["border"],
            button_color=COLORS["accent"],
            dropdown_fg_color=COLORS["surface"],
            text_color=COLORS["text"],
            corner_radius=12,
            command=self._on_account_selected,
        )
        self.account_combo.pack(fill="x", padx=16, pady=(0, 12))
        tip(self.account_combo, t("warmup.account_tip"))

        self.level_label = ctk.CTkLabel(
            left, text=t("warmup.level"), font=FONTS["body"], text_color=COLORS["muted"]
        )
        self.level_label.pack(anchor="w", padx=16, pady=(0, 4))
        self.level_var = ctk.StringVar(value="medium")
        self.level_radios: dict[str, ctk.CTkRadioButton] = {}
        for value in ("light", "medium", "intensive"):
            radio = ctk.CTkRadioButton(
                left,
                text=t(f"warmup.level.{value}"),
                variable=self.level_var,
                value=value,
                font=FONTS["small"],
                text_color=COLORS["text"],
                fg_color=COLORS["accent"],
                hover_color=COLORS["accent"],
                command=self._refresh_scenario_summary,
            )
            radio.pack(anchor="w", padx=24, pady=2)
            self.level_radios[value] = radio

        self.topic_label = ctk.CTkLabel(
            left, text=t("warmup.topic"), font=FONTS["body"], text_color=COLORS["muted"]
        )
        self.topic_label.pack(anchor="w", padx=16, pady=(12, 4))
        self.topic_entry = ctk.CTkEntry(
            left,
            placeholder_text=t("warmup.topic_placeholder"),
            height=36,
            font=FONTS["body"],
            fg_color=COLORS["bg"],
            border_color=COLORS["border"],
            text_color=COLORS["text"],
            corner_radius=12,
        )
        self.topic_entry.pack(fill="x", padx=16, pady=(0, 8))
        self.topic_entry.bind("<KeyRelease>", lambda _event: self._refresh_scenario_summary(), add="+")

        memory_frame = ctk.CTkFrame(left, fg_color="transparent")
        memory_frame.pack(fill="x", padx=16, pady=(0, 8))
        self.ai_memory_label = ctk.CTkLabel(
            memory_frame,
            text=t("warmup.ai_memory"),
            font=FONTS["body"],
            text_color=COLORS["muted"],
        )
        self.ai_memory_label.pack(side="left")
        self.memory_switch = ctk.CTkSwitch(
            memory_frame,
            text="",
            variable=self.ai_memory_enabled,
            command=self._on_ai_memory_toggle,
            fg_color=COLORS["border"],
            progress_color=COLORS["accent"],
        )
        self.memory_switch.pack(side="right")

        self.selected_chats_label = ctk.CTkLabel(
            left, text=t("warmup.selected_chats"), font=FONTS["label"], text_color=COLORS["muted"]
        )
        self.selected_chats_label.pack(anchor="w", padx=16, pady=(8, 4))
        channels_header = ctk.CTkFrame(left, fg_color="transparent")
        channels_header.pack(fill="x", padx=16, pady=(0, 6))
        self.channels_in_label = ctk.CTkLabel(
            channels_header, text=t("warmup.channels_in"), font=FONTS["small"], text_color=COLORS["muted"]
        )
        self.channels_in_label.pack(side="left")
        self.refresh_channels_btn = ctk.CTkButton(
            channels_header,
            text=t("warmup.refresh"),
            width=90,
            height=28,
            corner_radius=12,
            fg_color=COLORS["border"],
            hover_color=COLORS["bg"],
            text_color=COLORS["accent"],
            border_color=COLORS["accent"],
            border_width=1,
            font=FONTS["small"],
            command=self._sync_channels,
        )
        self.refresh_channels_btn.pack(side="right")

        self.channels_scroll = ctk.CTkScrollableFrame(
            left,
            fg_color=COLORS["bg"],
            border_color=COLORS["border"],
            border_width=1,
            corner_radius=12,
            height=160,
        )
        self.channels_scroll.pack(fill="x", padx=16, pady=(0, 8))

        memory_actions = ctk.CTkFrame(left, fg_color="transparent")
        memory_actions.pack(fill="x", padx=16, pady=(0, 8))
        self.clear_mem_account_btn = ctk.CTkButton(
            memory_actions,
            text=t("warmup.clear_mem_account"),
            height=30,
            corner_radius=12,
            font=FONTS["small"],
            fg_color=COLORS["surface"],
            border_color=COLORS["border"],
            border_width=1,
            text_color=COLORS["muted"],
            command=self._clear_memory_for_account,
        )
        self.clear_mem_account_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.clear_mem_selected_btn = ctk.CTkButton(
            memory_actions,
            text=t("warmup.clear_mem_selected"),
            height=30,
            corner_radius=12,
            font=FONTS["small"],
            fg_color=COLORS["surface"],
            border_color=COLORS["accent"],
            border_width=1,
            text_color=COLORS["accent"],
            command=self._clear_memory_for_selected_channels,
        )
        self.clear_mem_selected_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))

        self.ai_style_label = ctk.CTkLabel(
            left, text=t("warmup.ai_style"), font=FONTS["label"], text_color=COLORS["muted"]
        )
        self.ai_style_label.pack(anchor="w", padx=16, pady=(4, 4))
        ai_behavior = ctk.CTkFrame(
            left, fg_color=COLORS["bg"], border_color=COLORS["border"], border_width=1, corner_radius=12
        )
        ai_behavior.pack(fill="x", padx=16, pady=(0, 8))

        tone_row = ctk.CTkFrame(ai_behavior, fg_color="transparent")
        tone_row.pack(fill="x", padx=10, pady=(8, 6))
        self.tone_label = ctk.CTkLabel(
            tone_row, text=t("warmup.tone_label"), font=FONTS["small"], text_color=COLORS["muted"]
        )
        self.tone_label.pack(side="left")
        self.ai_tone_menu = ctk.CTkOptionMenu(
            tone_row,
            values=list(self._tone_labels().values()),
            variable=self.ai_tone_var,
            height=30,
            width=150,
            font=FONTS["small"],
            command=self._on_tone_changed,
        )
        self.ai_tone_menu.pack(side="right")
        tip(self.ai_tone_menu, t("warmup.tone_tip"))
        tip(self.tone_label, t("warmup.tone_tip"))

        limit_row = ctk.CTkFrame(ai_behavior, fg_color="transparent")
        limit_row.pack(fill="x", padx=10, pady=(0, 6))
        self.limit_hour_label = ctk.CTkLabel(
            limit_row, text=t("warmup.limit_hour"), font=FONTS["small"], text_color=COLORS["muted"]
        )
        self.limit_hour_label.pack(side="left")
        self.ai_hourly_entry = ctk.CTkEntry(limit_row, width=80, height=30, font=FONTS["small"])
        self.ai_hourly_entry.pack(side="right")
        self.ai_hourly_entry.insert(0, "6")
        self.ai_hourly_entry.bind("<KeyRelease>", lambda _event: self._refresh_scenario_summary(), add="+")

        cooldown_row = ctk.CTkFrame(ai_behavior, fg_color="transparent")
        cooldown_row.pack(fill="x", padx=10, pady=(0, 6))
        self.chat_pause_label = ctk.CTkLabel(
            cooldown_row, text=t("warmup.chat_pause"), font=FONTS["small"], text_color=COLORS["muted"]
        )
        self.chat_pause_label.pack(side="left")
        self.ai_cooldown_entry = ctk.CTkEntry(cooldown_row, width=80, height=30, font=FONTS["small"])
        self.ai_cooldown_entry.pack(side="right")
        self.ai_cooldown_entry.insert(0, "12")
        self.ai_cooldown_entry.bind("<KeyRelease>", lambda _event: self._refresh_scenario_summary(), add="+")

        reply_row = ctk.CTkFrame(ai_behavior, fg_color="transparent")
        reply_row.pack(fill="x", padx=10, pady=(0, 8))
        self.reply_chance_label = ctk.CTkLabel(
            reply_row, text=t("warmup.reply_chance"), font=FONTS["small"], text_color=COLORS["muted"]
        )
        self.reply_chance_label.pack(side="left")
        self.ai_reply_entry = ctk.CTkEntry(reply_row, width=80, height=30, font=FONTS["small"])
        self.ai_reply_entry.pack(side="right")
        self.ai_reply_entry.insert(0, "100")
        self.ai_reply_entry.bind("<KeyRelease>", lambda _event: self._refresh_scenario_summary(), add="+")
        tip(self.reply_chance_label, t("warmup.reply_tip"))

        self.save_ai_btn = ctk.CTkButton(
            ai_behavior,
            text=t("warmup.save_ai"),
            height=30,
            corner_radius=12,
            font=FONTS["small"],
            fg_color=COLORS["surface"],
            hover_color=COLORS["border"],
            border_color=COLORS["accent"],
            border_width=1,
            text_color=COLORS["accent"],
            command=self._save_ai_behavior_settings,
        )
        self.save_ai_btn.pack(fill="x", padx=10, pady=(0, 10))

        btn_frame = footer
        self.btn_start = ctk.CTkButton(
            btn_frame,
            text=t("warmup.start"),
            height=44,
            corner_radius=12,
            font=FONTS["subhead"],
            fg_color=COLORS["accent"],
            hover_color=COLORS["error"],
            text_color=COLORS["text"],
            command=self._start_warmup,
        )
        self.btn_start.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.btn_stop = ctk.CTkButton(
            btn_frame,
            text=t("warmup.stop"),
            height=36,
            corner_radius=12,
            font=FONTS["body"],
            fg_color=COLORS["surface"],
            hover_color=COLORS["border"],
            text_color=COLORS["muted"],
            border_color=COLORS["border"],
            border_width=1,
            command=self._stop_warmup,
        )
        self.btn_stop.pack(side="left", fill="x", expand=True)
        tip(self.btn_start, t("warmup.start_tip"))
        tip(self.btn_stop, t("warmup.stop_tip"))
        self.btn_stop_all = ctk.CTkButton(
            btn_frame,
            text=t("warmup.stop_all"),
            height=36,
            corner_radius=12,
            font=FONTS["body"],
            fg_color=COLORS["surface"],
            hover_color=COLORS["border"],
            text_color=COLORS["muted"],
            border_color=COLORS["border"],
            border_width=1,
            width=130,
            command=self._stop_all_warmups,
        )
        self.btn_stop_all.pack(side="left", padx=(8, 0))
        tip(self.btn_stop_all, t("warmup.stop_all_tip"))
        self._set_warmup_controls()

        right = ctk.CTkFrame(
            main, fg_color=COLORS["surface"], border_color=COLORS["border"], border_width=1, corner_radius=12
        )
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        style_card(right, color=COLORS["accent"])
        self.status_heading = ctk.CTkLabel(
            right, text=t("warmup.status_heading"), font=FONTS["label"], text_color=COLORS["muted"]
        )
        self.status_heading.pack(anchor="w", padx=16, pady=(16, 8))

        progress_block = ctk.CTkFrame(
            right, fg_color=COLORS["bg"], border_color=COLORS["border"], border_width=1, corner_radius=12
        )
        progress_block.pack(fill="x", padx=16, pady=(0, 8))
        self.status_account = ctk.CTkLabel(
            progress_block, text=t("warmup.no_account"), font=FONTS["subhead"], text_color=COLORS["text"]
        )
        self.status_account.pack(anchor="w", padx=12, pady=(10, 2))
        self.status_label = ctk.CTkLabel(
            progress_block, text=t("warmup.session.idle"), font=FONTS["body"], text_color=COLORS["muted"]
        )
        self.status_label.pack(anchor="w", padx=12, pady=(0, 8))
        self.progress_bar = ctk.CTkProgressBar(
            progress_block, progress_color=COLORS["accent"], fg_color=COLORS["border"], height=6, corner_radius=12
        )
        self.progress_bar.pack(fill="x", padx=12, pady=(0, 8))
        self.progress_bar.set(0)
        stats_row = ctk.CTkFrame(progress_block, fg_color="transparent")
        stats_row.pack(fill="x", padx=12, pady=(0, 10))
        self.day_label = ctk.CTkLabel(
            stats_row, text=t("warmup.day_empty"), font=FONTS["small"], text_color=COLORS["muted"]
        )
        self.day_label.pack(side="left")
        self.actions_label = ctk.CTkLabel(
            stats_row, text=t("warmup.actions_today_empty"), font=FONTS["small"], text_color=COLORS["muted"]
        )
        self.actions_label.pack(side="right")

        active_block = ctk.CTkFrame(
            right, fg_color=COLORS["bg"], border_color=COLORS["border"], border_width=1, corner_radius=12
        )
        active_block.pack(fill="x", padx=16, pady=(0, 8))
        self.active_runs_heading = ctk.CTkLabel(
            active_block, text=t("warmup.active_runs"), font=FONTS["label"], text_color=COLORS["muted"]
        )
        self.active_runs_heading.pack(anchor="w", padx=10, pady=(8, 2))
        self.active_runs_empty = ctk.CTkLabel(
            active_block,
            text=t("warmup.active_runs_empty"),
            font=FONTS["small"],
            text_color=COLORS["muted"],
            anchor="w",
        )
        self.active_runs_empty.pack(fill="x", padx=10, pady=(0, 8))
        self.active_runs_list = ctk.CTkFrame(active_block, fg_color="transparent")
        self.active_runs_list.pack(fill="x", padx=6, pady=(0, 8))

        scenario_block = ctk.CTkFrame(
            right,
            fg_color=COLORS["bg"],
            border_color=COLORS["border"],
            border_width=1,
            corner_radius=12,
        )
        scenario_block.pack(fill="x", padx=16, pady=(0, 8))
        self.scenario_title = ctk.CTkLabel(
            scenario_block,
            text=t("warmup.scenario.title"),
            font=FONTS["label"],
            text_color=COLORS["muted"],
        )
        self.scenario_title.pack(anchor="w", padx=10, pady=(8, 2))
        self.scenario_name = ctk.CTkLabel(
            scenario_block,
            text="",
            font=FONTS["body"],
            text_color=COLORS["text"],
            anchor="w",
            justify="left",
        )
        self.scenario_name.pack(fill="x", padx=10, pady=(0, 2))
        self.scenario_details = ctk.CTkLabel(
            scenario_block,
            text="",
            font=FONTS["small"],
            text_color=COLORS["muted"],
            anchor="w",
            justify="left",
        )
        self.scenario_details.pack(fill="x", padx=10, pady=(0, 4))
        self.scenario_prompt_title = ctk.CTkLabel(
            scenario_block,
            text=t("warmup.scenario.prompt_title"),
            font=FONTS["label"],
            text_color=COLORS["muted"],
        )
        self.scenario_prompt_title.pack(anchor="w", padx=10, pady=(4, 2))
        self.scenario_prompt_note = ctk.CTkLabel(
            scenario_block,
            text=t("warmup.scenario.prompt_note"),
            font=FONTS["small"],
            text_color=COLORS["muted"],
            anchor="w",
            justify="left",
        )
        self.scenario_prompt_note.pack(fill="x", padx=10, pady=(0, 4))
        self.scenario_prompt_box = ctk.CTkTextbox(
            scenario_block,
            height=140,
            wrap="word",
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color=COLORS["surface"],
            text_color=COLORS["text"],
            border_color=COLORS["border"],
            border_width=1,
            corner_radius=8,
        )
        self.scenario_prompt_box.pack(fill="x", padx=10, pady=(0, 10))
        self.scenario_prompt_box.configure(state="disabled")

        ai_stats_block = ctk.CTkFrame(
            right,
            fg_color=COLORS["bg"],
            border_color=COLORS["border"],
            border_width=1,
            corner_radius=12,
        )
        ai_stats_block.pack(fill="x", padx=16, pady=(0, 8))
        self.ai_usage_heading = ctk.CTkLabel(
            ai_stats_block, text=t("warmup.ai_usage"), font=FONTS["label"], text_color=COLORS["muted"]
        )
        self.ai_usage_heading.pack(anchor="w", padx=10, pady=(8, 2))
        ai_row = ctk.CTkFrame(ai_stats_block, fg_color="transparent")
        ai_row.pack(fill="x", padx=10, pady=(0, 8))
        self.ai_tokens_label = ctk.CTkLabel(
            ai_row, text=t("ai.tokens", tokens="0"), font=FONTS["small"], text_color=COLORS["muted"]
        )
        self.ai_tokens_label.pack(side="left")
        self.ai_cost_label = ctk.CTkLabel(
            ai_row, text=t("ai.cost", cost=t("ai.cost_unknown")), font=FONTS["small"], text_color=COLORS["success"]
        )
        self.ai_cost_label.pack(side="right")

        self.history_heading = ctk.CTkLabel(
            right, text=t("warmup.history"), font=FONTS["label"], text_color=COLORS["muted"]
        )
        self.history_heading.pack(anchor="w", padx=16, pady=(4, 4))
        self.log_box = ctk.CTkTextbox(
            right,
            fg_color=COLORS["bg"],
            text_color=COLORS["text"],
            font=ctk.CTkFont(family="Consolas", size=11),
            border_color=COLORS["border"],
            border_width=1,
            corner_radius=12,
            state="normal",
        )
        self.log_box.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        bind_text_tags(
            self.log_box,
            {
                "SUCCESS": COLORS["success"],
                "ERROR": COLORS["error"],
                "INFO": COLORS["muted"],
                "WARN": COLORS["warning"],
            },
        )

    def _load_ai_memory_setting(self) -> None:
        fut = self.run_coro(self.db.get_setting("ai_memory_enabled", "1"))

        def done(f):
            try:
                value = str(f.result() or "1")
            except Exception:
                value = "1"

            def apply_setting():
                self.ai_memory_enabled.set(value == "1")
                self._refresh_scenario_summary()

            self._safe_after(apply_setting)

        fut.add_done_callback(done)

    def _on_ai_memory_toggle(self) -> None:
        enabled = bool(self.ai_memory_enabled.get())
        self._refresh_scenario_summary()
        fut = self.run_coro(self.db.set_setting("ai_memory_enabled", "1" if enabled else "0"))

        def done(result):
            def render():
                self._log_operation(result, f"Память AI {'включена' if enabled else 'выключена'}")
                if not result.cancelled() and result.exception():
                    self._load_ai_memory_setting()

            self._safe_after(render)

        fut.add_done_callback(done)

    def _load_ai_behavior_settings(self) -> None:
        async def load_all():
            tone = await self.db.get_setting("ai_style_tone", "neutral")
            hourly = await self.db.get_setting("ai_hourly_limit", "6")
            cooldown = await self.db.get_setting("ai_chat_cooldown_min", "12")
            reply_chance = await self.db.get_setting("ai_reply_chance", "100")
            return {
                "tone": str(tone or "neutral").strip().lower(),
                "hourly": str(hourly or "6").strip(),
                "cooldown": str(cooldown or "12").strip(),
                "reply_chance": str(reply_chance or "100").strip(),
            }

        fut = self.run_coro(load_all())

        def done(f):
            try:
                data = f.result()
            except Exception:
                data = {"tone": "neutral", "hourly": "6", "cooldown": "12", "reply_chance": "100"}

            def apply():
                tone = data.get("tone", "neutral")
                if tone not in ("neutral", "friendly", "expert"):
                    tone = "neutral"
                self.ai_tone_var.set(self._tone_labels()[tone])
                self.ai_hourly_entry.delete(0, "end")
                self.ai_hourly_entry.insert(0, data.get("hourly", "6"))
                self.ai_cooldown_entry.delete(0, "end")
                self.ai_cooldown_entry.insert(0, data.get("cooldown", "12"))
                self.ai_reply_entry.delete(0, "end")
                self.ai_reply_entry.insert(0, data.get("reply_chance", "100"))
                self._refresh_scenario_summary()

            self._safe_after(apply)

        fut.add_done_callback(done)

    def _read_ai_behavior_values(self) -> dict[str, object] | None:
        tone = self._tone_key()
        try:
            hourly_limit = int(self.ai_hourly_entry.get().strip() or "6")
            cooldown_min = int(self.ai_cooldown_entry.get().strip() or "12")
            reply_chance = int(self.ai_reply_entry.get().strip() or "100")
        except Exception:
            return None
        if not 1 <= hourly_limit <= 30 or not 1 <= cooldown_min <= 180 or not 0 <= reply_chance <= 100:
            return None
        return {
            "tone": tone,
            "hourly_limit": hourly_limit,
            "cooldown_min": cooldown_min,
            "reply_chance": reply_chance,
        }

    def _on_tone_changed(self, _value=None) -> None:
        self._refresh_scenario_summary()
        tone = self._tone_key()
        fut = self.run_coro(self.db.set_setting("ai_style_tone", tone))

        def done(result):
            if result.cancelled() or result.exception():
                self._safe_after(lambda: self._log_operation(result, None))

        fut.add_done_callback(done)

    def _save_ai_behavior_settings(self) -> None:
        values = self._read_ai_behavior_values()
        if values is None:
            self._log(
                "ERROR",
                "Укажите лимит 1–30 сообщений в час, паузу 1–180 минут и вероятность 0–100% (целые числа)",
            )
            return

        async def save_all():
            await self.db.set_setting("ai_style_tone", str(values["tone"]))
            await self.db.set_setting("ai_hourly_limit", str(values["hourly_limit"]))
            await self.db.set_setting("ai_chat_cooldown_min", str(values["cooldown_min"]))
            await self.db.set_setting("ai_reply_chance", str(values["reply_chance"]))

        fut = self.run_coro(save_all())

        def done(result):
            def render():
                self._log_operation(result, "AI-параметры сохранены")
                self._refresh_scenario_summary()

            self._safe_after(render)

        fut.add_done_callback(done)

    def _selected_chats_count(self) -> int:
        return sum(1 for var in self.channel_vars.values() if bool(var.get()))

    def _refresh_scenario_summary(self) -> None:
        if not hasattr(self, "scenario_name") or not hasattr(self, "scenario_details"):
            return
        level = self.level_var.get() if hasattr(self, "level_var") else "medium"
        if level not in WARMUP_LEVELS:
            level = "medium"
        config = WARMUP_LEVELS[level]
        topic = ""
        if hasattr(self, "topic_entry"):
            try:
                topic = self.topic_entry.get().strip()
            except Exception:
                topic = ""
        if not topic:
            topic = t("warmup.scenario.topic_default")

        action_order = []
        for action in config.get("actions") or []:
            if action not in action_order:
                action_order.append(action)
        actions_text = ", ".join(t(f"warmup.action.{name}") for name in action_order)

        try:
            hourly = int(self.ai_hourly_entry.get().strip() or "6")
        except Exception:
            hourly = 6
        try:
            cooldown = int(self.ai_cooldown_entry.get().strip() or "12")
        except Exception:
            cooldown = 12
        try:
            reply = int(self.ai_reply_entry.get().strip() or "100")
        except Exception:
            reply = 100
        reply = max(0, min(100, reply))

        tone_label = self._tone_labels().get(self._tone_key(), t("warmup.tone.neutral"))
        memory_text = (
            t("warmup.scenario.memory_on")
            if bool(self.ai_memory_enabled.get())
            else t("warmup.scenario.memory_off")
        )
        chats_n = self._selected_chats_count()
        chats_text = (
            t("warmup.scenario.chats", n=chats_n) if chats_n else t("warmup.scenario.chats_none")
        )
        pace = config.get("actions_per_day") or {"min": 0, "max": 0}

        self.scenario_title.configure(text=t("warmup.scenario.title"))
        self.scenario_name.configure(text=t(f"warmup.scenario.desc.{level}"))
        details = "\n".join(
            [
                t("warmup.scenario.level", level=t(f"warmup.level.{level}")),
                t("warmup.scenario.topic", topic=topic),
                t(
                    "warmup.scenario.pace",
                    days=int(config.get("days") or 0),
                    min=int(pace.get("min") or 0),
                    max=int(pace.get("max") or 0),
                ),
                t("warmup.scenario.actions", actions=actions_text),
                f"{t('warmup.scenario.tone', tone=tone_label)} · {memory_text}",
                t(
                    "warmup.scenario.limits",
                    hourly=hourly,
                    cooldown=cooldown,
                    reply=reply,
                ),
                chats_text,
            ]
        )
        self.scenario_details.configure(text=details)
        self.scenario_prompt_title.configure(text=t("warmup.scenario.prompt_title"))
        uses_ai = "send_message" in (config.get("actions") or [])
        if uses_ai:
            self.scenario_prompt_note.configure(text=t("warmup.scenario.prompt_note"))
            prompt_text = preview_warmup_prompt(topic=topic, tone=self._tone_key())
        else:
            self.scenario_prompt_note.configure(text=t("warmup.scenario.prompt_light"))
            prompt_text = t("warmup.scenario.prompt_light")
        self.scenario_prompt_box.configure(state="normal")
        self.scenario_prompt_box.delete("1.0", "end")
        self.scenario_prompt_box.insert("1.0", prompt_text)
        self.scenario_prompt_box.configure(state="disabled")
        try:
            width = max(int(self.scenario_details.winfo_width()) - 8, 220)
            self.scenario_name.configure(wraplength=width)
            self.scenario_details.configure(wraplength=width)
            self.scenario_prompt_note.configure(wraplength=width)
        except Exception:
            pass

    def _warmup_status_label(self, account: dict) -> str:
        if not account.get("is_enabled", 1):
            return t("warmup.status.disabled")
        states = {
            "cold": t("warmup.status.cold"),
            "warming": t("warmup.status.warming"),
            "paused": t("warmup.status.paused"),
            "ready": t("warmup.status.ready"),
            "error": t("warmup.status.error"),
        }
        return states.get(account.get("warmup_status") or "cold", t("warmup.status.waiting"))

    def _account_menu_label(self, account: dict) -> str:
        headline = account_headline(account) or str(account.get("phone") or account.get("id") or "")
        return f"{headline} · {self._warmup_status_label(account)}"

    def _load_accounts(self):
        fut = self.run_coro(self.db.get_accounts())

        def done(f):
            try:
                accounts = f.result()
            except Exception:
                loading = t("warmup.loading")
                self._safe_after(lambda: self.account_combo.configure(values=[loading]))
                self._safe_after(lambda: self.account_combo.set(loading))
                self._safe_after(lambda: self.after(2000, self._load_accounts))
                return

            def apply_accounts():
                previous_id = self.current_account_id
                current_value = self.account_combo.get().strip()
                if current_value in self.accounts_map:
                    previous_id = self.accounts_map[current_value].get("id", previous_id)
                self.accounts_map = {self._account_menu_label(a): a for a in accounts}
                values = list(self.accounts_map.keys())
                if not values:
                    empty = t("warmup.no_accounts")
                    self.account_combo.configure(values=[empty])
                    self.account_combo.set(empty)
                    self.btn_start.configure(state="disabled")
                    return
                self.account_combo.configure(values=values)
                match = next(
                    (key for key, item in self.accounts_map.items() if item.get("id") == previous_id),
                    None,
                )
                if match:
                    self.account_combo.set(match)
                    self._on_account_selected(match)
                else:
                    default = next(
                        (key for key, item in self.accounts_map.items() if item.get("is_enabled", 1)), values[0]
                    )
                    self.account_combo.set(default)
                    self._on_account_selected(default)

            self._safe_after(apply_accounts)

        fut.add_done_callback(done)

    def _on_account_selected(self, value):
        account = self.accounts_map.get(value)
        if not account:
            return
        self.current_account_id = int(account["id"])
        self.status_account.configure(
            text=account_headline(account) or str(account.get("phone") or account.get("id") or "")
        )
        self._set_warmup_controls()
        self._load_session(account["id"])
        self._populate_channels_list(account["id"])
        self._load_warmup_logs(account["id"])
        self._refresh_active_runs_panel()
        self._update_ai_stats()

    def _load_session(self, account_id: int) -> None:
        fut = self.run_coro(self.db.get_warmup_session(account_id))

        def done(f):
            try:
                session = f.result()
            except Exception:
                session = None
            if session:
                self._safe_after(lambda: self._apply_session_to_ui(session))
            else:
                self._safe_after(
                    lambda: self._update_progress_ui(
                        {"status": "idle", "day_current": 0, "day_total": 7, "actions_today": 0}
                    )
                )
                self._safe_after(self._refresh_scenario_summary)

        fut.add_done_callback(done)

    def _load_warmup_logs(self, account_id: int) -> None:
        fut = self.run_coro(self.db.get_warmup_logs(account_id, limit=120))

        def done(f):
            try:
                logs = f.result()
            except Exception:
                logs = []
            self._safe_after(lambda: self._render_warmup_logs(logs))

        fut.add_done_callback(done)

    def _render_warmup_logs(self, logs: list[dict]) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        if not logs:
            self.log_box.insert("end", "[--:--:--]  Нет сохранённых записей\n", "INFO")
            self.log_box.see("end")
            return

        for entry in reversed(logs):
            try:
                dt = datetime.fromisoformat(str(entry.get("created_at") or ""))
                time_str = dt.strftime("%H:%M:%S")
            except Exception:
                time_str = "--:--:--"

            action = _action_full_label(str(entry.get("action") or ""))
            detail = str(entry.get("detail") or "")
            status = str(entry.get("status") or "").lower()
            if status == "success":
                tag = "SUCCESS"
                icon = "[УСПЕХ]"
            elif status == "error":
                tag = "ERROR"
                icon = "[ОШИБКА]"
            else:
                tag = "WARN"
                icon = "[ВНИМАНИЕ]"
            line = f"[{time_str}]  {icon} {action}: {detail}\n"
            self.log_box.insert("end", line, tag)

        self.log_box.see("end")
        self._trim_log_lines()

    def _sync_channels(self):
        selected = self.account_combo.get()
        account = self.accounts_map.get(selected)
        if not account:
            return
        self._log("INFO", "Загрузка каналов аккаунта...")

        async def do_sync():
            mgr = AccountManager(account, self.db)
            try:
                await mgr.connect()
                dialogs = await mgr.get_joined_dialogs()
            finally:
                await mgr.disconnect()
            await self.db.save_account_channels(account["id"], dialogs)
            return dialogs

        future = asyncio.run_coroutine_threadsafe(do_sync(), self.async_loop)

        def on_done():
            try:
                dialogs = future.result(timeout=30)
                self._safe_after(lambda: self._populate_channels_list(account["id"]))
                self._safe_after(lambda: self._log("SUCCESS", f"Загружено {len(dialogs)} каналов/групп"))
            except Exception as exc:  # noqa: BLE001
                self._safe_after(lambda exc=exc: self._log("ERROR", "Ошибка: " + explain_error(exc)))

        future.add_done_callback(lambda _: on_done())

    def _populate_channels_list(self, account_id: int):
        fut = self.run_coro(self.db.get_account_channels(account_id))

        def done(f):
            try:
                channels = f.result()
            except Exception:
                channels = []
            self._safe_after(lambda: self._render_channels(channels))

        fut.add_done_callback(done)

    def _render_channels(self, channels: list[dict]) -> None:
        for w in self.channels_scroll.winfo_children():
            w.destroy()
        self.channel_vars.clear()
        self.channel_rows.clear()
        if not channels:
            empty_state(
                self.channels_scroll,
                "Выберите свои чаты",
                "Обновите список диалогов аккаунта, затем отметьте нужные чаты.",
                "targets",
            )
            return
        for ch in channels:
            row = ctk.CTkFrame(self.channels_scroll, fg_color="transparent")
            row.pack(fill="x", pady=1)
            var = ctk.BooleanVar(value=bool(ch.get("is_selected", 0)))
            self.channel_vars[ch["id"]] = var
            self.channel_rows[ch["id"]] = ch
            icon = "📢" if ch.get("channel_type") == "channel" else "👥"
            title = ch.get("title") or ch.get("username") or f"ID:{ch.get('channel_id')}"
            members_count = int(ch.get("members_count") or 0)
            members = f"  {members_count:,}" if members_count else ""
            ctk.CTkCheckBox(
                row,
                text=f"{icon} {title}{members}",
                variable=var,
                font=FONTS["small"],
                text_color=COLORS["text"],
                fg_color=COLORS["accent"],
                hover_color=COLORS["warning"],
                checkmark_color=COLORS["bg"],
                command=lambda c=ch, v=var: self._toggle_channel(c, v),
            ).pack(anchor="w", padx=4)

        style_tree(self.channels_scroll)
        self._refresh_scenario_summary()

    def _toggle_channel(self, channel: dict, var: ctk.BooleanVar):
        self._refresh_scenario_summary()
        future = self.run_coro(self.db.toggle_channel_selected(channel["id"], bool(var.get())))
        future.add_done_callback(lambda result: self._safe_after(lambda: self._log_operation(result, None)))

    def _log_operation(self, result, success):
        if result.cancelled():
            return
        if result.exception():
            self._log("ERROR", "Изменения не сохранены: " + str(result.exception()))
        elif success:
            self._log("SUCCESS", success)

    def _clear_memory_for_account(self) -> None:
        account_id = self.current_account_id
        if not account_id:
            self._log("WARN", "Сначала выберите аккаунт")
            return
        fut = self.run_coro(self.db.clear_ai_memory_for_account(account_id))

        def done(result):
            self._safe_after(lambda: self._log_operation(result, "Память AI очищена для аккаунта"))

        fut.add_done_callback(done)

    def _clear_memory_for_selected_channels(self) -> None:
        account_id = self.current_account_id
        if not account_id:
            self._log("WARN", "Сначала выберите аккаунт")
            return
        selected_keys: list[str] = []
        for row_id, var in self.channel_vars.items():
            if not bool(var.get()):
                continue
            ch = self.channel_rows.get(row_id) or {}
            username = str(ch.get("username") or "").strip()
            channel_id = str(ch.get("channel_id") or "").strip()
            if username:
                selected_keys.append(username)
            elif channel_id:
                selected_keys.append(channel_id)
        if not selected_keys:
            self._log("WARN", "Нет отмеченных каналов для очистки памяти")
            return
        fut = self.run_coro(self.db.clear_ai_memory_for_chat_keys(account_id, selected_keys))

        def done(f):
            try:
                deleted = int(f.result() or 0)
            except Exception as exc:
                self._safe_after(lambda exc=exc: self._log("ERROR", "Память не очищена: " + explain_error(exc)))
                return
            self._safe_after(
                lambda: self._log("SUCCESS", f"Память очищена для {len(selected_keys)} чатов (сообщений: {deleted})")
            )

        fut.add_done_callback(done)

    def _apply_session_to_ui(self, session: dict) -> None:
        # Only overwrite level/topic when the focused account is not mid warm-up.
        if not self.is_warming(self.current_account_id):
            level = str(session.get("level") or "").strip()
            if level in self.level_radios:
                self.level_var.set(level)
            topic = str(session.get("topic") or "").strip()
            if topic:
                self.topic_entry.delete(0, "end")
                self.topic_entry.insert(0, topic)
        self._update_progress_ui(session)
        self._refresh_scenario_summary()

    def _update_progress_ui(self, session: dict):
        day = int(session.get("day_current") or 0)
        total = int(session.get("day_total") or 7)
        progress = min(day / total, 1.0) if total > 0 else 0
        self.progress_bar.set(progress)
        self.day_label.configure(text=t("warmup.day", day=day, total=total))
        self.actions_label.configure(
            text=t("warmup.actions_today", n=int(session.get("actions_today") or 0))
        )
        status_map = {
            "idle": ("warmup.session.idle", COLORS["muted"]),
            "running": ("warmup.session.running", COLORS["success"]),
            "paused": ("warmup.session.paused", COLORS["warning"]),
            "error": ("warmup.session.error", COLORS["error"]),
            "completed": ("warmup.session.completed", COLORS["success"]),
        }
        key, color = status_map.get(session.get("status", "idle"), ("warmup.session.unknown", COLORS["muted"]))
        self.status_label.configure(text=t(key), text_color=color)

    def _log(self, level: str, message: str, *, account_label: str | None = None):
        prefix = f"[{account_label}] " if account_label else ""

        def append_line():
            time_str = datetime.now().strftime("%H:%M:%S")
            tag = level.upper()
            icons = {
                "SUCCESS": "[УСПЕХ]",
                "ERROR": "[ОШИБКА]",
                "INFO": "[СВЕДЕНИЕ]",
                "WARN": "[ВНИМАНИЕ]",
            }
            line = f"[{time_str}]  {icons.get(tag, '')} {prefix}{message}\n"
            self.log_box.configure(state="normal")
            self.log_box.insert("end", line, tag)
            self.log_box.see("end")
            self.log_box.configure(state="normal")
            self._trim_log_lines()

        self._safe_after(append_line)

    def _make_account_logger(self, label: str):
        def log(level: str, message: str) -> None:
            self._log(level, message, account_label=label)

        return log

    def _trim_log_lines(self) -> None:
        try:
            current_lines = int(float(self.log_box.index("end-1c").split(".")[0]))
        except Exception:
            return
        if current_lines <= self.MAX_LOG_LINES:
            return
        to_remove = current_lines - self.MAX_LOG_LINES
        self.log_box.delete("1.0", f"{to_remove + 1}.0")

    def _set_warmup_controls(self, _running: bool | None = None) -> None:
        # Account combo stays enabled so another account can be started while one warms.
        self.account_combo.configure(state="normal")
        selected = self.accounts_map.get(self.account_combo.get())
        selected_id = int(selected["id"]) if selected else self.current_account_id
        focused_running = self.is_warming(selected_id)
        any_running = self.any_warming()
        enabled = bool(selected and selected.get("is_enabled", 1))
        if focused_running:
            self.btn_start.configure(state="disabled", text=t("warmup.running"))
            self.btn_stop.configure(state="normal", text=t("warmup.stop"))
        else:
            self.btn_start.configure(
                state="normal" if enabled else "disabled",
                text=t("warmup.start"),
            )
            # If another account is warming, Stop still works as «stop all».
            self.btn_stop.configure(
                state="normal" if any_running else "disabled",
                text=t("warmup.stop"),
            )
        if hasattr(self, "btn_stop_all"):
            count = len(self._runs.active_ids())
            self.btn_stop_all.configure(
                state="normal" if count > 1 else "disabled",
                text=t("warmup.stop_all"),
            )

    def _refresh_active_runs_panel(self) -> None:
        if not hasattr(self, "active_runs_list"):
            return
        for child in self.active_runs_list.winfo_children():
            child.destroy()
        items = self._runs.items()
        if not items:
            self.active_runs_empty.configure(text=t("warmup.active_runs_empty"))
            self.active_runs_empty.pack(fill="x", padx=10, pady=(0, 8))
            return
        self.active_runs_empty.pack_forget()
        for account_id, run in items:
            row = ctk.CTkFrame(self.active_runs_list, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(
                row,
                text=run.get("label") or str(account_id),
                font=FONTS["small"],
                text_color=COLORS["text"],
                anchor="w",
            ).pack(side="left", fill="x", expand=True, padx=(4, 8))
            ctk.CTkButton(
                row,
                text=t("warmup.stop"),
                width=70,
                height=26,
                font=FONTS["small"],
                fg_color=COLORS["surface"],
                border_width=1,
                border_color=COLORS["border"],
                text_color=COLORS["muted"],
                command=lambda aid=account_id: self._stop_one(aid),
            ).pack(side="right")

    def _account_start_conflict(self, account: dict) -> str | None:
        account_id = int(account["id"])
        if self.is_warming(account_id):
            return t("warmup.already_running")
        if AccountManager.is_session_busy(account["phone"], account.get("session_name")):
            return t("warmup.account_busy")
        try:
            app = self.winfo_toplevel()
        except Exception:
            return None
        parsing = getattr(app, "parsing_tab", None)
        if parsing is not None and parsing.busy():
            if getattr(parsing, "multi_mode", lambda: False)():
                chosen = {
                    aid for aid, var in getattr(parsing, "multi_vars", {}).items() if var.get()
                }
                if account_id in chosen:
                    return t("warmup.account_in_parsing")
            elif getattr(parsing, "_selected_account_id", None) == account_id:
                return t("warmup.account_in_parsing")
        task = getattr(app, "task_tab", None)
        scheduler = getattr(app, "scheduler", None)
        if task is not None and scheduler is not None:
            if scheduler.is_running() or getattr(task, "_starting", False):
                selected = {
                    key for key, var in getattr(task, "account_vars", {}).items() if var.get()
                }
                if account_id in selected:
                    return t("warmup.account_in_task")
        return None

    def _update_ai_stats(self):
        total_tokens = 0
        cost_parts = []
        any_running = False
        for _account_id, run in self._runs.items():
            engine = run.get("engine")
            if not engine:
                continue
            if engine.running:
                any_running = True
            writer = getattr(engine, "ai_writer", None)
            if not writer:
                continue
            stats = writer.get_session_stats()
            total_tokens += int(stats.get("tokens") or 0)
            formatted = stats.get("cost_formatted")
            if formatted:
                cost_parts.append(formatted)
        focus = self._runs.get(self.current_account_id)
        if focus and focus.get("engine") and getattr(focus["engine"], "ai_writer", None):
            stats = focus["engine"].ai_writer.get_session_stats()
            self.ai_tokens_label.configure(
                text=t("ai.tokens", tokens=f"{int(stats.get('tokens', 0)):,}")
            )
            cost = stats.get("cost_formatted") or t("ai.cost_unknown")
            self.ai_cost_label.configure(text=t("ai.cost", cost=cost))
        else:
            self.ai_tokens_label.configure(text=t("ai.tokens", tokens=f"{total_tokens:,}"))
            cost = cost_parts[0] if len(cost_parts) == 1 else (
                t("ai.cost_unknown") if not cost_parts else " / ".join(cost_parts[:3])
            )
            self.ai_cost_label.configure(text=t("ai.cost", cost=cost))
        if any_running:
            if self._ai_refresh_job:
                try:
                    self.after_cancel(self._ai_refresh_job)
                except Exception:
                    pass
            self._ai_refresh_job = self.after(10000, self._update_ai_stats)

    def _start_warmup(self):
        selected = self.account_combo.get()
        account = self.accounts_map.get(selected)
        if not account:
            return
        conflict = self._account_start_conflict(account)
        if conflict:
            self._log("WARN", conflict)
            return
        account_id = int(account["id"])
        self.current_account_id = account_id
        level = self.level_var.get()
        topic = self.topic_entry.get().strip() or "общее"
        ai_values = self._read_ai_behavior_values()
        if ai_values is None:
            self._log(
                "ERROR",
                "Перед запуском укажите лимит 1–30/ч, паузу 1–180 мин и вероятность ответа 0–100%",
            )
            return
        self._refresh_scenario_summary()
        label = account_headline(account) or str(account.get("phone") or account_id)
        phone = str(account.get("phone") or "")
        engine = WarmupEngine(self.db, self._make_account_logger(label))

        async def persist_and_start():
            # Persist the visible Tone/limits so the engine does not use a stale saved style.
            await self.db.set_setting("ai_style_tone", str(ai_values["tone"]))
            await self.db.set_setting("ai_hourly_limit", str(ai_values["hourly_limit"]))
            await self.db.set_setting("ai_chat_cooldown_min", str(ai_values["cooldown_min"]))
            await self.db.set_setting("ai_reply_chance", str(ai_values["reply_chance"]))
            await engine.start(account, level, topic)

        future = self.run_coro(persist_and_start())
        self._runs.put(account_id, engine=engine, future=future, phone=phone, label=label)
        self._set_warmup_controls()
        self._refresh_active_runs_panel()
        self.btn_start.configure(state="disabled", text=t("warmup.connecting"))
        self._log(
            "INFO",
            f"Подключаем для прогрева… (тон: {ai_values['tone']})",
            account_label=label,
        )

        def report_started():
            if future.done():
                return
            if engine.client is None:
                self.after(200, report_started)
                return
            self._set_warmup_controls()
            self._log("INFO", "Подключение выполнено; прогрев начат", account_label=label)

        self.after(200, report_started)

        def done(f):
            try:
                _ = f.result()
            except FutureCancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                self._safe_after(
                    lambda exc=exc: self._log("ERROR", f"Ошибка прогрева: {exc}", account_label=label)
                )
            finally:
                def finish():
                    self._runs.discard(account_id)
                    self._set_warmup_controls()
                    self._refresh_active_runs_panel()
                    self._update_ai_stats()

                self._safe_after(finish)

        future.add_done_callback(done)
        if self._ai_refresh_job:
            try:
                self.after_cancel(self._ai_refresh_job)
            except Exception:
                pass
        self._ai_refresh_job = self.after(10000, self._update_ai_stats)
        if self._status_refresh_job:
            try:
                self.after_cancel(self._status_refresh_job)
            except Exception:
                pass
        self._status_refresh_job = self.after(30000, self._refresh_status)

    def _stop_one(self, account_id: int) -> None:
        run = self._runs.get(account_id)
        if not run:
            return
        label = run.get("label") or str(account_id)
        if self.stop_account(account_id):
            self._log("INFO", t("warmup.stopping_log"), account_label=label)
            self._set_warmup_controls()
            if self.is_warming(self.current_account_id) and int(account_id) == int(self.current_account_id or 0):
                self.btn_start.configure(state="disabled", text=t("warmup.stopping"))

    def _stop_warmup(self):
        selected_id = self.current_account_id
        if self.is_warming(selected_id):
            self._stop_one(int(selected_id))
            return
        if self.any_warming():
            self._stop_all_warmups()

    def _stop_all_warmups(self):
        count = self.stop_all()
        if count:
            self._log("INFO", t("warmup.stopping_all_log", count=count))
            self.btn_stop.configure(state="disabled")
            self.btn_stop_all.configure(state="disabled")
            self.btn_start.configure(state="disabled", text=t("warmup.stopping"))

    def _refresh_status(self):
        self._runs.prune()
        self._refresh_active_runs_panel()
        self._set_warmup_controls()
        selected = self.accounts_map.get(self.account_combo.get())
        if selected and self.is_warming(int(selected["id"])):
            self._load_session(selected["id"])
        if self.any_warming():
            if self._status_refresh_job:
                try:
                    self.after_cancel(self._status_refresh_job)
                except Exception:
                    pass
            self._status_refresh_job = self.after(30000, self._refresh_status)

    def retranslate(self) -> None:
        set_page_intro(
            self.intro,
            title=t("warmup.title"),
            description=t("warmup.desc"),
            kicker=t("warmup.kicker"),
        )
        self.settings_label.configure(text=t("warmup.settings"))
        self.account_label.configure(text=t("warmup.account"))
        tip(self.account_combo, t("warmup.account_tip"))
        self.level_label.configure(text=t("warmup.level"))
        for value, radio in self.level_radios.items():
            radio.configure(text=t(f"warmup.level.{value}"))
        self.topic_label.configure(text=t("warmup.topic"))
        self.topic_entry.configure(placeholder_text=t("warmup.topic_placeholder"))
        self.ai_memory_label.configure(text=t("warmup.ai_memory"))
        self.selected_chats_label.configure(text=t("warmup.selected_chats"))
        self.channels_in_label.configure(text=t("warmup.channels_in"))
        self.refresh_channels_btn.configure(text=t("warmup.refresh"))
        self.clear_mem_account_btn.configure(text=t("warmup.clear_mem_account"))
        self.clear_mem_selected_btn.configure(text=t("warmup.clear_mem_selected"))
        self.ai_style_label.configure(text=t("warmup.ai_style"))
        self.tone_label.configure(text=t("warmup.tone_label"))
        tip(self.ai_tone_menu, t("warmup.tone_tip"))
        tip(self.tone_label, t("warmup.tone_tip"))
        self.limit_hour_label.configure(text=t("warmup.limit_hour"))
        self.chat_pause_label.configure(text=t("warmup.chat_pause"))
        self.reply_chance_label.configure(text=t("warmup.reply_chance"))
        tip(self.reply_chance_label, t("warmup.reply_tip"))
        self.save_ai_btn.configure(text=t("warmup.save_ai"))
        tip(self.btn_start, t("warmup.start_tip"))
        tip(self.btn_stop, t("warmup.stop_tip"))
        self.btn_stop.configure(text=t("warmup.stop"))
        if hasattr(self, "btn_stop_all"):
            self.btn_stop_all.configure(text=t("warmup.stop_all"))
            tip(self.btn_stop_all, t("warmup.stop_all_tip"))
        self._set_warmup_controls()
        self.status_heading.configure(text=t("warmup.status_heading"))
        if hasattr(self, "active_runs_heading"):
            self.active_runs_heading.configure(text=t("warmup.active_runs"))
        self._refresh_active_runs_panel()
        self.ai_usage_heading.configure(text=t("warmup.ai_usage"))
        self.history_heading.configure(text=t("warmup.history"))
        if not self.current_account_id:
            self.status_account.configure(text=t("warmup.no_account"))
            self.status_label.configure(text=t("warmup.session.idle"))
            self.day_label.configure(text=t("warmup.day_empty"))
            self.actions_label.configure(text=t("warmup.actions_today_empty"))
        tone_key = self._tone_key()
        tone_labels = self._tone_labels()
        self.ai_tone_menu.configure(values=list(tone_labels.values()))
        self.ai_tone_var.set(tone_labels.get(tone_key, tone_labels["neutral"]))
        self.scenario_prompt_title.configure(text=t("warmup.scenario.prompt_title"))
        self._refresh_scenario_summary()
        self._update_ai_stats()
        self._load_accounts()

    def destroy(self):
        if self._status_refresh_job:
            try:
                self.after_cancel(self._status_refresh_job)
            except Exception:
                pass
            self._status_refresh_job = None
        if self._ai_refresh_job:
            try:
                self.after_cancel(self._ai_refresh_job)
            except Exception:
                pass
            self._ai_refresh_job = None
        super().destroy()
