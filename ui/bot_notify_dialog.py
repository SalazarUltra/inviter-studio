"""Settings for the user's own Telegram bot: token (DPAPI-protected), chat_id, detect and test message."""

from __future__ import annotations

import customtkinter as ctk

from core.bot_notify import (
    NOTIFY_CHAT_ID_KEY,
    NOTIFY_TOKEN_KEY,
    BotNotifyError,
    clear_bot_token,
    detect_chat_id,
    save_bot_settings,
    saved_token,
    scrub,
    send_message,
    validate_chat_id,
    validate_token,
)
from core.summary import build_summary_text
from core.user_feedback import explain_error
from ui.dispatch import post_ui
from ui.entry_utils import setup_entry
from ui.hints import hint
from ui.i18n import get_language, t
from ui.theme import COLORS, FONTS, apply_button_role, style_tree


def friendly_error(exc: BaseException, token: str | None = None) -> str:
    """Bot errors are already user-safe Russian text; anything else is reduced. The token is always scrubbed."""
    text = str(exc) if isinstance(exc, BotNotifyError) else explain_error(exc)
    return scrub(text, token)


class BotNotifyDialog(ctk.CTkToplevel):
    def __init__(self, parent, get_db, run_coro, on_saved=None):
        super().__init__(parent)
        self.get_db = get_db
        self.run_coro = run_coro
        self.on_saved = on_saved
        self.busy = False
        self.has_saved_token = False
        self.configure(fg_color=COLORS["bg"])
        self.title(t("bot.title"))
        self.geometry("520x520")
        self.minsize(460, 460)
        self.transient(parent.winfo_toplevel())
        self.bind("<Escape>", lambda _event: self.destroy())
        ctk.CTkLabel(self, text=t("bot.title"), font=FONTS["heading"], anchor="w").pack(fill="x", padx=20, pady=(16, 4))
        self.help_label = hint(self, t("bot.help"), padx=20)
        ctk.CTkLabel(self, text=t("bot.token"), font=FONTS["small"], text_color=COLORS["muted"], anchor="w").pack(
            fill="x", padx=20
        )
        self.token_entry = ctk.CTkEntry(self, placeholder_text=t("bot.token_placeholder"), show="•", height=36)
        self.token_entry.pack(fill="x", padx=20, pady=(2, 8))
        setup_entry(self.token_entry)
        ctk.CTkLabel(self, text=t("bot.chat_id"), font=FONTS["small"], text_color=COLORS["muted"], anchor="w").pack(
            fill="x", padx=20
        )
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(2, 8))
        row.grid_columnconfigure(0, weight=1)
        self.chat_entry = ctk.CTkEntry(row, height=36)
        self.chat_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        setup_entry(self.chat_entry)
        self.detect_button = ctk.CTkButton(row, text=t("bot.detect"), width=170, height=36, command=self.detect)
        apply_button_role(self.detect_button, "secondary")
        self.detect_button.grid(row=0, column=1)
        self.status = ctk.CTkLabel(
            self, text="", font=FONTS["small"], text_color=COLORS["muted"], wraplength=460, justify="left", anchor="w"
        )
        self.status.pack(fill="x", padx=20, pady=(0, 8))
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(0, 8))
        self.save_button = ctk.CTkButton(actions, text=t("bot.save"), height=36, command=self.save)
        apply_button_role(self.save_button, "primary")
        self.save_button.pack(side="left", padx=(0, 6))
        self.test_button = ctk.CTkButton(actions, text=t("bot.test"), height=36, command=self.send_test)
        apply_button_role(self.test_button, "secondary")
        self.test_button.pack(side="left", padx=(0, 6))
        self.remove_button = ctk.CTkButton(actions, text=t("bot.remove_token"), height=36, command=self.remove_token)
        apply_button_role(self.remove_button, "secondary")
        self.remove_button.pack(side="left")
        close = ctk.CTkButton(self, text=t("bot.close"), height=34, width=100, command=self.destroy)
        apply_button_role(close, "secondary")
        close.pack(anchor="e", padx=20, pady=(0, 14))
        style_tree(self)
        self.load()
        self.after(50, self._raise)

    def _raise(self) -> None:
        if self.winfo_exists():
            self.lift()

    # -- helpers -------------------------------------------------------------------------
    def _say(self, text: str, error: bool = False) -> None:
        self.status.configure(text=text, text_color=COLORS["error"] if error else COLORS["muted"])

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (self.detect_button, self.save_button, self.test_button, self.remove_button):
            button.configure(state=state)
        if text:
            self._say(text)

    def _typed_token(self) -> str:
        return self.token_entry.get().strip()

    def _show_token_state(self) -> None:
        self.token_entry.delete(0, "end")
        placeholder = t("bot.token_saved") if self.has_saved_token else t("bot.token_placeholder")
        self.token_entry.configure(placeholder_text=placeholder)
        self.remove_button.configure(state="normal" if self.has_saved_token and not self.busy else "disabled")

    def _run(self, coro, on_done, *, busy_text: str | None = None, token: str | None = None) -> None:
        """Run ``coro`` off the UI thread; ``on_done(value)`` on success, errors are shown scrubbed."""
        if self.busy:
            return
        self._set_busy(True, busy_text or t("bot.busy"))
        future = self.run_coro(coro)

        def done(result):
            def render():
                if not self.winfo_exists():
                    return
                self._set_busy(False)
                if result.cancelled():
                    self._say(friendly_error(BotNotifyError("Операция отменена")), error=True)
                elif result.exception() is not None:
                    self._say(friendly_error(result.exception(), token), error=True)
                else:
                    on_done(result.result())
                self.remove_button.configure(state="normal" if self.has_saved_token else "disabled")

            post_ui(self, render)

        future.add_done_callback(done)

    # -- persistence ---------------------------------------------------------------------
    def load(self) -> None:
        db = self.get_db()

        async def read():
            return await db.get_settings_map([NOTIFY_TOKEN_KEY, NOTIFY_CHAT_ID_KEY])

        future = self.run_coro(read())

        def done(result):
            if result.cancelled() or result.exception():
                return

            def render():
                values = result.result()
                self.has_saved_token = bool(values.get(NOTIFY_TOKEN_KEY))  # the token itself is never read here
                self.chat_entry.delete(0, "end")
                self.chat_entry.insert(0, str(values.get(NOTIFY_CHAT_ID_KEY) or ""))
                self._show_token_state()

            post_ui(self, render)

        future.add_done_callback(done)

    def save(self) -> None:
        token = self._typed_token()
        chat = self.chat_entry.get().strip()
        try:
            if token:
                validate_token(token)
            if chat:
                validate_chat_id(chat)
        except BotNotifyError as exc:
            self._say(str(exc), error=True)
            return
        db = self.get_db()

        def done(_value):
            self.has_saved_token = self.has_saved_token or bool(token)
            self._show_token_state()
            self._say(t("bot.saved"))
            if self.on_saved:
                self.on_saved()

        self._run(save_bot_settings(db, token=token or None, chat_id=chat), done, token=token)

    def remove_token(self) -> None:
        db = self.get_db()

        def done(_value):
            self.has_saved_token = False
            self._show_token_state()
            self._say(t("bot.removed"))
            if self.on_saved:
                self.on_saved()

        self._run(clear_bot_token(db), done)

    # -- Telegram calls ------------------------------------------------------------------
    async def _token_or_saved(self, typed: str) -> str:
        return validate_token(typed) if typed else await saved_token(self.get_db())

    def detect(self) -> None:
        typed = self._typed_token()
        if not typed and not self.has_saved_token:
            self._say(t("bot.need_token"), error=True)
            return

        async def work():
            return await detect_chat_id(await self._token_or_saved(typed))

        def done(chat_id):
            self.chat_entry.delete(0, "end")
            self.chat_entry.insert(0, chat_id)
            self._say(t("bot.detected", chat_id=chat_id))

        self._run(work(), done, token=typed)

    def send_test(self) -> None:
        typed = self._typed_token()
        chat = self.chat_entry.get().strip()
        if not typed and not self.has_saved_token:
            self._say(t("bot.need_token"), error=True)
            return
        lang = get_language()
        text = build_summary_text({"sent": 12, "added": 3, "errors": 1, "skipped": 2}, lang=lang, outcome="completed")
        text += "\n" + t("summary.sample", lang)

        async def work():
            await send_message(await self._token_or_saved(typed), chat, text)

        self._run(work(), lambda _value: self._say(t("bot.test_ok")), token=typed)
